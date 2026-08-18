"""Tests for the normalized error taxonomy (FR-2.4, TECHNICAL-DESIGN.md §5.4).

The truth table below is transcribed from §5.4 **by hand**, deliberately not
read from ``keel.providers.errors``. That independence is the whole value: if
someone edits the shipped table, this file disagrees and CI fails, rather than
the breaker quietly starting to trip on different conditions than the design
says it does. Changing when a breaker opens should require changing a test that
spells out the new behaviour.

No network, no Redis, no clock (NFR-2).
"""

from __future__ import annotations

import pytest

from keel.providers.errors import ErrorClass, NormalizedError

# §5.4, transcribed: (error class value, counts toward breaker, retry elsewhere).
SECTION_5_4_TRUTH_TABLE: list[tuple[str, bool, bool]] = [
    ("rate_limit", True, True),
    ("timeout", True, True),
    ("server_error", True, True),
    ("quota_exhausted", True, True),
    ("auth_failure", False, False),
    ("content_filter", False, False),
    ("bad_request", False, False),
]


# --------------------------------------------------------------------------
# The truth table. This is what the task exists to pin.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "counts", "retry"),
    [pytest.param(*row, id=row[0].replace("_", "-")) for row in SECTION_5_4_TRUTH_TABLE],
)
def test_section_5_4_truth_table(value: str, counts: bool, retry: bool) -> None:
    error_class = ErrorClass(value)

    assert error_class.counts_toward_breaker is counts
    assert error_class.retry_elsewhere is retry


def test_the_taxonomy_has_exactly_the_seven_classes_of_section_5_4() -> None:
    """An eighth class is a design change, so it must break a test first."""
    assert [member.value for member in ErrorClass] == [row[0] for row in SECTION_5_4_TRUTH_TABLE]


def test_every_class_declares_its_breaker_semantics() -> None:
    """No member may fall through to a default. Guarded at import; asserted here."""
    for member in ErrorClass:
        assert isinstance(member.counts_toward_breaker, bool)
        assert isinstance(member.retry_elsewhere, bool)


# --------------------------------------------------------------------------
# D7 — the two rows that matter more than they look
# --------------------------------------------------------------------------


def test_auth_failure_does_not_trip_a_breaker() -> None:
    """One bad API key must not open every breaker and declare a total outage (D7)."""
    assert ErrorClass.AUTH_FAILURE.counts_toward_breaker is False


def test_content_filter_does_not_trip_a_breaker_or_get_retried() -> None:
    """The provider behaved correctly. Retrying walks the prompt across every provider."""
    assert ErrorClass.CONTENT_FILTER.counts_toward_breaker is False
    assert ErrorClass.CONTENT_FILTER.retry_elsewhere is False


def test_exactly_three_classes_are_excluded_from_the_breaker() -> None:
    """Excluding a fourth would blind the breaker; excluding none would blow it up."""
    excluded = {member.value for member in ErrorClass if not member.counts_toward_breaker}

    assert excluded == {"auth_failure", "content_filter", "bad_request"}


def test_nothing_is_retried_that_is_not_also_counted() -> None:
    """A class worth retrying elsewhere is by definition evidence about this provider.

    The reverse is allowed — a class can count toward the breaker without being
    retryable — but this direction would be incoherent.
    """
    for member in ErrorClass:
        if member.retry_elsewhere:
            assert member.counts_toward_breaker, f"{member.value} is retried but not counted"


# --------------------------------------------------------------------------
# Wire stability. These values are a dashboard query and a Redis field name.
# --------------------------------------------------------------------------


def test_class_values_are_stable_lowercase_identifiers() -> None:
    """They become the `error_class` metric label (§6) and health hash fields (§5.5)."""
    for member in ErrorClass:
        assert member.value == member.value.lower()
        assert member.value.replace("_", "").isalpha()


def test_error_class_is_a_string_at_runtime() -> None:
    """StrEnum, so it can be handed straight to a Prometheus label or a Redis field."""
    assert ErrorClass.RATE_LIMIT == "rate_limit"
    assert f"err_{ErrorClass.RATE_LIMIT}" == "err_rate_limit"


# --------------------------------------------------------------------------
# NormalizedError
# --------------------------------------------------------------------------


def test_normalized_error_delegates_to_one_truth_table() -> None:
    """Two sources of breaker semantics is how they start to disagree."""
    error = NormalizedError(error_class=ErrorClass.TIMEOUT, message="upstream timed out")

    assert error.counts_toward_breaker is True
    assert error.retry_elsewhere is True

    filtered = NormalizedError(error_class=ErrorClass.CONTENT_FILTER, message="blocked")

    assert filtered.counts_toward_breaker is False


def test_normalized_error_keeps_the_raw_provider_type_for_mapping_gaps() -> None:
    """P2-T1 defaults unmapped exceptions to SERVER_ERROR and warns naming this."""
    error = NormalizedError(
        error_class=ErrorClass.SERVER_ERROR,
        message="unmapped provider exception",
        provider_error_type="SomeVendorSpecificError",
        status_code=503,
    )

    assert error.provider_error_type == "SomeVendorSpecificError"
    assert error.status_code == 503


def test_normalized_error_defaults_the_optional_diagnostics_to_none() -> None:
    error = NormalizedError(error_class=ErrorClass.BAD_REQUEST, message="bad input")

    assert error.provider_error_type is None
    assert error.status_code is None


def test_normalized_error_is_frozen() -> None:
    error = NormalizedError(error_class=ErrorClass.TIMEOUT, message="m")

    with pytest.raises(ValueError, match="frozen"):
        error.error_class = ErrorClass.RATE_LIMIT


def test_normalized_error_rejects_an_unknown_class() -> None:
    """The taxonomy is closed. A free-form string would bypass the truth table."""
    with pytest.raises(ValueError, match="error_class"):
        NormalizedError(error_class="teapot", message="m")  # type: ignore[arg-type]
