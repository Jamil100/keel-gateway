"""Gateway overhead: how much of a request was us rather than the provider.

One metric, ``keel_gateway_overhead_seconds``, and it exists for one reason —
§6 says it is separate from total duration "specifically so S5 can be measured
rather than asserted". S5 is *"gateway latency overhead, excluding provider time:
p95 <= 15 ms added"*, and until this middleware existed that number was a claim in
a table with nothing producing it.

overhead = request wall clock - provider time

**Two clocks, deliberately.** The wall clock here is ``time.perf_counter()``, not
the injected ``Clock``. ADR 0001 already anticipated this: the reservoir samples
go through the injected clock and are documented as approximate, while "the
latency figures the README reports come from Prometheus histograms, which time
independently of this clock". Timing a scrape metric off a ``ManualClock`` would
also make it report exactly zero in every test, which is worse than useless — it
would pass.

**Which is why the result can be negative, and why it is clamped.** The provider
time subtracted here is ``ProviderResult.latency_ms``, measured *through* the
injected clock. Under ``ManualClock`` the two diverge on purpose:
``MockAdapter`` at ``latency_ms=3000`` advances the clock three seconds while
about a millisecond of real time passes, giving an overhead of **-2.999 s**. That
is not a bug in either clock — it is what makes the mock testable — but
``prometheus_client`` accepts a negative observation *silently*, counts it in
every bucket, and leaves ``_sum`` permanently wrong. One such request would
corrupt the S5 histogram for the life of the process, so the subtraction is
clamped at zero. Same guard, and the same reasoning, as
``Executor._elapsed_ms``, which already clamps the NTP-step version of this.

In production, where the clock is a ``SystemClock``, the two agree to within the
scheduling jitter of one event loop turn and the clamp never fires.

**A request that never reached a provider records nothing.** An envelope
rejection is a 400 produced entirely by the gateway, so *all* of its duration is
overhead by the arithmetic — and adding it would let a client drag the S5 figure
wherever it liked by sending malformed requests. S5 is a claim about requests the
gateway actually served, so only those are measured.
"""

from __future__ import annotations

import time
from typing import Any, Final

from starlette.types import ASGIApp, Receive, Scope, Send

from keel.observability.metrics import MetricsCatalogue

__all__ = [
    "PROVIDER_SECONDS_ATTR",
    "REQUEST_CLASS_ATTR",
    "OverheadMiddleware",
]

PROVIDER_SECONDS_ATTR: Final = "keel_provider_seconds"
REQUEST_CLASS_ATTR: Final = "keel_request_class"
"""The handoff from the route to this middleware, via ``request.state``.

Middleware sees a ``Request`` and a ``Response`` and nothing in between: not the
envelope, not the ``ProviderResult``, so neither the request class nor the
provider time is reachable from here. The ingress route sets both before it
returns, on the success and the upstream-failure path alike.

``request.state`` rather than a ``contextvar`` because ``BaseHTTPMiddleware`` runs
the downstream app in a separate task, and context set there does not reliably
propagate back out. P2-T5's ``request_id`` binding will hit the same boundary and
should read this note first.
"""


class OverheadMiddleware:
    """Times each request and records what the gateway itself cost.

    A raw ASGI middleware rather than ``BaseHTTPMiddleware``. The latter wraps
    every request in an anyio task group and an extra pair of queues, which is
    measurable overhead added by the thing measuring overhead — and it is exactly
    the quantity under test. This form adds one function call.
    """

    def __init__(self, app: ASGIApp, metrics: MetricsCatalogue) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Starlette backs `request.state` with `scope["state"]`, so seeding it here
        # gives the route a dict to write into that this frame still holds a
        # reference to. Without it the route's first `request.state.x = ...` would
        # create a dict on a scope copy and the handoff would silently read empty.
        scope.setdefault("state", {})

        started = time.perf_counter()
        try:
            await self._app(scope, receive, send)
        finally:
            # `finally`, so an unhandled exception on the way out is still
            # measured. A request that 500s consumed gateway time like any other,
            # and dropping it would flatter the S5 figure precisely when the
            # gateway is misbehaving.
            self._observe(scope, time.perf_counter() - started)

    def _observe(self, scope: Scope, elapsed_seconds: float) -> None:
        state: dict[str, Any] = scope.get("state") or {}
        request_class = state.get(REQUEST_CLASS_ATTR)
        provider_seconds = state.get(PROVIDER_SECONDS_ATTR)

        if not isinstance(request_class, str) or not isinstance(provider_seconds, float):
            # No provider was reached: an envelope 400, `/healthz`, `/metrics`, or
            # a 404. None of those are what S5 measures.
            return

        self._metrics.observe_overhead(request_class, elapsed_seconds - provider_seconds)
