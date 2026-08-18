# Build Plan — Phases 1–2 (Milestones M1 and M2)

**Owner:** Jamil
**Status:** Draft v1.0
**Last updated:** 2026-08-18
**Related documents:** `PRD.md`, `TECHNICAL-DESIGN.md`, `ROADMAP.md`

---

## 0. Where the repo is, and what this covers

Phase 0 / M0 is complete apart from three carry-over items. In place today: the three design documents, `keel/config.py` with the full pydantic schema and cross-reference validation, `tests/test_config.py` mutating the shipped `config/keel.yaml`, `pyproject.toml` with the dependency set chosen, and empty package directories.

Not yet built: FastAPI ingress, provider adapters, router, health tracking, metrics, the compose stack.

This document covers roadmap **Phases 1–2 (Milestones M1 and M2)**, ~21h at ~10h/week. Ordering is fixed by design principle 2 and FR-3.4: **observe before you react.** No breaker, no capability filter, no failover here — those are Phase 3.

Three Phase 0 exit items never landed and are folded in, sized at ~1.5h total:

| # | Carry-over | Where it lands |
|---|---|---|
| C1 | `.env.example` (`.gitignore` already references it) | P1-T1 |
| C2 | CI running the test suite (`.github/workflows/ci.yml`) | P1-T1 |
| C3 | `docker compose up` starts a stack | P2-T6 — deferred from P0, since nothing existed to compose until Redis and Prometheus do |

---

## 1. Decisions to make before code

Three questions the design documents leave underspecified. Each is answered here; the first is worth an ADR.

**D-A — The mock provider is an in-process adapter, not a `:9001` container.**
Technical design §8 draws `mock-provider:9001` as a compose service, but §5.3 and roadmap Phase 1 both describe an *adapter*. Decide for the adapter, and drop the container from the diagram. NFR-2 wants it callable from unit tests with no network; the Phase 6 chaos API (FR-7.2) mutates gateway-side state either way; and one fewer container helps the S8 five-minute cold start. The cost: the mock cannot simulate connection-level failures — DNS, TCP reset, half-open sockets. Record that cost in the ADR.
→ Write `docs/adr/0001-mock-provider-is-an-in-process-adapter.md` and update technical design §8's deployment diagram.

**D-B — `Clock` lives at `keel/clock.py`, top level.**
Absent from the §8 repo layout because it did not exist yet. It is consumed by health, breaker, executor, and queue, so it belongs to none of them.

**D-C — Health recording happens in the executor, not the adapter.**
Adapters return a `ProviderResult` and know nothing about Redis. The executor is the single place that records outcome, latency, and taxonomy. This keeps the mock adapter honest, and gives Phase 3's breaker exactly one call site to read from.

---

## 2. Phase 1 — Ingress and routing seam (M1)

**Exit criterion:** an identical request is served by Cohere or by a mock depending only on config; requests missing metadata are rejected with a machine-readable 400; the test suite runs with no network access.

### P1-T1 — Foundations: clock, CI, env example
**1.0h · NFR-2, C1, C2**

- `keel/clock.py` — `Clock` protocol (`now() -> float`, `async sleep(seconds)`), `SystemClock`, and `ManualClock` with an explicit `advance(seconds)`. No test may call `time.sleep`.
- `.env.example` — `COHERE_API_KEY`, `KEEL_CONFIG_PATH`, `REDIS_URL`. Azure and Bedrock placeholders commented out until access lands.
- `.github/workflows/ci.yml` — `ruff check`, `mypy --strict`, `pytest`. No provider credentials in CI; the real-provider suite is marker-excluded from the start (`-m "not real_provider"`), before there is anything to exclude, so it is never a retrofit.

**Done when:** `ManualClock` unit tests pass and CI is green on the existing `test_config.py`.

### P1-T2 — Request envelope and header validation
**1.5h · FR-1.2, FR-1.3, FR-1.4, FR-1.5**

- `keel/api/envelope.py` — `RequestEnvelope` exactly as technical design §5.1: `request_id`, `tenant`, `feature`, `request_class`, `capabilities: frozenset[str]`, `deferrable`, `idempotency_key`, `payload`, `received_at` (from the injected clock, never `time.time()`).
- Parse from the `X-Keel-*` headers in §5.1. `request_class` must exist in `KeelConfig.request_classes`; `deferrable` is **derived from that class's config, not taken from the client**. `X-Keel-Idempotency-Key` is required when the class is deferrable.
- `keel/api/errors.py` — one machine-readable error body used by every 4xx/5xx, listing the offending fields. Collect *all* missing fields in one response, not the first — a client fixing headers one round-trip at a time is a bad first impression of the gateway.

**Done when:** table-driven tests cover every required header missing individually and in combination, an unknown `request_class`, and a deferrable class with no idempotency key.

### P1-T3 — Provider adapter protocol and error taxonomy skeleton
**1.0h · FR-2.4**

- `keel/providers/base.py` — `ProviderAdapter` protocol per §5.3, plus `ProviderResult` carrying the normalized response, `prompt_tokens` / `completion_tokens`, `latency_ms`, and either success or a `NormalizedError`.
- `keel/providers/errors.py` — `ErrorClass` enum with the seven §5.4 classes and a `counts_toward_breaker` property. `AUTH_FAILURE`, `CONTENT_FILTER`, and `BAD_REQUEST` return `False` (D7). Per-provider *mapping* is deliberately deferred to P2-T1; this task fixes only the taxonomy.

**Done when:** a test asserts the exact §5.4 truth table for `counts_toward_breaker`, so a later edit to that table fails loudly rather than quietly changing when breakers trip.

### P1-T4 — Mock adapter
**2.0h — hard cap · FR-2.2, NFR-2**

Per D-A, in-process. Scope is capped at four things and nothing else: **latency distribution, error rate, error class, seeded RNG.** State lives in a mutable `MockChaosState` object that the Phase 6 chaos API will mutate.

- Seeded RNG, so a test can assert an exact outcome sequence.
- Injected latency goes through `Clock.sleep`, so tests advance time rather than wait.
- Returns a plausible OpenAI-shaped completion with token counts, so the Phase 4 cost engine has real numbers to work with.

> **Scope tripwire** (roadmap §6; top-ranked PRD risk). If this exceeds 2.0h, freeze it at whatever exists and move on. No capability simulation, no streaming, no per-tenant behaviour.

**Done when:** `error_rate=0.5` with a fixed seed produces a deterministic, asserted pass/fail sequence, and injected latency advances `ManualClock` without real waiting.

### P1-T5 — Cohere adapter and provider registry
**2.0h · FR-2.1**

- `keel/providers/cohere.py` — wraps LiteLLM per D4. The adapter layer sits *above* the library, not in place of it.
- Error mapping is best-effort here; it gets real fixtures in P2-T1.
- `keel/providers/registry.py` — build the adapter set from `KeelConfig.providers`, dispatching on `AdapterName`. An unknown adapter is impossible by construction (the enum is closed), but a **missing credential must fail at startup, not at first request** (NFR-4).

**Done when:** an offline test builds the registry from the shipped `config/keel.yaml` and asserts each provider maps to the right adapter class. Any live Cohere call is a `real_provider`-marked test excluded from CI.

### P1-T6 — Static router and executor
**1.0h**

- `keel/routing/router.py` — resolve the request class's `preference` list into an ordered candidate list. **No health awareness, no capability filter, no breaker gate.** Name those slots in the code with a comment pointing at Phase 3, so the seam is visible to the next reader rather than invented later.
- `keel/routing/executor.py` — invoke candidate 1, apply `timeout_ms`, return the `ProviderResult`. This is the single call site P2-T2 hooks health recording into.

**Done when:** table-driven tests over `(request_class) → ordered candidates` for all three shipped classes.

### P1-T7 — FastAPI app and the ingress endpoint
**1.5h · FR-1.1**

- `keel/api/app.py` — app factory with a lifespan that calls `load_config()` once and fails the process on `ConfigError` (NFR-4). Config, clock, and registry live on app state.
- `POST /v1/chat/completions` — OpenAI-compatible, so adoption is a base-URL and key change.
- `GET /healthz` — liveness, for the compose healthcheck in P2-T6.
- Response headers `X-Keel-Provider` and `X-Keel-Attempts` (§4). `X-Keel-Cost-Micros` waits for Phase 4.

**Done when:** a `TestClient` smoke test sends the same body against two configs — one preferring `mock_chaos`, one preferring `cohere_primary` with a stubbed adapter — and gets the same response shape with a different `X-Keel-Provider`. **This is the M1 exit criterion.**

**Phase 1 total: 10.0h.**

---

## 3. Phase 2 — Health tracking and observability (M2)

**Exit criterion:** drive load through mocks with a configured error rate and watch success rate, latency percentiles, and error taxonomy move on a Grafana board that was provisioned from version control, not clicked together.

### P2-T1 — Error normalization, per provider
**2.0h · FR-2.4**

- Fill in `keel/providers/errors.py`: map each adapter's raw failures onto the §5.4 taxonomy. HTTP 429, `ThrottlingException`, and `RateLimitError` all become `RATE_LIMIT` — the breaker must not see one concept as three.
- `tests/fixtures/providers/{cohere,azure,bedrock}/*.json` — captured error responses replayed offline (§7). Cohere fixtures can be captured live now. Azure and Bedrock fixtures are hand-written from published error shapes and re-captured in Phase 4 when access lands; **mark the hand-written ones as unverified inside the fixture file itself**, not in a commit message nobody reads.
- Unmapped provider exceptions default to `SERVER_ERROR` and emit a `WARNING` naming the exception type, so mapping gaps surface instead of hiding inside a catch-all.

**Done when:** every fixture replays to its expected `ErrorClass` in a parametrized test.

### P2-T2 — Redis bucketed sliding window
**2.0h · FR-3.1, FR-3.2, D3**

- `keel/health/window.py` — key schema exactly as §5.5: `keel:health:{provider}:{bucket_epoch}` as a HASH of `ok` plus one field per error class, TTL 2× window.
- Bucket index derived from the injected clock and `breaker.bucket_seconds`. A read merges the last `window_seconds / bucket_seconds` buckets — 12 with the shipped config. `config.py` already guarantees the window divides evenly into buckets, so no partial edge bucket exists to skew the rate.
- Writes are one pipelined `HINCRBY` + `EXPIRE`: O(1) per request, bounded memory.
- Hook the recording call into `keel/routing/executor.py` from P1-T6 (D-C). Recording must not block the response path.

**Done when:** `fakeredis` + `ManualClock` tests show counts entering the current bucket, the window rolling as time advances, and old buckets falling out of the merged view. Assert the 5s edge staleness explicitly — it is a documented property (D3), not a bug, and a test that pins it stops someone "fixing" it later.

### P2-T3 — Latency reservoir and percentiles
**1.5h · FR-3.1**

- `keel/health/latency.py` — capped reservoir per bucket at `keel:latency:{provider}:{bucket_epoch}` (LIST, `LPUSH` + `LTRIM` to 200, TTL 2× window).
- `keel/health/snapshot.py` — `ProviderHealth` carrying success rate, error counts by class, and p50/p95/p99 computed in-process across merged buckets.
- These percentiles are approximate under load. That is acceptable because they drive a threshold comparison in Phase 3, not a reported SLA. **The README's latency numbers come from the Prometheus histograms in P2-T4, not from here** — say so in the module docstring, so the two never get confused by someone looking for a number to quote.

**Done when:** a known sample distribution yields the expected percentiles, `LTRIM` caps at 200 under 10k writes, and an empty window returns `None` rather than `0.0` — a provider with no traffic is *unknown*, not perfect.

### P2-T4 — Prometheus exporter
**2.0h · FR-3.3**

- `keel/observability/metrics.py` — the **full §6 catalogue**, defined now even where the producer arrives later. `keel_breaker_state`, `keel_breaker_transitions_total`, and `keel_failover_events_total` are declared in Phase 2 and sit at zero until Phase 3; `keel_queue_*` until Phase 5; `keel_cost_micros_total` until Phase 4. Declaring them now means the Grafana panels in P2-T6 exist and are simply flat, rather than being built twice.
- `keel/observability/middleware.py` — `keel_gateway_overhead_seconds`, measured as wall clock minus provider time. It is a separate metric from total duration specifically so S5 can be measured rather than asserted (§6).
- `GET /metrics` on the gateway.

**Done when:** every metric in the §6 table appears in `/metrics` with the exact labels from that table — one test that iterates the catalogue, so a typo in a label name fails CI instead of quietly breaking a dashboard query.

### P2-T5 — Structured logging
**1.0h · FR-7.3**

- `keel/observability/logging.py` — structlog, JSON to stdout, `request_id` bound once at ingress and carried through every attempt via contextvars.
- Every line for a request carries `request_id`, `tenant`, `feature`, `request_class`, `provider`, `attempt`, `outcome`, `error_class`.
- **Never log request or response bodies.** There is no PII redaction in v1 (PRD §8 stretch), so the safe default is to log nothing from the payload at all.

**Done when:** a two-attempt request emits lines sharing one `request_id`, asserted by capturing structlog output.

### P2-T6 — Compose stack, Prometheus, provisioned Grafana
**2.5h · NFR-1, S8, C3**

Sized 1.0h above the roadmap's 1.5h: it absorbs the Phase 0 compose carry-over (C3) and the load driver the M2 exit criterion needs but the roadmap never budgeted.

- `deploy/Dockerfile` — gateway image.
- `deploy/docker-compose.yml` — `keel-gateway:8080`, `redis:7`, `prometheus:9090`, `grafana:3000`. **No `mock-provider` container** (D-A). No `keel-worker` — that is Phase 5. Healthchecks on every service with `depends_on: condition: service_healthy`, so "up" means ready rather than started.
- `deploy/prometheus/prometheus.yml` — scrape the gateway. Scrape interval ≤ 5s, matching the bucket width; a slower interval makes the dashboard lag the health view and the demo look sluggish.
- `deploy/grafana/provisioning/datasources/prometheus.yml` and `.../dashboards/keel.yml` — datasource and dashboard provider as version-controlled files.
- `deploy/grafana/dashboards/keel-health.json` — the four panels Phase 2 can actually fill: **RPS by provider · error rate by normalized class · p95 latency by provider · gateway overhead.** Anonymous viewer access on, so a reviewer is not stopped by a login prompt (S8).
- `scripts/loadgen.py` — small async driver sending tagged traffic through the gateway at a set RPS against `mock_chaos`, with a flag to change the mock's error rate mid-run. This is what makes the M2 exit demonstrable, and the Phase 6 measurement run reuses it rather than reinventing it.

The remaining three panels — circuit state timeline, failover annotations, queue depth and cost — land in Phase 6 with the full seven-panel board.

**Done when:** on a clean machine, `docker compose up` → `python scripts/loadgen.py --rps 20 --error-rate 0.4` → all four panels move within 30 seconds, and the whole path from clone to moving dashboard is under five minutes. **This is the M2 exit criterion.**

**Phase 2 total: 11.0h** — 1.0h over the roadmap's 10.0h, entirely the P2-T6 carry-over.

---

## 4. Documentation updates as work lands

Not a separate phase. Each of these ships in the same commit as the code that makes it true.

| Change | Trigger |
|---|---|
| `docs/adr/0001-mock-provider-is-an-in-process-adapter.md` | Before P1-T4 |
| Technical design §8 — remove `mock-provider:9001` from the deployment diagram | With ADR 0001 |
| Technical design §8 repo layout — add `keel/clock.py`, `scripts/` | End of Phase 1 |
| README "Status" placeholder → what works today | End of each phase |
| README "Quickstart" placeholder → real commands | P2-T6 |

The README quickstart placeholder already says to fill it in during Phase 2 — P2-T6 is where that happens. Leave the measured-results table alone: it is filled from the Phase 6 run, with real numbers or not at all.

---

## 5. Tripwires for these two phases

| Tripwire | Response |
|---|---|
| Mock adapter (P1-T4) exceeds 2.0h | Freeze at current capability immediately. Top-ranked PRD risk |
| Either phase exceeds 1.5× budget (15h / 16.5h) | Apply the roadmap §4 descope ladder rather than extending the phase |
| Tempted to add the breaker "while I'm in here" during P2-T2 | Do not. FR-3.4 exists because a breaker with invisible inputs is guesswork |
| Cohere spend approaches €10 across Phases 1–2 | Move all load generation to `mock_chaos` — the load driver defaults there anyway |

---

## 6. Verification

**Every task:**

```bash
ruff check . && mypy --strict keel && pytest
```

The full suite must run with no network and no Redis (NFR-2): `fakeredis` for health tests, `ManualClock` for anything time-dependent, `real_provider`-marked tests excluded.

**M1 exit — end of Phase 1:**

```bash
uvicorn keel.api.app:app --port 8080

curl -X POST localhost:8080/v1/chat/completions \
  -H 'X-Keel-Tenant: acme' \
  -H 'X-Keel-Feature: support-summary' \
  -H 'X-Keel-Request-Id: 00000000-0000-0000-0000-000000000001' \
  -H 'X-Keel-Class: interactive_chat' \
  -H 'Content-Type: application/json' \
  -d '{"model":"keel","messages":[{"role":"user","content":"hi"}]}' -i
```

Expect `200` with an `X-Keel-Provider` header. Drop any single `X-Keel-*` header and expect `400` naming the missing field. Then flip `interactive_chat.preference[0]` between `cohere_primary` and `mock_chaos` in `config/keel.yaml`, restart, and re-run: same response shape, different `X-Keel-Provider`. That config flip *is* the milestone.

**M2 exit — end of Phase 2:**

```bash
docker compose -f deploy/docker-compose.yml up -d
python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.0
python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.4 --latency-ms 3000
```

Open Grafana at `localhost:3000`. The error-rate panel must climb to roughly 40%, split across taxonomy classes, and the p95 panel must climb toward 3s — both within about one window (60s). Confirm `curl localhost:8080/metrics` lists every metric from technical design §6.

Finally, time a cold start from `git clone` on a clean machine against the S8 five-minute target. If it misses, record the real number. An honest 7 minutes is worth more than a restated target.
