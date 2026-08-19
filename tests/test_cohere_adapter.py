"""Tests for the Cohere adapter (FR-2.1, FR-2.4, NFR-2).

Three claims this module has to earn:

1. **Keel decides the target, not the client.** The model, the credential, and
   the streaming flag are overridden on every call, whatever the payload said.
2. **A provider failure is a return value.** Nothing the provider does — not an
   exception, not a malformed body — may unwind past the executor that has to
   record it (D-C).
3. **The §5.4 mapping respects the real exception hierarchy.** LiteLLM's
   exceptions subclass the OpenAI SDK's, so the tree's root is
   `openai.APIError` and *not* `litellm.APIError` — which is a sibling of the
   rest rather than their ancestor. A table that gets either the root or the
   ordering wrong still looks right and still classifies most things
   plausibly, while silently reclassifying timeouts and content filters.

That last point costs about 4.5 seconds of `import litellm` at collection time,
and it is worth it. A test written against stand-in exception classes would
happily pass a map built on the wrong root — which is exactly the bug the
hierarchy tests below caught while this module was being written.

LiteLLM itself is never called. Every test injects a fake `acompletion`, so
there is no network and no credential — except the single `real_provider` test
at the end, which CI excludes.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import openai
import pytest
from litellm import exceptions as lle

from keel.api.envelope import RequestEnvelope
from keel.clock import ManualClock
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.cohere import CohereAdapter, normalize_litellm_error
from keel.providers.errors import ErrorClass

PROVIDER_KEY = "cohere_primary"
MODEL = "command-a"
START = 1_000.0

SUCCESS_BODY: dict[str, Any] = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "created": 1_700_000_000,
    "model": "command-a",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}


def envelope(request_id: str = "req-1", **payload_overrides: Any) -> RequestEnvelope:
    payload: dict[str, Any] = {
        "model": "keel",
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(payload_overrides)
    return RequestEnvelope(
        request_id=request_id,
        tenant="acme",
        feature="support-summary",
        request_class="interactive_chat",
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload=payload,
        received_at=START,
    )


class FakeCompletion:
    """Stands in for ``litellm.acompletion``: records the call, then obeys the test.

    Injected rather than monkeypatched, which is what lets a test read back the
    exact kwargs the adapter built.
    """

    def __init__(
        self,
        *,
        returns: Any = None,
        raises: Exception | None = None,
        advance: float = 0.0,
        clock: ManualClock | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._returns = SUCCESS_BODY if returns is None else returns
        self._raises = raises
        self._advance = advance
        self._clock = clock

    @property
    def call(self) -> dict[str, Any]:
        """The one call, for the common single-invocation case."""
        assert len(self.calls) == 1, f"expected exactly one provider call, got {len(self.calls)}"
        return self.calls[0]

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # Time only moves if the test says so: ManualClock does not advance on
        # its own, so latency assertions are explicit rather than incidental.
        if self._clock is not None and self._advance:
            self._clock.advance(self._advance)
        if self._raises is not None:
            raise self._raises
        return self._returns


def build(
    **fake_kwargs: Any,
) -> tuple[CohereAdapter, FakeCompletion, ManualClock]:
    clock = ManualClock(start=START)
    timeout_ms = fake_kwargs.pop("timeout_ms", 30000)
    capabilities = fake_kwargs.pop("capabilities", frozenset())
    fake = FakeCompletion(clock=clock, **fake_kwargs)
    adapter = CohereAdapter(
        name=PROVIDER_KEY,
        model=MODEL,
        api_key="test-key",
        clock=clock,
        capabilities=capabilities,
        timeout_ms=timeout_ms,
        acompletion=fake,
    )
    return adapter, fake, clock


# --------------------------------------------------------------------------
# Claim #1 — Keel decides the target, not the client
# --------------------------------------------------------------------------


async def test_the_configured_model_overrides_whatever_the_client_asked_for() -> None:
    """The preference list would be advisory if a client could name its own target."""
    adapter, fake, _ = build()

    await adapter.invoke(envelope(model="gpt-4o-mini"))

    assert fake.call["model"] == "cohere/command-a"


async def test_the_credential_is_passed_explicitly() -> None:
    """So the registry's startup check and this call read one value, not two."""
    adapter, fake, _ = build()

    await adapter.invoke(envelope())

    assert fake.call["api_key"] == "test-key"


async def test_streaming_is_forced_off() -> None:
    """D8 defers streaming; a stream slipping through returns a generator."""
    adapter, fake, _ = build()

    await adapter.invoke(envelope(stream=True))

    assert fake.call["stream"] is False


async def test_the_timeout_is_passed_down_in_seconds() -> None:
    """Config speaks milliseconds, LiteLLM takes seconds; the adapter converts."""
    adapter, fake, _ = build(timeout_ms=30000)

    await adapter.invoke(envelope())

    assert fake.call["timeout"] == 30.0


async def test_no_timeout_is_sent_when_config_omits_one() -> None:
    """`None` means "no gateway-imposed timeout", not "zero"."""
    adapter, fake, _ = build(timeout_ms=None)

    await adapter.invoke(envelope())

    assert "timeout" not in fake.call


async def test_the_rest_of_the_payload_is_passed_through_untouched() -> None:
    adapter, fake, _ = build()

    await adapter.invoke(envelope(temperature=0.2, messages=[{"role": "user", "content": "x"}]))

    assert fake.call["temperature"] == 0.2
    assert fake.call["messages"] == [{"role": "user", "content": "x"}]


async def test_the_envelope_payload_is_not_mutated() -> None:
    """The envelope is frozen and shared; the adapter must copy before editing."""
    adapter, _, _ = build()
    request = envelope()

    await adapter.invoke(request)

    assert request.payload["model"] == "keel"
    assert "api_key" not in request.payload


# --------------------------------------------------------------------------
# The successful result
# --------------------------------------------------------------------------


async def test_a_successful_call_returns_the_openai_shaped_body() -> None:
    adapter, _, _ = build()

    result = await adapter.invoke(envelope())

    assert result.ok
    assert result.response == SUCCESS_BODY


async def test_the_result_names_the_config_key_not_the_adapter() -> None:
    """Several entries may share one adapter; everything downstream keys on the entry."""
    adapter, _, _ = build()

    result = await adapter.invoke(envelope())

    assert adapter.name == PROVIDER_KEY
    assert result.provider == PROVIDER_KEY


async def test_token_counts_are_read_from_the_usage_block() -> None:
    adapter, _, _ = build()

    result = await adapter.invoke(envelope())

    assert result.prompt_tokens == 11
    assert result.completion_tokens == 5


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**SUCCESS_BODY, "usage": None}, id="usage-is-null"),
        pytest.param({k: v for k, v in SUCCESS_BODY.items() if k != "usage"}, id="usage-absent"),
        pytest.param({**SUCCESS_BODY, "usage": {}}, id="usage-is-empty"),
        pytest.param(
            {**SUCCESS_BODY, "usage": {"prompt_tokens": "11", "completion_tokens": 5}},
            id="usage-is-not-a-number",
        ),
    ],
)
async def test_absent_token_counts_are_none_rather_than_zero(body: dict[str, Any]) -> None:
    """`0` would make a provider that omits usage the cheapest one in the Phase 4 report."""
    adapter, _, _ = build(returns=body)

    result = await adapter.invoke(envelope())

    assert result.ok
    assert result.prompt_tokens is None


async def test_a_pydantic_response_is_normalized_to_a_plain_dict() -> None:
    """LiteLLM returns a ModelResponse, not a dict; downstream expects a mapping."""
    from pydantic import BaseModel

    class ModelResponseLike(BaseModel):
        id: str
        object: str

    adapter, _, _ = build(returns=ModelResponseLike(id="chatcmpl-x", object="chat.completion"))

    result = await adapter.invoke(envelope())

    assert result.response == {"id": "chatcmpl-x", "object": "chat.completion"}


async def test_latency_comes_from_the_injected_clock() -> None:
    """Never `time.time()` (ADR 0001) — so a test can pin the number exactly."""
    adapter, _, _ = build(advance=1.25)

    result = await adapter.invoke(envelope())

    assert result.latency_ms == 1250.0


# --------------------------------------------------------------------------
# Claim #2 — a provider failure is a return value, not an exception
# --------------------------------------------------------------------------


async def test_a_raising_provider_produces_a_result_rather_than_an_exception() -> None:
    """Failure is the case the gateway exists to handle (D-C)."""
    adapter, _, _ = build(
        raises=lle.RateLimitError(message="slow down", llm_provider="cohere", model=MODEL)
    )

    result = await adapter.invoke(envelope())

    assert isinstance(result, ProviderResult)
    assert not result.ok
    assert result.error is not None
    assert result.error.error_class is ErrorClass.RATE_LIMIT


async def test_a_failure_still_reports_latency() -> None:
    """A slow failure is a different signal from a fast one; both reach the health window."""
    adapter, _, _ = build(advance=0.5, raises=lle.APIError(500, "boom", "cohere", MODEL))

    result = await adapter.invoke(envelope())

    assert result.latency_ms == 500.0


async def test_a_response_that_is_not_a_completion_becomes_a_failure() -> None:
    """A stream wrapper reaching the executor would fail far from its cause."""
    adapter, _, _ = build(returns="not a completion")

    result = await adapter.invoke(envelope())

    assert not result.ok
    assert result.error is not None
    assert result.error.error_class is ErrorClass.SERVER_ERROR


async def test_a_cancelled_request_is_not_recorded_as_a_provider_failure() -> None:
    """Cancellation is the caller giving up, not the provider failing.

    `except Exception` must not swallow it: recording a cancelled request as
    provider health would be a lie, and in Phase 3 it would trip a breaker over
    a client disconnecting.
    """
    import asyncio

    adapter, _, _ = build(raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await adapter.invoke(envelope())


# --------------------------------------------------------------------------
# Claim #3 — the §5.4 mapping, and the ordering it depends on
#
# The table itself moved to keel/providers/normalize.py in P2-T1 (ADR 0007);
# these tests stay here because they exercise it through the Cohere entry point,
# which is the seam the adapter actually calls. Fixture replay lives in
# tests/test_provider_normalize.py and does not replace them: this file pins the
# exception *type* table and its two load-bearing orderings, which no captured
# JSON body can express.
# --------------------------------------------------------------------------


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.cohere.com/v2/chat"))


# Transcribed by hand rather than read from the module, the same way
# tests/test_provider_errors.py transcribes the §5.4 truth table: the two must
# independently agree, or the test is only asserting that the code equals itself.
ERROR_MAPPING = [
    pytest.param(
        lle.Timeout(message="timed out", model=MODEL, llm_provider="cohere"),
        ErrorClass.TIMEOUT,
        id="timeout",
    ),
    pytest.param(
        lle.RateLimitError(message="429", llm_provider="cohere", model=MODEL),
        ErrorClass.RATE_LIMIT,
        id="rate-limit",
    ),
    pytest.param(
        lle.AuthenticationError(message="bad key", llm_provider="cohere", model=MODEL),
        ErrorClass.AUTH_FAILURE,
        id="authentication",
    ),
    pytest.param(
        lle.PermissionDeniedError(
            message="forbidden", llm_provider="cohere", model=MODEL, response=_response(403)
        ),
        ErrorClass.AUTH_FAILURE,
        id="permission-denied",
    ),
    pytest.param(
        lle.BudgetExceededError(current_cost=12.0, max_budget=10.0),
        ErrorClass.QUOTA_EXHAUSTED,
        id="budget-exceeded",
    ),
    pytest.param(
        lle.ContentPolicyViolationError(message="blocked", model=MODEL, llm_provider="cohere"),
        ErrorClass.CONTENT_FILTER,
        id="content-policy-violation",
    ),
    pytest.param(
        lle.ContextWindowExceededError(message="too long", model=MODEL, llm_provider="cohere"),
        ErrorClass.BAD_REQUEST,
        id="context-window-exceeded",
    ),
    pytest.param(
        lle.BadRequestError(message="bad", model=MODEL, llm_provider="cohere"),
        ErrorClass.BAD_REQUEST,
        id="bad-request",
    ),
    pytest.param(
        lle.UnprocessableEntityError(
            message="unprocessable", model=MODEL, llm_provider="cohere", response=_response(422)
        ),
        ErrorClass.BAD_REQUEST,
        id="unprocessable-entity",
    ),
    pytest.param(
        lle.NotFoundError(message="no such model", model=MODEL, llm_provider="cohere"),
        ErrorClass.BAD_REQUEST,
        id="not-found",
    ),
    pytest.param(
        lle.InternalServerError(message="500", llm_provider="cohere", model=MODEL),
        ErrorClass.SERVER_ERROR,
        id="internal-server-error",
    ),
    pytest.param(
        lle.ServiceUnavailableError(message="503", llm_provider="cohere", model=MODEL),
        ErrorClass.SERVER_ERROR,
        id="service-unavailable",
    ),
    pytest.param(
        lle.BadGatewayError(message="502", llm_provider="cohere", model=MODEL),
        ErrorClass.SERVER_ERROR,
        id="bad-gateway",
    ),
    pytest.param(
        lle.APIConnectionError(message="connection reset", llm_provider="cohere", model=MODEL),
        ErrorClass.SERVER_ERROR,
        id="api-connection-error",
    ),
    pytest.param(
        lle.APIError(500, "generic", "cohere", MODEL),
        ErrorClass.SERVER_ERROR,
        id="api-error",
    ),
]


@pytest.mark.parametrize(("exc", "expected"), ERROR_MAPPING)
def test_each_litellm_exception_maps_to_its_taxonomy_class(
    exc: Exception, expected: ErrorClass
) -> None:
    assert normalize_litellm_error(exc).error_class is expected


@pytest.mark.parametrize(("exc", "expected"), ERROR_MAPPING)
def test_every_mapped_error_names_the_exception_it_came_from(
    exc: Exception, expected: ErrorClass
) -> None:
    """P2-T1 diagnoses mapping gaps from this field; it must never be empty."""
    normalized = normalize_litellm_error(exc)

    assert normalized.provider_error_type == type(exc).__name__
    assert normalized.message


def test_a_timeout_is_not_swallowed_by_its_connection_error_parent() -> None:
    """`Timeout` subclasses `openai.APITimeoutError` -> `APIConnectionError` -> `APIError`.

    Ordered below any of them it would normalize to SERVER_ERROR — and §5.4
    separates the two precisely because latency budgets treat them differently.
    This test fails the moment the map is reordered.
    """
    exc = lle.Timeout(message="timed out", model=MODEL, llm_provider="cohere")

    assert isinstance(exc, openai.APIConnectionError), "the hierarchy this test guards has changed"
    assert normalize_litellm_error(exc).error_class is ErrorClass.TIMEOUT


def test_the_catch_all_is_the_openai_root_not_the_litellm_one() -> None:
    """`litellm.APIError` is a *sibling* of LiteLLM's other exceptions, not their ancestor.

    A table that used it as its catch-all would catch essentially nothing, and
    every real provider failure would fall through to the unmapped default —
    correct by luck (both say SERVER_ERROR) but warning on every single error
    and hiding genuine mapping gaps in the noise. This pins the relationship the
    map is built on.
    """
    rate_limit = lle.RateLimitError(message="429", llm_provider="cohere", model=MODEL)

    assert not isinstance(rate_limit, lle.APIError)
    assert isinstance(rate_limit, openai.APIError)


def test_an_unmapped_openai_sdk_error_still_classifies_rather_than_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raw OpenAI SDK error passed through by LiteLLM must not hit the default."""
    caplog.set_level(logging.WARNING, logger="keel.providers.normalize")
    exc = openai.RateLimitError(
        message="429", response=_response(429), body=None
    )

    assert normalize_litellm_error(exc).error_class is ErrorClass.RATE_LIMIT
    assert caplog.text == ""


def test_a_content_filter_is_not_swallowed_by_its_bad_request_parent() -> None:
    """`ContentPolicyViolationError` subclasses `BadRequestError`.

    Ordered below it, every content filter would read as BAD_REQUEST. Both are
    excluded from the breaker (D7) so the breaker would not notice — but the M2
    taxonomy panel, whose whole job is showing that split, would be wrong.
    """
    exc = lle.ContentPolicyViolationError(message="blocked", model=MODEL, llm_provider="cohere")

    assert isinstance(exc, lle.BadRequestError), "the hierarchy this test guards has changed"
    assert normalize_litellm_error(exc).error_class is ErrorClass.CONTENT_FILTER


def test_the_status_code_is_carried_through_when_there_is_one() -> None:
    exc = lle.RateLimitError(message="429", llm_provider="cohere", model=MODEL)

    assert normalize_litellm_error(exc).status_code == 429


def test_a_status_code_outside_the_http_range_is_dropped() -> None:
    """A library sentinel is not a provider response; `None` says so honestly."""
    exc = ValueError("no status here")
    exc.status_code = 0  # type: ignore[attr-defined]

    assert normalize_litellm_error(exc).status_code is None


def test_an_unmapped_exception_defaults_to_server_error_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The P2-T1 contract: a catch-all that records what it caught."""
    caplog.set_level(logging.WARNING, logger="keel.providers.normalize")

    normalized = normalize_litellm_error(ValueError("something new"))

    assert normalized.error_class is ErrorClass.SERVER_ERROR
    assert normalized.provider_error_type == "ValueError"
    assert "ValueError" in caplog.text


# --------------------------------------------------------------------------
# Conformance and capabilities
# --------------------------------------------------------------------------


async def test_the_cohere_adapter_satisfies_the_adapter_protocol() -> None:
    adapter: ProviderAdapter = CohereAdapter(
        name=PROVIDER_KEY,
        model=MODEL,
        api_key="test-key",
        clock=ManualClock(),
        acompletion=FakeCompletion(),
    )

    result = await adapter.invoke(envelope())

    assert result.ok
    assert adapter.capabilities() == frozenset()


def test_capabilities_are_whatever_config_declared() -> None:
    """The adapter reports them and never enforces them — §5.7 is the router's job."""
    adapter, _, _ = build(capabilities=frozenset({"citations", "tool_use"}))

    assert adapter.capabilities() == frozenset({"citations", "tool_use"})


# --------------------------------------------------------------------------
# The one live call. Excluded from CI (NFR-2); mind the EUR 75 budget (NFR-3).
# --------------------------------------------------------------------------


@pytest.mark.real_provider
async def test_a_live_cohere_call_returns_a_usable_completion() -> None:
    """Run deliberately: `pytest -m real_provider`. Costs real money.

    Everything above proves the adapter behaves correctly against a fake. Only
    this proves the fake resembles Cohere — that the model string, the
    credential, and the response shape are what the provider actually expects.
    """
    import os

    from keel.clock import SystemClock

    api_key = os.environ.get("COHERE_API_KEY")
    if not api_key:
        pytest.skip("COHERE_API_KEY is not set")

    adapter = CohereAdapter(
        name=PROVIDER_KEY,
        model=MODEL,
        api_key=api_key,
        clock=SystemClock(),
        timeout_ms=30000,
    )

    result = await adapter.invoke(
        envelope(messages=[{"role": "user", "content": "Reply with the single word: ok"}])
    )

    assert result.ok, result.error
    assert result.response is not None
    assert result.response["choices"][0]["message"]["content"]
    assert result.prompt_tokens is not None and result.prompt_tokens > 0
    assert result.latency_ms > 0
