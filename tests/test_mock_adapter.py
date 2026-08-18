"""Tests for the in-process mock provider (FR-2.2, NFR-2).

Two claims this module has to earn, both from the P1-T4 "Done when":

1. A fixed seed produces an exact outcome sequence, so a chaos scenario is
   reproducible rather than merely plausible.
2. Injected latency advances ``ManualClock`` without real waiting — a 3-second
   delay costs the suite microseconds. Asserted in wall-clock terms below
   rather than trusted, the way ``tests/test_clock.py`` does it.

A third property is asserted here that the plan did not require but the M2 runs
depend on: **which** calls fail is a function of the seed and ``error_rate``
alone. Changing latency or the class mix must not perturb it, or the two M2
load runs cannot be compared to each other.

No network, no Redis, no real time.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from keel.api.envelope import RequestEnvelope
from keel.clock import ManualClock
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.errors import ErrorClass
from keel.providers.mock import DEFAULT_ERROR_MIX, MockAdapter, MockChaosState

PROVIDER_KEY = "mock_chaos"
START = 1_000.0


def envelope(request_id: str = "req-1", content: str = "hi") -> RequestEnvelope:
    return RequestEnvelope(
        request_id=request_id,
        tenant="acme",
        feature="support-summary",
        request_class="interactive_chat",
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload={"model": "keel", "messages": [{"role": "user", "content": content}]},
        received_at=START,
    )


def build(**knobs: Any) -> tuple[MockAdapter, ManualClock]:
    clock = ManualClock(start=START)
    adapter = MockAdapter(name=PROVIDER_KEY, clock=clock, state=MockChaosState(**knobs))
    return adapter, clock


async def outcomes(adapter: MockAdapter, count: int) -> str:
    """A compact pass/fail string, so a sequence can be asserted as a literal."""
    results = [await adapter.invoke(envelope(f"req-{index}")) for index in range(count)]
    return "".join("." if result.ok else "X" for result in results)


# --------------------------------------------------------------------------
# "Done when" #1 — a fixed seed gives an exact sequence
# --------------------------------------------------------------------------


async def test_a_fixed_seed_produces_an_exact_pass_fail_sequence() -> None:
    """The literal is the point: any change to draw order has to be deliberate."""
    adapter, _ = build(error_rate=0.5, seed=7)

    assert await outcomes(adapter, 20) == "XX.X.XX.XXXXX.XX...X"


async def test_the_same_seed_replays_the_same_sequence() -> None:
    first, _ = build(error_rate=0.5, seed=7)
    second, _ = build(error_rate=0.5, seed=7)

    assert await outcomes(first, 20) == await outcomes(second, 20)


async def test_a_different_seed_gives_a_different_sequence() -> None:
    """Otherwise the seed is decoration and reproducibility means nothing."""
    first, _ = build(error_rate=0.5, seed=7)
    second, _ = build(error_rate=0.5, seed=8)

    assert await outcomes(first, 20) != await outcomes(second, 20)


async def test_setting_a_new_seed_re_derives_the_streams() -> None:
    """The chaos API sets a seed and the sequence follows, with no second call."""
    adapter, _ = build(error_rate=0.5, seed=7)
    await outcomes(adapter, 20)

    adapter.state.seed = 99
    after_change = await outcomes(adapter, 20)

    fresh, _ = build(error_rate=0.5, seed=99)
    assert after_change == await outcomes(fresh, 20), "must match a mock born with seed 99"


async def test_reseed_replays_the_same_scenario() -> None:
    """Re-running one chaos demo must reproduce it; the seed value has not changed,
    so the value-change check alone would not fire."""
    adapter, _ = build(error_rate=0.5, seed=7)
    original = await outcomes(adapter, 20)

    adapter.reseed()

    assert await outcomes(adapter, 20) == original


@pytest.mark.parametrize(
    ("error_rate", "expected"),
    [
        pytest.param(0.0, "." * 20, id="zero-is-a-valid-fully-quiet-setting"),
        pytest.param(1.0, "X" * 20, id="one-fails-everything"),
    ],
)
async def test_the_error_rate_extremes(error_rate: float, expected: str) -> None:
    adapter, _ = build(error_rate=error_rate, seed=7)

    assert await outcomes(adapter, 20) == expected


async def test_the_failure_sequence_ignores_latency_and_class_mix() -> None:
    """Three separate RNG streams exist for exactly this.

    The two M2 runs differ only by `--latency-ms`; if that shifted which calls
    failed, the dashboards could not be compared against each other.
    """
    plain, _ = build(error_rate=0.4, seed=7)
    decorated, _ = build(
        error_rate=0.4,
        seed=7,
        latency_ms=3000.0,
        latency_sigma=0.6,
        error_classes={ErrorClass.TIMEOUT: 1.0},
    )

    assert await outcomes(plain, 30) == await outcomes(decorated, 30)


# --------------------------------------------------------------------------
# "Done when" #2 — latency advances the clock, and costs no real time
# --------------------------------------------------------------------------


async def test_injected_latency_advances_the_manual_clock_without_waiting() -> None:
    """The load-bearing NFR-2 property, asserted in wall-clock terms."""
    adapter, clock = build(latency_ms=3000.0, seed=7)

    started = time.perf_counter()
    results = [await adapter.invoke(envelope(f"req-{index}")) for index in range(12)]
    elapsed = time.perf_counter() - started

    assert clock.now() - START == pytest.approx(36.0), "12 calls x 3s of simulated time"
    assert all(result.latency_ms == pytest.approx(3000.0) for result in results)
    assert elapsed < 0.5, "simulated latency must not become real waiting"


async def test_sigma_zero_makes_every_call_identical() -> None:
    """p50 == p95 == p99, so a configured latency is exactly what the panel shows."""
    adapter, _ = build(latency_ms=250.0, latency_sigma=0.0, seed=7)

    samples = [(await adapter.invoke(envelope())).latency_ms for _ in range(10)]

    assert samples == [250.0] * 10


async def test_a_positive_sigma_spreads_latency_around_the_median() -> None:
    """The right tail Phase 3's p95-breach trigger needs to be tested against."""
    adapter, _ = build(latency_ms=100.0, latency_sigma=0.5, seed=7)

    samples = sorted([(await adapter.invoke(envelope())).latency_ms for _ in range(200)])

    assert len(set(samples)) > 1, "sigma > 0 must actually vary the draw"
    assert all(sample > 0.0 for sample in samples), "Clock.sleep rejects negatives"
    assert samples[100] == pytest.approx(100.0, rel=0.25), "median near latency_ms"
    assert samples[-1] > samples[100], "a right tail exists"


async def test_zero_latency_is_allowed_and_draws_nothing() -> None:
    """Guards log(0). A zero median with jitter is meaningless, not an error."""
    adapter, clock = build(latency_ms=0.0, latency_sigma=0.5, seed=7)

    result = await adapter.invoke(envelope())

    assert result.latency_ms == 0.0
    assert clock.now() == START


# --------------------------------------------------------------------------
# The error class mix
# --------------------------------------------------------------------------


async def test_failures_split_across_the_taxonomy_by_default() -> None:
    """The M2 exit criterion: the error-rate panel splits across classes."""
    adapter, _ = build(error_rate=1.0, seed=7)

    results = [await adapter.invoke(envelope()) for _ in range(200)]
    seen = {result.error.error_class for result in results if result.error is not None}

    assert len(seen) > 1, "a single class would make the taxonomy panel one flat bar"
    assert seen <= set(DEFAULT_ERROR_MIX)


async def test_the_default_mix_roughly_matches_its_weights() -> None:
    adapter, _ = build(error_rate=1.0, seed=7)

    results = [await adapter.invoke(envelope()) for _ in range(1_000)]
    rate_limits = sum(
        1 for result in results if result.error is not None
        and result.error.error_class is ErrorClass.RATE_LIMIT
    )

    assert 0.30 < rate_limits / 1_000 < 0.50, "RATE_LIMIT is weighted 0.40"


async def test_a_single_entry_pins_one_class() -> None:
    adapter, _ = build(error_rate=1.0, seed=7, error_classes={ErrorClass.TIMEOUT: 1.0})

    results = [await adapter.invoke(envelope()) for _ in range(50)]

    assert all(
        result.error is not None and result.error.error_class is ErrorClass.TIMEOUT
        for result in results
    )


def test_the_default_mix_excludes_the_classes_a_provider_cannot_cause() -> None:
    """AUTH_FAILURE is our config and BAD_REQUEST is the client's payload."""
    assert ErrorClass.AUTH_FAILURE not in DEFAULT_ERROR_MIX
    assert ErrorClass.BAD_REQUEST not in DEFAULT_ERROR_MIX


def test_the_default_mix_keeps_one_class_that_does_not_trip_the_breaker() -> None:
    """So the M2 dashboard shows the breaker's input below the raw error rate (D7)."""
    excluded = [member for member in DEFAULT_ERROR_MIX if not member.counts_toward_breaker]

    assert excluded == [ErrorClass.CONTENT_FILTER]


# --------------------------------------------------------------------------
# The result and the response body
# --------------------------------------------------------------------------


async def test_a_failure_is_returned_not_raised() -> None:
    adapter, _ = build(error_rate=1.0, seed=7)

    result = await adapter.invoke(envelope())

    assert isinstance(result, ProviderResult)
    assert result.ok is False
    assert result.error is not None
    assert result.error.provider_error_type == "MockInjectedError"
    assert result.response is None


async def test_the_result_names_the_config_key_not_the_adapter() -> None:
    """Metrics, health, and X-Keel-Provider all key on the provider entry."""
    adapter, _ = build(seed=7)

    result = await adapter.invoke(envelope())

    assert result.provider == PROVIDER_KEY
    assert adapter.name == PROVIDER_KEY


async def test_the_response_is_a_plausible_openai_completion() -> None:
    adapter, _ = build(seed=7)

    result = await adapter.invoke(envelope(request_id="req-42"))

    assert result.response is not None
    body = result.response
    assert set(body) == {"id", "object", "created", "model", "choices", "usage"}
    assert body["id"] == "chatcmpl-mock-req-42", "derived from request_id, so replayable"
    assert body["object"] == "chat.completion"
    assert body["created"] == int(START)
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["finish_reason"] == "stop"


async def test_usage_totals_are_internally_consistent() -> None:
    adapter, _ = build(seed=7)

    result = await adapter.invoke(envelope())

    assert result.response is not None
    usage = result.response["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["prompt_tokens"] == result.prompt_tokens
    assert usage["completion_tokens"] == result.completion_tokens


async def test_prompt_tokens_move_with_input_size() -> None:
    """A constant would make every request cost the same in Phase 4."""
    adapter, _ = build(seed=7)

    short = await adapter.invoke(envelope(content="hi"))
    long = await adapter.invoke(envelope(content="word " * 200))

    assert short.prompt_tokens is not None and long.prompt_tokens is not None
    assert long.prompt_tokens > short.prompt_tokens


async def test_an_empty_payload_still_reports_at_least_one_token() -> None:
    """Zero tokens would read as a free request rather than an unmeasured one."""
    adapter, clock = build(seed=7)
    bare = RequestEnvelope(
        request_id="req-1",
        tenant="acme",
        feature="f",
        request_class="interactive_chat",
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload={},
        received_at=START,
    )

    result = await adapter.invoke(bare)

    assert result.prompt_tokens == 1


# --------------------------------------------------------------------------
# The adapter interface, and the chaos state's guard rails
# --------------------------------------------------------------------------


async def test_the_mock_satisfies_the_adapter_protocol() -> None:
    adapter: ProviderAdapter = MockAdapter(
        name=PROVIDER_KEY,
        clock=ManualClock(start=START),
        capabilities=frozenset({"citations", "tool_use"}),
    )

    assert adapter.name == PROVIDER_KEY
    assert adapter.capabilities() == frozenset({"citations", "tool_use"})
    assert (await adapter.invoke(envelope())).ok is True


def test_capabilities_default_to_empty_rather_than_to_everything() -> None:
    """Claiming a capability it was not configured with would defeat the §5.7 filter."""
    adapter = MockAdapter(name=PROVIDER_KEY, clock=ManualClock(start=START))

    assert adapter.capabilities() == frozenset()


def test_default_state_is_quiet_and_fast() -> None:
    """An unconfigured mock must not inject chaos nobody asked for."""
    state = MockChaosState()

    assert state.error_rate == 0.0
    assert state.latency_sigma == 0.0
    assert state.error_classes == DEFAULT_ERROR_MIX


def test_each_state_gets_its_own_copy_of_the_default_mix() -> None:
    """A shared mutable default would let one adapter's chaos leak into another's."""
    first = MockChaosState()
    second = MockChaosState()

    first.error_classes[ErrorClass.TIMEOUT] = 99.0

    assert second.error_classes == DEFAULT_ERROR_MIX
    assert DEFAULT_ERROR_MIX[ErrorClass.TIMEOUT] == 0.25


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("error_rate", 5.0, id="error-rate-above-one"),
        pytest.param("error_rate", -0.1, id="negative-error-rate"),
        pytest.param("latency_ms", -1.0, id="negative-latency"),
        pytest.param("latency_sigma", -0.1, id="negative-sigma"),
    ],
)
def test_invalid_knobs_are_rejected_on_assignment(field: str, value: float) -> None:
    """The chaos API writes straight from HTTP input, so assignment is the boundary."""
    state = MockChaosState()

    with pytest.raises(ValueError, match="less than or equal to|greater than or equal to"):
        setattr(state, field, value)


@pytest.mark.parametrize(
    ("mix", "expected"),
    [
        pytest.param({}, "error_classes is empty", id="empty-mix-leaves-nothing-to-draw"),
        pytest.param(
            {ErrorClass.TIMEOUT: 0.0}, "at least one weight must be positive",
            id="all-zero-weights-leave-nothing-to-draw",
        ),
    ],
)
def test_an_undrawable_error_mix_is_rejected(mix: dict[ErrorClass, float], expected: str) -> None:
    """Otherwise this raises inside the executor on the first failure — during a demo."""
    with pytest.raises(ValueError, match=expected):
        MockChaosState(error_classes=mix)


def test_an_undrawable_mix_is_also_rejected_on_assignment() -> None:
    state = MockChaosState()

    with pytest.raises(ValueError, match="error_classes is empty"):
        state.error_classes = {}
