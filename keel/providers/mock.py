"""The in-process mock provider: a failure source that breaks on cue.

A permanent component, not scaffolding (D1, ADR 0002). Real providers refuse to
fail on demand during a demo, and NFR-2 forbids live API calls in the test
suite, so every breaker, hedging, and failover test in this project is written
against this adapter. PRD constraint C1 makes that load-bearing: only Cohere
access exists today, so the whole failover engine is built here first and real
providers are substituted later.

**The scope cap is the feature.** §5.3 and the PRD risk register cap this at
latency distribution, error rate, error class, and a chaos endpoint, and the
roadmap names an over-built mock as "the most common way this project fails".
So: no capability simulation, no streaming, no per-tenant behaviour, no
connection-level faults (ADR 0002 records that last one as the real cost of
being in-process). Adding a fifth knob needs a reason that survives that table.

Everything mutable lives on :class:`MockChaosState`, which the Phase 6 chaos API
(FR-7.2) mutates in place. Nothing here reads configuration at request time.
"""

from __future__ import annotations

import math
from random import Random
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keel.api.envelope import RequestEnvelope
from keel.clock import Clock
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.errors import ErrorClass, NormalizedError

__all__ = ["DEFAULT_ERROR_MIX", "MockAdapter", "MockChaosState"]

# The shipped failure mix. Weights are relative, not probabilities — they are
# normalized at draw time, so editing one entry does not require rebalancing
# the rest.
#
# AUTH_FAILURE and BAD_REQUEST are deliberately absent: one is our
# configuration and the other is the client's payload, so a *provider*
# producing them on a healthy request would be incoherent. CONTENT_FILTER is
# present precisely because it does **not** count toward the breaker (D7) — it
# makes the M2 dashboard show a taxonomy split where the breaker's input is
# visibly smaller than the raw error rate, which is the D7 argument made
# visible rather than asserted.
DEFAULT_ERROR_MIX: Final[dict[ErrorClass, float]] = {
    ErrorClass.RATE_LIMIT: 0.40,
    ErrorClass.TIMEOUT: 0.25,
    ErrorClass.SERVER_ERROR: 0.25,
    ErrorClass.QUOTA_EXHAUSTED: 0.05,
    ErrorClass.CONTENT_FILTER: 0.05,
}

_MOCK_MODEL_NAME: Final = "keel-mock"
_COMPLETION_TEXT: Final = (
    "This is a mock completion produced by Keel's in-process mock provider."
)
# The conventional ~4 characters per token. Good enough for the cost engine to
# have numbers that move with input size; not a tokenizer, and not claimed to be.
_CHARS_PER_TOKEN: Final = 4


class MockChaosState(BaseModel):
    """The four knobs, and nothing else.

    Mutable by design — the Phase 6 chaos API (FR-7.2) writes to it in place
    while traffic is running. ``validate_assignment`` is therefore load-bearing
    rather than decorative: that API sets these fields straight from HTTP
    input, and ``state.error_rate = 5.0`` has to fail at the assignment rather
    than silently at the next request. Same posture as NFR-4 for config.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    error_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    """Fraction of calls that fail. ``0.0`` is a valid, fully-quiet setting."""

    latency_ms: Annotated[float, Field(ge=0.0)] = 50.0
    """The **median** injected latency."""

    latency_sigma: Annotated[float, Field(ge=0.0)] = 0.0
    """Lognormal shape. ``0.0`` means fixed latency, so p50 == p95 == p99.

    Left at zero by default so a configured latency is exactly what shows up on
    the percentile panels. Raise it for a realistic right tail — which is what
    Phase 3's "p95 over the class budget" breaker trigger needs to be exercised
    against something other than a flat line.
    """

    error_classes: dict[ErrorClass, float] = Field(default_factory=lambda: dict(DEFAULT_ERROR_MIX))
    """Relative weights over the taxonomy. One entry pins a single class."""

    seed: int = 0
    """Changing this restarts every RNG stream — see :class:`MockAdapter`."""

    @model_validator(mode="after")
    def _check_a_failure_is_drawable(self) -> MockChaosState:
        # Both of these would raise at request time, inside the executor, on
        # the first failure — which is during a chaos demo or an incident.
        if not self.error_classes:
            raise ValueError(
                "error_classes is empty; there would be no class to draw when a call fails. "
                "Set at least one ErrorClass weight."
            )
        if sum(self.error_classes.values()) <= 0.0:
            raise ValueError(
                f"error_classes weights sum to {sum(self.error_classes.values())}; "
                f"at least one weight must be positive."
            )
        return self


class MockAdapter:
    """A ``ProviderAdapter`` whose failures are decided by a seeded RNG.

    **Three independent RNG streams**, all derived from ``state.seed``: one
    decides pass/fail, one picks the error class, one draws latency. A single
    shared stream would mean a failed call consumes an extra draw for its
    class, so changing the class mix — or turning on latency jitter — would
    shift *which* calls fail. Keeping them apart makes the pass/fail sequence a
    function of the seed and ``error_rate`` alone, which is what lets the two
    M2 runs (``--error-rate 0.4`` with and without ``--latency-ms 3000``) be
    compared against each other at all.

    The streams are re-derived whenever ``state.seed`` changes, so a chaos-API
    reseed restarts the sequence without needing a second call.
    """

    def __init__(
        self,
        *,
        name: str,
        clock: Clock,
        state: MockChaosState | None = None,
        capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        """The provider's **config key** (``mock_chaos``), not the adapter name (``mock``).

        Several config entries may share one adapter, and metrics, health, and
        the ``X-Keel-Provider`` header all key on the entry.
        """

        self.state = state if state is not None else MockChaosState()
        self._clock = clock
        self._capabilities = capabilities
        self._seed_streams(self.state.seed)

    def _seed_streams(self, seed: int) -> None:
        self._failure_rng = Random(seed)
        self._class_rng = Random(seed + 1)
        self._latency_rng = Random(seed + 2)
        self._seeded_from = seed

    def _resync_streams(self) -> None:
        """Re-derive when the seed *value* has changed since the streams were built."""
        if self.state.seed != self._seeded_from:
            self._seed_streams(self.state.seed)

    def reseed(self) -> None:
        """Restart every stream from ``state.seed``, even if the value is unchanged.

        :meth:`_resync_streams` only fires on a value change, so replaying the
        *same* scenario twice — the common case when re-running a chaos demo —
        needs this explicit call. Setting a new seed value does not.
        """
        self._seed_streams(self.state.seed)

    def capabilities(self) -> frozenset[str]:
        """What config says this provider can do. The mock never checks a request against it.

        Capability *simulation* is outside the scope cap — the §5.7 filter is
        the router's job, and it reads config, not the adapter.
        """
        return self._capabilities

    def _draw_latency_ms(self) -> float:
        """Lognormal with ``latency_ms`` as the median, or fixed when sigma is zero."""
        median = self.state.latency_ms
        sigma = self.state.latency_sigma
        if sigma == 0.0 or median == 0.0:
            # Also guards log(0) below. A zero median with jitter is meaningless
            # rather than an error, so it collapses to zero latency.
            return median
        return self._latency_rng.lognormvariate(math.log(median), sigma)

    def _draw_error_class(self) -> ErrorClass:
        """Weighted pick over the configured mix. Validated non-empty on the state."""
        classes = list(self.state.error_classes)
        weights = [self.state.error_classes[member] for member in classes]
        return self._class_rng.choices(classes, weights=weights, k=1)[0]

    def _build_response(
        self, envelope: RequestEnvelope, prompt_tokens: int, completion_tokens: int
    ) -> dict[str, Any]:
        """A plausible OpenAI completion, deterministic given the request.

        No RNG is consumed here: the id derives from ``request_id`` and the
        token counts from payload size, so replaying a seed reproduces bodies
        as well as outcomes.
        """
        return {
            "id": f"chatcmpl-mock-{envelope.request_id}",
            "object": "chat.completion",
            "created": int(self._clock.now()),
            "model": _MOCK_MODEL_NAME,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _COMPLETION_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        """Sleep for the drawn latency, then succeed or fail per the chaos state.

        Latency goes through ``Clock.sleep``, so a test advances time rather
        than waiting for it: a 30-second injected delay costs the suite
        microseconds (NFR-2).
        """
        self._resync_streams()

        started = self._clock.now()
        # The state is in milliseconds because that is what the operator and
        # `loadgen --latency-ms` speak; Clock.sleep takes seconds. The mock owns
        # the conversion so nothing downstream has to remember which unit it holds.
        await self._clock.sleep(self._draw_latency_ms() / 1000.0)
        latency_ms = (self._clock.now() - started) * 1000.0

        if self._failure_rng.random() < self.state.error_rate:
            error_class = self._draw_error_class()
            return ProviderResult.failure(
                provider=self.name,
                latency_ms=latency_ms,
                # A provider failure is a return value, not an exception — the
                # executor has to record it, and an exception would unwind past it.
                error=NormalizedError(
                    error_class=error_class,
                    message=f"mock provider injected {error_class.value}",
                    provider_error_type="MockInjectedError",
                ),
            )

        prompt_tokens = _estimate_prompt_tokens(envelope.payload)
        completion_tokens = max(1, len(_COMPLETION_TEXT) // _CHARS_PER_TOKEN)
        return ProviderResult.success(
            provider=self.name,
            response=self._build_response(envelope, prompt_tokens, completion_tokens),
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def _estimate_prompt_tokens(payload: dict[str, Any]) -> int:
    """Rough token count from message text, so cost numbers move with input size.

    The conventional four-characters-per-token approximation. It is not a
    tokenizer and the Phase 4 cost engine should not be described as if it were
    fed one — but a constant would make every request cost the same, which is
    worse for exercising per-tenant cost attribution.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 1

    characters = 0
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                characters += len(content)
    return max(1, characters // _CHARS_PER_TOKEN)


if TYPE_CHECKING:
    # Static conformance, same idiom as keel/clock.py: fails `mypy --strict` if
    # MockAdapter drifts from the protocol. Phrased as a function rather than an
    # assignment because constructing one would need a real Clock instance.
    def _mock_is_an_adapter(adapter: MockAdapter) -> ProviderAdapter:
        return adapter
