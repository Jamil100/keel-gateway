"""Tests for `ProviderHealth` (P2-T3, FR-3.1, §5.6).

Pure composition, no Redis: `HealthTracker.snapshot` is what reads Redis and it is
tested in `tests/test_health_window.py`. What is pinned here is the arithmetic and,
much more importantly, **the three ways this object says "I do not know"**, which
are not the same and must not be allowed to collapse into each other:

===========================  ===============================================
Redis unreadable             `snapshot()` returns `None` — tested next door
Reachable, no traffic        `total == 0`, `success_rate` and percentiles `None`
Counts but no samples        counts real, percentiles `None`
===========================  ===============================================

The first two are ADR 0008's rule restated one layer up. The third is new here and
is the subtle one: "this provider is failing and we do not know how slow it is"
must never read as "this provider is failing and is infinitely fast".

Every `success_rate` case exists to defend one number — the zero-volume answer.
Returning `1.0` there is the single most dangerous plausible bug in this file: it
would let a breaker look at a provider nothing has reached and conclude it is
perfect, which is precisely the reading `min_requests_in_window` exists to prevent
and which would arrive *underneath* that floor.
"""

from __future__ import annotations

import pytest

from keel.health.snapshot import ProviderHealth
from keel.health.window import WindowCounts
from keel.providers.errors import ErrorClass

PROVIDER = "mock_chaos"


def counts(ok: int = 0, **errors: int) -> WindowCounts:
    """A `WindowCounts` with all seven classes present, as the window always returns."""
    table = dict.fromkeys(ErrorClass, 0)
    for name, value in errors.items():
        table[ErrorClass(name)] = value
    return WindowCounts(provider=PROVIDER, ok=ok, errors=table)


# --------------------------------------------------------------------------
# Composition: counts in, counts out
# --------------------------------------------------------------------------


def test_the_counts_are_carried_across_not_recomputed() -> None:
    """One constructor, so the snapshot cannot disagree with the window it came from."""
    health = ProviderHealth.from_window(counts(ok=8, rate_limit=2), [])

    assert health.provider == PROVIDER
    assert health.ok == 8
    assert health.errors[ErrorClass.RATE_LIMIT] == 2
    assert health.total == 10


def test_every_error_class_survives_into_the_snapshot() -> None:
    """All seven, not just the four D7 counts toward the breaker.

    Which rows a breaker may look at is Phase 3's decision; pre-summing them here
    would make it invisibly, and the M2 error-rate panel needs the whole split.
    """
    health = ProviderHealth.from_window(counts(ok=1, content_filter=1, auth_failure=1), [])

    assert set(health.errors) == set(ErrorClass)
    assert health.errors[ErrorClass.CONTENT_FILTER] == 1
    assert health.total == 3, "classes outside the breaker still count as attempts"


def test_the_snapshot_is_frozen() -> None:
    """A view of the past. Writing to it would be a belief that something changed."""
    health = ProviderHealth.from_window(counts(ok=1), [10.0])

    with pytest.raises(ValueError):
        health.ok = 99  # type: ignore[misc]


# --------------------------------------------------------------------------
# Rates: and the zero-volume answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ok", "failures", "expected"),
    [
        pytest.param(10, 0, 1.0, id="all-succeeded"),
        pytest.param(0, 10, 0.0, id="all-failed"),
        pytest.param(8, 2, 0.8, id="eighty-percent"),
        pytest.param(1, 3, 0.25, id="one-in-four"),
    ],
)
def test_success_rate_is_successes_over_attempts(
    ok: int, failures: int, expected: float
) -> None:
    health = ProviderHealth.from_window(counts(ok=ok, server_error=failures), [])

    assert health.success_rate == expected
    assert health.error_rate == pytest.approx(1.0 - expected)


def test_an_untouched_provider_has_no_success_rate_rather_than_a_perfect_one() -> None:
    """The single most dangerous plausible bug in this module, pinned.

    `1.0` here would tell a breaker that a provider nothing has reached is
    flawless. Idle and healthy are different states, and only one of them is
    evidence.
    """
    health = ProviderHealth.from_window(counts(), [])

    assert health.total == 0
    assert health.success_rate is None
    assert health.error_rate is None


def test_a_single_failure_is_a_zero_success_rate_not_unknown() -> None:
    """One attempt is thin evidence, but it is evidence.

    Phase 3 declines to act below `min_requests_in_window`; conflating "too little
    data to act on" with "no data at all" here would take that decision away from
    the component that owns it.
    """
    health = ProviderHealth.from_window(counts(server_error=1), [])

    assert health.success_rate == 0.0


# --------------------------------------------------------------------------
# Percentiles, and the third flavour of unknown
# --------------------------------------------------------------------------


def test_percentiles_come_from_the_samples_sorted_once() -> None:
    """Deliberately handed in unsorted — `from_window` owns the ordering."""
    health = ProviderHealth.from_window(counts(ok=4), [40.0, 10.0, 30.0, 20.0])

    assert (health.p50_ms, health.p95_ms, health.p99_ms) == (20.0, 40.0, 40.0)
    assert health.sample_count == 4


def test_counts_without_samples_keep_the_counts_and_report_no_latency() -> None:
    """The third unknown. Reachable from an older build's data or a partial write.

    The counts are real and must survive; the percentiles are not known and must
    not be invented. `0.0` would make a failing provider look instant.
    """
    health = ProviderHealth.from_window(counts(ok=3, timeout=2), [])

    assert health.total == 5
    assert health.success_rate == 0.6
    assert health.sample_count == 0
    assert (health.p50_ms, health.p95_ms, health.p99_ms) == (None, None, None)


def test_samples_without_counts_are_possible_and_reported() -> None:
    """The mirror case, and equally not this module's business to reconcile.

    Both halves are reported as found rather than one being validated against the
    other: a snapshot that refused to describe inconsistent data would be at its
    least useful exactly when something is wrong.
    """
    health = ProviderHealth.from_window(counts(), [10.0, 20.0])

    assert health.total == 0
    assert health.success_rate is None
    assert health.sample_count == 2
    assert health.p50_ms == 10.0


def test_sample_count_may_be_lower_than_total_under_the_cap() -> None:
    """`LTRIM` caps each bucket at 200, so a busy provider has fewer samples than attempts.

    Surfaced rather than hidden, because a breaker should know how much evidence a
    percentile rests on before acting on it.
    """
    health = ProviderHealth.from_window(counts(ok=1000), [float(i) for i in range(200)])

    assert health.total == 1000
    assert health.sample_count == 200


def test_a_snapshot_has_no_request_class_dimension() -> None:
    """The documented gap, pinned so Phase 3 meets it as a decision rather than a surprise.

    §5.5's key is `keel:latency:{provider}:{bucket}` — no class — while
    `latency_budget_p95_ms` is configured per request class. So one provider has
    one p95 and, across `classification` (800 ms) and `batch_enrichment`
    (60000 ms), two verdicts on the same evidence at the same instant.

    This test exists to fail if someone adds a class dimension without deciding
    what it means: §5.6's trip condition, the key schema, and this shape all have
    to move together.
    """
    health = ProviderHealth.from_window(counts(ok=1), [1200.0])

    assert not hasattr(health, "request_class")
    assert set(ProviderHealth.model_fields) == {
        "provider",
        "ok",
        "errors",
        "sample_count",
        "p50_ms",
        "p95_ms",
        "p99_ms",
    }
