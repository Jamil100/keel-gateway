"""Tests for the chaos endpoint (P2-T6, ADR 0010, an early slice of FR-7.2).

This route exists because the M2 exit criterion runs two load profiles against
one gateway without restarting it, and `MockChaosState` is otherwise unreachable
from outside the process: `build_registry` never passes one, so a running mock
sits at `error_rate=0.0, latency_ms=50.0` forever and `loadgen --error-rate 0.4`
would have nowhere to put the number.

Three properties are worth the weight here.

**It is off unless asked for.** The gateway has no authentication of any kind
(§10), so an always-registered failure injector is a denial of service with a
REST interface. The route is registered only when `KEEL_CHAOS_ENABLED` is set —
conditionally, so the route table stays a fact about configuration that
`tests/test_app.py` asserts in both directions rather than a runtime branch.

**The bounds live in `MockChaosState`, not here.** That model is
`validate_assignment=True`, so `error_rate = 5.0` raises at the assignment. The
endpoint translates that into the ADR 0003 error body rather than restating the
range, which is what stops the two from drifting apart.

**A control that does nothing must not answer 200.** A real provider cannot be
told to fail 40% of the time, so naming one is a 409 rather than a cheerful
no-op — the failure mode this guards against is a chaos demo that silently does
not work.

Every app here is built against `deploy/keel.demo.yaml`, which is mock-only, so
these tests need no credentials — the same property the compose stack relies on.
No network, `fakeredis` for Redis (NFR-2).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.clock import Clock, ManualClock
from keel.providers.mock import MockAdapter

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO_ROOT / "deploy" / "keel.demo.yaml"
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

ENDPOINT = "/chaos/mock_chaos"

HEADERS = {
    "X-Keel-Tenant": "acme",
    "X-Keel-Feature": "support-summary",
    "X-Keel-Request-Id": "req-chaos-1",
    "X-Keel-Class": "interactive_chat",
}
BODY: dict[str, Any] = {"model": "keel", "messages": [{"role": "user", "content": "hi"}]}


@contextmanager
def gateway(*, chaos_enabled: bool = True) -> Iterator[TestClient]:
    """A started app over the demo config, with chaos on unless told otherwise.

    The registry is built by the *real* `build_registry` from the *real* demo
    config, with **no credentials passed at all**. That is deliberate: it is the
    same path `docker compose up` takes, so if the demo config ever grows a
    provider that needs a key, every test in this module fails rather than the
    compose stack failing on someone else's machine.
    """
    clock: Clock = ManualClock(start=1_000.0)
    app = create_app(
        config_path=DEMO_CONFIG,
        clock=clock,
        redis=FakeRedis(decode_responses=True),
        chaos_enabled=chaos_enabled,
    )
    with TestClient(app) as client:
        yield client


def adapter_of(client: TestClient) -> MockAdapter:
    """The live mock the endpoint mutates, reached the way the route reaches it."""
    found = client.app.state.keel.registry["mock_chaos"]
    assert isinstance(found, MockAdapter)
    return found


def statuses(client: TestClient, count: int, prefix: str) -> list[int]:
    """Drive `count` requests, each with its own request id."""
    return [
        client.post(
            "/v1/chat/completions",
            headers={**HEADERS, "X-Keel-Request-Id": f"{prefix}-{index}"},
            json=BODY,
        ).status_code
        for index in range(count)
    ]


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_the_endpoint_is_absent_unless_enabled() -> None:
    """Off by default, and absent rather than forbidden.

    A 404 from an unregistered route says the same thing to a caller as a 403
    would, and says less to an attacker.
    """
    with gateway(chaos_enabled=False) as client:
        assert client.post(ENDPOINT, json={"error_rate": 0.4}).status_code == 404


def test_the_endpoint_is_present_when_enabled() -> None:
    with gateway() as client:
        assert client.post(ENDPOINT, json={"error_rate": 0.4}).status_code == 200


def test_the_env_var_enables_it_when_no_argument_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KEEL_CHAOS_ENABLED` is what compose sets; the argument is what tests set.

    Pinned because the compose stack depends entirely on the env path, and
    nothing else in the suite exercises it — every other test here passes
    `chaos_enabled=` explicitly.
    """
    monkeypatch.setenv("KEEL_CHAOS_ENABLED", "true")
    app = create_app(
        config_path=DEMO_CONFIG,
        clock=ManualClock(start=1_000.0),
        redis=FakeRedis(decode_responses=True),
    )
    with TestClient(app) as client:
        assert client.post(ENDPOINT, json={"error_rate": 0.1}).status_code == 200


@pytest.mark.parametrize("value", ["", "  ", "0", "false", "no", "off", "maybe"])
def test_falsey_env_values_leave_it_disabled(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that is not an affirmative spelling means off.

    Fail-closed on an unrecognised value, unlike `KEEL_LOG_LEVEL`, which raises.
    A typo here should not stop a gateway from starting; it should stop it from
    exposing a failure injector.
    """
    monkeypatch.setenv("KEEL_CHAOS_ENABLED", value)
    app = create_app(
        config_path=DEMO_CONFIG,
        clock=ManualClock(start=1_000.0),
        redis=FakeRedis(decode_responses=True),
    )
    with TestClient(app) as client:
        assert client.post(ENDPOINT, json={"error_rate": 0.1}).status_code == 404


# --------------------------------------------------------------------------
# What it does
# --------------------------------------------------------------------------


def test_it_mutates_the_live_adapter_and_returns_the_whole_state() -> None:
    with gateway() as client:
        response = client.post(ENDPOINT, json={"error_rate": 0.4, "latency_ms": 3000})

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "mock_chaos"
        assert body["state"]["error_rate"] == 0.4
        assert body["state"]["latency_ms"] == 3000.0

        state = adapter_of(client).state
        assert state.error_rate == 0.4
        assert state.latency_ms == 3000.0


def test_omitted_fields_are_left_alone() -> None:
    """Why the request model is not `MockChaosState` itself.

    That model has a default for every field, so binding it directly would turn
    "set the error rate" into "set the error rate and reset latency, the class
    mix, and the seed" — a footgun in the middle of a chaos run, and exactly the
    kind of thing loadgen does when it flips the rate mid-flight.
    """
    with gateway() as client:
        client.post(ENDPOINT, json={"latency_ms": 1234, "latency_sigma": 0.5})
        client.post(ENDPOINT, json={"error_rate": 0.25})

        state = adapter_of(client).state
        assert state.error_rate == 0.25
        assert state.latency_ms == 1234.0, "latency survived a later error-rate change"
        assert state.latency_sigma == 0.5


def test_setting_the_error_rate_actually_changes_what_requests_do() -> None:
    """The property the whole endpoint exists for, asserted end to end.

    Not just that the field changed — that the *gateway's behaviour* changed,
    through the real routing and execution path. `latency_ms=0` keeps the test
    fast; the mock sleeps through the injected clock either way.
    """
    with gateway() as client:
        client.post(ENDPOINT, json={"error_rate": 1.0, "latency_ms": 0})
        assert all(code >= 400 for code in statuses(client, 6, "fail"))

        client.post(ENDPOINT, json={"error_rate": 0.0})
        assert statuses(client, 6, "pass") == [200] * 6


def test_a_failing_run_splits_across_taxonomy_classes() -> None:
    """What the M2 error-rate panel is required to show.

    The mock's default mix is weighted across five classes, and the panel groups
    by `error_class`. A run that produced one status would make that panel a flat
    line and the demo pointless, so the spread is pinned here rather than assumed
    from the mix.
    """
    with gateway() as client:
        client.post(ENDPOINT, json={"error_rate": 1.0, "latency_ms": 0})
        observed = set(statuses(client, 40, "mix"))

    assert len(observed) > 1, f"every failure produced the same status: {observed}"
    assert all(code >= 400 for code in observed)


def test_the_seed_is_only_restarted_when_it_is_named() -> None:
    """`reseed()` restarts the streams even when the value is unchanged.

    That is what re-running the same scenario needs and what a mid-run error-rate
    change must not do — otherwise every `loadgen` flip would rewind the failure
    sequence and the two M2 runs could not be compared.
    """
    with gateway() as client:
        client.post(ENDPOINT, json={"error_rate": 1.0, "latency_ms": 0, "seed": 7})
        first = statuses(client, 8, "a")

        # Same seed, named again: the sequence restarts.
        client.post(ENDPOINT, json={"seed": 7})
        assert statuses(client, 8, "b") == first

        # A change that does not name the seed continues the stream rather than
        # rewinding it. These are draws 17-24 of a deterministic sequence, so a
        # match against draws 1-8 would mean the streams had been restarted.
        client.post(ENDPOINT, json={"error_rate": 1.0})
        assert statuses(client, 8, "c") != first, (
            "changing the error rate rewound the failure sequence; a mid-run "
            "loadgen flip would then make the two M2 runs incomparable"
        )
        assert adapter_of(client).state.seed == 7


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_an_out_of_range_value_is_rejected_by_the_model_that_owns_the_bound() -> None:
    """422-worthy, rendered as the one ADR 0003 body with a 400.

    The message is pydantic's own, carried through rather than paraphrased, so
    the bound stated in `MockChaosState` is the only place it is written down.
    """
    with gateway() as client:
        response = client.post(ENDPOINT, json={"error_rate": 5.0})

        assert response.status_code == 400
        problem = response.json()["error"]["keel"]["fields"][0]
        assert problem["field"] == "error_rate"
        assert "less than or equal to 1" in problem["message"]
        assert adapter_of(client).state.error_rate == 0.0, "nothing was applied"


def test_an_unknown_knob_is_rejected_in_the_keel_error_body() -> None:
    """ADR 0003 says *every* 4xx uses one body, including FastAPI's own validator.

    `ChaosRequest` is the first bound body model in the gateway — the ingress
    route deliberately has none — so it is the first thing that could have
    returned a bare `{"detail": [...]}` and quietly introduced a second error
    shape.
    """
    with gateway() as client:
        response = client.post(ENDPOINT, json={"error_rat": 0.4})

        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["keel"]["fields"][0]["field"] == "body.error_rat"


def test_an_unknown_provider_is_a_404() -> None:
    with gateway() as client:
        response = client.post("/chaos/not_a_provider", json={"error_rate": 0.1})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "unknown_provider"
        assert "mock_chaos" in response.json()["error"]["keel"]["fields"][0]["message"]


def test_a_real_provider_is_a_409_rather_than_a_cheerful_no_op() -> None:
    """The shipped config is used here precisely because it has a Cohere entry.

    A control that reports success while doing nothing is worse than one that
    refuses: it would make a chaos demo look like it worked. Needs a credential,
    so one is supplied — this is the only test in the module that does.
    """
    from keel.providers.credentials import ProviderCredentials

    app = create_app(
        config_path=SHIPPED_CONFIG,
        clock=ManualClock(start=1_000.0),
        credentials=ProviderCredentials(cohere_api_key="test-key"),
        redis=FakeRedis(decode_responses=True),
        chaos_enabled=True,
    )
    with TestClient(app) as client:
        response = client.post("/chaos/cohere_primary", json={"error_rate": 0.4})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "chaos_unsupported"
        assert "cohere" in response.json()["error"]["keel"]["fields"][0]["message"]
