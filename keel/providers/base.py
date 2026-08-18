"""The provider adapter interface, and the result every adapter returns.

One interface, four implementations (TECHNICAL-DESIGN.md §5.3): Cohere, Azure
OpenAI, Bedrock, and the mock. The adapter layer sits *above* LiteLLM rather
than being replaced by it (D4) for three reasons — it isolates the codebase
from library version churn, it is where per-provider error normalization lives,
and it is the only way to give the mock adapter first-class status.

**Adapters know nothing about Redis, health, or breakers.** An adapter calls
one provider and reports what happened. Recording the outcome is the executor's
job and only the executor's (decision D-C in PHASE-2-PLAN.md), which is what
keeps the mock honest and gives Phase 3's breaker exactly one call site to read
from. An adapter that recorded its own health would be a second, divergent
source of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keel.api.envelope import RequestEnvelope
from keel.providers.errors import NormalizedError

__all__ = ["ProviderAdapter", "ProviderResult"]


class ProviderResult(BaseModel):
    """The outcome of one attempt against one provider.

    Success or failure, never both and never neither — the invariant is
    enforced below rather than left to each adapter to respect. Build one
    through :meth:`success` or :meth:`failure`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """The config key of the provider that was called, e.g. ``cohere_primary``.

    The provider entry, not the adapter — several entries may share one adapter,
    and metrics, health, and the ``X-Keel-Provider`` header all key on the entry.
    """

    latency_ms: Annotated[float, Field(ge=0.0)]
    """Wall-clock time for this attempt, from the injected clock (ADR 0001).

    Measured around the provider call alone. The gap between this and the
    request's total duration is what ``keel_gateway_overhead_seconds`` reports,
    so S5 can be measured rather than asserted (§6).
    """

    response: dict[str, Any] | None = None
    """The normalized, OpenAI-shaped completion. ``None`` on failure."""

    error: NormalizedError | None = None
    """The failure in Keel's vocabulary. ``None`` on success."""

    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    """Token counts as the provider reported them.

    ``None`` when it reported none — which the Phase 4 cost engine must treat as
    *unknown* rather than as zero, or a provider that omits usage data silently
    becomes the cheapest one in the report.
    """

    @model_validator(mode="after")
    def _check_success_xor_failure(self) -> ProviderResult:
        if self.error is None and self.response is None:
            raise ValueError(
                "ProviderResult must carry either a response or an error; got neither. "
                "Use ProviderResult.success(...) or ProviderResult.failure(...)."
            )
        if self.error is not None and self.response is not None:
            raise ValueError(
                "ProviderResult carries both a response and an error. A failed attempt "
                "that also returned a body is a mapping bug, not a partial success."
            )
        return self

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def success(
        cls,
        *,
        provider: str,
        response: dict[str, Any],
        latency_ms: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> ProviderResult:
        return cls(
            provider=provider,
            response=response,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    @classmethod
    def failure(
        cls, *, provider: str, error: NormalizedError, latency_ms: float
    ) -> ProviderResult:
        return cls(provider=provider, error=error, latency_ms=latency_ms)


class ProviderAdapter(Protocol):
    """What every provider implementation must offer (§5.3).

    Structural, like :class:`keel.clock.Clock` — an adapter conforms by having
    the right shape, with no base class to inherit and no registration step.
    """

    @property
    def name(self) -> str:
        """The provider entry's config key, echoed into ``X-Keel-Provider``.

        Declared read-only so an implementation may satisfy it with either a
        plain attribute or a property.
        """
        ...

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        """Call the provider once.

        Returns a :class:`ProviderResult` for *both* outcomes — a provider
        failure is a normal return value, not an exception. Failure is the case
        the whole gateway exists to handle, so it travels through the same path
        as success rather than unwinding the stack past the executor that needs
        to record it.
        """
        ...

    def capabilities(self) -> frozenset[str]:
        """What this provider can semantically do, for the §5.7 capability filter.

        ``frozenset`` rather than §5.3's ``set``, matching
        ``ProviderConfig.capabilities`` and ``RequestEnvelope.capabilities``.
        """
        ...


if TYPE_CHECKING:
    # Static conformance for the protocol itself: this fails `mypy --strict` if
    # the shape below ever drifts from ProviderAdapter. The real adapters arrive
    # in P1-T4 and P1-T5; until one exists, this is what keeps the interface
    # honest. Same idiom as keel/clock.py.
    class _ReferenceAdapter:
        name: str

        async def invoke(self, envelope: RequestEnvelope) -> ProviderResult: ...

        def capabilities(self) -> frozenset[str]: ...

    _reference_is_an_adapter: ProviderAdapter = _ReferenceAdapter()
