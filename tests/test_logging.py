"""Tests for structured logging (P2-T5, FR-7.3, ADR 0008).

Three properties are worth the weight of this module, and only one of them is
about formatting.

**Correlation across a request.** FR-7.3 asks for logs "correlated by
`request_id` across retries and failovers". The task card's done-when — "a
two-attempt request emits lines sharing one `request_id`" — cannot be produced
today: Phase 1 invokes candidate 1 and stops, and `tests/test_executor.py`
actively pins that candidate 2 is never called. So the property is asserted two
ways instead. First, two `Executor.execute` calls under one bound context, which
is the shape Phase 3's failover loop will produce. Second, and better, a single
real HTTP request against a broken Redis, which emits **two lines from two
different modules** — the executor's `provider_attempt` and `window.py`'s
dropped-write warning — sharing one `request_id`. That second one is a genuine
multi-line correlation available in Phase 2, and it is exactly the diagnostic
ADR 0008 says a reviewer needs.

**The stdlib bridge.** ADR 0008 promised P2-T5 would put the dropped-health-write
warnings "into structured JSON". Those four modules were not edited, so the only
thing making that true is `ProcessorFormatter`'s `foreign_pre_chain`. Tested
against the real formatter rather than a stand-in, because a stand-in would prove
nothing about the handler that actually runs.

**No payloads, ever.** PRD §2.1 lists PII redaction as a non-goal, so nothing
downstream would scrub a prompt. A sentinel string is driven through the whole
HTTP path and asserted absent from every line.

`structlog.testing.capture_logs` is deliberately **not** used as the main tool.
In structlog 26 it clears the entire processor chain — contextvars are invisible
unless passed back explicitly — and it never sees stdlib records at all, which is
half of what this module is about. The `captured()` helper below swaps a buffer
under the real handler instead, so what is asserted is what production writes.

No network and no Redis (NFR-2).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import structlog
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from keel.api.app import create_app
from keel.api.envelope import RequestEnvelope
from keel.clock import Clock, ManualClock
from keel.config import KeelConfig, load_config
from keel.health.window import HealthTracker
from keel.observability.logging import (
    FORMAT_CONSOLE,
    FORMAT_JSON,
    LogSettings,
    configure_logging,
    get_logger,
)
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.credentials import ProviderCredentials
from keel.providers.errors import ErrorClass, NormalizedError
from keel.providers.registry import build_registry
from keel.routing.executor import Executor
from keel.routing.router import Router

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

CREDENTIALS = ProviderCredentials(cohere_api_key="test-key")

MOCK_FIRST = (
    "    preference: [cohere_primary, mock_chaos]\n    latency_budget_p95_ms: 4000\n",
    "    preference: [mock_chaos, cohere_primary]\n    latency_budget_p95_ms: 4000\n",
)

HEADERS = {
    "X-Keel-Tenant": "acme",
    "X-Keel-Feature": "support-summary",
    "X-Keel-Request-Id": "req-log-1",
    "X-Keel-Class": "interactive_chat",
}

# The one string that must never appear in a log line. Distinctive enough that a
# substring search over the whole captured buffer is conclusive.
SECRET = "sk-prompt-payload-must-never-be-logged"
BODY: dict[str, Any] = {
    "model": "keel",
    "messages": [{"role": "user", "content": SECRET}],
}

ENDPOINT = "/v1/chat/completions"


# --------------------------------------------------------------------------
# Capture: the real handler, writing into a buffer instead of stdout.
# --------------------------------------------------------------------------


def keel_handler() -> logging.StreamHandler[Any]:
    """The handler `configure_logging` installed, found the way the module marks it."""
    marked = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_keel_log_handler", False)
    ]
    assert len(marked) == 1, f"expected exactly one keel handler, found {len(marked)}"
    found = marked[0]
    assert isinstance(found, logging.StreamHandler)
    return found


@contextmanager
def captured(settings: LogSettings | None = None) -> Iterator[io.StringIO]:
    """Configure logging for real, then redirect its stream into a buffer.

    Swapping the stream rather than adding a second handler is what makes this
    honest: the formatter, the processor chain, and the renderer under test are
    the exact objects a running gateway uses. A parallel handler built by the
    test could drift from the real one and still pass.

    Contextvars are cleared on the way in and out. They are process-global, and a
    `request_id` left bound by one test would silently satisfy a correlation
    assertion in the next.
    """
    structlog.contextvars.clear_contextvars()
    configure_logging(settings)
    handler = keel_handler()
    buffer = io.StringIO()
    previous = handler.setStream(buffer)
    try:
        yield buffer
    finally:
        if previous is not None:
            handler.setStream(previous)
        structlog.contextvars.clear_contextvars()


def lines(buffer: io.StringIO) -> list[dict[str, Any]]:
    """Every captured line, parsed. Fails loudly if one is not JSON."""
    parsed: list[dict[str, Any]] = []
    for raw in buffer.getvalue().splitlines():
        if not raw.strip():
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError as exc:  # pragma: no cover - a real failure
            raise AssertionError(f"log line is not JSON: {raw!r}") from exc
    return parsed


def events(buffer: io.StringIO, name: str) -> list[dict[str, Any]]:
    return [line for line in lines(buffer) if line.get("event") == name]


# --------------------------------------------------------------------------
# Fixtures and harnesses, local to this module — there is no conftest.py.
# --------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Put the root logger back as it was found.

    `configure_logging` mutates process-global state, and pytest's own `caplog`
    handler lives on the same logger. Snapshotting and restoring means this module
    cannot leak a handler or a level into the six tests elsewhere in the suite
    that assert on `caplog.text`.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)
        structlog.contextvars.clear_contextvars()


class BrokenRedis:
    """A Redis that refuses every command, so every health write is dropped.

    Same stub as `tests/test_health_window.py` uses, for the same reason: it is
    what a connection failure looks like from `HealthTracker`'s side, and it is
    what makes `window.py` emit the ADR 0008 warning this module needs to catch.
    """

    def pipeline(self, transaction: bool = True) -> object:
        raise RedisConnectionError("connection refused (test stub)")


class StubCohere:
    """Stands in for `CohereAdapter`, so a cohere-preferring config needs no network."""

    def __init__(self) -> None:
        self.calls: list[RequestEnvelope] = []

    @property
    def name(self) -> str:
        return "cohere_primary"

    def capabilities(self) -> frozenset[str]:
        return frozenset({"citations", "tool_use", "structured_output"})

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        self.calls.append(envelope)
        return ProviderResult.success(
            provider=self.name,
            response={"id": "chatcmpl-stub", "object": "chat.completion"},
            latency_ms=12.0,
        )


class FailingAdapter:
    """Returns a failure as a value, the way every real adapter does."""

    def __init__(self, name: str, error_class: ErrorClass = ErrorClass.SERVER_ERROR) -> None:
        self.name = name
        self._error_class = error_class

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        return ProviderResult.failure(
            provider=self.name,
            latency_ms=12.0,
            error=NormalizedError(
                error_class=self._error_class,
                message=f"injected {self._error_class.value}",
                provider_error_type="FakeInjectedError",
            ),
        )


def envelope(request_id: str = "req-log-1") -> RequestEnvelope:
    return RequestEnvelope(
        request_id=request_id,
        tenant="acme",
        feature="support-summary",
        request_class="interactive_chat",
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload={"model": "keel", "messages": [{"role": "user", "content": SECRET}]},
        received_at=0.0,
    )


def build_executor(
    config: KeelConfig, registry: dict[str, ProviderAdapter], redis: Any = None
) -> Executor:
    clock: Clock = ManualClock()
    return Executor(
        router=Router(config=config),
        config=config,
        registry=registry,
        clock=clock,
        tracker=HealthTracker(
            redis=redis if redis is not None else FakeRedis(decode_responses=True),
            breaker=config.breaker,
            clock=clock,
        ),
    )


@contextmanager
def gateway(path: Path, *, redis: Any = None) -> Iterator[TestClient]:
    """A started app over one config, with `cohere_primary` stubbed.

    Mirrors `tests/test_app.py`'s helper: the registry is built by the *real*
    `build_registry`, so `mock_chaos` is a genuine `MockAdapter` and the ADR 0004
    startup check runs; only the Cohere entry is replaced.
    """
    clock: Clock = ManualClock(start=1_000.0)
    config = load_config(path)
    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)
    registry["cohere_primary"] = StubCohere()

    app = create_app(
        config_path=path,
        clock=clock,
        registry=registry,
        redis=redis if redis is not None else FakeRedis(decode_responses=True),
    )
    with TestClient(app) as client:
        yield client


# --------------------------------------------------------------------------
# Configuration: idempotency, and not trampling foreign handlers
# --------------------------------------------------------------------------


def test_configuring_twice_leaves_exactly_one_keel_handler() -> None:
    """The lifespan runs once per app, and the suite builds thirty-odd apps.

    Without the marker-and-remove step every `TestClient` context would add
    another handler and every line would be emitted as many times as apps had
    been built — which reads as a duplicated log rather than as a bug in setup.
    """
    configure_logging()
    configure_logging()
    configure_logging()

    marked = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_keel_log_handler", False)
    ]
    assert len(marked) == 1


def test_configuring_does_not_remove_a_foreign_handler() -> None:
    """Guards a trap that is currently unsprung, which is the only time to guard it.

    pytest installs its `caplog` handler on the root logger for the duration of a
    test, and six tests in this suite assert on `caplog.text`. `configure_logging`
    runs inside the lifespan, so an app built *during* one of those tests would —
    if it reset `root.handlers` — throw pytest's handler away and empty
    `caplog.text`, failing an assertion about health writes for a reason having
    nothing to do with health writes.

    Verified by mutation that no existing test would catch that: replacing the
    remove-only-marked loop with `root.handlers = [handler]` leaves all six
    passing, because none of them happens to build an app. This test is the one
    that fails, and it exists so the combination is caught the first time someone
    writes a test that does both.

    A stand-in for pytest's handler is planted here and must survive.
    """
    foreign = logging.StreamHandler(io.StringIO())
    root = logging.getLogger()
    root.addHandler(foreign)

    configure_logging()
    configure_logging()

    assert foreign in root.handlers, "a handler this module did not install was removed"


def test_the_level_comes_from_settings() -> None:
    """`KEEL_LOG_LEVEL` reaches the root logger, so DEBUG is reachable in an incident."""
    configure_logging(LogSettings(log_level="WARNING"))
    assert logging.getLogger().level == logging.WARNING

    configure_logging(LogSettings(log_level="DEBUG"))
    assert logging.getLogger().level == logging.DEBUG


@pytest.mark.parametrize("bad", ["INF0", "verbose", "12", "warn ing"])
def test_an_unknown_log_level_is_rejected(bad: str) -> None:
    """Loudly, at startup (NFR-4), because the quiet failure is the dangerous one.

    `logging` resolves an unrecognised name to level 0, which admits *every*
    record — so a typo would turn on debug logging in production rather than
    doing nothing. Guessing is worse than refusing.
    """
    with pytest.raises(ValueError, match="unknown log level"):
        LogSettings(log_level=bad)


@pytest.mark.parametrize("bad", ["JSN", "pretty", "text"])
def test_an_unknown_log_format_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="unknown log format"):
        LogSettings(log_format=bad)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_setting_falls_back_to_the_default(blank: str) -> None:
    """The same blank-is-absent rule `RedisSettings` and `ProviderCredentials` apply.

    `.env.example` ships assignments people copy and do not fill in, and
    "copied the template" must not become an empty level name.
    """
    settings = LogSettings(log_level=blank, log_format=blank)
    assert settings.log_level == "INFO"
    assert settings.log_format == FORMAT_JSON


def test_console_format_selects_a_human_renderer() -> None:
    """The dev affordance, and the reason it cannot break the JSON default.

    The renderer lives in the handler's formatter, not in the structlog processor
    chain — which is what makes `cache_logger_on_first_use` safe, since a logger
    cached under one format still renders correctly under the other.
    """
    configure_logging(LogSettings(log_format=FORMAT_CONSOLE))
    formatter = keel_handler().formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)

    with captured(LogSettings(log_format=FORMAT_CONSOLE)) as buffer:
        get_logger("keel.test").info("hello_console", provider="mock_chaos")

    text = buffer.getvalue()
    assert "hello_console" in text
    with pytest.raises(json.JSONDecodeError):
        json.loads(text.splitlines()[0])


# --------------------------------------------------------------------------
# The line shape
# --------------------------------------------------------------------------


def test_every_line_is_one_json_object_with_the_standard_keys() -> None:
    with captured() as buffer:
        get_logger("keel.test").info("something_happened", provider="mock_chaos")

    (line,) = lines(buffer)
    assert line["event"] == "something_happened"
    assert line["level"] == "info"
    assert line["logger"] == "keel.test"
    assert line["provider"] == "mock_chaos"
    assert line["timestamp"].endswith("Z"), "timestamps are UTC and ISO-8601"


def test_bound_context_reaches_a_structlog_line() -> None:
    with captured() as buffer:
        structlog.contextvars.bind_contextvars(request_id="req-1", tenant="acme")
        get_logger("keel.test").info("bound")

    (line,) = lines(buffer)
    assert line["request_id"] == "req-1"
    assert line["tenant"] == "acme"


# --------------------------------------------------------------------------
# The stdlib bridge — what ADR 0008 asked this task for
# --------------------------------------------------------------------------


def test_a_foreign_stdlib_warning_is_rendered_as_json_with_the_request_context() -> None:
    """The ADR 0008 promise, tested against the module that makes it.

    `keel/health/window.py` was not edited by P2-T5 and must not be: it is
    imported by the executor and by Phase 3's breaker, and P2-T4 kept
    `keel/health/` free of any dependency on `keel/observability/` on purpose.
    So the only thing turning its `logger.warning(...)` into a correlated JSON
    line is the formatter's `foreign_pre_chain`.
    """
    with captured() as buffer:
        structlog.contextvars.bind_contextvars(request_id="req-42", tenant="acme")
        logging.getLogger("keel.health.window").warning(
            "health write dropped for provider %r (field %r): %s: %s",
            "mock_chaos",
            "ok",
            "TimeoutError",
            "Timeout connecting to server",
        )

    (line,) = lines(buffer)
    assert line["logger"] == "keel.health.window"
    assert line["level"] == "warning"
    assert line["request_id"] == "req-42", "the ADR 0008 warning must name its request"
    assert "health write dropped" in line["event"]
    assert "mock_chaos" in line["event"], "%-args are interpolated, not left as a template"


def test_the_other_three_stdlib_loggers_are_bridged_too() -> None:
    """One assertion per module that logs, so a new handler cannot miss one."""
    names = [
        "keel.health.latency",
        "keel.observability.metrics",
        "keel.providers.normalize",
    ]
    with captured() as buffer:
        structlog.contextvars.bind_contextvars(request_id="req-7")
        for name in names:
            logging.getLogger(name).warning("something about %s", "a provider")

    assert [line["logger"] for line in lines(buffer)] == names
    assert all(line["request_id"] == "req-7" for line in lines(buffer))


# --------------------------------------------------------------------------
# Correlation — the FR-7.3 property
# --------------------------------------------------------------------------


def test_two_attempts_under_one_context_share_one_request_id(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The task card's done-when, in the only shape Phase 1 can produce.

    Phase 1 invokes candidate 1 and stops, so a single request cannot make two
    attempts — `tests/test_executor.py` pins that it does not. Driving `execute`
    twice under one binding is the same thing Phase 3's failover loop will do
    inside one request, and it asserts the property that matters: the correlation
    key comes from the context, not from an argument the loop would have to
    remember to pass.

    When that loop lands, this test keeps passing and `attempt` starts varying.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))
    config = load_config(path)
    executor = build_executor(config, {**_registry(config), "cohere_primary": StubCohere()})

    with captured() as buffer:
        structlog.contextvars.bind_contextvars(
            request_id="req-two-attempts",
            tenant="acme",
            feature="support-summary",
            request_class="interactive_chat",
        )
        import asyncio

        asyncio.run(_two_attempts(executor))

    attempts = events(buffer, "provider_attempt")
    assert len(attempts) == 2
    assert {line["request_id"] for line in attempts} == {"req-two-attempts"}
    assert all(line["tenant"] == "acme" for line in attempts)
    assert all(line["request_class"] == "interactive_chat" for line in attempts)
    assert all(line["attempt"] == 1 for line in attempts), (
        "fixed at 1 until Phase 3's loop counts; the field exists so the query does not change"
    )


async def _two_attempts(executor: Executor) -> None:
    await executor.execute(envelope())
    await executor.execute(envelope())


def _registry(config: KeelConfig) -> dict[str, ProviderAdapter]:
    return build_registry(config=config, clock=ManualClock(), credentials=CREDENTIALS)


def test_one_request_correlates_the_attempt_line_with_the_dropped_write_warning(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Two lines, two modules, one `request_id` — and it is the ADR 0008 diagnostic.

    This is the real answer to "a two-attempt request emits lines sharing one
    `request_id`" in a phase that cannot make two attempts. With Redis refusing
    every command, one HTTP request produces the executor's `provider_attempt`
    line *and* `keel.health.window`'s dropped-write warning. ADR 0008 says a
    reviewer looking at a suspiciously clean dashboard should check the logs;
    this is the pair of lines they would find, and they now join up.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))

    with gateway(path, redis=BrokenRedis()) as client, captured() as buffer:
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 200, "a Redis outage costs an observation, not a request"

    (attempt,) = events(buffer, "provider_attempt")
    dropped = [
        line
        for line in lines(buffer)
        if line["logger"] == "keel.health.window" and "dropped" in line["event"]
    ]
    assert len(dropped) == 1

    assert attempt["request_id"] == HEADERS["X-Keel-Request-Id"]
    assert dropped[0]["request_id"] == attempt["request_id"]
    assert dropped[0]["tenant"] == "acme", "the foreign line inherits the full envelope context"


def test_a_served_request_logs_an_attempt_and_a_completion(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    path = write_config(base_text.replace(*MOCK_FIRST))

    with gateway(path) as client, captured() as buffer:
        assert client.post(ENDPOINT, headers=HEADERS, json=BODY).status_code == 200

    (attempt,) = events(buffer, "provider_attempt")
    (completed,) = events(buffer, "request_completed")

    assert attempt["provider"] == "mock_chaos"
    assert attempt["outcome"] == "ok"
    assert attempt["error_class"] is None
    assert isinstance(attempt["latency_ms"], float | int)

    assert completed["status_code"] == 200
    assert completed["outcome"] == "ok"
    assert completed["attempts"] == 1
    assert completed["request_id"] == HEADERS["X-Keel-Request-Id"]
    assert completed["feature"] == "support-summary"


def test_an_upstream_failure_logs_its_taxonomy_class(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """`error_class`, not the HTTP status, is what a dashboard groups by (ADR 0006).

    Both lines carry it so the taxonomy is readable without joining the attempt
    line to the completion line.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))
    clock: Clock = ManualClock(start=1_000.0)
    config = load_config(path)
    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)
    registry["mock_chaos"] = FailingAdapter("mock_chaos", ErrorClass.RATE_LIMIT)

    app = create_app(
        config_path=path,
        clock=clock,
        registry=registry,
        redis=FakeRedis(decode_responses=True),
    )

    with TestClient(app) as client, captured() as buffer:
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 429

    (attempt,) = events(buffer, "provider_attempt")
    (completed,) = events(buffer, "request_completed")

    assert attempt["outcome"] == "error"
    assert attempt["error_class"] == "rate_limit"
    assert attempt["provider_error_type"] == "FakeInjectedError"
    assert completed["error_class"] == "rate_limit"
    assert completed["status_code"] == 429


def test_a_rejected_request_still_carries_the_request_id_from_the_header(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Why binding happens before validation rather than after.

    A 400 is the line a client most needs correlated — it is the one they will
    quote in a support ticket — and at that point there is no envelope to read a
    `request_id` from, because building one is what failed.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))
    headers = {key: value for key, value in HEADERS.items() if key != "X-Keel-Class"}

    with gateway(path) as client, captured() as buffer:
        response = client.post(ENDPOINT, headers=headers, json=BODY)

    assert response.status_code == 400

    (rejected,) = events(buffer, "request_rejected")
    assert rejected["request_id"] == HEADERS["X-Keel-Request-Id"]
    assert rejected["level"] == "warning"
    assert "missing" in " ".join(rejected["problems"])
    assert "request_class" in rejected["problem_fields"]
    assert not events(buffer, "provider_attempt"), "no provider was reached"


def test_a_stale_request_id_does_not_leak_into_the_next_request(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """A rejected request is never labelled with the previous caller's tenant.

    `request_id` alone could not show this — every request rebinds it, so the
    second value overwrites the first either way. The fields that would expose a
    leak are the envelope's: `tenant`, `feature`, and `request_class` are bound
    only *after* `build_envelope` succeeds, so a rejected request binds none of
    them and would show whatever the last one left behind. A rejection line
    carrying someone else's tenant is worse than an unlabelled one — it is wrong,
    and it looks right.

    **What actually provides this, honestly.** Not `clear_contextvars()`.
    Measured: every request starts with an empty context, because the ASGI server
    runs each one in its own task and a binding made inside does not escape it.
    Deleting the clear leaves this test — and the whole suite — passing. The clear
    stays anyway, for the reason given at its call site, and this test pins the
    property rather than the mechanism: if a future server or middleware ever does
    reuse a context, this is what fails.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))
    rejected_headers = {key: value for key, value in HEADERS.items() if key != "X-Keel-Class"}
    rejected_headers["X-Keel-Tenant"] = "other-tenant"
    rejected_headers["X-Keel-Request-Id"] = "req-log-2"

    with gateway(path) as client, captured() as buffer:
        assert client.post(ENDPOINT, headers=HEADERS, json=BODY).status_code == 200
        assert client.post(ENDPOINT, headers=rejected_headers, json=BODY).status_code == 400

    (attempt,) = events(buffer, "provider_attempt")
    (rejected,) = events(buffer, "request_rejected")

    assert attempt["request_id"] == "req-log-1"
    assert attempt["tenant"] == "acme"

    assert rejected["request_id"] == "req-log-2", "the header is bound before validation"
    assert "tenant" not in rejected, "the rejected request never got an envelope to bind from"
    assert "feature" not in rejected
    assert "request_class" not in rejected


# --------------------------------------------------------------------------
# The rule with nothing behind it
# --------------------------------------------------------------------------


def test_no_log_line_contains_the_request_payload(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """PRD §2.1 rules out redaction in v1, so the only safe amount to log is none.

    Asserted over the whole captured buffer rather than per line, because the
    failure this guards against is a future `payload=` kwarg added to any one of
    the three call sites — or an exception rendering a body into a traceback.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))

    with gateway(path, redis=BrokenRedis()) as client, captured() as buffer:
        client.post(ENDPOINT, headers=HEADERS, json=BODY)
        client.post(ENDPOINT, headers=HEADERS, json={"model": "keel", "stream": True})

    text = buffer.getvalue()
    assert SECRET not in text, "the prompt reached a log line"
    assert "messages" not in text, "no part of the payload may be logged"
    assert lines(buffer), "the capture itself must not be empty, or this proves nothing"


def test_healthz_and_metrics_emit_no_request_lines(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Only the chat route binds and logs.

    The P2-T6 compose healthcheck polls `/healthz` continuously and Prometheus
    scrapes `/metrics` every few seconds; a request line on either would bury the
    request lines that matter under machine traffic.
    """
    path = write_config(base_text.replace(*MOCK_FIRST))

    with gateway(path) as client, captured() as buffer:
        assert client.get("/healthz").status_code == 200
        assert client.get("/metrics").status_code == 200

    assert not events(buffer, "request_completed")
    assert not events(buffer, "provider_attempt")
    assert not events(buffer, "request_rejected")
