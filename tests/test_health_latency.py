"""Tests for the latency reservoir's pure functions (P2-T3, FR-3.1, §5.5).

No Redis at all, not even `fakeredis`. Everything in `keel/health/latency.py`
except `stage_record` is a function over data, and the Redis-facing half is
exercised in `tests/test_health_window.py` where the pipeline it stages onto
actually exists.

**The percentile method is pinned, not merely tested.** Nearest rank and linear
interpolation agree on plenty of inputs and disagree on the ones below, so the
tables here are chosen to disagree: `p50` of `[10, 20, 30, 40]` is `20` under
nearest rank and `25.0` under interpolation. Switching methods later would be a
change in what every Phase 3 breaker decision means, and it must not be possible
to make it quietly.

The other thing being pinned is that "no samples" is `None` and never `0.0`. A
provider with no traffic is *unknown, not perfect* — the same rule ADR 0008 fixes
for the window's counts, and the one that stops a Redis outage reading as a wall
of instant, flawless providers.
"""

from __future__ import annotations

import logging

import pytest

from keel.health.latency import (
    LATENCY_KEY_PREFIX,
    SAMPLE_CAP,
    latency_key,
    parse_samples,
    percentile,
)

# --------------------------------------------------------------------------
# The key, and the cap
# --------------------------------------------------------------------------


def test_the_latency_key_matches_the_schema_transcribed_by_hand() -> None:
    """§5.5's `keel:latency:{provider}:{bucket_epoch}`, written out rather than derived."""
    assert latency_key("mock_chaos", 11) == "keel:latency:mock_chaos:11"
    assert LATENCY_KEY_PREFIX == "keel:latency"


def test_the_sample_cap_is_two_hundred() -> None:
    """§5.5 says "e.g. 200"; this is where the "e.g." was resolved.

    Asserted as a literal because it is a memory bound (NFR-5) and an accuracy
    floor at once: raising it costs bytes per provider per bucket, lowering it
    costs percentile fidelity. Neither should happen by accident.
    """
    assert SAMPLE_CAP == 200


# --------------------------------------------------------------------------
# Percentiles: nearest rank
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("samples", "p", "expected"),
    [
        pytest.param([10.0, 20.0, 30.0, 40.0], 50, 20.0, id="p50-is-20-not-25"),
        pytest.param([10.0, 20.0, 30.0, 40.0], 95, 40.0, id="p95-of-four"),
        pytest.param([10.0, 20.0, 30.0, 40.0], 99, 40.0, id="p99-of-four"),
        pytest.param([float(i) for i in range(1, 101)], 50, 50.0, id="p50-of-1-to-100"),
        pytest.param([float(i) for i in range(1, 101)], 95, 95.0, id="p95-of-1-to-100"),
        pytest.param([float(i) for i in range(1, 101)], 99, 99.0, id="p99-of-1-to-100"),
        pytest.param([7.0], 50, 7.0, id="single-sample-p50"),
        pytest.param([7.0], 99, 7.0, id="single-sample-p99"),
        pytest.param([1.0, 2.0], 50, 1.0, id="two-samples-p50"),
        pytest.param([1.0, 2.0], 95, 2.0, id="two-samples-p95"),
    ],
)
def test_percentiles_are_nearest_rank(samples: list[float], p: int, expected: float) -> None:
    """Hand-computed `ceil(p/100 * n) - 1`, transcribed rather than derived.

    The `1..100` rows are the readable ones — p95 of the first hundred integers is
    95 exactly — and the four-sample rows are the ones that would break under
    interpolation.
    """
    assert percentile(samples, p) == expected


def test_the_p50_of_four_samples_is_a_real_observation() -> None:
    """The single assertion that forbids interpolation.

    `statistics.quantiles` would answer `25.0` here — a latency no request in this
    set experienced. These numbers drive §5.6's threshold comparison, where "some
    request actually took this long" is a more defensible input than a synthetic
    midpoint, and they are never published as an SLA where interpolation would be
    the convention.
    """
    samples = [10.0, 20.0, 30.0, 40.0]

    assert percentile(samples, 50) in samples
    assert percentile(samples, 50) != 25.0


def test_one_sample_answers_every_percentile_identically() -> None:
    """Honest rather than clever: one observation says the same about the median as the tail."""
    assert [percentile([42.0], p) for p in (50, 95, 99)] == [42.0, 42.0, 42.0]


def test_no_samples_is_unknown_rather_than_zero() -> None:
    """`None`, never `0.0`. The rule the whole module exists to keep.

    Zero would be a latency claim, and the most flattering one possible, made
    about a provider nothing is known about.
    """
    assert percentile([], 50) is None
    assert percentile([], 95) is None
    assert percentile([], 99) is None


@pytest.mark.parametrize("p", [0, 1, 100], ids=["p0", "p1", "p100"])
def test_the_rank_is_clamped_into_the_sample_range(p: int) -> None:
    """`p=0` floors the rank at the first sample; `p=100` is the last. No IndexError either way."""
    samples = [1.0, 2.0, 3.0]

    assert percentile(samples, p) in samples


def test_percentiles_are_monotonic_across_a_spread() -> None:
    """p50 <= p95 <= p99 for any distribution — the sanity property Phase 3 assumes."""
    samples = sorted(float(i) * 1.5 for i in range(1, 251))

    p50, p95, p99 = (percentile(samples, p) for p in (50, 95, 99))

    assert p50 is not None and p95 is not None and p99 is not None
    assert p50 < p95 < p99


# --------------------------------------------------------------------------
# Parsing what Redis hands back
# --------------------------------------------------------------------------


def test_samples_are_flattened_across_buckets() -> None:
    """The merged window is every bucket's list concatenated; order does not matter."""
    parsed = parse_samples([["3.0", "1.0"], [], ["2.0"]], "mock_chaos")

    assert sorted(parsed) == [1.0, 2.0, 3.0]


def test_an_empty_window_parses_to_no_samples() -> None:
    assert parse_samples([[], [], []], "mock_chaos") == []


@pytest.mark.parametrize(
    "bad",
    ["not-a-number", "", "-1.0", "nan"],
    ids=["text", "empty", "negative", "nan"],
)
def test_an_unusable_sample_is_skipped_rather_than_raised_on(
    bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    """`snapshot` promises never to raise, so this cannot raise on data it did not write.

    Only `stage_record` writes these keys, so a bad entry means a hand-edited key
    or another tool sharing the keyspace. Negative and NaN are impossible from
    `ProviderResult.latency_ms` (validated `ge=0.0`, and both call sites clamp for
    the NTP reason ADR 0001 gives), which is exactly why they are treated as
    corruption rather than data.
    """
    with caplog.at_level(logging.WARNING, logger="keel.health.latency"):
        parsed = parse_samples([["5.0", bad, "7.0"]], "mock_chaos")

    assert parsed == [5.0, 7.0], "the good samples on either side survive"
    assert "mock_chaos" in caplog.text, "the log must name the provider"


def test_a_zero_latency_sample_is_kept() -> None:
    """Zero is a legitimate measurement, unlike a negative one.

    `ManualClock` produces it in tests and a sub-millisecond cached response could
    produce it in production; dropping it would bias every percentile upward.
    """
    assert parse_samples([["0.0"]], "mock_chaos") == [0.0]


def test_an_integer_formatted_sample_parses() -> None:
    """Redis stores whatever was pushed; `float()` accepts both forms."""
    assert parse_samples([["12", "12.5"]], "mock_chaos") == [12.0, 12.5]
