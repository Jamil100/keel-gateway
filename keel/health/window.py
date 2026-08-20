"""The per-provider health window: what happened lately, kept in Redis.

FR-3.1 and FR-3.2 — a rolling record of successes and failures-by-class per
provider, persisted so a gateway restart does not reset the view of provider
health. The counts live here; the latency samples that ride alongside them live in
:mod:`keel.health.latency`, and :meth:`HealthTracker.snapshot` composes the two
into the :class:`keel.health.snapshot.ProviderHealth` Phase 3's breaker reads.

**One Redis round trip per attempt, counts and samples together.** The latency
write is *staged onto the pipeline this module already opens* rather than issued
by a second recorder with its own connection and its own time box. Two writers
would double the cost ADR 0008 measured when Redis is unreachable — the whole
reason that ADR exists — and would let a bucket's count and its samples disagree
about the same attempt.

**Nothing reads this yet, and that is the point.** FR-3.4 fixes the order of
construction: health tracking must be complete and observable before anything
reacts to it, because a breaker whose inputs cannot be seen is guesswork. So this
module records and merges, and §5.6's breaker — the first thing that will *act* on
what it says — is Phase 3.

**Bucketed, not continuous (D3).** The window is the union of the last
``window_seconds / bucket_seconds`` whole buckets: 12 with the shipped config. The
rejected alternative is a sorted set of individual events, which windows exactly
at the cost of O(n) memory per provider and a ``ZREMRANGEBYSCORE`` on every
request. The price of buckets is that an event leaves the window up to
``bucket_seconds`` *earlier* than a true continuous window would evict it — at
most 5 s, well inside the S2 target of "p95 reroute time <= 2x window length".
That staleness is a documented property and ``tests/test_health_window.py`` pins
both of its edges, so a later reader who mistakes it for a bug has to argue with
D3 first.

**A failure to record is dropped, never raised (ADR 0008).** Both public methods
carry a *never raises* contract: recording is not required to produce the response
(design principle 5), so it must not be able to break one. The guard lives here
rather than at the call site so that the executor's hook stays the one line it was
promised, and so Phase 3's breaker cannot forget to repeat a guard it never saw.
The asymmetry that matters: a failed *write* is silent data loss, but a failed
*read* returns ``None`` meaning **unknown**, never a zero-filled window. A Redis
outage must not read as a wall of perfectly healthy providers.

Key schema is §5.5, transcribed rather than invented::

    keel:health:{provider}:{bucket_epoch}   HASH  ok + one err_* per class  TTL 2xwindow
    keel:latency:{provider}:{bucket_epoch}  LIST  capped latency samples    TTL 2xwindow

``{provider}`` is the config entry name (``cohere_primary``), not the adapter name
— several entries may share one adapter, and metrics, health, and
``X-Keel-Provider`` all key on the entry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from keel.clock import Clock
from keel.config import BreakerConfig
from keel.health.latency import latency_key, parse_samples, stage_record
from keel.health.snapshot import ProviderHealth
from keel.providers.base import ProviderResult
from keel.providers.errors import ErrorClass

__all__ = [
    "ERROR_FIELD_PREFIX",
    "FIELD_OK",
    "HEALTH_KEY_PREFIX",
    "REDIS_TIMEOUT_SECONDS",
    "HealthTracker",
    "WindowCounts",
    "error_field",
]

logger = logging.getLogger(__name__)

HEALTH_KEY_PREFIX: Final = "keel:health"
FIELD_OK: Final = "ok"
ERROR_FIELD_PREFIX: Final = "err_"

REDIS_TIMEOUT_SECONDS: Final = 0.25
"""A time box on the hot path, not a retry budget.

S5 allows 15 ms of p95 gateway overhead. A pipelined round trip to a local Redis
is well under a millisecond, so this is nowhere near a normal cost — it exists so
that a Redis which accepts a connection and then stops answering costs one request
a quarter second instead of hanging it until the client gives up.
"""

# redis-py lets a socket failure through as a bare OSError in cases it does not
# wrap, so both are caught everywhere. CancelledError is deliberately absent: it
# derives from BaseException and must pass through untouched, or the executor's
# wait_for could no longer cancel an attempt that runs through this code.
_REDIS_FAILURES: Final = (RedisError, OSError, TimeoutError)


def error_field(error_class: ErrorClass) -> str:
    """The hash field one error class counts into, e.g. ``err_rate_limit``.

    ``ErrorClass`` is a ``StrEnum`` whose values are already wire-visible and
    therefore stable API — ``tests/test_provider_errors.py`` pins that a member
    can be handed straight to a Redis field. Renaming one orphans the counters
    already in Redis, which is why that module says so at the top.
    """
    return f"{ERROR_FIELD_PREFIX}{error_class.value}"


_FIELD_TO_ERROR_CLASS: Final[Mapping[str, ErrorClass]] = {
    error_field(member): member for member in ErrorClass
}
"""Built from ``ErrorClass`` itself, so an eighth class needs no edit here."""


class WindowCounts(BaseModel):
    """One provider's merged window: how many attempts, and how they ended.

    Counts only. Success *rate* and percentiles belong to P2-T3's
    ``ProviderHealth``, and the decision of what a rate means belongs to Phase 3's
    breaker — including which classes it may look at, since D7 keeps three of them
    out of that judgement entirely.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str

    ok: Annotated[int, Field(ge=0)]
    """Successful attempts in the window."""

    errors: Mapping[ErrorClass, int]
    """Every one of the seven classes, zero-filled — never a partial map.

    All seven are recorded and returned, not just the four D7 counts toward the
    breaker. The M2 exit criterion needs the error-rate panel to split *across
    taxonomy classes*, and ``MockAdapter``'s default mix includes
    ``CONTENT_FILTER`` precisely because it does not trip a breaker. Filtering is
    the breaker's job; a recorder that filtered would leave that panel unable to
    show the distinction it exists to show.

    Typed ``Mapping`` rather than ``dict`` so a caller cannot write into a
    snapshot of the past and believe it changed something.
    """

    @property
    def total(self) -> int:
        """Every attempt in the window, however it ended.

        What ``breaker.min_requests_in_window`` will be compared against in Phase
        3 — the volume floor that stops two failures out of three from reading as
        a 67% error rate.
        """
        return self.ok + sum(self.errors.values())


class HealthTracker:
    """Records attempt outcomes and latencies into fixed-width buckets.

    Named for §5.5's own title. It was ``HealthWindow`` while it recorded only
    counts; P2-T3 added the latency samples, and "window" now describes the
    *range it merges over* rather than everything it holds.
    """

    def __init__(self, *, redis: Redis, breaker: BreakerConfig, clock: Clock) -> None:
        self._redis = redis
        self._clock = clock
        self._bucket_seconds = breaker.bucket_seconds
        # config.py already guarantees the window is a whole multiple of the
        # bucket width (_check_window_divides_into_buckets), so this division is
        # exact and no partial edge bucket exists to skew a rate. Startup
        # guarantees are not re-checked at request time (NFR-4).
        self._bucket_count = breaker.window_seconds // breaker.bucket_seconds
        self._ttl_seconds = breaker.window_seconds * 2

    async def record(self, result: ProviderResult) -> None:
        """Count one attempt into the current bucket. **Never raises** (ADR 0008).

        Called from ``Executor.execute`` for both outcomes, which is the single
        call site decision D-C exists to create: adapters were kept ignorant of
        Redis so that exactly one place in the codebase decides what an attempt
        meant.

        One pipeline, and transactional on purpose. With ``transaction=False`` a
        connection lost between the ``HINCRBY`` and the ``EXPIRE`` leaves a key
        with no TTL, and a provider that serves one request and then goes quiet
        would leak it forever — the bounded-memory half of D3 quietly undone.

        **No test covers that flag**, and the reason is worth knowing before
        changing it: ``fakeredis`` cannot drop a connection mid-pipeline, so an
        atomicity test would pass either way. Flipping it to ``False`` breaks
        nothing in the suite, which makes this docstring the only guard there is.
        """
        bucket = self._current_bucket()
        key = self._bucket_key(result.provider, bucket)
        field = _field_for(result)

        try:
            async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                async with self._redis.pipeline(transaction=True) as pipe:
                    pipe.hincrby(key, field, 1)
                    pipe.expire(key, self._ttl_seconds)
                    # Staged onto the same transaction, not a second write. One
                    # round trip, one time box, and a count that cannot survive
                    # without its sample or the other way round.
                    stage_record(
                        pipe,
                        key=latency_key(result.provider, bucket),
                        latency_ms=result.latency_ms,
                        ttl_seconds=self._ttl_seconds,
                    )
                    await pipe.execute()
        except _REDIS_FAILURES as exc:
            # WARNING rather than ERROR: the request itself already succeeded or
            # failed on its own merits and the client is unaffected. What is lost
            # is one observation, and P2-T4's exporter is where that becomes
            # visible as something other than a log line.
            logger.warning(
                "health write dropped for provider %r (field %r): %s: %s",
                result.provider,
                field,
                type(exc).__name__,
                exc,
            )

    async def read(self, provider: str) -> WindowCounts | None:
        """Merge the last ``window_seconds`` of buckets. **Never raises**.

        Returns ``None`` when Redis could not be read — meaning *unknown*, which
        is a different thing from a window that is genuinely empty and comes back
        as zeros. P2-T3 states the same rule for percentiles: a provider with no
        traffic is unknown, not perfect. Collapsing the two would let a Redis
        outage present every provider as flawless at the moment health data
        matters most.
        """
        indices = self._window_indices()

        try:
            async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                # Not transactional: these are independent reads, and MULTI/EXEC
                # would buy an atomicity a bucketed window does not need — a count
                # landing mid-read belongs to the current bucket either way, and
                # D3 already accepts 5 s of edge imprecision.
                async with self._redis.pipeline(transaction=False) as pipe:
                    for index in indices:
                        pipe.hgetall(self._bucket_key(provider, index))
                    buckets = await pipe.execute()
        except _REDIS_FAILURES as exc:
            logger.warning(
                "health read failed for provider %r; reporting unknown: %s: %s",
                provider,
                type(exc).__name__,
                exc,
            )
            return None

        return _merge(provider, buckets)

    async def snapshot(self, provider: str) -> ProviderHealth | None:
        """Counts *and* percentiles over the same window. **Never raises**.

        The object Phase 3's breaker reads, and the reason it exists rather than
        the caller calling :meth:`read` and a latency read separately: §5.6
        evaluates two trip conditions — error rate and p95 — and both must
        describe the same instant. Two round trips could straddle a bucket
        boundary and answer from two.

        So both halves ride one pipeline: ``HGETALL`` for each bucket, then
        ``LRANGE`` for each, issued together and split apart on the way back.
        ``None`` means Redis could not be read, exactly as :meth:`read` does; a
        reachable but empty window is a ``ProviderHealth`` whose ``total`` is zero
        and whose percentiles are ``None``, which is *unknown*, not perfect.
        """
        indices = self._window_indices()

        try:
            async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                async with self._redis.pipeline(transaction=False) as pipe:
                    for index in indices:
                        pipe.hgetall(self._bucket_key(provider, index))
                    for index in indices:
                        pipe.lrange(latency_key(provider, index), 0, -1)
                    replies = await pipe.execute()
        except _REDIS_FAILURES as exc:
            logger.warning(
                "health snapshot failed for provider %r; reporting unknown: %s: %s",
                provider,
                type(exc).__name__,
                exc,
            )
            return None

        # The counts were queued first and the sample lists second, so the replies
        # split at exactly that boundary. Slicing by the index count rather than by
        # a hard-coded 12 keeps this correct under any configured geometry.
        split = len(indices)
        counts = _merge(provider, replies[:split])
        samples = parse_samples(replies[split:], provider)
        return ProviderHealth.from_window(counts, samples)

    def bucket_key(self, provider: str, index: int) -> str:
        """The §5.5 key for one bucket. Public so P2-T3 and the tests agree with it."""
        return self._bucket_key(provider, index)

    def current_bucket(self) -> int:
        """The bucket index :meth:`record` would write to right now."""
        return self._current_bucket()

    def _bucket_key(self, provider: str, index: int) -> str:
        return f"{HEALTH_KEY_PREFIX}:{provider}:{index}"

    def _window_indices(self) -> list[int]:
        """The bucket indices the window covers, oldest first, current included.

        Shared by :meth:`read` and :meth:`snapshot` so the two cannot come to
        disagree about what "the window" means — the failure that would let a
        breaker read a rate over one range and a p95 over another.
        """
        current = self._current_bucket()
        return list(range(current - self._bucket_count + 1, current + 1))

    def _current_bucket(self) -> int:
        """The floor-divided epoch *index* of the bucket covering now.

        An index, not a timestamp, and read from the injected wall clock rather
        than a monotonic one. ADR 0001 is explicit about why: the gateway and the
        deferred worker compute this independently, and a monotonic origin is
        per-process, so the two would write disjoint key spaces for the same
        instant. The window would then split in half, each half failing
        ``min_requests_in_window``, and the breaker would never trip at all.
        """
        return int(self._clock.now() // self._bucket_seconds)


def _field_for(result: ProviderResult) -> str:
    """Which hash field this attempt counts into.

    Branches on ``result.error is None`` rather than ``result.ok`` so the type
    checker narrows ``error`` for the access below; ``ProviderResult`` enforces
    the success-xor-failure invariant at runtime, but that is invisible to mypy.
    """
    if result.error is None:
        return FIELD_OK
    return error_field(result.error.error_class)


def _merge(provider: str, buckets: Iterable[Mapping[str, str]]) -> WindowCounts:
    """Sum a set of bucket hashes into one window view."""
    ok = 0
    errors = dict.fromkeys(ErrorClass, 0)

    for bucket in buckets:
        for field, raw in bucket.items():
            count = _as_count(provider, field, raw)
            if field == FIELD_OK:
                ok += count
                continue
            error_class = _FIELD_TO_ERROR_CLASS.get(field)
            if error_class is None:
                # A field this build does not know about. Skipped rather than
                # rejected, so a gateway rolled back beside one that writes a new
                # class keeps serving instead of crashing on the other's data.
                logger.warning(
                    "ignoring unknown health field %r for provider %r", field, provider
                )
                continue
            errors[error_class] += count

    return WindowCounts(provider=provider, ok=ok, errors=errors)


def _as_count(provider: str, field: str, raw: str) -> int:
    """Parse a hash value, tolerating one ``HINCRBY`` could not have written.

    Redis guarantees an integer on a field only ever touched by ``HINCRBY``, so
    this fires only if something else wrote the key. Counting zero and saying so
    beats a ``ValueError`` out of a method whose whole contract is never raising.
    """
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "non-integer health count %r in field %r for provider %r; counting zero",
            raw,
            field,
            provider,
        )
        return 0
