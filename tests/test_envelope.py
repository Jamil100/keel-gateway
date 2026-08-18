"""Tests for envelope construction and header validation (FR-1.2 - FR-1.5).

Every case runs against the *shipped* ``config/keel.yaml``, following
``tests/test_config.py``. That gives real request classes — one deferrable
(``batch_enrichment``) and two not — so the deferrable rules are exercised
against the configuration the gateway actually ships rather than a fixture
invented to make them pass.

The two claims this module has to earn:

1. A rejection lists *every* problem at once (FR-1.3). Several tests assert the
   exact problem count, because "collects all of them" is only true if a second
   problem never gets swallowed by the first.
2. ``deferrable`` comes from config and nowhere else — a client cannot assert
   its way into the durable queue.

No network, no Redis, and time comes from ``ManualClock`` (NFR-2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from keel.api.envelope import (
    BODY_EXTENSION_KEY,
    HEADER_CAPABILITIES,
    HEADER_CLASS,
    HEADER_FEATURE,
    HEADER_IDEMPOTENCY_KEY,
    HEADER_REQUEST_ID,
    HEADER_TENANT,
    build_envelope,
)
from keel.api.errors import EnvelopeValidationError, KeelError, MalformedRequestError, ProblemCode
from keel.clock import ManualClock
from keel.config import KeelConfig, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

RECEIVED_AT = 1_000.0

# The shipped config's only deferrable class, and a non-deferrable one.
DEFERRABLE_CLASS = "batch_enrichment"
INTERACTIVE_CLASS = "interactive_chat"

COMPLETE_HEADERS = {
    HEADER_TENANT: "acme",
    HEADER_FEATURE: "support-summary",
    HEADER_REQUEST_ID: "00000000-0000-0000-0000-000000000001",
    HEADER_CLASS: INTERACTIVE_CLASS,
}

OPENAI_BODY: dict[str, Any] = {
    "model": "keel",
    "messages": [{"role": "user", "content": "hi"}],
}


@pytest.fixture
def config() -> KeelConfig:
    return load_config(SHIPPED_CONFIG)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=RECEIVED_AT)


def build(
    config: KeelConfig,
    clock: ManualClock,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    return build_envelope(
        headers=COMPLETE_HEADERS if headers is None else headers,
        body=OPENAI_BODY if body is None else body,
        config=config,
        clock=clock,
    )


def problem_fields(error: KeelError) -> list[str]:
    return [problem.field for problem in error.fields]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_complete_request_becomes_a_full_envelope(config: KeelConfig, clock: ManualClock) -> None:
    envelope = build(config, clock)

    assert envelope.tenant == "acme"
    assert envelope.feature == "support-summary"
    assert envelope.request_id == "00000000-0000-0000-0000-000000000001"
    assert envelope.request_class == INTERACTIVE_CLASS
    assert envelope.capabilities == frozenset()
    assert envelope.deferrable is False
    assert envelope.idempotency_key is None
    assert envelope.payload == OPENAI_BODY
    assert envelope.received_at == RECEIVED_AT


def test_received_at_comes_from_the_injected_clock(config: KeelConfig) -> None:
    """Not ``time.time()`` (ADR 0001) — a manual clock must fully determine it."""
    clock = ManualClock(start=42.5)

    first = build(config, clock)
    clock.advance(10.0)
    second = build(config, clock)

    assert first.received_at == 42.5
    assert second.received_at == 52.5


def test_envelope_is_frozen(config: KeelConfig, clock: ManualClock) -> None:
    """Decided once at ingress; the router and executor only read it."""
    envelope = build(config, clock)

    with pytest.raises(ValueError, match="frozen"):
        envelope.tenant = "someone-else"


def test_request_id_accepts_a_non_uuid_correlation_id(
    config: KeelConfig, clock: ManualClock
) -> None:
    """§5.1's `uuid` is an example value. A caller's own trace id is not a 400."""
    envelope = build(config, clock, headers={**COMPLETE_HEADERS, HEADER_REQUEST_ID: "trace-abc-7"})

    assert envelope.request_id == "trace-abc-7"


def test_header_names_are_matched_case_insensitively(
    config: KeelConfig, clock: ManualClock
) -> None:
    """HTTP says headers are case-insensitive, and real clients lower-case them."""
    envelope = build(
        config, clock, headers={name.lower(): value for name, value in COMPLETE_HEADERS.items()}
    )

    assert envelope.tenant == "acme"


def test_header_values_are_stripped(config: KeelConfig, clock: ManualClock) -> None:
    envelope = build(config, clock, headers={**COMPLETE_HEADERS, HEADER_TENANT: "  acme  "})

    assert envelope.tenant == "acme"


# --------------------------------------------------------------------------
# Missing required metadata. Individually, then in combination — the whole
# reason the collector exists (FR-1.3).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dropped", "expected_field"),
    [
        pytest.param(HEADER_TENANT, "tenant", id="tenant-header-missing"),
        pytest.param(HEADER_FEATURE, "feature", id="feature-header-missing"),
        pytest.param(HEADER_REQUEST_ID, "request_id", id="request-id-header-missing"),
        pytest.param(HEADER_CLASS, "request_class", id="class-header-missing"),
    ],
)
def test_each_required_header_is_rejected_on_its_own(
    config: KeelConfig, clock: ManualClock, dropped: str, expected_field: str
) -> None:
    headers = {name: value for name, value in COMPLETE_HEADERS.items() if name != dropped}

    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers=headers)

    assert problem_fields(caught.value) == [expected_field]
    assert caught.value.fields[0].code is ProblemCode.MISSING
    assert caught.value.fields[0].header == dropped
    assert dropped in caught.value.fields[0].message, "the message must name the fix"


def test_all_four_missing_headers_are_reported_in_one_response(
    config: KeelConfig, clock: ManualClock
) -> None:
    """Fixing headers one round trip at a time is a bad first impression."""
    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers={})

    assert problem_fields(caught.value) == ["tenant", "feature", "request_id", "request_class"]


def test_a_combination_of_missing_headers_reports_exactly_those(
    config: KeelConfig, clock: ManualClock
) -> None:
    headers = {
        name: value
        for name, value in COMPLETE_HEADERS.items()
        if name not in {HEADER_TENANT, HEADER_CLASS}
    }

    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers=headers)

    assert problem_fields(caught.value) == ["tenant", "request_class"]


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_a_blank_header_is_treated_as_missing(
    config: KeelConfig, clock: ManualClock, blank: str
) -> None:
    """`X-Keel-Tenant: ` is the same client mistake as omitting it entirely."""
    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers={**COMPLETE_HEADERS, HEADER_TENANT: blank})

    assert problem_fields(caught.value) == ["tenant"]
    assert caught.value.fields[0].code is ProblemCode.MISSING


def test_rejection_echoes_the_request_id_when_it_was_supplied(
    config: KeelConfig, clock: ManualClock
) -> None:
    """So a client can correlate the rejection with what it sent."""
    headers = {name: value for name, value in COMPLETE_HEADERS.items() if name != HEADER_TENANT}

    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers=headers)

    assert caught.value.request_id == "00000000-0000-0000-0000-000000000001"


# --------------------------------------------------------------------------
# The request class drives everything downstream (FR-1.4)
# --------------------------------------------------------------------------


def test_unknown_request_class_names_the_classes_that_do_exist(
    config: KeelConfig, clock: ManualClock
) -> None:
    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers={**COMPLETE_HEADERS, HEADER_CLASS: "chat"})

    assert problem_fields(caught.value) == ["request_class"]
    assert caught.value.fields[0].code is ProblemCode.UNKNOWN_REQUEST_CLASS
    message = caught.value.fields[0].message
    assert "'chat'" in message
    assert "batch_enrichment" in message and INTERACTIVE_CLASS in message


def test_unknown_class_does_not_also_demand_an_idempotency_key(
    config: KeelConfig, clock: ManualClock
) -> None:
    """We cannot know if an unknown class is deferrable, so we must not guess.

    Emitting a second problem here would send the client chasing a requirement
    that may not exist once the class name is fixed.
    """
    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers={**COMPLETE_HEADERS, HEADER_CLASS: "chat"})

    assert problem_fields(caught.value) == ["request_class"], "exactly one problem, not two"


# --------------------------------------------------------------------------
# deferrable is derived, and the idempotency key it implies (FR-5.1, FR-5.3)
# --------------------------------------------------------------------------


def test_deferrable_class_without_an_idempotency_key_is_rejected(
    config: KeelConfig, clock: ManualClock
) -> None:
    with pytest.raises(EnvelopeValidationError) as caught:
        build(config, clock, headers={**COMPLETE_HEADERS, HEADER_CLASS: DEFERRABLE_CLASS})

    assert problem_fields(caught.value) == ["idempotency_key"]
    assert caught.value.fields[0].header == HEADER_IDEMPOTENCY_KEY


def test_deferrable_class_with_an_idempotency_key_is_accepted(
    config: KeelConfig, clock: ManualClock
) -> None:
    envelope = build(
        config,
        clock,
        headers={
            **COMPLETE_HEADERS,
            HEADER_CLASS: DEFERRABLE_CLASS,
            HEADER_IDEMPOTENCY_KEY: "key-1",
        },
    )

    assert envelope.deferrable is True
    assert envelope.idempotency_key == "key-1"


def test_deferrable_is_read_from_config_not_from_the_client(
    config: KeelConfig, clock: ManualClock
) -> None:
    """The load-bearing rule: a caller cannot promote itself into the queue."""
    interactive = build(config, clock, headers={**COMPLETE_HEADERS, HEADER_CLASS: "classification"})

    assert interactive.deferrable is config.request_classes["classification"].deferrable
    assert interactive.deferrable is False


def test_an_idempotency_key_on_a_non_deferrable_class_is_carried_not_dropped(
    config: KeelConfig, clock: ManualClock
) -> None:
    """Harmless, and Phase 5 may still want it. Silently discarding it would not be."""
    envelope = build(config, clock, headers={**COMPLETE_HEADERS, HEADER_IDEMPOTENCY_KEY: "key-2"})

    assert envelope.deferrable is False
    assert envelope.idempotency_key == "key-2"


# --------------------------------------------------------------------------
# Capabilities (FR-1.5) — optional, and never a reason to reject
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("citations", {"citations"}, id="single-tag"),
        pytest.param("citations,tool_use", {"citations", "tool_use"}, id="comma-separated"),
        pytest.param("citations, tool_use ", {"citations", "tool_use"}, id="whitespace-tolerated"),
        pytest.param("citations,,tool_use", {"citations", "tool_use"}, id="empty-segment-dropped"),
        pytest.param(" , ", set(), id="all-empty-segments-yield-no-constraint"),
        pytest.param("", set(), id="blank-header-is-no-constraint"),
    ],
)
def test_capabilities_header_parsing(
    config: KeelConfig, clock: ManualClock, raw: str, expected: set[str]
) -> None:
    envelope = build(config, clock, headers={**COMPLETE_HEADERS, HEADER_CAPABILITIES: raw})

    assert envelope.capabilities == frozenset(expected)
    assert isinstance(envelope.capabilities, frozenset), "must match ProviderConfig.capabilities"


def test_absent_capabilities_means_no_constraint_not_an_error(
    config: KeelConfig, clock: ManualClock
) -> None:
    assert build(config, clock).capabilities == frozenset()


# --------------------------------------------------------------------------
# The request-body extension, for clients that cannot set headers (§5.1)
# --------------------------------------------------------------------------


def test_every_field_can_arrive_through_the_body_extension(
    config: KeelConfig, clock: ManualClock
) -> None:
    envelope = build(
        config,
        clock,
        headers={},
        body={
            **OPENAI_BODY,
            BODY_EXTENSION_KEY: {
                "tenant": "acme",
                "feature": "support-summary",
                "request_id": "req-1",
                "request_class": DEFERRABLE_CLASS,
                "capabilities": ["citations", "tool_use"],
                "idempotency_key": "key-3",
            },
        },
    )

    assert envelope.tenant == "acme"
    assert envelope.request_class == DEFERRABLE_CLASS
    assert envelope.capabilities == frozenset({"citations", "tool_use"})
    assert envelope.deferrable is True
    assert envelope.idempotency_key == "key-3"


def test_capabilities_in_the_body_also_accept_the_comma_separated_form(
    config: KeelConfig, clock: ManualClock
) -> None:
    envelope = build(
        config,
        clock,
        body={**OPENAI_BODY, BODY_EXTENSION_KEY: {"capabilities": "citations, tool_use"}},
    )

    assert envelope.capabilities == frozenset({"citations", "tool_use"})


def test_the_header_wins_when_both_channels_supply_a_field(
    config: KeelConfig, clock: ManualClock
) -> None:
    """§5.1 states headers are the preferred channel; precedence has to match."""
    envelope = build(
        config,
        clock,
        body={**OPENAI_BODY, BODY_EXTENSION_KEY: {"tenant": "impostor"}},
    )

    assert envelope.tenant == "acme"


def test_the_extension_is_stripped_from_the_provider_bound_payload(
    config: KeelConfig, clock: ManualClock
) -> None:
    """A header survives pass-through; a body field does not.

    Forwarding ``x_keel`` to the provider would earn a 400 for a request the
    client wrote exactly as documented.
    """
    body = {**OPENAI_BODY, BODY_EXTENSION_KEY: {"tenant": "acme"}}

    envelope = build(config, clock, body=body)

    assert BODY_EXTENSION_KEY not in envelope.payload
    assert envelope.payload == OPENAI_BODY
    assert BODY_EXTENSION_KEY in body, "the caller's body must not be mutated"


def test_a_body_without_the_extension_passes_through_untouched(
    config: KeelConfig, clock: ManualClock
) -> None:
    assert build(config, clock).payload == OPENAI_BODY


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("tenant=acme", id="string"),
        pytest.param(["tenant"], id="list"),
        pytest.param(7, id="number"),
    ],
)
def test_an_extension_that_is_not_an_object_is_rejected(
    config: KeelConfig, clock: ManualClock, raw: object
) -> None:
    with pytest.raises(MalformedRequestError) as caught:
        build(config, clock, body={**OPENAI_BODY, BODY_EXTENSION_KEY: raw})

    assert problem_fields(caught.value) == [BODY_EXTENSION_KEY]
    assert caught.value.fields[0].code is ProblemCode.INVALID


def test_a_malformed_extension_still_reports_the_missing_headers_too(
    config: KeelConfig, clock: ManualClock
) -> None:
    """One response, everything wrong with it — even across both channels."""
    with pytest.raises(MalformedRequestError) as caught:
        build(config, clock, headers={}, body={BODY_EXTENSION_KEY: "nope"})

    assert problem_fields(caught.value) == [
        BODY_EXTENSION_KEY,
        "tenant",
        "feature",
        "request_id",
        "request_class",
    ]


def test_an_unknown_key_inside_the_extension_is_rejected(
    config: KeelConfig, clock: ManualClock
) -> None:
    """Same posture as the config models: a silently ignored key reads as configured."""
    with pytest.raises(MalformedRequestError) as caught:
        build(config, clock, headers={}, body={BODY_EXTENSION_KEY: {"tenat": "acme"}})

    # The typo is named, and the field it failed to supply is still reported —
    # both halves of the fix in one response.
    assert problem_fields(caught.value) == [
        f"{BODY_EXTENSION_KEY}.tenat",
        "tenant",
        "feature",
        "request_id",
        "request_class",
    ]
    assert caught.value.fields[0].code is ProblemCode.UNKNOWN_FIELD


def test_a_client_cannot_declare_itself_deferrable(
    config: KeelConfig, clock: ManualClock
) -> None:
    """Not merely ignored — rejected, and the message says why.

    ``deferrable`` decides whether a failed request survives in the durable
    queue. A client that thinks it set the flag and silently did not would be
    told its work was durable when it was not.
    """
    with pytest.raises(MalformedRequestError) as caught:
        build(config, clock, body={**OPENAI_BODY, BODY_EXTENSION_KEY: {"deferrable": True}})

    assert problem_fields(caught.value) == [f"{BODY_EXTENSION_KEY}.deferrable"]
    assert "cannot be set by a client" in caught.value.fields[0].message


def test_a_non_string_field_in_the_extension_is_rejected(
    config: KeelConfig, clock: ManualClock
) -> None:
    with pytest.raises(MalformedRequestError) as caught:
        build(config, clock, headers={}, body={BODY_EXTENSION_KEY: {"tenant": 7}})

    assert f"{BODY_EXTENSION_KEY}.tenant" in problem_fields(caught.value)
    assert caught.value.fields[0].code is ProblemCode.INVALID


def test_malformed_capabilities_in_the_extension_are_rejected(
    config: KeelConfig, clock: ManualClock
) -> None:
    with pytest.raises(MalformedRequestError) as caught:
        build(config, clock, body={**OPENAI_BODY, BODY_EXTENSION_KEY: {"capabilities": {"a": 1}}})

    assert problem_fields(caught.value) == [f"{BODY_EXTENSION_KEY}.capabilities"]
