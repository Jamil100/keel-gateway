"""Tests for the executor (P1-T6, D-C, TECHNICAL-DESIGN.md §4, §5.4).

Two things are being pinned here, and they pull in opposite directions.

The first is that the executor does *less* than it eventually will: it invokes
candidate 1 and returns whatever comes back, even a failure whose
`retry_elsewhere` says another provider is worth trying. Failover before the
Phase 2 health window exists would be the reactive-before-observable mistake
FR-3.4 rules out, so "candidate 2 was never called" is asserted rather than
assumed.

The second is the gateway timeout, which is the one place in this suite that
uses real time. `asyncio.wait_for` measures event-loop time, not the injected
`Clock`, and it cannot be made to do otherwise — `ManualClock.sleep` advances
time and returns on the same tick, so racing an invocation against
`clock.sleep(timeout)` has both finish together and double-advances the clock.
The timeout tests therefore use `SystemClock`, a fake adapter awaiting a real
`asyncio.sleep`, and a deadline of a few milliseconds. Total cost to the suite
is well under a tenth of a second, and nothing calls `time.sleep` (NFR-2).

P2-T2 adds a third thing: the executor now records every attempt into the health
window (D-C), which is the one call site Phase 3's breaker will read from. The
cases at the bottom pin that both branches above — a returned provider failure and
a synthesized gateway timeout — reach it under the right taxonomy field, because a
recorder wired to only the happy path would leave the breaker blind to exactly the
attempts it exists to notice.

Everything else runs on `ManualClock`. No network, and `fakeredis` rather than
Redis (NFR-2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from keel.api.envelope import RequestEnvelope
from keel.clock import Clock, ManualClock, SystemClock
from keel.config import KeelConfig, load_config
from keel.health.window import FIELD_OK, HealthWindow
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.credentials import ProviderCredentials
from keel.providers.errors import ErrorClass, NormalizedError
from keel.providers.mock import MockAdapter
from keel.providers.registry import build_registry
from keel.routing.executor import GATEWAY_TIMEOUT_ERROR_TYPE, Executor
from keel.routing.router import Router

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

CREDENTIALS = ProviderCredentials(cohere_api_key="test-key")

# `interactive_chat` with the mock first, so the default path in these tests
# needs no credentials and no network. The trailing `cohere_primary` is the
# candidate that must never be reached.
MOCK_FIRST = (
    "    preference: [cohere_primary, mock_chaos]\n    latency_budget_p95_ms: 4000\n",
    "    preference: [mock_chaos, cohere_primary]\n    latency_budget_p95_ms: 4000\n",
)


@pytest.fixture
def base_text() -> str:
    return SHIPPED_CONFIG.read_text(encoding="utf-8")


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def _write(text: str) -> Path:
        path = tmp_path / "keel.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write


def envelope(request_class: str = "interactive_chat") -> RequestEnvelope:
    return RequestEnvelope(
        request_id="req-1",
        tenant="acme",
        feature="support-summary",
        request_class=request_class,
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload={"model": "keel", "messages": [{"role": "user", "content": "hi"}]},
        received_at=0.0,
    )


def health_window(config: KeelConfig, clock: Clock) -> HealthWindow:
    """A window over `fakeredis`, so the executor's recording call has somewhere to go.

    Every executor now records (D-C), and `HealthWindow` is a required
    constructor argument precisely so a caller cannot forget to give it one. The
    tests below mostly ignore what lands in it; the ones under "What the executor
    records" are the ones that read it back.
    """
    return HealthWindow(redis=FakeRedis(decode_responses=True), breaker=config.breaker, clock=clock)


def executor(
    config: KeelConfig,
    registry: dict[str, ProviderAdapter],
    clock: Clock | None = None,
    window: HealthWindow | None = None,
) -> Executor:
    resolved_clock = clock if clock is not None else ManualClock()
    return Executor(
        router=Router(config=config),
        config=config,
        registry=registry,
        clock=resolved_clock,
        window=window if window is not None else health_window(config, resolved_clock),
    )


# --------------------------------------------------------------------------
# Fake adapters. `ProviderAdapter` is structural — no base class to inherit.
# --------------------------------------------------------------------------


class RecordingAdapter:
    """Succeeds, and counts how many times it was asked to."""

    def __init__(self, name: str, latency_ms: float = 0.0) -> None:
        self.name = name
        self.calls = 0
        self._latency_ms = latency_ms

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        self.calls += 1
        return ProviderResult.success(
            provider=self.name,
            response={"id": f"chatcmpl-{self.name}", "object": "chat.completion"},
            latency_ms=self._latency_ms,
        )


class FailingAdapter:
    """Returns a failure — as a value, the way every real adapter does."""

    def __init__(self, name: str, error_class: ErrorClass = ErrorClass.SERVER_ERROR) -> None:
        self.name = name
        self.calls = 0
        self._error_class = error_class

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        self.calls += 1
        return ProviderResult.failure(
            provider=self.name,
            latency_ms=12.0,
            error=NormalizedError(
                error_class=self._error_class,
                message=f"injected {self._error_class.value}",
                provider_error_type="FakeInjectedError",
            ),
        )


class HangingAdapter:
    """Sleeps in **real** time, so the gateway deadline has something to cut.

    `asyncio.sleep`, not `Clock.sleep`: this is the one behaviour in the suite
    that a `ManualClock` cannot express, since `wait_for` reads the event loop's
    clock and nothing else.
    """

    def __init__(self, name: str, seconds: float = 0.05) -> None:
        self.name = name
        self.calls = 0
        self.cancelled = False
        self._seconds = seconds

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        self.calls += 1
        try:
            await asyncio.sleep(self._seconds)
        except asyncio.CancelledError:
            # Recorded, then re-raised. A real adapter's `except Exception` does
            # not catch this at all; observing it here is how the test proves
            # the provider call was actually torn down rather than left running.
            self.cancelled = True
            raise
        return ProviderResult.success(
            provider=self.name, response={"id": "too-late"}, latency_ms=0.0
        )


def fakes(**adapters: ProviderAdapter) -> dict[str, ProviderAdapter]:
    return dict(adapters)


class BrokenRedis:
    """A client that refuses every command, standing in for a Redis that is down.

    `pipeline()` raises rather than returning a context manager, which is what a
    connection failure looks like from `HealthWindow`'s side. Typed loosely and
    passed where a `Redis` is expected: `mypy --strict` covers `keel/` only, and a
    real client is far too large to subclass honestly.
    """

    def pipeline(self, transaction: bool = True) -> object:
        raise RedisConnectionError("connection refused (test stub)")


# --------------------------------------------------------------------------
# Candidate 1, and only candidate 1
# --------------------------------------------------------------------------


async def test_the_first_candidate_is_invoked_and_its_result_returned() -> None:
    """The happy path: preference[0] is called, and its result comes back intact."""
    config = load_config(SHIPPED_CONFIG)
    first = RecordingAdapter("cohere_primary")
    second = RecordingAdapter("mock_chaos")

    result = await executor(config, fakes(cohere_primary=first, mock_chaos=second)).execute(
        envelope()
    )

    assert result.ok
    assert result.provider == "cohere_primary"
    assert first.calls == 1
    assert second.calls == 0


async def test_the_second_candidate_is_never_called_on_success() -> None:
    """Nothing speculative in Phase 1: no hedging, no warming, one call per request."""
    config = load_config(SHIPPED_CONFIG)
    second = RecordingAdapter("mock_chaos")

    await executor(
        config, fakes(cohere_primary=RecordingAdapter("cohere_primary"), mock_chaos=second)
    ).execute(envelope())

    assert second.calls == 0


async def test_a_failure_is_returned_rather_than_failed_over() -> None:
    """The central P1-T6 pin, and the one most likely to be "improved" by accident.

    `SERVER_ERROR` has `retry_elsewhere=True`, so a Phase 3 executor *will* try
    `mock_chaos` here. Phase 1 must not, because failover before the health
    window exists is precisely the reactive-before-observable mistake FR-3.4
    rules out. When Phase 3 lands, this test should fail loudly and be rewritten.
    """
    config = load_config(SHIPPED_CONFIG)
    first = FailingAdapter("cohere_primary", ErrorClass.SERVER_ERROR)
    second = RecordingAdapter("mock_chaos")

    result = await executor(config, fakes(cohere_primary=first, mock_chaos=second)).execute(
        envelope()
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.error_class is ErrorClass.SERVER_ERROR
    assert result.error.retry_elsewhere, "the taxonomy says retry; Phase 1 still must not"
    assert first.calls == 1
    assert second.calls == 0


async def test_a_provider_failure_is_a_return_value_not_an_exception() -> None:
    """`execute` raising would unwind past the P2-T2 health recording seam (D-C)."""
    config = load_config(SHIPPED_CONFIG)

    result = await executor(
        config,
        fakes(cohere_primary=FailingAdapter("cohere_primary"), mock_chaos=RecordingAdapter("m")),
    ).execute(envelope())

    assert isinstance(result, ProviderResult)


@pytest.mark.parametrize(
    "request_class",
    [
        pytest.param("interactive_chat", id="interactive_chat"),
        pytest.param("classification", id="classification"),
        pytest.param("batch_enrichment", id="batch_enrichment"),
    ],
)
async def test_the_request_class_selects_the_provider(request_class: str) -> None:
    """Every shipped class routes through the executor, not just the default one."""
    config = load_config(SHIPPED_CONFIG)
    first = RecordingAdapter("cohere_primary")

    result = await executor(
        config, fakes(cohere_primary=first, mock_chaos=RecordingAdapter("mock_chaos"))
    ).execute(envelope(request_class))

    assert result.provider == "cohere_primary"


async def test_the_result_names_the_config_entry_not_the_adapter(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """`X-Keel-Provider` (§4) shows the entry, and P1-T7 reads it straight off this."""
    config = load_config(write_config(base_text.replace(*MOCK_FIRST, 1)))

    result = await executor(
        config,
        fakes(
            cohere_primary=RecordingAdapter("cohere_primary"),
            mock_chaos=RecordingAdapter("mock_chaos"),
        ),
    ).execute(envelope())

    assert result.provider == "mock_chaos"


# --------------------------------------------------------------------------
# The gateway timeout (real time — see the module docstring)
# --------------------------------------------------------------------------


async def test_the_gateway_timeout_fires_and_normalizes_to_timeout(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """An adapter that hangs past `timeout_ms` yields a TIMEOUT, not a hung request.

    This is the backstop `keel/providers/cohere.py` points at: the adapter's own
    `timeout` frees the connection, this one frees the request when the adapter
    does not honour its deadline at all.
    """
    config = load_config(write_config(base_text.replace("timeout_ms: 30000", "timeout_ms: 5", 1)))
    adapter = HangingAdapter("cohere_primary", seconds=0.05)

    result = await executor(
        config,
        fakes(cohere_primary=adapter, mock_chaos=RecordingAdapter("mock_chaos")),
        SystemClock(),
    ).execute(envelope())

    assert not result.ok
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TIMEOUT
    assert result.provider == "cohere_primary"


async def test_a_timed_out_attempt_is_actually_cancelled(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The point of a deadline is releasing the call, not just giving up on it.

    A `wait_for` that returned while the provider call kept running would leak a
    connection per timeout — which under a provider outage is every request.
    """
    config = load_config(write_config(base_text.replace("timeout_ms: 30000", "timeout_ms: 5", 1)))
    adapter = HangingAdapter("cohere_primary", seconds=0.05)

    await executor(
        config,
        fakes(cohere_primary=adapter, mock_chaos=RecordingAdapter("mock_chaos")),
        SystemClock(),
    ).execute(envelope())

    assert adapter.calls == 1
    assert adapter.cancelled


async def test_a_gateway_timeout_counts_toward_the_breaker(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The reason the class matters: this is what Phase 3's breaker will read.

    Mapped to TIMEOUT rather than SERVER_ERROR because §5.4 separates them —
    latency budgets treat them differently — and both count, so a provider we
    can never wait for is correctly seen as unhealthy.
    """
    config = load_config(write_config(base_text.replace("timeout_ms: 30000", "timeout_ms: 5", 1)))

    result = await executor(
        config,
        fakes(cohere_primary=HangingAdapter("cohere_primary"), mock_chaos=RecordingAdapter("m")),
        SystemClock(),
    ).execute(envelope())

    assert result.error is not None
    assert result.error.counts_toward_breaker


async def test_a_gateway_timeout_names_itself_as_the_source(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """A slow provider and an impatient gateway must be distinguishable in a log.

    A provider-side timeout arrives as LiteLLM's `Timeout` and carries that type
    name; this one never reached the provider's error path at all.
    """
    config = load_config(write_config(base_text.replace("timeout_ms: 30000", "timeout_ms: 5", 1)))

    result = await executor(
        config,
        fakes(cohere_primary=HangingAdapter("cohere_primary"), mock_chaos=RecordingAdapter("m")),
        SystemClock(),
    ).execute(envelope())

    assert result.error is not None
    assert result.error.provider_error_type == GATEWAY_TIMEOUT_ERROR_TYPE
    assert "5 ms" in result.error.message


async def test_an_adapter_that_answers_in_time_is_not_timed_out(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The deadline must not fire on a call that beat it — the boring half of the pair."""
    config = load_config(
        write_config(base_text.replace("timeout_ms: 30000", "timeout_ms: 2000", 1))
    )
    adapter = HangingAdapter("cohere_primary", seconds=0.001)

    result = await executor(
        config,
        fakes(cohere_primary=adapter, mock_chaos=RecordingAdapter("m")),
        SystemClock(),
    ).execute(envelope())

    assert result.ok
    assert not adapter.cancelled


async def test_no_timeout_ms_means_no_gateway_deadline(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """`mock_chaos` ships without `timeout_ms`, and §5.2 omits it deliberately.

    `None` means "no gateway-imposed deadline", never "zero". A 30 second
    injected latency on a `ManualClock` therefore succeeds — and the test costs
    microseconds, which is the whole argument for the injected clock (NFR-2).
    """
    config = load_config(write_config(base_text.replace(*MOCK_FIRST, 1)))
    assert config.providers["mock_chaos"].timeout_ms is None

    clock = ManualClock()
    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)
    mock = registry["mock_chaos"]
    assert isinstance(mock, MockAdapter)
    mock.state.latency_ms = 30_000.0

    result = await executor(config, dict(registry), clock).execute(envelope())

    assert result.ok
    assert clock.now() == 30.0, "the mock slept 30s of injected time, and nothing cut it short"


# --------------------------------------------------------------------------
# Latency, and the real registry
# --------------------------------------------------------------------------


async def test_a_successful_attempt_keeps_the_adapters_own_latency() -> None:
    """The executor measures only when it has to synthesize a result.

    The adapter timed the provider call; re-measuring around it would fold the
    executor's own overhead into provider latency, and §6 keeps those apart on
    purpose so `keel_gateway_overhead_seconds` can be measured rather than
    asserted (S5).
    """
    config = load_config(SHIPPED_CONFIG)

    result = await executor(
        config,
        fakes(
            cohere_primary=RecordingAdapter("cohere_primary", latency_ms=1234.5),
            mock_chaos=RecordingAdapter("mock_chaos"),
        ),
    ).execute(envelope())

    assert result.latency_ms == 1234.5


async def test_the_shipped_config_executes_end_to_end_against_the_real_registry(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Router, registry, and executor wired exactly as P1-T7 will wire them.

    Offline: `mock_chaos` is in-process (ADR 0002), so this is the whole request
    path below ingress with no network and no Redis.
    """
    config = load_config(write_config(base_text.replace(*MOCK_FIRST, 1)))
    clock = ManualClock(start=500.0)
    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)

    result = await executor(config, dict(registry), clock).execute(envelope())

    assert result.ok
    assert result.provider == "mock_chaos"
    assert result.response is not None
    assert result.response["object"] == "chat.completion"
    assert result.prompt_tokens is not None, "the Phase 4 cost engine needs real numbers"


async def test_the_mocks_injected_latency_reaches_the_result(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Latency travels adapter -> result -> P2-T3's reservoir, on injected time."""
    config = load_config(write_config(base_text.replace(*MOCK_FIRST, 1)))
    clock = ManualClock()
    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)
    mock = registry["mock_chaos"]
    assert isinstance(mock, MockAdapter)
    mock.state.latency_ms = 3000.0

    result = await executor(config, dict(registry), clock).execute(envelope())

    assert result.latency_ms == 3000.0


async def test_a_registry_missing_a_configured_provider_says_so() -> None:
    """ADR 0004 makes this unreachable in production; a mis-wired test can still hit it.

    The message points at the registry rather than at the request, because that
    is where the mistake is.
    """
    config = load_config(SHIPPED_CONFIG)
    partial: dict[str, ProviderAdapter] = {"mock_chaos": RecordingAdapter("mock_chaos")}

    with pytest.raises(RuntimeError) as excinfo:
        await executor(config, partial).execute(envelope())

    assert "cohere_primary" in str(excinfo.value)


def test_the_fake_adapters_satisfy_the_protocol() -> None:
    """Otherwise these tests could drift from `ProviderAdapter` and still pass."""
    checked: list[ProviderAdapter] = [
        RecordingAdapter("a"),
        FailingAdapter("b"),
        HangingAdapter("c"),
    ]

    assert [adapter.name for adapter in checked] == ["a", "b", "c"]


def test_the_mock_first_mutation_still_matches_the_shipped_config(base_text: str) -> None:
    """Guards every test above that relies on it: a silent no-op replace proves nothing."""
    anchor, replacement = MOCK_FIRST

    assert base_text.count(anchor) == 1
    assert base_text.replace(anchor, replacement, 1) != base_text


# --------------------------------------------------------------------------
# What the executor records (P2-T2, D-C)
# --------------------------------------------------------------------------


async def test_a_success_is_recorded_against_the_provider_that_served_it() -> None:
    """The `ok` field, keyed on the config entry rather than the adapter name."""
    config = load_config(SHIPPED_CONFIG)
    clock = ManualClock(start=55.0)
    window = health_window(config, clock)

    await executor(
        config,
        fakes(cohere_primary=RecordingAdapter("cohere_primary")),
        clock,
        window,
    ).execute(envelope())

    counts = await window.read("cohere_primary")
    assert counts is not None
    assert counts.ok == 1, "the attempt succeeded, so it belongs in `ok`"
    assert counts.total == 1


async def test_a_returned_provider_failure_is_recorded_under_its_error_class() -> None:
    """A failure travels as a return value, and must still reach the window.

    The failure branch is the one the breaker exists for. An executor that
    recorded only successes would show a provider failing every request as a
    provider with no traffic at all.
    """
    config = load_config(SHIPPED_CONFIG)
    clock = ManualClock(start=55.0)
    window = health_window(config, clock)

    await executor(
        config,
        fakes(cohere_primary=FailingAdapter("cohere_primary", ErrorClass.RATE_LIMIT)),
        clock,
        window,
    ).execute(envelope())

    counts = await window.read("cohere_primary")
    assert counts is not None
    assert counts.ok == 0
    assert counts.errors[ErrorClass.RATE_LIMIT] == 1


async def test_a_gateway_timeout_is_recorded_even_though_no_adapter_returned(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The synthesized result is recorded like any other, under `err_timeout`.

    `wait_for` cancelled the adapter, so nothing below the executor ever produced
    a result — and this is precisely the attempt Phase 3 most needs counted, since
    a provider we consistently cannot wait for is unhealthy by any useful
    definition. Real time, for the reason the module docstring gives.
    """
    config = load_config(write_config(base_text.replace("timeout_ms: 30000", "timeout_ms: 5", 1)))
    clock = SystemClock()
    window = health_window(config, clock)

    result = await executor(
        config,
        fakes(cohere_primary=HangingAdapter("cohere_primary", seconds=0.05)),
        clock,
        window,
    ).execute(envelope())

    assert result.error is not None and result.error.error_class is ErrorClass.TIMEOUT
    counts = await window.read("cohere_primary")
    assert counts is not None
    assert counts.errors[ErrorClass.TIMEOUT] == 1


async def test_recording_lands_in_the_bucket_the_attempt_happened_in() -> None:
    """The literal §5.5 key, so a schema change fails here rather than in Grafana."""
    config = load_config(SHIPPED_CONFIG)
    clock = ManualClock(start=55.0)
    redis = FakeRedis(decode_responses=True)
    window = HealthWindow(redis=redis, breaker=config.breaker, clock=clock)

    await executor(
        config,
        fakes(cohere_primary=RecordingAdapter("cohere_primary")),
        clock,
        window,
    ).execute(envelope())

    assert await redis.hgetall("keel:health:cohere_primary:11") == {FIELD_OK: "1"}


async def test_a_dead_redis_does_not_break_the_request() -> None:
    """The whole point of ADR 0008, asserted where a client would feel it.

    A Redis outage costs an observation. It must not cost the response, or the
    gateway has turned a dependency's failure into its own — the failure it exists
    to absorb.
    """
    config = load_config(SHIPPED_CONFIG)
    clock = ManualClock(start=55.0)
    dead = HealthWindow(redis=BrokenRedis(), breaker=config.breaker, clock=clock)

    result = await executor(
        config,
        fakes(cohere_primary=RecordingAdapter("cohere_primary")),
        clock,
        dead,
    ).execute(envelope())

    assert result.ok, "the provider answered; Redis being down is not the client's problem"
    assert result.response == {"id": "chatcmpl-cohere_primary", "object": "chat.completion"}
    assert await dead.read("cohere_primary") is None, "unreadable is unknown, never zero"
