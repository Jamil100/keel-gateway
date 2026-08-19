"""Calling one provider, under a deadline, and reporting what happened.

The executor is the narrow waist of the request path (TECHNICAL-DESIGN.md §4):
everything above it decides *what* to call, everything below it knows only how
to call one provider, and this is the one place that watches an attempt from
start to finish. That makes it the only honest place to record the outcome —
decision D-C — which is why adapters know nothing about Redis and why P2-T2's
health recording is the single line in ``execute`` below.

That line is ``await``ed rather than spawned, and it cannot fail: ``HealthWindow``
carries a never-raises contract and its own time box (ADR 0008), so a Redis outage
costs an observation rather than a request. Guarding here as well would only give
Phase 3's breaker a guard it could forget to copy.

**Phase 1 invokes candidate 1 and stops.** No failover, no retry, no hedging.
A returned failure is returned, not routed around, even when its
``retry_elsewhere`` says another provider would be worth trying. That property
is pinned by a test rather than merely intended, because failover added before
Phase 2's health window exists would be exactly the reactive-before-observable
mistake FR-3.4 rules out.

**The timeout here is a backstop, not the timeout.** ``CohereAdapter`` already
passes ``timeout_ms`` down to LiteLLM so the socket actually closes; this one
sits above it and catches the case where an adapter hangs without honouring its
own deadline — a library bug, or an adapter with no network layer to set a
deadline on. Two of them is not redundancy: the inner one frees a connection,
the outer one frees the request.

One asymmetry worth knowing before reading the tests. This deadline is real
event-loop time, not injected ``Clock`` time, because that is what
``asyncio.wait_for`` measures. It cannot be otherwise: ``ManualClock.sleep``
advances time and returns on the same tick, so racing an invocation against
``clock.sleep(timeout)`` would have both finish together *and* double-advance
the clock. So the one test that makes this fire uses ``SystemClock`` and a few
milliseconds of real waiting; everything else in the suite stays on
``ManualClock`` (NFR-2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Final

from keel.api.envelope import RequestEnvelope
from keel.clock import Clock
from keel.config import KeelConfig
from keel.health.window import HealthWindow
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.errors import ErrorClass, NormalizedError
from keel.routing.router import Router

__all__ = ["GATEWAY_TIMEOUT_ERROR_TYPE", "Executor"]

# Named so a log line separates our deadline from the provider's own. A Cohere
# timeout arrives as LiteLLM's `Timeout` and normalizes carrying that type name;
# this one never reached the provider's error path at all, and telling the two
# apart is the difference between "they were slow" and "we were impatient".
GATEWAY_TIMEOUT_ERROR_TYPE: Final = "KeelExecutorTimeout"


class Executor:
    """Invokes the first candidate for an envelope and returns its result."""

    def __init__(
        self,
        *,
        router: Router,
        config: KeelConfig,
        registry: Mapping[str, ProviderAdapter],
        clock: Clock,
        window: HealthWindow,
    ) -> None:
        # `window` is required rather than defaulting to None. An optional
        # recorder is one that can be omitted by accident, and the result would be
        # a gateway that starts cleanly, serves every request, and silently
        # records nothing — the same shape of failure ADR 0004 refuses for a
        # provider it cannot serve. Degrading when Redis is unreachable is
        # `HealthWindow`'s job and it is unconditional, so no caller needs None.
        self._router = router
        self._config = config
        self._registry = registry
        self._clock = clock
        self._window = window

    async def execute(self, envelope: RequestEnvelope) -> ProviderResult:
        """Run one attempt and return its outcome, success or failure.

        Never raises for a provider failure — that is a ``ProviderResult``
        carrying an ``error``, the same contract every adapter honours. An
        exception out of here means the gateway itself is broken.
        """
        candidates = self._router.candidates(envelope)

        if not candidates:  # pragma: no cover - unreachable until Phase 3
            # `preference` has min_length=1 and the router does not yet filter,
            # so nothing can empty this list today. Phase 3's capability filter
            # can, and that case is the §5.7 `422` — a semantic failure, not a
            # transient one. Deliberately not invented here.
            raise RuntimeError(
                f"no candidate providers for request class {envelope.request_class!r}; "
                f"the §5.7 capability filter and its 422 land in Phase 3"
            )

        # Phase 3 turns this into a loop: walk the candidates while the last
        # failure's `retry_elsewhere` is True, counting attempts for
        # `X-Keel-Attempts` and emitting `keel_failover_events_total`.
        provider = candidates[0]
        adapter = self._adapter_for(provider)
        timeout_ms = self._config.providers[provider].timeout_ms

        started = self._clock.now()
        try:
            result = await self._attempt(adapter, envelope, timeout_ms)
        except TimeoutError:
            # `wait_for` cancelled the adapter, so `CancelledError` was raised
            # inside `invoke`. Neither adapter catches it — both use
            # `except Exception` for exactly this reason — so no adapter got the
            # chance to describe this failure, and the executor must.
            result = self._timed_out(provider, timeout_ms, started)

        # One call site covering both branches above (D-C) — the whole reason
        # adapters were kept ignorant of Redis. P2-T3 extends this to the latency
        # reservoir, and Phase 3's breaker is the first thing that reads any of it
        # back; recording it here is what FR-3.4 means by observing first.
        await self._window.record(result)

        return result

    async def _attempt(
        self, adapter: ProviderAdapter, envelope: RequestEnvelope, timeout_ms: int | None
    ) -> ProviderResult:
        """One provider call, under the gateway deadline when there is one."""
        if timeout_ms is None:
            # §5.2 omits `timeout_ms` for the mock, so it cannot be required, and
            # `None` means "no gateway-imposed deadline" rather than "zero".
            # Awaited directly rather than wrapped, so the no-timeout path does
            # not pay for a task and a cancellation scope it will never use.
            return await adapter.invoke(envelope)

        # Config and operators speak milliseconds; asyncio speaks seconds. The
        # conversion happens at the boundary owning the millisecond value, the
        # same way `MockAdapter.invoke` and `CohereAdapter._build_request` each
        # own theirs.
        return await asyncio.wait_for(adapter.invoke(envelope), timeout=timeout_ms / 1000.0)

    def _adapter_for(self, provider: str) -> ProviderAdapter:
        """The live adapter for a candidate name.

        ``KeelConfig`` guarantees the name is a configured provider, and ADR 0004
        guarantees the registry refuses to start without one adapter per
        configured provider — so registry keys and config keys agree by
        construction. This says so out loud rather than indexing and hoping.
        """
        adapter = self._registry.get(provider)
        if adapter is None:  # pragma: no cover - ADR 0004 makes this unreachable
            raise RuntimeError(
                f"provider {provider!r} is configured but absent from the registry; "
                f"build_registry refuses to return a partial one (ADR 0004), so this "
                f"executor was handed a registry it did not build"
            )
        return adapter

    def _timed_out(self, provider: str, timeout_ms: int | None, started: float) -> ProviderResult:
        """The result the adapter never got to return.

        ``TIMEOUT`` rather than ``SERVER_ERROR``: §5.4 separates the two because
        latency budgets treat them differently, and this one counts toward the
        breaker — a provider we consistently cannot wait for is unhealthy by any
        useful definition.
        """
        return ProviderResult.failure(
            provider=provider,
            latency_ms=self._elapsed_ms(started),
            error=NormalizedError(
                error_class=ErrorClass.TIMEOUT,
                message=(
                    f"provider {provider!r} did not respond within the gateway timeout of "
                    f"{timeout_ms} ms; the attempt was cancelled"
                ),
                provider_error_type=GATEWAY_TIMEOUT_ERROR_TYPE,
            ),
        )

    def _elapsed_ms(self, started: float) -> float:
        """Wall-clock milliseconds since ``started``, never negative.

        Clamped for the same reason ``CohereAdapter._elapsed_ms`` is: the
        injected clock is wall-clock (ADR 0001), so an NTP step mid-attempt
        would otherwise produce a negative latency that ``ProviderResult``
        rejects — turning a timeout into a crash.
        """
        return max(0.0, (self._clock.now() - started) * 1000.0)
