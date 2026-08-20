"""Tests for the health window (P2-T2, D3, D-C, TECHNICAL-DESIGN.md §5.5).

Three properties are being pinned, and only the first is obvious.

**The key schema.** Keys and field names are wire-visible: Phase 3's breaker reads
them, P2-T4's exporter labels metrics from the same vocabulary, and P2-T6's Grafana
queries hard-code it. So the literal `keel:health:mock_chaos:11` is transcribed by
hand here rather than rebuilt from the module under test, exactly as
`tests/test_provider_errors.py` transcribes the §5.4 truth table. The two must
agree, and a rename has to break something loud rather than silently orphan the
counters already in Redis.

**The bucket edge (D3).** The window is 12 discrete 5-second buckets, not a
continuous 60 seconds, and the difference is observable: an event can leave the
window up to `bucket_seconds` *earlier* than a true sliding window would evict it.
That is a documented trade — bounded memory and O(1) writes, against at most 5 s of
edge imprecision that S2 has ample room for. Both edges are asserted below so that
someone who reads it as an off-by-one has to argue with D3 before "fixing" it.

**Never raising (ADR 0008).** Recording is not required to produce a response, so
it must not be able to break one. A Redis that refuses connections and a Redis that
accepts one and then goes silent are both exercised, and both must be survivable.
The asymmetry between the two directions is the load-bearing part: a failed write
is silent data loss, but a failed *read* returns `None` for **unknown** and never a
zero-filled window, because a Redis outage that reported every provider as flawless
would be worst precisely when health data matters most.

`fakeredis` rather than Redis and `ManualClock` rather than real time (NFR-2). The
one exception is the hang test at the bottom, which needs `asyncio.timeout` to
actually fire — that measures event-loop time and no injected clock can reach it,
the same asymmetry `tests/test_executor.py` documents for `wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from keel.clock import Clock, ManualClock, SystemClock
from keel.config import BreakerConfig, load_config
from keel.health.latency import LATENCY_KEY_PREFIX, SAMPLE_CAP, latency_key
from keel.health.window import (
    ERROR_FIELD_PREFIX,
    FIELD_OK,
    HEALTH_KEY_PREFIX,
    REDIS_TIMEOUT_SECONDS,
    HealthTracker,
    WindowCounts,
    error_field,
)
from keel.providers.base import ProviderResult
from keel.providers.errors import ErrorClass, NormalizedError
from keel.redis import (
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_REDIS_URL,
    SOCKET_TIMEOUT_SECONDS,
    RedisSettings,
    create_redis_client,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

PROVIDER = "mock_chaos"

# The shipped breaker block: 60 s of window in 5 s buckets, so 12 of them and a
# 120 s TTL. Read from the file rather than hard-coded, because these tests are
# about the *behaviour* those numbers produce, not about the numbers.
BREAKER = load_config(SHIPPED_CONFIG).breaker

# Transcribed by hand from §5.5, not built from the module under test. At
# `ManualClock(start=55.0)` with 5 s buckets the current index is 11.
BUCKET_11_KEY = "keel:health:mock_chaos:11"


def window(clock: Clock, redis: FakeRedis | None = None) -> HealthTracker:
    resolved = redis if redis is not None else FakeRedis(decode_responses=True)
    return HealthTracker(redis=resolved, breaker=BREAKER, clock=clock)


def success(provider: str = PROVIDER) -> ProviderResult:
    return ProviderResult.success(
        provider=provider, response={"id": "chatcmpl-1"}, latency_ms=12.0
    )


def failure(error_class: ErrorClass, provider: str = PROVIDER) -> ProviderResult:
    return ProviderResult.failure(
        provider=provider,
        latency_ms=8.0,
        error=NormalizedError(error_class=error_class, message=f"injected {error_class.value}"),
    )


class BrokenRedis:
    """A client that refuses every command — a Redis that is down.

    `pipeline()` raises instead of returning a context manager, which is what a
    connection failure looks like from `HealthTracker`'s side. A hand-written stub
    rather than a `Mock`, matching the fake adapters in `tests/test_executor.py`;
    subclassing a real `Redis` honestly is not worth the surface.
    """

    def pipeline(self, transaction: bool = True) -> object:
        raise RedisConnectionError("connection refused (test stub)")


class CountingPipelineRedis:
    """Delegates to a real fake, counting how many pipelines were opened.

    The only way to observe decision A from outside: one write or two produce
    identical Redis state and differ only in round trips.
    """

    def __init__(self, inner: FakeRedis) -> None:
        self._inner = inner
        self.pipelines = 0

    def pipeline(self, transaction: bool = True) -> Any:
        self.pipelines += 1
        return self._inner.pipeline(transaction=transaction)


class HangingRedis:
    """A client that accepts the command and then never answers.

    The failure `REDIS_TIMEOUT_SECONDS` exists for, and a different one from
    `BrokenRedis`: a refused connection fails fast on its own, while this would
    hang the request until the client gave up if nothing bounded it.
    """

    def __init__(self, seconds: float = 5.0) -> None:
        self._seconds = seconds

    def pipeline(self, transaction: bool = True) -> HangingRedis:
        return self

    async def __aenter__(self) -> HangingRedis:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def hincrby(self, key: str, field: str, amount: int) -> None:
        return None

    def expire(self, key: str, seconds: int) -> None:
        return None

    def hgetall(self, key: str) -> None:
        return None

    def lpush(self, key: str, value: float) -> None:
        return None

    def ltrim(self, key: str, start: int, end: int) -> None:
        return None

    def lrange(self, key: str, start: int, end: int) -> None:
        return None

    async def execute(self) -> list[object]:
        await asyncio.sleep(self._seconds)
        return []  # pragma: no cover - the timeout fires first


# --------------------------------------------------------------------------
# The key schema (§5.5)
# --------------------------------------------------------------------------


async def test_a_success_lands_in_the_current_bucket_under_ok() -> None:
    """The literal §5.5 key and field, so a schema change fails here, not in Grafana."""
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(success())

    assert await redis.hgetall(BUCKET_11_KEY) == {FIELD_OK: "1"}


@pytest.mark.parametrize(
    "error_class",
    list(ErrorClass),
    ids=[member.value.replace("_", "-") for member in ErrorClass],
)
async def test_every_error_class_counts_into_its_own_field(error_class: ErrorClass) -> None:
    """All seven, not just the four D7 counts toward the breaker.

    The M2 exit criterion needs the error-rate panel to split across taxonomy
    classes, and `MockAdapter`'s default mix carries `CONTENT_FILTER` precisely
    because it does *not* trip a breaker. A recorder that filtered here would
    leave that panel unable to show the distinction it exists to show — deciding
    what counts is the breaker's job, in Phase 3.
    """
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(failure(error_class))

    assert await redis.hgetall(BUCKET_11_KEY) == {f"err_{error_class.value}": "1"}


def test_the_error_field_parametrization_covers_every_class() -> None:
    """A `parametrize` over a short list yields a *green* suite, which is how a gap hides.

    Same posture as the completeness guard in `keel/providers/errors.py`: an
    eighth class must fail something rather than quietly go unrecorded.
    """
    covered = {error_field(member) for member in ErrorClass}

    assert len(covered) == 7
    assert all(field.startswith(ERROR_FIELD_PREFIX) for field in covered)
    assert FIELD_OK not in covered, "`ok` is an outcome, not an error class"


async def test_the_key_names_the_config_entry_not_the_adapter() -> None:
    """Several entries may share one adapter.

    Health, metrics, and `X-Keel-Provider` all key on the entry, which is exactly
    what `ProviderResult.provider` already carries.
    """
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(success(provider="cohere_primary"))

    assert sorted(await redis.keys("*")) == [
        "keel:health:cohere_primary:11",
        "keel:latency:cohere_primary:11",
    ], "both key families name the entry, and neither invents a suffix"


async def test_a_bucket_expires_at_twice_the_window() -> None:
    """TTL 2x window (§5.5) is what makes the keyspace bounded without a sweeper.

    ADR 0001 leans on it directly: an NTP step that skips bucket indices orphans
    keys, and this is what makes them go away on their own.
    """
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(success())

    assert await redis.ttl(BUCKET_11_KEY) == 2 * BREAKER.window_seconds == 120


async def test_the_ttl_is_refreshed_by_a_later_write_into_the_same_bucket() -> None:
    """Otherwise a busy bucket could expire mid-window, mid-incident.

    The `EXPIRE` rides along on every write rather than being set once at bucket
    creation, so there is no ordering in which a hot bucket is left to die early.
    """
    redis = FakeRedis(decode_responses=True)
    health = window(ManualClock(start=55.0), redis)

    await health.record(success())
    await redis.expire(BUCKET_11_KEY, 5)
    await health.record(success())

    assert await redis.ttl(BUCKET_11_KEY) == 120


# Not tested here, and deliberately said out loud: that the write pipeline is
# *transactional*. The failure it guards is a connection lost between the HINCRBY
# and the EXPIRE, leaving a counted key with no TTL — and `fakeredis` cannot drop a
# connection mid-pipeline, so a test asserting it would pass with
# `transaction=False` too and prove nothing. Verified by mutation instead: flipping
# that flag breaks no test, which is why the reason lives in `record`'s docstring
# where the next reader will actually meet it.


# --------------------------------------------------------------------------
# Rolling: buckets fill, roll over, and fall out
# --------------------------------------------------------------------------


async def test_counts_within_one_bucket_accumulate() -> None:
    clock = ManualClock(start=55.0)
    health = window(clock)

    await health.record(success())
    await health.record(success())
    await health.record(failure(ErrorClass.RATE_LIMIT))

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert (counts.ok, counts.errors[ErrorClass.RATE_LIMIT], counts.total) == (2, 1, 3)


async def test_time_advancing_past_a_bucket_starts_a_new_key() -> None:
    """Both keys exist, and the merged read spans them — the window is their union."""
    clock = ManualClock(start=55.0)
    redis = FakeRedis(decode_responses=True)
    health = window(clock, redis)

    await health.record(success())
    clock.advance(BREAKER.bucket_seconds)
    await health.record(success())

    assert sorted(await redis.keys("*")) == [
        "keel:health:mock_chaos:11",
        "keel:health:mock_chaos:12",
        "keel:latency:mock_chaos:11",
        "keel:latency:mock_chaos:12",
    ], "counts and samples roll together — they share one bucket index"
    counts = await health.read(PROVIDER)
    assert counts is not None and counts.ok == 2


async def test_a_full_window_of_buckets_is_merged() -> None:
    """One count in each of the 12 buckets, read from inside the last of them."""
    bucket_count = BREAKER.window_seconds // BREAKER.bucket_seconds
    clock = ManualClock(start=0.0)
    health = window(clock)

    for index in range(bucket_count):
        if index:
            clock.advance(BREAKER.bucket_seconds)
        await health.record(success())

    assert health.current_bucket() == bucket_count - 1
    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.ok == bucket_count == 12


async def test_an_old_bucket_falls_out_of_the_merged_view() -> None:
    """The whole point of a window: what happened a minute ago stops counting."""
    clock = ManualClock(start=0.0)
    health = window(clock)
    await health.record(failure(ErrorClass.SERVER_ERROR))

    clock.advance(BREAKER.window_seconds)

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.total == 0, "one window later, the event is outside the merged range"


async def test_the_expired_bucket_is_only_hidden_not_yet_deleted() -> None:
    """Falling out of the merged *view* and expiring in Redis are different clocks.

    The read stops including a bucket after `window_seconds`; Redis deletes the key
    at 2x that, on its own wall clock, which `ManualClock` does not drive. Worth
    stating so nobody reads the TTL as the thing that windows the data — the range
    of keys the read asks for is.
    """
    clock = ManualClock(start=0.0)
    redis = FakeRedis(decode_responses=True)
    health = window(clock, redis)
    await health.record(success())

    clock.advance(BREAKER.window_seconds)

    assert await health.read(PROVIDER) == WindowCounts(
        provider=PROVIDER, ok=0, errors=dict.fromkeys(ErrorClass, 0)
    )
    assert await redis.exists("keel:health:mock_chaos:0") == 1


# --------------------------------------------------------------------------
# The bucket edge (D3) — documented behaviour, not an off-by-one
# --------------------------------------------------------------------------


async def test_an_event_at_the_start_of_a_bucket_survives_the_full_window() -> None:
    """The generous edge: recorded at t=0, still counted at t=59.9."""
    clock = ManualClock(start=0.0)
    health = window(clock)
    await health.record(success())

    clock.advance(BREAKER.window_seconds - 0.1)

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.ok == 1, "59.9 s old and inside a 60 s window"


async def test_an_event_late_in_a_bucket_is_evicted_early_and_that_is_d3() -> None:
    """The mean edge, and the cost D3 accepts by name.

    Recorded at t=4.9 it lands in bucket 0 alongside an event from t=0.0, so it is
    evicted when bucket 0 leaves the range — at t=60.0, having lived only 55.1 s.
    A true continuous window would have kept it another 4.9 s.

    This is *not* a bug and must not be "fixed": the alternative D3 rejected is a
    sorted set of individual events, which windows exactly at the cost of O(n)
    memory per provider and a `ZREMRANGEBYSCORE` on every request. At most
    `bucket_seconds` of imprecision is well inside S2's "p95 reroute time <= 2x
    window length". Anyone tempted to make this exact should change D3 first.
    """
    clock = ManualClock(start=4.9)
    health = window(clock)
    await health.record(success())
    assert health.current_bucket() == 0, "t=4.9 is still the first 5 s bucket"

    clock.advance(BREAKER.window_seconds - 4.9)  # now exactly t=60.0

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.total == 0, "evicted at 55.1 s of age — up to one bucket early, by design"


async def test_the_merged_range_is_exactly_the_configured_bucket_count() -> None:
    """12 with the shipped config, inclusive of the current partial bucket.

    `config.py` guarantees the window divides evenly into buckets, so this is
    exact and no partial edge bucket exists to skew a rate — which is why nothing
    in `window.py` re-checks it.
    """
    clock = ManualClock(start=1_000.0)
    redis = FakeRedis(decode_responses=True)
    health = window(clock, redis)
    current = health.current_bucket()

    # Seed one count into every bucket index the read should reach, and one into
    # the index just outside it on either side.
    for index in range(current - 12, current + 2):
        await redis.hincrby(health.bucket_key(PROVIDER, index), FIELD_OK, 1)

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.ok == 12, "the oldest and the future bucket are both outside the range"


# --------------------------------------------------------------------------
# Unknown is not zero (ADR 0008)
# --------------------------------------------------------------------------


async def test_an_empty_window_reads_as_zeros_not_none() -> None:
    """A provider with no traffic is legitimately zero — Redis answered.

    The counterpart to the test below. These two are the reason `read` returns an
    optional at all: distinguishing "nothing happened" from "we could not find
    out" is what stops Phase 3 treating an outage as a clean bill of health.
    """
    counts = await window(ManualClock(start=55.0)).read("never_called")

    assert counts is not None
    assert counts.total == 0
    assert counts.errors == dict.fromkeys(ErrorClass, 0), "all seven present, zero-filled"


async def test_a_read_against_a_dead_redis_is_unknown() -> None:
    """`None`, never a zero-filled window. The one that matters during an incident."""
    health = HealthTracker(redis=BrokenRedis(), breaker=BREAKER, clock=ManualClock(start=55.0))

    assert await health.read(PROVIDER) is None


async def test_a_write_against_a_dead_redis_is_dropped_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The contract the executor relies on: recording cannot break a response.

    Logged at WARNING and named, because the alternative to a loud drop is a
    silent one — and until P2-T4 exports a counter, this line is the only evidence
    that health data is being lost.
    """
    health = HealthTracker(redis=BrokenRedis(), breaker=BREAKER, clock=ManualClock(start=55.0))

    with caplog.at_level(logging.WARNING, logger="keel.health.window"):
        await health.record(success())

    assert "health write dropped" in caplog.text
    assert PROVIDER in caplog.text, "the log must say which provider went unrecorded"


async def test_a_redis_that_hangs_is_bounded_by_the_time_box() -> None:
    """Real time, and the only test here that uses it.

    `asyncio.timeout` measures event-loop time, so a `ManualClock` cannot make it
    fire — the same asymmetry `tests/test_executor.py` documents for `wait_for`.
    The cost is one `REDIS_TIMEOUT_SECONDS` of real waiting; nothing calls
    `time.sleep` (NFR-2).

    A refused connection fails on its own. This is the other shape: a Redis that
    accepts the command and then stops answering, which without the time box would
    hang the request rather than the write.
    """
    health = HealthTracker(redis=HangingRedis(), breaker=BREAKER, clock=SystemClock())

    started = asyncio.get_running_loop().time()
    await health.record(success())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < REDIS_TIMEOUT_SECONDS * 4, "the write must not outlive its time box"
    assert REDIS_TIMEOUT_SECONDS == 0.25, "a change here changes the worst-case hot-path cost"


# --------------------------------------------------------------------------
# Reading data this build did not write
# --------------------------------------------------------------------------


async def test_an_unknown_field_is_ignored_rather_than_fatal() -> None:
    """A newer gateway writing beside an older one must not crash the older one.

    Rolling deployments put two builds on one keyspace. Skipping the field keeps
    the rest of the bucket readable, which is strictly better than a reader that
    refuses the whole window over one name it does not know.
    """
    clock = ManualClock(start=55.0)
    redis = FakeRedis(decode_responses=True)
    health = window(clock, redis)
    await health.record(success())
    await redis.hincrby(BUCKET_11_KEY, "err_from_the_future", 4)

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.ok == 1
    assert counts.total == 1, "the unknown field is skipped, not folded into the total"


async def test_a_non_integer_count_is_survived() -> None:
    """`HINCRBY` cannot write this, so something else did — and `read` never raises."""
    clock = ManualClock(start=55.0)
    redis = FakeRedis(decode_responses=True)
    health = window(clock, redis)
    await redis.hset(BUCKET_11_KEY, FIELD_OK, "not-a-number")

    counts = await health.read(PROVIDER)
    assert counts is not None
    assert counts.ok == 0


# --------------------------------------------------------------------------
# Configuration reaches the window
# --------------------------------------------------------------------------


async def test_a_different_bucket_width_changes_the_key_and_the_merge_width() -> None:
    """Nothing here hard-codes 5 s or 12 buckets; both come from `BreakerConfig`.

    Pinned with a non-shipped geometry so a hard-coded constant sneaking into
    `window.py` fails rather than passing by coincidence with the shipped numbers.
    """
    breaker = BreakerConfig(
        window_seconds=30,
        bucket_seconds=10,
        min_requests_in_window=20,
        error_rate_threshold=0.30,
        open_cooldown_seconds=30,
        half_open_probe_ratio=0.10,
        half_open_successes_to_close=3,
    )
    clock = ManualClock(start=55.0)
    redis = FakeRedis(decode_responses=True)
    health = HealthTracker(redis=redis, breaker=breaker, clock=clock)

    await health.record(success())

    assert sorted(await redis.keys("*")) == [
        f"{HEALTH_KEY_PREFIX}:{PROVIDER}:5",
        f"{LATENCY_KEY_PREFIX}:{PROVIDER}:5",
    ], "55 // 10 == 5, for both families"
    assert await redis.ttl(f"{HEALTH_KEY_PREFIX}:{PROVIDER}:5") == 60
    assert await redis.ttl(f"{LATENCY_KEY_PREFIX}:{PROVIDER}:5") == 60

    clock.advance(30)
    rolled_out = await health.read(PROVIDER)
    assert rolled_out is not None
    assert rolled_out.total == 0, "three 10 s buckets is one window, not twelve"


# --------------------------------------------------------------------------
# The client the app actually builds (keel/redis.py)
# --------------------------------------------------------------------------


def test_the_client_carries_socket_deadlines_below_the_windows_own_box() -> None:
    """The inner half of the two-layer bound, and an ordering that is load-bearing.

    Without socket deadlines `redis-py` takes ~4 s to give up on a refused
    connection, so `HealthTracker`'s box fires first and every failure arrives as a
    bare `TimeoutError` with an empty message. Until P2-T4 exports a counter that
    log line is the only evidence health data is being dropped (ADR 0008), so an
    anonymous timeout is not evidence of anything.

    The inequality is the property worth pinning: let the socket deadlines drift
    above the box and the diagnosis silently goes blank again.
    """
    assert CONNECT_TIMEOUT_SECONDS < REDIS_TIMEOUT_SECONDS
    assert SOCKET_TIMEOUT_SECONDS < REDIS_TIMEOUT_SECONDS

    client = create_redis_client(RedisSettings(redis_url="redis://localhost:6379/0"))
    options = client.get_connection_kwargs()

    assert options["socket_connect_timeout"] == CONNECT_TIMEOUT_SECONDS
    assert options["socket_timeout"] == SOCKET_TIMEOUT_SECONDS
    assert options["decode_responses"] is True, "str fields compare against ErrorClass values"


def test_a_blank_redis_url_falls_back_to_the_default() -> None:
    """`.env.example` ships assignments people copy and do not fill in.

    Same rule `ProviderCredentials` applies to a blank `COHERE_API_KEY`, for the
    same reason: without it `REDIS_URL=` becomes an empty connection string that
    fails with a message about a URL rather than about the variable.
    """
    assert RedisSettings(redis_url="   ").redis_url == DEFAULT_REDIS_URL
    assert RedisSettings(redis_url="redis://elsewhere:6379/1").redis_url == (
        "redis://elsewhere:6379/1"
    )


# --------------------------------------------------------------------------
# Latency samples ride the same write (P2-T3)
# --------------------------------------------------------------------------


async def test_a_recorded_attempt_leaves_its_latency_in_the_sibling_key() -> None:
    """The literal §5.5 latency key, transcribed by hand like the health one.

    Same bucket index as the count it was recorded with, which is the property
    that lets `snapshot` merge the two over one range and mean it.
    """
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(success())

    assert await redis.lrange("keel:latency:mock_chaos:11", 0, -1) == ["12.0"]


async def test_a_failed_attempt_contributes_a_sample_too() -> None:
    """Latency is recorded for failures, not only successes.

    A provider answering every call in 50 ms with a 500 is fast and broken; one
    that times out is slow and broken. §5.6 trips on either, so discarding the
    failure path's latency would blind half of that.
    """
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(failure(ErrorClass.SERVER_ERROR))

    assert await redis.lrange("keel:latency:mock_chaos:11", 0, -1) == ["8.0"]


async def test_the_latency_key_expires_at_twice_the_window() -> None:
    """TTL 2x window on the LIST as well as the HASH, or samples outlive their counts."""
    redis = FakeRedis(decode_responses=True)

    await window(ManualClock(start=55.0), redis).record(success())

    assert await redis.ttl("keel:latency:mock_chaos:11") == 2 * BREAKER.window_seconds == 120


async def test_the_count_and_the_sample_are_one_round_trip() -> None:
    """Decision A, asserted rather than trusted: one `execute`, not two.

    Two writers would mean two connections, two `asyncio.timeout` boxes, and — per
    ADR 0008's measurement — twice the per-request cost when Redis is unreachable.
    A counting stub is the only way to see that from outside, since the observable
    end state is identical either way.
    """
    redis = FakeRedis(decode_responses=True)
    counter = CountingPipelineRedis(redis)
    health = HealthTracker(redis=counter, breaker=BREAKER, clock=ManualClock(start=55.0))

    await health.record(success())

    assert counter.pipelines == 1, "the latency write must ride the counts' pipeline"
    assert await redis.hgetall("keel:health:mock_chaos:11") == {FIELD_OK: "1"}
    assert await redis.lrange("keel:latency:mock_chaos:11", 0, -1) == ["12.0"]


async def test_the_reservoir_is_capped_under_sustained_load() -> None:
    """10 000 writes into one bucket leave exactly `SAMPLE_CAP` — the task card's bar.

    This is the NFR-5 bound: memory per bucket is capped by `LTRIM` on every write
    rather than by a sweeper that might not run. Without the trim this list grows
    unbounded for the whole 120 s TTL.
    """
    redis = FakeRedis(decode_responses=True)
    health = window(ManualClock(start=55.0), redis)

    for _ in range(10_000):
        await health.record(success())

    assert await redis.llen("keel:latency:mock_chaos:11") == SAMPLE_CAP == 200


async def test_the_survivors_of_the_cap_are_the_most_recent_samples() -> None:
    """`LPUSH` + `LTRIM` is a recency cap, not reservoir sampling — pinned deliberately.

    §5.5 calls it a "capped reservoir", but true reservoir sampling keeps an
    unbiased sample of the whole bucket. This keeps the newest `SAMPLE_CAP`, so a
    hot bucket's percentiles describe the tail end of its five seconds. The bias
    is real, it leans toward newer data (which a health signal should prefer), and
    it is asserted here so nobody reads "reservoir" and assumes otherwise.
    """
    redis = FakeRedis(decode_responses=True)
    health = window(ManualClock(start=55.0), redis)

    for index in range(SAMPLE_CAP + 50):
        await health.record(
            ProviderResult.success(
                provider=PROVIDER, response={"id": "x"}, latency_ms=float(index)
            )
        )

    stored = sorted(float(raw) for raw in await redis.lrange(latency_key(PROVIDER, 11), 0, -1))

    assert stored == [float(i) for i in range(50, SAMPLE_CAP + 50)]
    assert 0.0 not in stored, "the oldest samples are the ones dropped"


async def test_samples_older_than_the_window_fall_out_of_the_snapshot() -> None:
    """The latency half rolls on the same range as the counts."""
    clock = ManualClock(start=0.0)
    health = window(clock)
    await health.record(success())

    clock.advance(BREAKER.window_seconds)

    snap = await health.snapshot(PROVIDER)
    assert snap is not None
    assert snap.sample_count == 0
    assert snap.p95_ms is None


# --------------------------------------------------------------------------
# snapshot(): counts and percentiles from one read
# --------------------------------------------------------------------------


async def test_a_snapshot_carries_counts_and_percentiles_together() -> None:
    """One object, one window, so §5.6 cannot read its two triggers from two instants."""
    clock = ManualClock(start=55.0)
    health = window(clock)

    for latency in (10.0, 20.0, 30.0, 40.0):
        await health.record(
            ProviderResult.success(provider=PROVIDER, response={"id": "x"}, latency_ms=latency)
        )
    await health.record(failure(ErrorClass.RATE_LIMIT))

    snap = await health.snapshot(PROVIDER)
    assert snap is not None
    assert (snap.ok, snap.total) == (4, 5)
    assert snap.errors[ErrorClass.RATE_LIMIT] == 1
    assert snap.success_rate == 0.8
    assert snap.sample_count == 5, "the failure's latency is a sample too"


async def test_a_snapshot_of_an_untouched_provider_is_empty_not_unknown() -> None:
    """Redis answered, so this is real information: nothing happened."""
    snap = await window(ManualClock(start=55.0)).snapshot("never_called")

    assert snap is not None
    assert snap.total == 0
    assert snap.success_rate is None, "no traffic is unknown, not perfect"
    assert (snap.p50_ms, snap.p95_ms, snap.p99_ms) == (None, None, None)


async def test_a_snapshot_against_a_dead_redis_is_unknown() -> None:
    """`None`, matching `read`, and for the reason ADR 0008 gives."""
    health = HealthTracker(redis=BrokenRedis(), breaker=BREAKER, clock=ManualClock(start=55.0))

    assert await health.snapshot(PROVIDER) is None


async def test_counts_without_samples_yield_real_counts_and_no_percentiles() -> None:
    """The third no-answer flavour, and the one easiest to get wrong.

    Reachable by a count with no sibling sample — an older build's data, or a
    partially applied pipeline. "Failing, and we do not know how slow" must not
    collapse into "failing, and infinitely fast".
    """
    clock = ManualClock(start=55.0)
    redis = FakeRedis(decode_responses=True)
    health = window(clock, redis)
    await redis.hincrby("keel:health:mock_chaos:11", error_field(ErrorClass.TIMEOUT), 3)

    snap = await health.snapshot(PROVIDER)
    assert snap is not None
    assert snap.errors[ErrorClass.TIMEOUT] == 3
    assert snap.total == 3
    assert snap.sample_count == 0
    assert snap.p95_ms is None, "no samples means unknown latency, never zero"
