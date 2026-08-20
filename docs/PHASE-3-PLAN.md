# Build Plan — Phase 3 (Milestone M3)

**Owner:** Jamil
**Status:** Draft v1.0
**Last updated:** 2026-08-20
**Related documents:** `PRD.md`, `TECHNICAL-DESIGN.md`, `ROADMAP.md`, `PHASE-2-PLAN.md`

**Progress:** 0h of ~10.0h estimated. Not started. Next up: **P3-T1 — Capability filter and the "no candidate" status split.**

---

## 0. Where the repo is, and what this covers

Phases 1 and 2 are complete. The gateway serves `POST /v1/chat/completions` against Cohere or the in-process mock, config-driven; every attempt is normalized onto the §5.4 taxonomy, recorded to a Redis sliding window (`keel/health/window.py`), and exported on `GET /metrics` (`keel/observability/metrics.py`) with the full §6 catalogue declared — including the three metrics this phase produces for the first time.

This phase has more of its shape already sitting in the repo than either prior phase started with, because Phase 1 and Phase 2 each named the seam they were deliberately leaving open rather than inventing one later:

- `keel/routing/router.py` has three seams marked by name and section reference — capability filter (§5.7, D2), breaker gate (§5.6, §5.7), and the "no candidate survives" case — each currently a comment, not code.
- `keel/routing/executor.py` invokes exactly one candidate and stops; its own docstring calls that "Phase 1 invokes candidate 1 and stops," and the empty-candidates guard is `raise RuntimeError(...)` marked `pragma: no cover - unreachable until Phase 3`.
- `keel/api/errors.py`'s `KeelError` docstring already reserves `422` (no capable provider, §5.7) and `503` (all candidates exhausted, §4); `UpstreamUnavailableError` (503) is already defined and unused.
- `keel/providers/errors.py` already carries the full §5.4 truth table — `counts_toward_breaker` and `retry_elsewhere` per class — which is the breaker's only required input from the taxonomy.
- `keel/health/snapshot.py`'s `ProviderHealth` is built and **unconsumed**: its own docstring states "Nothing consumes this yet, deliberately" and names the exact open question this phase has to close — whether the p95 trip condition reads a per-provider figure against a per-class budget, and if so, which class's.
- `keel/config.py`'s `BreakerConfig` is fully specified — `window_seconds`, `bucket_seconds`, `min_requests_in_window`, `error_rate_threshold`, `open_cooldown_seconds`, `half_open_probe_ratio`, `half_open_successes_to_close` — and validated at startup. Nothing reads it yet.
- `keel/observability/metrics.py` already declares `keel_breaker_state`, `keel_breaker_transitions_total`, and `keel_failover_events_total`, primed at startup the same way the provider-keyed series were in P2-T4. Its module docstring says outright: "`keel_breaker_*` and `keel_failover_events_total` wait for Phase 3."
- `keel/api/app.py` sets `X-Keel-Attempts` from a named constant, `PHASE_1_ATTEMPTS = 1` — a placeholder the P1-T7 notes already flag as the real count's future home.
- **Added after this document was first written, and load-bearing for the M3 exit below.** P2-T5 shipped structured JSON logs correlated by `request_id` (`keel/observability/logging.py`), with an `attempt` field already on every `provider_attempt` line — fixed at `1` until this phase's loop counts for real, so a multi-attempt request threads through the logs with no new plumbing. P2-T6 shipped the compose stack, a provisioned Grafana board, `scripts/loadgen.py`, and — settling the open sequencing question it inherited — an env-gated `POST /chaos/{provider}` (**ADR 0010**). §5's exit check is written against all of that.

Nothing here is undiscovered work. This phase is closing seams the prior two left open on purpose, per design principle 2 and FR-3.4: observe before you react.

This document covers roadmap **Phase 3 (Milestone M3)**, ~10h at ~10h/week — "the core of the project," and the week containing the Sep 8 FDE call per the roadmap.

---

## 1. Decisions to make before code

Five questions the design documents leave open, continuing the `D-` lettering `PHASE-2-PLAN.md` §1 started (`D-A` through `D-C`).

**D-D — Three distinct "no candidate to try" moments exist, and they get three different outcomes, not one.**
The router's own "seam 3 of 3" comment currently lumps two of these together as a single "no candidate survives" case, and §5.7's flowchart routes both into the same `DEGRADE` diamond. They are not the same failure and should not share a status:

1. **Capability filter empties the list before any attempt.** No configured provider can satisfy `envelope.capabilities`. This is semantic — the request cannot be answered by this fleet, healthy or not — and gets **422** (`CapabilityUnsatisfiableError`, new in P3-T1).
2. **Breaker gating empties an otherwise non-empty list.** Every remaining candidate is `OPEN`, or `HALF_OPEN` with no probe drawn. This is a provider-health fact, not a semantic one, and gets **503** (`UpstreamUnavailableError`, already defined).
3. **The failover loop exhausts a non-empty candidate list via repeated retryable failures.** Also **503**, rendered from the last attempt's `NormalizedError` so the client's final error still names a real provider and class.

**Deferrable classes get no different treatment than interactive ones in any of the three, for now.** Phase 5 owns the queue; until it exists there is nothing to enqueue into, so all three moments return their status directly regardless of `envelope.deferrable`. This is a known, temporary conflation — recorded in the docstring of whichever module raises each error, not hidden — and Phase 5 is where the `DEGRADE -> QUEUE` branch actually gets built.

**D-E — Atomic breaker transitions are one Lua script (`EVAL`), not a `WATCH`/`MULTI` retry loop.**
§5.6 requires "concurrent requests cannot each independently decide to trip the same breaker" — real compare-and-set, not just isolation. `WATCH`/`MULTI` needs a retry loop on the caller's side for every touch, and under load "the breaker was touched" is not the exceptional path, it's every gated request. A single `EVAL` does the whole read-decide-write cycle in one round trip with genuine atomicity. Cost: the transition logic is an opaque string to `mypy --strict`, so its branches are pinned by tests reading Redis state back after each call rather than by the type checker.

**D-F — The p95 leg of the trip condition compares the provider's one global p95 against the *tightest* `latency_budget_p95_ms` among the request classes that list it, computed once from config, not the current request's class.**
`keel:latency:{provider}:{bucket}` has no class dimension (§5.5, fixed in P2-T3) and is not getting one now — tripling the keys to thin every bucket's samples in order to answer a question the breaker doesn't exist yet to ask was exactly the trade `health/snapshot.py`'s docstring declined to make prematurely. The persisted breaker state (`keel:breaker:{provider}`) is unambiguously per-provider by the same schema, so the p95 leg, if it feeds that one shared state, must pick a single number. The tightest configured budget among the classes that route to this provider is the conservative choice: a provider too slow for its strictest consumer never gets to look healthy to that consumer because a lenient one is also on its preference list. The named cost is the reverse case — a provider trips on a strict class's budget even while comfortably serving a lenient one (`classification`'s 800 ms against `batch_enrichment`'s 60000 ms in the shipped config, where both list the same two providers, so today the tightest figure is always 800 ms). That is the same "wrong direction is safe" reasoning P2-T3 used for the recency-cap latency bias, applied to a different number. Worth an ADR — see §3.

**D-G — Breaker error rate excludes non-breaker classes from *both* the numerator and the denominator, not just the numerator.**
`ProviderHealth.error_rate` (shipped in P2-T3) is explicitly documented as "not the number §5.6's threshold compares against." This decision settles what is: the rate the breaker reads is `breaker-counted errors / (ok + breaker-counted errors)`, computed over only the four classes where `counts_toward_breaker` is `True` (D7). `AUTH_FAILURE`, `CONTENT_FILTER`, and `BAD_REQUEST` attempts are removed from the ratio entirely — not counted as failures, and not counted as evidence of health either — because a burst of client-side bad requests should neither trip a healthy provider nor dilute an unhealthy one's apparent rate back toward looking fine. `min_requests_in_window`'s volume floor is checked against this same reduced total, for the identical reason: a provider could otherwise clear the floor on attempts that say nothing about whether it is healthy.

**D-H — Half-open probe admission needs an injectable, seeded random source, the same reasoning NFR-2 already applies to `Clock`.**
Production draws `random_source.draw() < half_open_probe_ratio` at gate time; a test asserting "exactly 3 of 30 requests were admitted as probes at a 0.10 ratio" needs that draw to be deterministic and replayable, the same argument that put a seeded RNG on `MockAdapter` in P1-T4. A small `RandomSource` protocol (`draw() -> float`) lives at `keel/clock.py`'s side — top level, for the same reason `Clock` is (D-B) — with a `SystemRandomSource` for production and a scripted double for tests. `Router` gains this as a constructor dependency alongside the new `CircuitBreaker`.

---

## 2. Phase 3 — Circuit breaker and capability-aware failover (M3)

**Exit criterion (roadmap):** with the chaos-configured mock degrading, traffic reroutes automatically and the breaker closes again unattended once the mock recovers. Circuit state timeline panel shows the full shape.

**The circuit-state-timeline panel is this phase's to build**, which is a correction to what this document originally said. It assumed Phase 2 would build it; P2-T6 deliberately deferred it — *"building them now would show flat lines meaning 'no breaker' rather than 'no trips', which is a worse lie than an absent panel"* — and `tests/test_deploy_assets.py` asserts it is absent. That deferral was right and it makes this phase the panel's natural home: `0` only honestly reads as *closed* once a breaker exists to close. `keel_breaker_state` is already primed at `0` for every configured provider, so the panel has data the moment the state machine produces a transition. It lands with P3-T6, and the test that currently asserts its absence is edited in the same commit — that test was written to be edited.

### P3-T1 — Capability filter and the "no candidate" status split
**~1.5h · FR-1.5, FR-2.5, FR-4.6**

- Fill `router.py`'s seam 1: `Router.candidates()` drops every provider whose `ProviderConfig.capabilities` is not a superset of `envelope.capabilities`, ahead of preference ordering (D2, already the position the comment reserves).
- New `keel/api/errors.py` class: `CapabilityUnsatisfiableError`, `422`, code `capability_unsatisfiable` — the class that finally claims the status `KeelError`'s docstring has reserved since P1-T2.
- `keel/routing/executor.py`'s placeholder guard (`routing/executor.py:101-109`) becomes the real capability-exhaustion path: an empty candidate tuple from the router raises `CapabilityUnsatisfiableError`. This is D-D's moment 1 specifically — moment 2 (breaker-gated empty) is not reachable until P3-T4.
- Two router tests from P1-T6 were pinned to fail once Phase 3 lands rather than survive it silently. The `citations`-tagged-request-sees-`mock_chaos` case is retired here; the `SERVER_ERROR`-with-`retry_elsewhere=True`-still-returned case waits for P3-T5.

**Done when:** a `citations`-tagged request against the shipped config (only `cohere_primary` offers `citations`) returns `422` naming the missing capability when `cohere_primary` is excluded from the candidate set, and succeeds normally when it isn't.

### P3-T2 — Circuit breaker state module
**~2.5h · FR-4.1**

- `keel/health/breaker.py` — `BreakerState` enum (`CLOSED` / `HALF_OPEN` / `OPEN`, matching the gauge's `0`/`1`/`2` encoding already documented on `keel_breaker_state`). `CircuitBreaker` reads and writes `keel:breaker:{provider}` (state, `opened_at`, `probe_successes`) per §5.5, via the D-E Lua script.
- A provider with no breaker key yet reads as `CLOSED` — matches the state diagram's `[*] --> Closed` and means a fresh Redis instance, or a newly added provider, never starts gated.
- Redis-unreachable posture is inherited from ADR 0008, not re-decided: an unreadable state is "unknown," and per `health/snapshot.py`'s own note ("Phase 3's breaker must therefore hold on `None`"), unknown holds the current gating behavior rather than assuming either healthy or unhealthy. A failed *write* (a transition that cannot be persisted) logs and is dropped — the same shape as a dropped health write, not a raised exception.
- Wire the two already-declared metrics: `breaker_state.labels(provider=...).set(...)` and `breaker_transitions_total.labels(provider=..., from_state=..., to_state=...).inc()` on every transition (`keel/observability/metrics.py:262-273`).

**Done when:** `fakeredis` + `ManualClock` tests drive the bare state machine through every edge in the §5.6 diagram in isolation — closed→open, open→half-open after cooldown, half-open→closed after N successes, half-open→open on one probe failure — asserting both the Redis-persisted state and the emitted transition metric after each. Nothing here reads `ProviderHealth` yet; that is P3-T3.

### P3-T3 — Trip conditions: error rate and p95, with volume floor
**~1.5h · FR-4.2**

- The condition `CircuitBreaker` evaluates to decide `CLOSED` → `OPEN`, reading one `ProviderHealth` snapshot and `BreakerConfig`.
- Error rate per D-G: breaker-counted classes only, in both numerator and denominator.
- p95 per D-F: compare `ProviderHealth.p95_ms` against the tightest `latency_budget_p95_ms` among the classes whose `preference` includes this provider — computed once per `CircuitBreaker` construction from `KeelConfig`, not recomputed per request.
- Volume floor: `min_requests_in_window` compared against the same D-G-reduced total, not `ProviderHealth.total`.
- `ProviderHealth` unreadable (Redis down) → cannot evaluate a trip; hold current state, the same rule D-E's Redis-unreachable posture already applies to reads.
- ADR 0010 and ADR 0011 land with this task — see §3.

**Done when:** table-driven tests pin the exact threshold arithmetic as off-by-one pairs — trips at the error-rate threshold, does not just below it; trips at the volume floor, does not just below it; trips against the tightest class budget, does not against a looser one applied to the same p95 figure.

### P3-T4 — Breaker gate and half-open probe admission in the router
**~1.5h · FR-4.1, FR-4.4**

- Fill `router.py`'s seam 2: for each capability-filtered, preference-ordered candidate, read `CircuitBreaker` state. `CLOSED` admits. `HALF_OPEN` admits only when this request is drawn as a probe (`random_source.draw() < half_open_probe_ratio`, D-H); otherwise it is skipped exactly like `OPEN`.
- The `OPEN` → `HALF_OPEN` transition is lazy, not timer-driven — there is no background scheduler in this design. It is evaluated the moment a gate check reads an `OPEN` breaker whose `opened_at + open_cooldown_seconds` has elapsed, and persisted via the D-E script before the admission decision is made on the now-current state.
- `Router` gains `breaker: CircuitBreaker` and `random_source: RandomSource` constructor dependencies; `Executor` passes both through unchanged in shape, the same way it already threads `tracker`.
- If gating empties an otherwise non-empty candidate list, that is D-D's moment 2: `UpstreamUnavailableError` (503), never `CapabilityUnsatisfiableError` — this is provider health, not semantics.

**Done when:** a scripted `RandomSource` double proves the exact probe/skip sequence at a configured `half_open_probe_ratio` over a fixed number of requests against a `HALF_OPEN` provider, and a candidate set that is entirely `OPEN` returns `503` rather than the `RuntimeError` placeholder it hits today.

### P3-T5 — Failover loop, attempt counting, half-open close/reopen
**~2.0h · FR-4.3, FR-4.4**

- `Executor.execute` becomes the loop its own seam comment (`routing/executor.py:111-113`) already names: walk `candidates`, continuing while the last result's `retry_elsewhere` is `True` and candidates remain. A non-retryable failure returns immediately without trying further candidates — matching the router flowchart's `RESULT -->|non-retryable| RETURN` — which is today's Phase 1 behavior for a single attempt and now becomes one explicit branch of a longer loop rather than the whole method.
- Every attempt, including ones that exist only because of failover, still passes through the single `tracker.record()` call site (D-C, unchanged) — that includes probe attempts, whose outcome is exactly the evidence half-open needs recorded.
- An attempt against a `HALF_OPEN` candidate reports its outcome to `CircuitBreaker` regardless of what the *overall request* eventually returns: a probe success moves toward closing (or closes, at `half_open_successes_to_close`), a probe failure reopens immediately per §5.6 — even if a later candidate in the same request ultimately succeeds.
- `keel_failover_events_total{from_provider,to_provider,class}` increments on each jump. `X-Keel-Attempts` becomes the real count, replacing `PHASE_1_ATTEMPTS` at `keel/api/app.py:78,436`.
- Exhaustion (D-D moment 3): `UpstreamUnavailableError` (503), rendered from the *last* attempt's `NormalizedError`, so the client's error still names a real provider and class rather than a generic message.

**Done when:** one test degrades the first candidate and watches a request succeed on the second, with `X-Keel-Attempts: 2` and `X-Keel-Provider` naming the survivor and `keel_failover_events_total` incremented once for that pair; a second test exhausts every candidate and gets `503` naming the last provider and error class tried.

### P3-T6 — End-to-end state machine verification
**~1.0h · NFR-2**

- No new production code. Everything above is unit-tested against its own module; this task is the assembled path — `Executor` + `Router` + `CircuitBreaker`, `fakeredis` and `ManualClock`, no HTTP layer — watched through one scripted scenario: healthy → degrade → trip (state persisted, metric emitted) → reroute (failover event, header) → cooldown elapses → half-open probe admitted → close.
- Confirms every seam comment left in `router.py` and `executor.py` since Phase 1 is retired: `grep -rn "Phase 3 seam" keel/` and `grep -rn "unreachable until Phase 3" keel/` both return nothing.

**Done when:** the scripted, `ManualClock`-driven scenario reproduces the exact §5.6 mermaid diagram from `Closed` through `Open`, `HalfOpen`, and back to `Closed` in one test, asserting the Redis-persisted state and the emitted `keel_breaker_transitions_total` series at each edge — no real waiting anywhere in the sequence.

**Phase 3 total: ~10.0h estimated** — at the roadmap's own Phase 3 budget. Both prior phases ran over (12.25h and 13.0h against 10h budgets each), so treat 10.0h as optimistic rather than a target to defend; the 15h tripwire (1.5×) is where the descope ladder applies, not the estimate itself.

---

## 3. Documentation updates as work lands

Not a separate phase. Each ships in the same commit as the code that makes it true.

**The three ADRs below are numbered 0011–0013, not 0010–0012.** `docs/adr/0010` was taken by P2-T6's chaos-endpoint decision, which landed after this document was written. `docs/adr/README.md` is explicit that numbers are monotonic and never reused, since a reused number breaks every reference to it.

| Change | Trigger |
|---|---|
| `docs/adr/0011-the-p95-trip-condition-compares-against-the-tightest-class-budget.md` | With P3-T3 |
| `docs/adr/0012-breaker-error-rate-excludes-non-breaker-classes-from-numerator-and-denominator.md` | With P3-T3 |
| `docs/adr/0013-capability-exhaustion-and-provider-exhaustion-return-different-statuses.md` | With P3-T1 |
| `deploy/grafana/dashboards/keel-health.json` — the circuit-state timeline panel, and the `tests/test_deploy_assets.py` absence check it inverts | With P3-T6 |
| Technical design §5.6 — replace the open "Phase 3 must decide" note with the D-F resolution and its named cost | With ADR 0011 |
| Technical design §5.7 — split the flowchart's shared `DEGRADE` diamond into the three D-D moments, with their distinct statuses | With P3-T1 / P3-T4 |
| Technical design §8 repo layout — add `keel/health/breaker.py` and the `RandomSource` neighbor to `keel/clock.py` | With P3-T2 |
| README "Status" — Phase 3 complete, M3 met; update "Not built yet" list | End of phase |

---

## 4. Tripwires

| Tripwire | Response |
|---|---|
| Breaker CAS module (P3-T2) exceeds 3.5h | Freeze at whatever passes the state-diagram tests. The Lua-vs-`WATCH`/`MULTI` choice (D-E) is settled and not worth relitigating mid-task |
| Phase exceeds 1.5× budget (15h) | Apply the roadmap §4 descope ladder. Hedged requests (FR-4.7) were never in this phase's scope to begin with, so the next real cut is per-class preference variation (FR-4.5) — nothing built here depends on it |
| Tempted to build hedging (§5.8) "while the router is already open" | Don't. FR-4.7/4.8 are Phase 4. The loop this phase adds is sequential failover; hedging is concurrent dispatch — the two share a seam, not an implementation |
| Tempted to build the Phase 5 queue so deferrable classes get something other than a direct error | Don't. D-D deliberately keeps deferrable and interactive classes on the same status-code path until Phase 5 lands the real `DEGRADE -> QUEUE` branch |
| Cohere spend approaches €10 across all phases to date | Move all load generation to `mock_chaos` — the breaker's own trip conditions are far easier to script deterministically against the mock than against a live provider anyway |

---

## 5. Verification

**Every task:**

```bash
ruff check . && mypy --strict keel scripts && pytest
```

The full suite must run with no network and no Redis (NFR-2): `fakeredis` for breaker and health tests, `ManualClock` for anything time-dependent, a scripted `RandomSource` for probe-admission tests.

**M3 exit:**

`P3-T6`'s scripted scenario remains the **authoritative** proof — it runs under `ManualClock`
with no real waiting, so it is deterministic and runs in CI on every push:

```bash
pytest                          # includes P3-T6's end-to-end state-machine scenario
```

The *demonstrable* proof is now a real scripted run, which it could not be when this document
was written: P2-T6 landed the compose stack, the provisioned board, `scripts/loadgen.py`, and
the env-gated chaos endpoint (**ADR 0010**). The whole M3 exit criterion can be driven live:

```bash
docker compose up -d

# 1. Healthy. Breaker closed, one attempt per request.
python scripts/loadgen.py --rps 20 --duration 60 --error-rate 0.0

# 2. Degrade the provider under load, without restarting anything.
curl -XPOST localhost:8080/chaos/mock_chaos \
  -H 'Content-Type: application/json' -d '{"error_rate": 1.0}'
#    -> error rate climbs, the breaker trips, traffic reroutes,
#       X-Keel-Attempts goes above 1, and the circuit-state panel steps 0 -> 2

# 3. Recover it, and touch nothing else.
curl -XPOST localhost:8080/chaos/mock_chaos \
  -H 'Content-Type: application/json' -d '{"error_rate": 0.0}'
#    -> cooldown expires, half-open probes are admitted, N successes close it
#       unattended, and the panel steps 2 -> 1 -> 0
```

Open Grafana at `localhost:3000` (anonymous viewer, no login) and watch the circuit-state
timeline render the full shape — trip, flat open period, half-open probes, close. That shape
is the roadmap's stated M3 exit criterion, and it is also a dry run for the FR-7.4 demo video,
which is the same sequence recorded.

Confirm the three metrics this phase produces for the first time have real series rather than
primed zeros:

```bash
curl -s localhost:8080/metrics | grep -E \
  'keel_breaker_state|keel_breaker_transitions_total|keel_failover_events_total'
```

`keel_breaker_state` is primed at `0` for every provider from startup (P2-T4's "declaring is
not enough" rule); the two counters are **absent until something has actually transitioned or
failed over**, so their appearance is itself evidence rather than decoration.

Finally, the logs should tell the same story from one `request_id` (§6.1): a failed-over
request emits two `provider_attempt` lines with `attempt` 1 and 2, naming different providers,
and one `request_completed`.

```bash
docker compose logs keel-gateway | grep '"event": "provider_attempt"'
```

**Carried over from P2-T6, and this is the phase to clear it.** The compose stack has been
written and statically asserted but **never actually run** — the machine it was built on has no
Docker. Before P3-T1, bring it up once: it validates the untested build, healthchecks, and
`depends_on` ordering while the change is still fresh, and it captures the two numbers P2-T6
could not — **S8**, the cold start from `git clone` against the five-minute target, and **S5
with Redis up**, still unmeasured. Both P2-T4 and P2-T6 measured ~150 ms of overhead with Redis
*down*; single-digit milliseconds is the expectation once it is up. Record the real numbers in
P2-T6's card, whatever they are.
