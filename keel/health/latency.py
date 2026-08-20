"""Latency samples per bucket, and the percentiles computed from them.

The other half of FR-3.1. :mod:`keel.health.window` counts *how* attempts ended;
this keeps *how long* they took, in the ``keel:latency:{provider}:{bucket_epoch}``
LIST §5.5 has specified since before any of this code existed.

**These numbers are approximate, and they are not the ones to quote.** They drive
a threshold comparison in Phase 3 — §5.6's ``p95 > class budget`` trip condition —
and nothing else. The authoritative latency figures for the README come from the
Prometheus histograms in P2-T4, which time the whole request independently and are
not subject to any sampling at all. Anyone looking for a number to put in a
document wants those, never these.

**"Reservoir" is §5.5's word, and it is not what this does.** ``LPUSH`` followed
by ``LTRIM 0 199`` keeps the 200 most *recent* samples, not 200 sampled uniformly
from the bucket. True reservoir sampling would keep an unbiased sample of the
whole bucket; a recency cap keeps a biased one. Above ``SAMPLE_CAP`` requests in a
single bucket, that bucket's percentiles therefore describe the **tail end of its
five seconds** rather than all of it. The trade is O(1) per write with no
read-modify-write, and the bias leans toward *newer* data, which is the direction
a health signal should lean anyway — but it is a different property from the one
the word implies.

The size of that bias was measured rather than assumed, against a lognormal
spread with a 3 s median and six buckets:

======================  ===========  ===========  =========
Requests per bucket     samples kept p95 reported error
======================  ===========  ===========  =========
100 (under the cap)     600          7612 ms      0.0%
200 (at the cap)        1200         7896 ms      0.0%
500 (2.5x over)         1200         7874 ms      -1.1%
2000 (10x over)         1200         7985 ms      -0.9%
======================  ===========  ===========  =========

So the cap costs roughly **one percent on p95 at ten times oversampling**, and it
understates rather than overstates — a breaker reading these is marginally slower
to trip on latency, never quicker. That is the safe direction for a false signal,
and it is comfortably inside the margin a threshold comparison needs.

Everything here is a pure function over data, plus one that stages commands onto a
pipeline it does not own. Nothing in this module opens a connection, executes a
pipeline, or catches a Redis exception: :class:`keel.health.window.HealthTracker`
does all three, once, inside the guard ADR 0008 put there.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from typing import Any, Final

__all__ = [
    "LATENCY_KEY_PREFIX",
    "SAMPLE_CAP",
    "latency_key",
    "parse_samples",
    "percentile",
    "stage_record",
]

logger = logging.getLogger(__name__)

LATENCY_KEY_PREFIX: Final = "keel:latency"

SAMPLE_CAP: Final = 200
"""Samples kept per bucket. §5.5 says "e.g. 200"; this fixes it.

A module constant rather than a `BreakerConfig` field, because it is a
memory-against-accuracy trade rather than routing policy, and FR-2.3 scopes
`config/keel.yaml` to routing behaviour. With the shipped geometry the ceiling is
12 buckets x 200 samples per provider — a few kilobytes, bounded by `LTRIM` on
every write rather than by a sweeper, which is the NFR-5 half of D3.
"""


def latency_key(provider: str, index: int) -> str:
    """The §5.5 key for one bucket's samples.

    Same shape and same bucket index as
    :meth:`keel.health.window.HealthTracker.bucket_key`, so the counts and the
    samples for one attempt always land in the same slice of time.
    """
    return f"{LATENCY_KEY_PREFIX}:{provider}:{index}"


def stage_record(pipe: Any, *, key: str, latency_ms: float, ttl_seconds: int) -> None:
    """Add this attempt's sample to a pipeline someone else opened.

    Three commands, no execute. The caller owns the pipeline, the transaction, the
    time box, and the exception handling — which is the whole point: the latency
    write rides along on the pipeline ``HealthTracker.record`` already opens for
    the counts, so an attempt costs **one** Redis round trip rather than two.
    Two independent writes would also mean two independent 250 ms time boxes, and
    ADR 0008 measured what that costs when Redis is unreachable.

    Riding the same transaction has a second benefit worth naming: the count and
    the sample land together or not at all. A bucket whose ``ok`` says three
    attempts while its LIST holds two samples is a worse input for Phase 3 than a
    bucket missing both.

    ``pipe`` is typed ``Any`` deliberately. redis-py's ``Pipeline`` is a large
    generic whose async variant is awkward to name precisely, and the test suite
    passes ``fakeredis``'s own pipeline as well; the three calls below are the
    entire contract, and getting one wrong fails loudly at the first write.
    """
    pipe.lpush(key, latency_ms)
    # LPUSH prepends, so index 0 is newest and this keeps [0, SAMPLE_CAP-1] — the
    # most recent SAMPLE_CAP samples. Trimming on every write rather than
    # periodically is what makes the memory bound unconditional.
    pipe.ltrim(key, 0, SAMPLE_CAP - 1)
    pipe.expire(key, ttl_seconds)


def parse_samples(buckets: Iterable[Sequence[str]], provider: str) -> list[float]:
    """Flatten the merged buckets' raw LIST entries into usable samples.

    Unsorted — :func:`percentile` sorts once for all three percentiles rather than
    three times.

    A malformed entry is skipped and logged rather than raised on, matching
    ``_as_count`` in :mod:`keel.health.window` and for the same reason: the caller
    promises never to raise (ADR 0008), so it cannot be handed a ``ValueError``
    over data it did not write. Only ``stage_record`` writes these keys, so this
    fires for a hand-edited key or another tool sharing the keyspace.

    Negative samples are dropped too. ``ProviderResult.latency_ms`` is validated
    ``ge=0.0`` and both call sites clamp for the NTP-step reason ADR 0001 gives,
    so a negative here means the same thing a non-numeric one does.
    """
    samples: list[float] = []
    for bucket in buckets:
        for raw in bucket:
            try:
                value = float(raw)
            except ValueError:
                logger.warning(
                    "ignoring non-numeric latency sample %r for provider %r", raw, provider
                )
                continue
            if value < 0.0 or math.isnan(value):
                logger.warning(
                    "ignoring impossible latency sample %r for provider %r", raw, provider
                )
                continue
            samples.append(value)
    return samples


def percentile(sorted_samples: Sequence[float], p: float) -> float | None:
    """The ``p``-th percentile by **nearest rank**. ``None`` for no samples.

    Nearest rank rather than interpolation, so every value returned is a latency
    some request actually experienced. The interpolating alternative reports the
    p50 of ``[10, 20, 30, 40]`` as ``25.0`` — a number no attempt ever took — and
    ``statistics.quantiles`` additionally raises below two samples, which is a
    case this hits constantly. Since these feed a threshold comparison rather than
    a published SLA, interpolation buys precision that nothing consumes.

    The rank is ``ceil(p/100 * n)``, clamped into ``[1, n]``, then read
    zero-indexed. With one sample every percentile is that sample, which is the
    honest answer: one observation says the same thing about the median as it does
    about the tail.

    :param sorted_samples: ascending. Sorting is the caller's job so one sort
        serves p50, p95, and p99 rather than three.
    """
    count = len(sorted_samples)
    if count == 0:
        # No samples is *unknown*, not zero. A provider with no traffic is not a
        # provider with perfect latency — the same rule ADR 0008 fixes for the
        # window's counts, and P2-T3's own "done when" for these percentiles.
        return None

    rank = math.ceil((p / 100.0) * count)
    index = min(max(rank, 1), count) - 1
    return sorted_samples[index]
