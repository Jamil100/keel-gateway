"""Tests for the client-facing error body (FR-1.3).

The claim this module earns is that the body is a *contract*: a client can
branch on ``error.code`` and read ``error.keel.fields`` without parsing prose.
So the assertions here are key-by-key and exact — renaming a key must break a
test rather than a client.

No network, no Redis, no clock (NFR-2).
"""

from __future__ import annotations

import json

import pytest

from keel.api.errors import (
    EnvelopeValidationError,
    FieldProblem,
    KeelError,
    MalformedRequestError,
    ProblemCode,
    raise_for,
)


def problem(field: str, code: ProblemCode = ProblemCode.MISSING) -> FieldProblem:
    return FieldProblem(
        field=field,
        header=f"X-Keel-{field.title()}",
        code=code,
        message=f"{field} is wrong",
    )


# --------------------------------------------------------------------------
# The body shape. This is the wire contract.
# --------------------------------------------------------------------------


def test_body_nests_keel_detail_inside_the_openai_error_envelope() -> None:
    """An OpenAI SDK client reads error.message; a machine reads error.keel."""
    error = EnvelopeValidationError(
        "Request rejected: 1 problem (tenant).",
        fields=[problem("tenant")],
        request_id="req-1",
    )

    body = error.to_body()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"message", "type", "code", "keel"}
    assert body["error"]["message"] == "Request rejected: 1 problem (tenant)."
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "missing_metadata"
    assert set(body["error"]["keel"]) == {"request_id", "fields"}
    assert body["error"]["keel"]["request_id"] == "req-1"
    assert body["error"]["keel"]["fields"] == [
        {
            "field": "tenant",
            "header": "X-Keel-Tenant",
            "code": "missing",
            "message": "tenant is wrong",
        }
    ]


def test_body_is_json_serializable() -> None:
    """It crosses the wire, so a StrEnum leaking through as an enum would 500."""
    error = EnvelopeValidationError(
        "rejected",
        fields=[problem("tenant"), problem("x_keel.nope", ProblemCode.UNKNOWN_FIELD)],
    )

    round_tripped = json.loads(json.dumps(error.to_body()))

    codes = [entry["code"] for entry in round_tripped["error"]["keel"]["fields"]]
    assert codes == ["missing", "unknown_field"]
    assert all(isinstance(code, str) for code in codes)


def test_request_id_is_null_when_it_was_itself_missing() -> None:
    """A client that forgot the id gets an explicit null, not a fabricated one."""
    error = EnvelopeValidationError("rejected", fields=[problem("request_id")])

    assert error.to_body()["error"]["keel"]["request_id"] is None


def test_body_lists_every_problem_not_just_the_first() -> None:
    """The whole point: one round trip to learn everything that is wrong."""
    fields = [problem(name) for name in ("tenant", "feature", "request_id", "request_class")]

    body = EnvelopeValidationError("rejected", fields=fields).to_body()

    assert [entry["field"] for entry in body["error"]["keel"]["fields"]] == [
        "tenant",
        "feature",
        "request_id",
        "request_class",
    ]


def test_field_order_is_preserved_exactly_as_given() -> None:
    """Stable order, so a test or a client can index into the list."""
    fields = [problem("feature"), problem("tenant")]

    body = MalformedRequestError("rejected", fields=fields).to_body()

    assert [entry["field"] for entry in body["error"]["keel"]["fields"]] == ["feature", "tenant"]


def test_an_error_with_no_fields_still_produces_the_same_shape() -> None:
    """503s and 422s carry no field list; the shape must not change under them."""
    body = KeelError("upstream exhausted", request_id="req-9").to_body()

    assert set(body["error"]) == {"message", "type", "code", "keel"}
    assert body["error"]["keel"]["fields"] == []


# --------------------------------------------------------------------------
# raise_for — which error class a collected problem list produces
# --------------------------------------------------------------------------


def test_raise_for_returns_quietly_when_there_are_no_problems() -> None:
    assert raise_for([], "req-1") is None


@pytest.mark.parametrize(
    ("codes", "expected_class", "expected_code"),
    [
        pytest.param(
            [ProblemCode.MISSING],
            EnvelopeValidationError,
            "missing_metadata",
            id="absent-metadata-is-a-tagging-fault",
        ),
        pytest.param(
            [ProblemCode.UNKNOWN_REQUEST_CLASS],
            EnvelopeValidationError,
            "missing_metadata",
            id="unknown-class-is-a-tagging-fault",
        ),
        pytest.param(
            [ProblemCode.INVALID],
            MalformedRequestError,
            "invalid_request",
            id="wrong-shape-is-a-structural-fault",
        ),
        pytest.param(
            [ProblemCode.UNKNOWN_FIELD],
            MalformedRequestError,
            "invalid_request",
            id="unaccepted-field-is-a-structural-fault",
        ),
        pytest.param(
            [ProblemCode.MISSING, ProblemCode.UNKNOWN_FIELD],
            MalformedRequestError,
            "invalid_request",
            id="one-structural-problem-decides-the-whole-response",
        ),
    ],
)
def test_raise_for_picks_the_class_from_the_problems(
    codes: list[ProblemCode], expected_class: type[KeelError], expected_code: str
) -> None:
    fields = [problem(f"f{index}", code) for index, code in enumerate(codes)]

    with pytest.raises(expected_class) as caught:
        raise_for(fields, "req-1")

    assert caught.value.to_body()["error"]["code"] == expected_code
    assert caught.value.status_code == 400
    assert len(caught.value.fields) == len(codes)


def test_summary_message_names_the_offending_fields() -> None:
    """An OpenAI SDK surfaces only this string, so it has to be actionable alone."""
    with pytest.raises(EnvelopeValidationError) as caught:
        raise_for([problem("tenant"), problem("feature")], None)

    assert str(caught.value) == "Request rejected: 2 problems (tenant, feature)."


def test_summary_message_is_singular_for_one_problem() -> None:
    with pytest.raises(EnvelopeValidationError) as caught:
        raise_for([problem("tenant")], None)

    assert str(caught.value) == "Request rejected: 1 problem (tenant)."


# --------------------------------------------------------------------------
# The vocabulary itself
# --------------------------------------------------------------------------


def test_problem_codes_are_a_closed_set() -> None:
    """A client branches on these. Adding one is an API change, so pin the list."""
    assert {code.value for code in ProblemCode} == {
        "missing",
        "unknown_request_class",
        "invalid",
        "unknown_field",
    }


def test_field_problem_rejects_unknown_keys() -> None:
    """Same posture as the config models: a silently ignored key is a lie."""
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        FieldProblem(
            field="tenant",
            header=None,
            code=ProblemCode.MISSING,
            message="m",
            severity="fatal",  # type: ignore[call-arg]
        )
