"""Tests for the FastAPI app and the ingress endpoint (P1-T7, FR-1.1, FR-1.3, §4, §5.1).

The headline test here is the M1 exit criterion: an identical request body, sent
against two configs that differ only in the order of one preference list, is
served by two different providers. Nothing in the request changes and no code
path is selected by the test — the config decides, which is design principle 1
being demonstrable rather than merely claimed.

Every adapter is in-process. `mock_chaos` is the real `MockAdapter` (ADR 0002),
built by the real `build_registry` so the ADR 0004 startup guarantee is exercised
rather than bypassed; `cohere_primary` is a stub swapped in over the top, because
the shipped config lists it first and reaching Cohere from CI would break NFR-2.

`TestClient` is always used as a context manager. A bare `TestClient(app)` never
runs the lifespan, so `app.state` stays empty and every request fails on a
missing context — the `gateway` helper below exists partly to make that mistake
unreachable.

No network and no real time; `fakeredis` stands in for Redis (NFR-2). The
client is injected rather than built, because the lifespan otherwise reaches
for `REDIS_URL` and every test here would open a socket to localhost.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from keel.api.app import app as module_level_app
from keel.api.app import create_app
from keel.api.envelope import RequestEnvelope
from keel.clock import Clock, ManualClock
from keel.config import ConfigError, KeelConfig, load_config
from keel.health.window import HealthTracker
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.credentials import ProviderCredentials
from keel.providers.errors import ErrorClass, NormalizedError
from keel.providers.mock import MockAdapter
from keel.providers.registry import build_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

CREDENTIALS = ProviderCredentials(cohere_api_key="test-key")

# The config flip that *is* the M1 milestone: `interactive_chat`'s first two
# candidates swap, and nothing else in the file moves.
COHERE_FIRST = "    preference: [cohere_primary, mock_chaos]\n    latency_budget_p95_ms: 4000\n"
MOCK_FIRST = "    preference: [mock_chaos, cohere_primary]\n    latency_budget_p95_ms: 4000\n"

HEADERS = {
    "X-Keel-Tenant": "acme",
    "X-Keel-Feature": "support-summary",
    "X-Keel-Request-Id": "req-1",
    "X-Keel-Class": "interactive_chat",
}
BODY: dict[str, Any] = {"model": "keel", "messages": [{"role": "user", "content": "hi"}]}

ENDPOINT = "/v1/chat/completions"

# The keys every completion carries, whichever provider produced it. Transcribed
# by hand from `MockAdapter._completion` rather than read back from it, so the
# stub and the mock must agree with this list and with each other.
COMPLETION_KEYS = {"id", "object", "created", "model", "choices", "usage"}
USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}

STUB_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-stub-1",
    "object": "chat.completion",
    "created": 1_000,
    "model": "command-a",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "stubbed"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
}


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


class StubCohere:
    """Stands in for `CohereAdapter`, so a cohere-preferring config needs no network.

    Records the envelopes it was handed, which is how the tests reach behind the
    HTTP boundary to assert on what the provider actually saw — the stripped
    payload, and the clock-derived `received_at`.
    """

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self._response = response if response is not None else STUB_RESPONSE
        self.calls: list[RequestEnvelope] = []

    @property
    def name(self) -> str:
        return "cohere_primary"

    def capabilities(self) -> frozenset[str]:
        return frozenset({"citations", "tool_use", "structured_output"})

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        self.calls.append(envelope)
        return ProviderResult.success(
            provider=self.name, response=self._response, latency_ms=12.0
        )


class FailingCohere(StubCohere):
    """A provider that always fails, with a chosen taxonomy class."""

    def __init__(self, error_class: ErrorClass) -> None:
        super().__init__()
        self._error_class = error_class

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        self.calls.append(envelope)
        return ProviderResult.failure(
            provider=self.name,
            latency_ms=8.0,
            error=NormalizedError(
                error_class=self._error_class,
                message="the provider said no",
                provider_error_type="StubFailure",
            ),
        )


@contextmanager
def gateway(
    path: Path,
    *,
    clock: Clock | None = None,
    stub: StubCohere | None = None,
    mutate: Callable[[dict[str, ProviderAdapter]], None] | None = None,
) -> Iterator[tuple[TestClient, StubCohere]]:
    """A started app over one config, with `cohere_primary` stubbed.

    The registry is built by the *real* `build_registry`, so `mock_chaos` is a
    genuine `MockAdapter` and the ADR 0004 "one adapter per configured provider"
    check runs; only the Cohere entry is then replaced. Constructing the
    throwaway real `CohereAdapter` on the way costs nothing, because
    `keel/providers/cohere.py` imports litellm lazily rather than at module
    scope.

    `with TestClient(app)` and not a bare `TestClient(app)`: the context manager
    is what runs the lifespan, and without it every request would fail on an
    empty `app.state`.
    """
    resolved_stub = stub if stub is not None else StubCohere()
    resolved_clock: Clock = clock if clock is not None else ManualClock(start=1_000.0)

    config = load_config(path)
    registry = build_registry(config=config, clock=resolved_clock, credentials=CREDENTIALS)
    registry["cohere_primary"] = resolved_stub
    if mutate is not None:
        mutate(registry)

    app = create_app(
        config_path=path,
        clock=resolved_clock,
        registry=registry,
        redis=FakeRedis(decode_responses=True),
    )
    with TestClient(app) as client:
        yield client, resolved_stub


def error_of(payload: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = payload["error"]
    return body


def problem_fields(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = error_of(payload)["keel"]["fields"]
    return fields


# ---- The M1 exit criterion ----


@pytest.mark.parametrize(
    ("preference", "expected_provider"),
    [(MOCK_FIRST, "mock_chaos"), (COHERE_FIRST, "cohere_primary")],
    ids=["mock_chaos preferred", "cohere_primary preferred"],
)
def test_the_same_request_body_is_served_by_whichever_provider_the_config_prefers(
    base_text: str,
    write_config: Callable[[str], Path],
    preference: str,
    expected_provider: str,
) -> None:
    """**This is M1.** Identical request, one line of config apart, different provider.

    The expected provider names are written out here rather than read back from
    the config, so the config and this test must agree — the same posture
    `test_router.py` takes with its candidate lists.
    """
    path = write_config(base_text.replace(COHERE_FIRST, preference))

    with gateway(path) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert response.headers["X-Keel-Provider"] == expected_provider

    payload = response.json()
    assert set(payload) == COMPLETION_KEYS
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert set(payload["usage"]) == USAGE_KEYS


# ---- The happy path ----


def test_the_response_body_is_the_providers_own_completion_passed_through_verbatim() -> None:
    """Pins the no-`response_model` decision.

    The stub answers with a key no OpenAI schema knows. If someone annotates the
    route `-> dict[str, Any]`, FastAPI generates a response model, re-serializes
    through it, and this key disappears — which is exactly the silent regression
    the annotation comment in `app.py` warns about.
    """
    unusual = dict(STUB_RESPONSE) | {"keel_sentinel": {"nested": [1, 2]}}

    with gateway(SHIPPED_CONFIG, stub=StubCohere(unusual)) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert response.json() == unusual


def test_the_attempts_header_reports_one_attempt_while_phase_1_has_no_failover() -> None:
    """Meant to be *edited* in Phase 3, not to survive it.

    Same intent as the two absence-tests in `test_router.py`: pinning what the
    gateway does not do yet makes adding it a visible diff rather than a quiet
    change of meaning.
    """
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.headers["X-Keel-Attempts"] == "1"


def test_the_cost_header_is_absent_until_the_phase_4_cost_engine() -> None:
    """Emitting a zero would be a cost claim, and a wrong one."""
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert "X-Keel-Cost-Micros" not in response.headers


def test_the_envelope_carries_the_injected_clocks_time_rather_than_wall_clock() -> None:
    """ADR 0001, asserted through the HTTP seam rather than around it."""
    with gateway(SHIPPED_CONFIG, clock=ManualClock(start=1_234.0)) as (client, stub):
        client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert stub.calls[0].received_at == 1_234.0


def test_the_deferrable_flag_comes_from_config_and_not_from_the_client() -> None:
    """`interactive_chat` is not deferrable, and a client cannot say otherwise.

    The body extension rejects the key outright, so the only route to `True` is
    the class config — checked here at the provider, past every layer that could
    have been fooled.
    """
    with gateway(SHIPPED_CONFIG) as (client, stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert stub.calls[0].deferrable is False


def test_the_x_keel_body_extension_supplies_metadata_when_a_client_cannot_set_headers() -> None:
    """§5.1's fallback, and the stripping that makes it safe.

    The extension must be *removed* before the payload is forwarded: leaving it
    in place would send an unknown top-level field to the provider and earn a 400
    for a request the client wrote correctly.
    """
    body = dict(BODY) | {
        "x_keel": {
            "tenant": "acme",
            "feature": "support-summary",
            "request_id": "req-1",
            "request_class": "interactive_chat",
        }
    }

    with gateway(SHIPPED_CONFIG) as (client, stub):
        response = client.post(ENDPOINT, json=body)

    assert response.status_code == 200
    assert "x_keel" not in stub.calls[0].payload
    assert stub.calls[0].payload["messages"] == BODY["messages"]


# ---- The envelope boundary, over real HTTP (FR-1.3) ----


@pytest.mark.parametrize(
    ("header", "field"),
    [
        ("X-Keel-Tenant", "tenant"),
        ("X-Keel-Feature", "feature"),
        ("X-Keel-Request-Id", "request_id"),
        ("X-Keel-Class", "request_class"),
    ],
    ids=["tenant", "feature", "request id", "class"],
)
def test_dropping_any_single_required_keel_header_is_a_400_naming_that_field(
    header: str, field: str
) -> None:
    headers = {name: value for name, value in HEADERS.items() if name != header}

    with gateway(SHIPPED_CONFIG) as (client, stub):
        response = client.post(ENDPOINT, headers=headers, json=BODY)

    assert response.status_code == 400
    payload = response.json()
    assert error_of(payload)["type"] == "invalid_request_error"
    assert error_of(payload)["code"] == "missing_metadata"
    assert [problem["field"] for problem in problem_fields(payload)] == [field]
    assert problem_fields(payload)[0]["header"] == header
    assert problem_fields(payload)[0]["code"] == "missing"
    assert stub.calls == []


def test_every_problem_is_reported_in_one_response_rather_than_one_per_round_trip() -> None:
    """FR-1.3. A client fixing headers one round trip at a time is a bad first impression.

    The order is the declaration order in `envelope.py`, not whatever a set
    happened to iterate in, so this list is stable enough to assert.
    """
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, json=BODY)

    assert response.status_code == 400
    assert [problem["field"] for problem in problem_fields(response.json())] == [
        "tenant",
        "feature",
        "request_id",
        "request_class",
    ]


def test_an_unknown_request_class_is_rejected_with_the_configured_classes_named() -> None:
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS | {"X-Keel-Class": "nope"}, json=BODY)

    assert response.status_code == 400
    problem = problem_fields(response.json())[0]
    assert problem["code"] == "unknown_request_class"
    for known in ("interactive_chat", "classification", "batch_enrichment"):
        assert known in problem["message"]


def test_a_deferrable_class_without_an_idempotency_key_is_rejected() -> None:
    """FR-5.3: a replay of a queued job must not duplicate a side effect."""
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(
            ENDPOINT, headers=HEADERS | {"X-Keel-Class": "batch_enrichment"}, json=BODY
        )

    assert response.status_code == 400
    assert [problem["field"] for problem in problem_fields(response.json())] == ["idempotency_key"]


@pytest.mark.parametrize(
    ("drop_request_id", "expected"),
    [(False, "req-1"), (True, None)],
    ids=["request id present", "request id itself missing"],
)
def test_a_rejection_echoes_the_request_id_so_a_client_can_correlate_it(
    drop_request_id: bool, expected: str | None
) -> None:
    headers = {name: value for name, value in HEADERS.items() if name != "X-Keel-Tenant"}
    if drop_request_id:
        headers.pop("X-Keel-Request-Id")

    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=headers, json=BODY)

    assert response.status_code == 400
    assert error_of(response.json())["keel"]["request_id"] == expected


@pytest.mark.parametrize(
    "raw",
    [b"1", b'"hi"', b'["a"]', b"null", b"true"],
    ids=["number", "string", "array", "null", "boolean"],
)
def test_a_body_that_is_not_a_json_object_is_a_400_rather_than_a_500(raw: bytes) -> None:
    """The guard above `build_envelope`, which calls `.items()` and would 500.

    A client that sent an array made a plain mistake; answering it with a server
    error would blame the gateway for the client's typo.
    """
    with gateway(SHIPPED_CONFIG) as (client, stub):
        response = client.post(ENDPOINT, headers=HEADERS, content=raw)

    assert response.status_code == 400
    payload = response.json()
    assert error_of(payload)["code"] == "invalid_request"
    assert [problem["field"] for problem in problem_fields(payload)] == ["body"]
    assert problem_fields(payload)[0]["code"] == "invalid"
    assert stub.calls == []


@pytest.mark.parametrize(
    "raw",
    [b'{"messages":', b"", b"not json at all"],
    ids=["truncated object", "empty body", "plain text"],
)
def test_a_body_that_is_not_valid_json_at_all_is_a_400_rather_than_a_500(raw: bytes) -> None:
    """An empty body lands here too: Starlette hands the bytes to `json.loads`."""
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, content=raw)

    assert response.status_code == 400
    assert [problem["field"] for problem in problem_fields(response.json())] == ["body"]


def test_streaming_is_rejected_until_fr_1_6() -> None:
    """Better a clear "not yet" than one non-streamed body to a client parsing SSE."""
    with gateway(SHIPPED_CONFIG) as (client, stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY | {"stream": True})

    assert response.status_code == 400
    payload = response.json()
    assert [problem["field"] for problem in problem_fields(payload)] == ["stream"]
    assert "FR-1.6" in problem_fields(payload)[0]["message"]
    assert stub.calls == []


def test_stream_false_is_served_normally() -> None:
    """Only `true` is refused. An SDK that always sends the key still works."""
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY | {"stream": False})

    assert response.status_code == 200


def test_the_error_body_keeps_the_openai_envelope_shape(base_text: str) -> None:
    """ADR 0003, key by key: a rename must break a test rather than a client."""
    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, json=BODY)

    payload = response.json()
    assert set(payload) == {"error"}
    assert set(error_of(payload)) == {"message", "type", "code", "keel"}
    assert set(error_of(payload)["keel"]) == {"request_id", "fields"}
    assert set(problem_fields(payload)[0]) == {"field", "header", "code", "message"}


# ---- Upstream failures (§4) ----


@pytest.mark.parametrize(
    ("error_class", "expected_status", "expected_code"),
    [
        (ErrorClass.BAD_REQUEST, 400, "upstream_bad_request"),
        (ErrorClass.CONTENT_FILTER, 400, "upstream_bad_request"),
        (ErrorClass.RATE_LIMIT, 429, "upstream_rate_limit"),
        (ErrorClass.QUOTA_EXHAUSTED, 429, "upstream_rate_limit"),
        (ErrorClass.TIMEOUT, 504, "upstream_timeout"),
        (ErrorClass.AUTH_FAILURE, 503, "upstream_unavailable"),
        (ErrorClass.SERVER_ERROR, 503, "upstream_unavailable"),
    ],
    ids=[
        "bad_request",
        "content_filter",
        "rate_limit",
        "quota_exhausted",
        "timeout",
        "auth_failure",
        "server_error",
    ],
)
def test_each_error_class_maps_to_its_http_status(
    error_class: ErrorClass, expected_status: int, expected_code: str
) -> None:
    """The table, transcribed by hand so `_UPSTREAM_BY_CLASS` and this must agree.

    The two 400s are the interesting rows: `BAD_REQUEST` and `CONTENT_FILTER` are
    also the two classes D7 keeps out of the breaker, because neither is evidence
    about provider health. Telling a client to retry either would be advice that
    can never come good.
    """
    with gateway(SHIPPED_CONFIG, stub=FailingCohere(error_class)) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == expected_status
    assert error_of(response.json())["code"] == expected_code


def test_the_full_taxonomy_is_covered_so_a_new_class_cannot_slip_through() -> None:
    """A completeness guard on the parametrization above, not on the source table.

    `keel/api/errors.py` already refuses to import with an unmapped class. This
    catches the other half: a class that is mapped but never exercised here.
    """
    exercised = {
        ErrorClass.BAD_REQUEST,
        ErrorClass.CONTENT_FILTER,
        ErrorClass.RATE_LIMIT,
        ErrorClass.QUOTA_EXHAUSTED,
        ErrorClass.TIMEOUT,
        ErrorClass.AUTH_FAILURE,
        ErrorClass.SERVER_ERROR,
    }
    assert exercised == set(ErrorClass)


def test_a_failed_request_still_names_the_provider_it_tried() -> None:
    """A 503 that will not say who it called is useless in the demo (§4)."""
    with gateway(SHIPPED_CONFIG, stub=FailingCohere(ErrorClass.SERVER_ERROR)) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.headers["X-Keel-Provider"] == "cohere_primary"
    assert response.headers["X-Keel-Attempts"] == "1"


def test_an_upstream_error_body_carries_the_provider_and_error_class_and_no_fields() -> None:
    """`fields` stays present and empty (ADR 0003) — there is no client fault to name."""
    with gateway(SHIPPED_CONFIG, stub=FailingCohere(ErrorClass.RATE_LIMIT)) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    keel = error_of(response.json())["keel"]
    assert keel["provider"] == "cohere_primary"
    assert keel["error_class"] == "rate_limit"
    assert keel["request_id"] == "req-1"
    assert keel["fields"] == []
    assert error_of(response.json())["type"] == "api_error"


def test_a_mock_configured_to_always_fail_is_a_503_rather_than_an_exception(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """End to end through the real adapter: a provider failure is a response, not a crash."""
    path = write_config(base_text.replace(COHERE_FIRST, MOCK_FIRST))

    def always_fail(registry: dict[str, ProviderAdapter]) -> None:
        mock = registry["mock_chaos"]
        assert isinstance(mock, MockAdapter)
        mock.state.error_rate = 1.0
        mock.state.error_classes = {ErrorClass.SERVER_ERROR: 1.0}

    with gateway(path, mutate=always_fail) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 503
    assert response.headers["X-Keel-Provider"] == "mock_chaos"
    assert error_of(response.json())["keel"]["error_class"] == "server_error"


# ---- Startup fails the process, not the request (NFR-4) ----


def test_a_config_that_does_not_validate_fails_at_startup(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """No request is sent. That absence is the assertion.

    A preference naming a provider the config does not declare is caught by
    `KeelConfig`'s cross-reference validator, and it has to surface before the
    first request rather than as a routing surprise during one.
    """
    ghost = COHERE_FIRST.replace("mock_chaos", "ghost")
    path = write_config(base_text.replace(COHERE_FIRST, ghost))

    with pytest.raises(ConfigError), TestClient(create_app(config_path=path)):
        pass  # pragma: no cover - startup raises before the body runs


def test_a_registry_that_cannot_be_built_fails_at_startup() -> None:
    """ADR 0004, reached through the app rather than around it.

    No `registry` override here, so the real `build_registry` runs and refuses a
    `cohere_primary` it has no key for. This is the case the ADR argues hardest
    about: served lazily it would be an `AUTH_FAILURE` on every request, which D7
    keeps out of the breaker — 100% failure with every dashboard green.
    """
    app = create_app(
        config_path=SHIPPED_CONFIG,
        clock=ManualClock(),
        credentials=ProviderCredentials(cohere_api_key=None),
    )

    with pytest.raises(ConfigError), TestClient(app):
        pass  # pragma: no cover - startup raises before the body runs


def test_a_config_file_that_does_not_exist_fails_at_startup_with_the_path_named(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope.yaml"

    with pytest.raises(ConfigError, match="nope.yaml"), TestClient(create_app(config_path=missing)):
        pass  # pragma: no cover - startup raises before the body runs


# ---- Wiring: where the config path comes from, and the module-level app ----


def test_the_config_path_comes_from_keel_config_path_when_no_override_is_given(
    base_text: str,
    write_config: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first code in the repo to honour the variable `.env.example` has shipped all along."""
    path = write_config(base_text.replace(COHERE_FIRST, MOCK_FIRST))
    monkeypatch.setenv("KEEL_CONFIG_PATH", str(path))

    clock = ManualClock(start=1_000.0)
    app = create_app(clock=clock, credentials=CREDENTIALS, redis=FakeRedis(decode_responses=True))
    with TestClient(app) as client:
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert response.headers["X-Keel-Provider"] == "mock_chaos"


def test_an_explicit_config_path_wins_over_the_environment(
    base_text: str,
    write_config: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_first = write_config(base_text.replace(COHERE_FIRST, MOCK_FIRST))
    monkeypatch.setenv("KEEL_CONFIG_PATH", str(mock_first))

    with gateway(SHIPPED_CONFIG) as (client, _stub):
        response = client.post(ENDPOINT, headers=HEADERS, json=BODY)

    assert response.headers["X-Keel-Provider"] == "cohere_primary"


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_config_path_variable_is_treated_as_absent(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copied-but-unfilled template must not become `Path("")`.

    Same rule `ProviderCredentials` applies to a blank `COHERE_API_KEY`, for the
    same reason. Falling back to the shipped default is what makes this pass at
    all — `Path("")` would raise `IsADirectoryError`, not `ConfigError`.
    """
    monkeypatch.setenv("KEEL_CONFIG_PATH", value)
    monkeypatch.chdir(REPO_ROOT)

    app = create_app(
        clock=ManualClock(), credentials=CREDENTIALS, redis=FakeRedis(decode_responses=True)
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_the_module_level_app_exists_for_uvicorn_and_reads_no_config_at_import_time() -> None:
    """Pins `uvicorn keel.api.app:app` and the import-is-side-effect-free property.

    Nothing else in this file starts that app, so its state stays empty — which
    is the point: constructing it must not touch the filesystem or fail on a bad
    config. It fails at startup instead, which is where NFR-4 wants it.
    """
    assert isinstance(module_level_app, FastAPI)
    assert not hasattr(module_level_app.state, "keel")


def test_the_route_table_is_exactly_the_two_endpoints_phase_1_ships() -> None:
    """Anti-scope-creep, asserted rather than remembered.

    `/metrics` is P2-T4 and `/chaos` is Phase 6; neither is here yet. Filtering
    to `APIRoute` drops FastAPI's own `/docs`, `/redoc`, and `/openapi.json`,
    which are plain Starlette routes and are deliberately left switched on.
    """
    routes = {
        (route.path, tuple(sorted(route.methods)))
        for route in module_level_app.routes
        if isinstance(route, APIRoute)
    }

    assert routes == {("/v1/chat/completions", ("POST",)), ("/healthz", ("GET",))}


# ---- Liveness ----


def test_healthz_is_ok_and_touches_no_provider() -> None:
    """Liveness, not readiness. A probe that called a provider would restart the
    container during exactly the outage the gateway exists to absorb."""
    with gateway(SHIPPED_CONFIG) as (client, stub):
        responses = [client.get("/healthz") for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == {"status": "ok"}
    assert stub.calls == []


def test_the_config_and_registry_reach_app_state() -> None:
    """The lifespan wiring itself, since everything else asserts it only indirectly."""
    from keel.api.app import AppContext

    app = create_app(
        config_path=SHIPPED_CONFIG,
        clock=ManualClock(),
        credentials=CREDENTIALS,
        redis=FakeRedis(decode_responses=True),
    )
    with TestClient(app):
        context = app.state.keel

    assert isinstance(context, AppContext)
    assert isinstance(context.config, KeelConfig)
    assert set(context.registry) == {"cohere_primary", "mock_chaos"}
    assert isinstance(context.tracker, HealthTracker)


def test_a_served_request_leaves_a_count_in_the_health_window() -> None:
    """The P2-T2 wiring, proved through the real HTTP stack rather than at the seam.

    `tests/test_health_window.py` pins what the window does and
    `tests/test_executor.py` pins that the executor calls it; this pins that the
    two are actually connected inside the app the lifespan builds. A recorder
    wired correctly everywhere except in `create_app` would pass both of those
    and still record nothing in production.

    The injected client is deliberately read *after* the `TestClient` block
    exits, which is exactly why the lifespan closes only a client it built
    itself.
    """
    redis = FakeRedis(decode_responses=True)
    clock = ManualClock(start=1_000.0)
    config = load_config(SHIPPED_CONFIG)
    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)
    registry["cohere_primary"] = StubCohere()

    app = create_app(
        config_path=SHIPPED_CONFIG, clock=clock, registry=registry, redis=redis
    )
    with TestClient(app) as client:
        assert client.post(ENDPOINT, headers=HEADERS, json=BODY).status_code == 200
        window = app.state.keel.tracker

    counts = asyncio.run(window.read("cohere_primary"))
    assert counts is not None
    assert counts.ok == 1
    assert counts.total == 1


def test_unknown_json_paths_are_a_404_rather_than_an_ingress_attempt() -> None:
    """The gateway is not a catch-all proxy; only the declared surface answers."""
    with gateway(SHIPPED_CONFIG) as (client, stub):
        response = client.post("/v1/embeddings", headers=HEADERS, json=BODY)

    assert response.status_code == 404
    assert stub.calls == []
