# 0001 — The injected clock reads wall-clock time, not monotonic time

**Status:** Accepted
**Date:** 2026-08-18
**Relates to:** NFR-2, NFR-5, FR-3.2, S5 · TECHNICAL-DESIGN.md §3, §5.5, §5.6, §7 · PHASE-2-PLAN.md P1-T1, D-B

## Context

That Keel has an injectable clock is not what this record decides. NFR-2 requires the breaker to be unit-testable against a controllable clock, and technical design §7 already calls the injectable clock "the highest-leverage decision" in the testing strategy. Both were settled before any code existed.

Implementing `keel/clock.py` forced a question neither document answers: **what does `now()` return?**

The reflexive answer is `time.monotonic()`. Nearly every consumer of time in Keel is a *duration* — breaker open-cooldown expiry, hedge `after_ms` triggers, retry backoff with jitter, per-attempt latency — and monotonic time is the standard advice for all of them, precisely because it cannot be moved by NTP correction, DST, or an operator setting the system clock. Measuring an interval with wall-clock time is a well-known bug.

Two consumers are not local durations, and they are the two that decide the architecture:

| Consumer | Storage | Read by |
|---|---|---|
| Health window bucket key `keel:health:{provider}:{bucket_epoch}` (§5.5) | Redis | Gateway **and** deferred worker |
| Breaker `opened_at` in `keel:breaker:{provider}` (§5.5, §5.6) | Redis | Gateway **and** deferred worker |

Three properties of those two rows rule monotonic time out.

**The zero point is per-process and undefined.** Python documents the reference point of `time.monotonic()` as undefined; a reading is meaningful only when differenced against another reading from the same process. The deferred worker is a separate process by decision D6 (§3). Gateway and worker computing `int(monotonic() // bucket_seconds)` produce different bucket keys for the same instant, so the health window silently splits across two disjoint key spaces — each holding part of the traffic, each failing `min_requests_in_window`, and the breaker never trips at all.

**It resets on restart.** FR-3.2 exists specifically so a gateway restart does not reset the view of provider health. A monotonic origin resets with the process, so every bucket written before a restart becomes unaddressable afterwards. The counters survive in Redis and become unreachable, which is worse than losing them: the TTL keeps them occupying the keyspace while the gateway reads an empty window.

**It is not comparable to a stored value.** `opened_at` is written by whichever process trips the breaker and read by every process evaluating cooldown expiry. A stored monotonic reading compared against another process's reading is arithmetic on two unrelated origins.

Note what these have in common. The problem is not measuring an interval — it is **keying and persisting shared state**. That is a different job from timing, and it is the job that dominates here.

## Decision

`Clock.now()` returns **wall-clock epoch seconds** (`time.time()`). The protocol exposes one time-reading method and no monotonic companion.

Three supporting choices ship with it:

- **`ManualClock.sleep()` advances the clock by its full duration and returns immediately**, rather than requiring the test to advance time separately. A component that sleeps through a 30 second cooldown observes 30 seconds having passed, while the test costs microseconds. It also awaits a zero-second yield, so concurrently scheduled tasks resume and observe the new time — without that, a manual sleep is a bare assignment, and the concurrent behaviour in §5.8 hedging and §5.6 half-open probe admission could never interleave under test.
- **Both implementations reject negative durations identically.** If only `ManualClock` rejected them, a test could pass while production silently treated the same call as a zero-wait. A negative duration nearly always means an expired deadline went unclamped, so the error message names the fix.
- **No component imports `time` directly.** The clock is the only source of time in the codebase.

## Consequences

**A clock step moves the window underneath the system.** This is the cost, and it is real:

- A *backward* step can make a bucket epoch repeat, so new counts land in a bucket that already holds data from the previous pass, double-weighting one slice of the window. It can also place `opened_at` in the future, deferring a cooldown expiry by the size of the step — a breaker that stays open longer than configured.
- A *forward* step skips bucket keys, leaving the merged window sparse for up to one window length. The breaker then reads an under-populated window and declines to trip. `min_requests_in_window` (§5.6) absorbs this, but by accident rather than by design: a sparse window fails the volume floor, so the breaker holds rather than acting on nonsense.

The exposure is bounded rather than eliminated. Every comparison is against a window measured in tens of seconds and is recomputed continuously rather than accumulated, so a step perturbs one window and then washes out. The 2× window TTLs on bucket keys (§5.5) mean orphaned buckets expire on their own.

**Per-attempt latency inherits the same exposure.** A step landing inside a single in-flight request corrupts that one latency sample. This is tolerable because those samples feed the reservoir, which §5.5 already documents as approximate and threshold-driving rather than authoritative — the latency figures the README reports come from Prometheus histograms, which time independently of this clock.

**Synchronized time becomes a deployment assumption.** In the compose topology all services share the host clock, so this costs nothing today. It becomes a real constraint only in a multi-host deployment, which §10 already lists as untested. It should be stated in §10 beside the existing Redis single-point-of-failure note if that topology is ever attempted.

**The "no direct `time` import" rule is enforced by review, not tooling.** A single `time.time()` inside a component is invisible to the test suite and reintroduces nondeterminism exactly where the clock was meant to remove it. Worth closing with a `flake8-tidy-imports` banned-api rule in `pyproject.toml`, so ruff fails the build rather than relying on a reviewer noticing.

**If a consumer ever needs true elapsed-time safety, the fix is additive.** Add `monotonic()` to the protocol beside `now()`; do not change what `now()` returns. Nothing in Phases 1–6 needs it, so adding it now would be a second way to ask for time with no way to tell which is correct.

## Alternatives considered

**`time.monotonic()`.** Lost for the three reasons in Context. Worth being precise about why the usual advice does not apply here: the standard argument for monotonic time assumes a reading is differenced against another reading in the same process and then discarded. In Keel the reading is *persisted as a key* and read by another process, which is outside the guarantee monotonic time offers.

**Expose both `now()` and `monotonic()` on the protocol.** Rejected as speculative. Every current consumer either requires wall-clock semantics or is indifferent to the difference, and two ways to ask for time invites picking the wrong one in the component where it matters. Revisit when a concrete consumer needs it; the change is additive.

**Use Redis `TIME` as the single clock authority.** The most interesting alternative. It gives one authority immune to per-container drift, and Phase 3's breaker transitions already need a Lua script for atomic compare-and-set, which could read `TIME` server-side and make the trip decision and its timestamp perfectly consistent. Rejected for now on three counts: it puts a network round trip on the hot path for every timestamp, against a 15 ms p95 budget (NFR-5, S5); it makes the clock un-injectable, so every unit test would need a Redis fake purely to ask the time, which is the opposite of what NFR-2 asks for; and it makes Redis a dependency of the ingress path in Phase 1, before health tracking exists. **Worth revisiting in Phase 3 for the breaker CAS specifically**, where the script exists anyway and the consistency argument is strongest.

**No protocol — pass raw floats to anything that needs a timestamp.** Rejected by NFR-2 and §7 before the build started. Recorded here only because it is the shape the code drifts into if the protocol is quietly abandoned under schedule pressure.
