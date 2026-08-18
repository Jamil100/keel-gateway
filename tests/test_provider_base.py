"""Tests for the adapter protocol and its result type (§5.3, FR-2.4).

Two claims to earn. First, ``ProviderResult`` is a genuine sum type: an adapter
cannot return a result that is both a success and a failure, or neither,
because the executor and the health recorder branch on exactly that. Second,
the protocol is implementable — a stub adapter here stands in until the mock
(P1-T4) and Cohere (P1-T5) adapters exist.

No network, no Redis, and time comes from ``ManualClock`` (NFR-2).
"""

from __future__ import annotations

from typing import Any

import pytest

from keel.api.envelope import RequestEnvelope
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.errors import ErrorClass, NormalizedError

COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
}


def envelope(**overrides: Any) -> RequestEnvelope:
    defaults: dict[str, Any] = {
        "request_id": "req-1",
        "tenant": "acme",
        "feature": "support-summary",
        "request_class": "interactive_chat",
        "capabilities": frozenset(),
        "deferrable": False,
        "idempotency_key": None,
        "payload": {"model": "keel", "messages": []},
        "received_at": 1_000.0,
    }
    return RequestEnvelope(**{**defaults, **overrides})


def timeout_error() -> NormalizedError:
    return NormalizedError(error_class=ErrorClass.TIMEOUT, message="upstream timed out")


# --------------------------------------------------------------------------
# Success or failure, never both and never neither
# --------------------------------------------------------------------------


def test_success_carries_the_response_and_no_error() -> None:
    result = ProviderResult.success(
        provider="cohere_primary",
        response=COMPLETION,
        latency_ms=142.0,
        prompt_tokens=11,
        completion_tokens=3,
    )

    assert result.ok is True
    assert result.error is None
    assert result.response == COMPLETION
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 3


def test_failure_carries_the_error_and_no_response() -> None:
    result = ProviderResult.failure(
        provider="cohere_primary", error=timeout_error(), latency_ms=30_000.0
    )

    assert result.ok is False
    assert result.response is None
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TIMEOUT


def test_a_result_that_is_neither_is_rejected() -> None:
    """An adapter returning an empty result would read as a success downstream."""
    with pytest.raises(ValueError, match="either a response or an error"):
        ProviderResult(provider="cohere_primary", latency_ms=1.0)


def test_a_result_that_is_both_is_rejected() -> None:
    """A failure that also returned a body is a mapping bug, not a partial success."""
    with pytest.raises(ValueError, match="both a response and an error"):
        ProviderResult(
            provider="cohere_primary",
            latency_ms=1.0,
            response=COMPLETION,
            error=timeout_error(),
        )


def test_latency_is_always_recorded_even_on_failure() -> None:
    """A timeout's duration is the most interesting latency sample there is."""
    result = ProviderResult.failure(
        provider="cohere_primary", error=timeout_error(), latency_ms=30_000.0
    )

    assert result.latency_ms == 30_000.0


# --------------------------------------------------------------------------
# Field constraints
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("latency_ms", -1.0, id="negative-latency"),
        pytest.param("prompt_tokens", -1, id="negative-prompt-tokens"),
        pytest.param("completion_tokens", -1, id="negative-completion-tokens"),
    ],
)
def test_negative_measurements_are_rejected(field: str, value: float) -> None:
    kwargs: dict[str, Any] = {
        "provider": "cohere_primary",
        "response": COMPLETION,
        "latency_ms": 1.0,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match="greater than or equal to 0"):
        ProviderResult(**kwargs)


def test_absent_token_counts_are_none_not_zero() -> None:
    """Zero would make a provider that omits usage data the cheapest in Phase 4."""
    result = ProviderResult.success(
        provider="mock_chaos", response=COMPLETION, latency_ms=1.0
    )

    assert result.prompt_tokens is None
    assert result.completion_tokens is None


def test_result_is_frozen() -> None:
    result = ProviderResult.success(provider="mock_chaos", response=COMPLETION, latency_ms=1.0)

    with pytest.raises(ValueError, match="frozen"):
        result.provider = "somewhere_else"


def test_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        ProviderResult(
            provider="mock_chaos",
            response=COMPLETION,
            latency_ms=1.0,
            cached=True,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------
# The protocol is implementable, and a failure is a return value
# --------------------------------------------------------------------------


class StubAdapter:
    """Minimal conforming adapter. The real ones arrive in P1-T4 and P1-T5."""

    def __init__(self, *, name: str, result: ProviderResult) -> None:
        self.name = name
        self._result = result

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        return self._result

    def capabilities(self) -> frozenset[str]:
        return frozenset({"citations"})


async def test_a_plain_class_satisfies_the_adapter_protocol() -> None:
    """Structural, like Clock — no base class, no registration step."""
    result = ProviderResult.success(provider="mock_chaos", response=COMPLETION, latency_ms=5.0)
    adapter: ProviderAdapter = StubAdapter(name="mock_chaos", result=result)

    assert adapter.name == "mock_chaos"
    assert adapter.capabilities() == frozenset({"citations"})
    assert await adapter.invoke(envelope()) is result


async def test_a_provider_failure_is_returned_not_raised() -> None:
    """Failure is the case the gateway exists to handle, so it must not unwind
    the stack past the executor that has to record it."""
    failed = ProviderResult.failure(
        provider="cohere_primary", error=timeout_error(), latency_ms=30_000.0
    )
    adapter: ProviderAdapter = StubAdapter(name="cohere_primary", result=failed)

    returned = await adapter.invoke(envelope())

    assert returned.ok is False
    assert returned.error is not None
    assert returned.error.counts_toward_breaker is True
