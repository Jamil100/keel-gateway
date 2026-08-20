# Build Plan — Phases 1–2 (Milestones M1 and M2)

**Owner:** Jamil
**Status:** Draft v1.0 — in progress
**Last updated:** 2026-08-19
**Related documents:** `PRD.md`, `TECHNICAL-DESIGN.md`, `ROADMAP.md`

**Progress:** 27.25h of 27.25h. ✅ P1-T1 … P1-T7 — **Phase 1 complete, M1 met** · ✅ P2-T1 … P2-T6 — **Phase 2 complete**. The M2 exit is met on every part that does not need Docker; the stack itself is written and statically checked but **unrun** — see P2-T6. Next up: **Phase 3 — the circuit breaker and capability-aware failover.**
Completed work is struck through and marked ✅ DONE. Unmarked items are outstanding.

---

## 0. Where the repo is, and what this covers

Phase 0 / M0 is complete apart from three carry-over items — ~~C1~~ and ~~C2~~ have since landed with P1-T1; only C3 remains. In place today: the three design documents, `keel/config.py` with the full pydantic schema and cross-reference validation, `tests/test_config.py` mutating the shipped `config/keel.yaml`, `pyproject.toml` with the dependency set chosen, empty package directories, and — as of P1-T1 — `keel/clock.py`, `.env.example`, and `.github/workflows/ci.yml`. P1-T2 adds `keel/api/envelope.py` and `keel/api/errors.py`; P1-T3 adds `keel/providers/base.py` and `keel/providers/errors.py`; P1-T4 adds `keel/providers/mock.py`; P1-T5 adds `keel/providers/cohere.py`, `keel/providers/credentials.py`, and `keel/providers/registry.py`; P1-T6 adds `keel/routing/router.py` and `keel/routing/executor.py`; P1-T7 adds `keel/api/app.py` and the `UpstreamError` family in `keel/api/errors.py`. P2-T1 adds `keel/providers/normalize.py`, `tests/fixtures/providers/`, and `scripts/capture_error_fixtures.py`; P2-T2 adds `keel/health/window.py` and `keel/redis.py`, and wires the recording call into `keel/routing/executor.py`; P2-T3 adds `keel/health/latency.py` and `keel/health/snapshot.py`, and renames `HealthWindow` to `HealthTracker` now that it records both; P2-T4 adds `keel/observability/metrics.py` and `keel/observability/middleware.py`, and the `GET /metrics` endpoint; P2-T5 adds `keel/observability/logging.py`, binds the correlation context in `keel/api/app.py`, and adds the per-attempt line to `keel/routing/executor.py`; P2-T6 adds `docker-compose.yml`, `.dockerignore`, `deploy/` (Dockerfile, `keel.demo.yaml`, Prometheus, Grafana provisioning and the board), `scripts/loadgen.py`, and the env-gated `POST /chaos/{provider}` in `keel/api/app.py`.

Everything Phase 2 scopes is now built. FR-3.1, FR-3.3, and FR-7.3 are complete as of P2-T5 — outcomes, error classes, and latencies are recorded to Redis *and* exported on `/metrics`, so the health data is finally **visible** rather than merely stored. The breaker still waits for Phase 3, and **the FR-3.4 precondition it was waiting on is now fully met**: health is recorded, exported, correlated in logs, and visible on a provisioned board. Phase 3 can be written against inputs somebody can watch.

This document covers roadmap **Phases 1–2 (Milestones M1 and M2)**, ~22.75h at ~10h/week. Ordering is fixed by design principle 2 and FR-3.4: **observe before you react.** No breaker, no capability filter, no failover here — those are Phase 3.

Three Phase 0 exit items never landed and are folded in, sized at ~1.5h total:

| # | Carry-over | Where it lands |
|---|---|---|
| ~~C1~~ | ~~`.env.example` (`.gitignore` already references it)~~ | P1-T1 — ✅ **DONE** |
| ~~C2~~ | ~~CI running the test suite (`.github/workflows/ci.yml`)~~ | P1-T1 — ✅ **DONE** |
| ~~C3~~ | ~~`docker compose up` starts a stack~~ | P2-T6 — ✅ **DONE**, four services with healthchecks. Written and asserted as files; not yet run on a machine with Docker |

---

## 1. Decisions to make before code

Three questions the design documents leave underspecified. Each is answered here; the first is worth an ADR.

**D-A — The mock provider is an in-process adapter, not a `:9001` container.**
Technical design §8 draws `mock-provider:9001` as a compose service, but §5.3 and roadmap Phase 1 both describe an *adapter*. Decide for the adapter, and drop the container from the diagram. NFR-2 wants it callable from unit tests with no network; the Phase 6 chaos API (FR-7.2) mutates gateway-side state either way; and one fewer container helps the S8 five-minute cold start. The cost: the mock cannot simulate connection-level failures — DNS, TCP reset, half-open sockets. Record that cost in the ADR.
→ ~~Write `docs/adr/0002-...md` and update technical design §8's deployment diagram.~~ ✅ **Done with P1-T4** — ADR 0002 accepted; the `mock-provider:9001` node and its `GW --> MK` edge are both gone from §8.

**D-B — `Clock` lives at `keel/clock.py`, top level.**
Absent from the §8 repo layout because it did not exist yet. It is consumed by health, breaker, executor, and queue, so it belongs to none of them.

**D-C — Health recording happens in the executor, not the adapter.**
Adapters return a `ProviderResult` and know nothing about Redis. The executor is the single place that records outcome, latency, and taxonomy. This keeps the mock adapter honest, and gives Phase 3's breaker exactly one call site to read from.

---

## 2. Phase 1 — Ingress and routing seam (M1)

**Exit criterion:** an identical request is served by Cohere or by a mock depending only on config; requests missing metadata are rejected with a machine-readable 400; the test suite runs with no network access.

### ~~P1-T1 — Foundations: clock, CI, env example~~ ✅ **DONE**
**1.0h · NFR-2, C1, C2**

- ~~`keel/clock.py` — `Clock` protocol (`now() -> float`, `async sleep(seconds)`), `SystemClock`, and `ManualClock` with an explicit `advance(seconds)`. No test may call `time.sleep`.~~ ✅ `keel/clock.py` + `tests/test_clock.py`
- ~~`.env.example` — `COHERE_API_KEY`, `KEEL_CONFIG_PATH`, `REDIS_URL`. Azure and Bedrock placeholders commented out until access lands.~~ ✅
- ~~`.github/workflows/ci.yml` — `ruff check`, `mypy --strict`, `pytest`. No provider credentials in CI; the real-provider suite is marker-excluded from the start (`-m "not real_provider"`), before there is anything to exclude, so it is never a retrofit.~~ ✅ 3.11/3.12 matrix

**Done when:** ~~`ManualClock` unit tests pass and CI is green on the existing `test_config.py`.~~ ✅ Met — 36 tests. Local runs need `pip install -e ".[dev]"` first; without `pytest-asyncio` the seven async clock tests error out on collection.

### ~~P1-T2 — Request envelope and header validation~~ ✅ **DONE**
**2.25h (est. 1.5h) · FR-1.2, FR-1.3, FR-1.4, FR-1.5**

The 0.75h overrun is the request-body extension field. §5.1 calls for it as a fallback for clients that cannot set headers, but never named it and this task bullet never listed it; building it now rather than leaving §5.1 half-implemented cost precedence rules, payload stripping, and a second test table. Phase 1 is now budgeted at 10.75h.

- ~~`keel/api/envelope.py` — `RequestEnvelope` exactly as technical design §5.1: `request_id`, `tenant`, `feature`, `request_class`, `capabilities: frozenset[str]`, `deferrable`, `idempotency_key`, `payload`, `received_at` (from the injected clock, never `time.time()`).~~ ✅
- ~~Parse from the `X-Keel-*` headers in §5.1. `request_class` must exist in `KeelConfig.request_classes`; `deferrable` is **derived from that class's config, not taken from the client**. `X-Keel-Idempotency-Key` is required when the class is deferrable.~~ ✅ Plus the `x_keel` body fallback: header wins on conflict, unknown keys rejected (so a client cannot claim `deferrable`), and the object is stripped from the provider-bound payload.
- ~~`keel/api/errors.py` — one machine-readable error body used by every 4xx/5xx, listing the offending fields. Collect *all* missing fields in one response, not the first — a client fixing headers one round-trip at a time is a bad first impression of the gateway.~~ ✅ OpenAI error envelope with a nested `keel` extension — **ADR 0003**.

**Done when:** ~~table-driven tests cover every required header missing individually and in combination, an unknown `request_class`, and a deferrable class with no idempotency key.~~ ✅ Met — 58 tests (94 total). One worth naming: an unknown class does **not** also demand an idempotency key, because we cannot know whether it is deferrable and a guessed second problem sends the client chasing a requirement that may not exist.

### ~~P1-T3 — Provider adapter protocol and error taxonomy skeleton~~ ✅ **DONE**
**1.0h · FR-2.4**

- ~~`keel/providers/base.py` — `ProviderAdapter` protocol per §5.3, plus `ProviderResult` carrying the normalized response, `prompt_tokens` / `completion_tokens`, `latency_ms`, and either success or a `NormalizedError`.~~ ✅ The sum type is enforced on the model — never both, never neither — and a provider failure is a *return value, not an exception*, so it cannot unwind past the executor that must record it (D-C).
- ~~`keel/providers/errors.py` — `ErrorClass` enum with the seven §5.4 classes and a `counts_toward_breaker` property. `AUTH_FAILURE`, `CONTENT_FILTER`, and `BAD_REQUEST` return `False` (D7). Per-provider *mapping* is deliberately deferred to P2-T1; this task fixes only the taxonomy.~~ ✅ Both §5.4 columns are implemented — `retry_elsewhere` alongside `counts_toward_breaker` — since they are one table and Phase 3's router needs the second.

**Done when:** ~~a test asserts the exact §5.4 truth table for `counts_toward_breaker`, so a later edit to that table fails loudly rather than quietly changing when breakers trip.~~ ✅ Met — 33 tests (127 total). The table is transcribed by hand in `tests/test_provider_errors.py` rather than read from the module, so the two must agree. A completeness guard also **refuses to import** if a class is added without a row, rather than raising `KeyError` inside the breaker during an incident (verified by mutation).

### ~~P1-T4 — Mock adapter~~ ✅ **DONE**
**2.0h — hard cap, met · FR-2.2, NFR-2**

Per D-A, in-process. Scope is capped at four things and nothing else: **latency distribution, error rate, error class, seeded RNG.** State lives in a mutable `MockChaosState` object that the Phase 6 chaos API will mutate.

- ~~Seeded RNG, so a test can assert an exact outcome sequence.~~ ✅ **Three** streams derived from one seed — pass/fail, error class, latency — so *which* calls fail depends on the seed and `error_rate` alone. A single stream would let a change to the class mix or latency shift the failure pattern, and the two M2 load runs could not be compared.
- ~~Injected latency goes through `Clock.sleep`, so tests advance time rather than wait.~~ ✅ Lognormal with `latency_ms` as the **median** and `latency_sigma` defaulting to `0.0` (fixed), so the M2 run's p95 lands exactly on the configured 3s. Raise sigma for the right tail Phase 3's p95-breach trigger needs.
- ~~Returns a plausible OpenAI-shaped completion with token counts, so the Phase 4 cost engine has real numbers to work with.~~ ✅ Token counts derive from payload size, so cost moves with input rather than being constant.
- **Beyond the bullet, and why:** `error_classes` is a weighted *mix*, not a single class. §5.3 says "error class" singular, but the M2 exit criterion requires the error-rate panel to split **across taxonomy classes** and `loadgen` has no flag to select one. One entry pins a single class, so the singular reading still works. `AUTH_FAILURE` and `BAD_REQUEST` are excluded from the default mix — a *provider* cannot cause either — while `CONTENT_FILTER` is included precisely because it does not count toward the breaker (D7).

> **Scope tripwire** (roadmap §6; top-ranked PRD risk). If this exceeds 2.0h, freeze it at whatever exists and move on. No capability simulation, no streaming, no per-tenant behaviour.

**Done when:** ~~`error_rate=0.5` with a fixed seed produces a deterministic, asserted pass/fail sequence, and injected latency advances `ManualClock` without real waiting.~~ ✅ Met — 34 tests (161 total). The sequence is asserted as a literal string; 12 calls at `latency_ms=3000` advance `ManualClock` by 36s in 0.4ms of real time.

### ~~P1-T5 — Cohere adapter and provider registry~~ ✅ **DONE**
**3.0h (est. 2.0h) · FR-2.1**

The 1.0h overrun is **ADR 0004** and everything it drags with it. The shipped `config/keel.yaml` declared `azure_fallback` and `bedrock_fallback`, which have neither an adapter nor credentials, so a registry that fails on what it cannot build could not start on the repo's own config. Trimming the config cost six re-anchored cases in `tests/test_config.py`, the ADR, and a §5.2 note. Phase 1 is now budgeted at 11.75h.

- ~~`keel/providers/cohere.py` — wraps LiteLLM per D4. The adapter layer sits *above* the library, not in place of it.~~ ✅ Plus `keel/providers/credentials.py` — a `pydantic-settings` model that treats a **blank** `COHERE_API_KEY` as absent, since `.env.example` ships it empty and "copied the template, forgot to fill it in" is the likeliest real misconfiguration.
- ~~Error mapping is best-effort here; it gets real fixtures in P2-T1.~~ ✅ Ordered isinstance table, most-specific-first. **The root of LiteLLM's exception tree is `openai.APIError`, not `litellm.APIError`** — LiteLLM's exceptions subclass the OpenAI SDK's, and its own `APIError` is a *sibling* of the rest, so a table using it as the catch-all matches almost nothing. Two orderings are load-bearing and pinned by tests: `Timeout` above `APIConnectionError` (§5.4 separates them because latency budgets do), and `ContentPolicyViolationError` above `BadRequestError` (or the D7 taxonomy split the M2 panel exists to show is wrong).
- ~~`keel/providers/registry.py` — build the adapter set from `KeelConfig.providers`, dispatching on `AdapterName`. An unknown adapter is impossible by construction (the enum is closed), but a **missing credential must fail at startup, not at first request** (NFR-4).~~ ✅ `assert_never` on the match, so a fifth `AdapterName` is a type error rather than a runtime surprise. Every problem is reported at once, not the first.
- **Beyond the bullet, and why:** the NFR-4 requirement is sharper than it reads. A missing credential surfaces at request time as `AUTH_FAILURE`, which **D7 excludes from the breaker** — so a lazily-built registry would fail 100% of traffic with every breaker closed and every dashboard green. That argument is what settled ADR 0004 against the cheaper "skip with a warning" option.

**Done when:** ~~an offline test builds the registry from the shipped `config/keel.yaml` and asserts each provider maps to the right adapter class. Any live Cohere call is a `real_provider`-marked test excluded from CI.~~ ✅ Met — 74 tests (235 total, 1 skipped). Test-suite runtime went 0.9s → 5.2s: `import litellm` alone costs 4.5s, which is also why the adapter imports it lazily rather than at module scope. The error-mapping tests build **real** LiteLLM exceptions rather than stand-ins, which is what caught the wrong-root bug above.

### ~~P1-T6 — Static router and executor~~ ✅ **DONE**
**1.0h — met**

- ~~`keel/routing/router.py` — resolve the request class's `preference` list into an ordered candidate list. **No health awareness, no capability filter, no breaker gate.** Name those slots in the code with a comment pointing at Phase 3, so the seam is visible to the next reader rather than invented later.~~ ✅ Three seams named in §5.7's order, capability filter **first** per D2. Returns a `tuple`, not the config's `list`: `KeelConfig` is frozen but pydantic does not freeze the list inside it, so handing it out would let a caller rewrite routing policy through the return value.
- ~~`keel/routing/executor.py` — invoke candidate 1, apply `timeout_ms`, return the `ProviderResult`. This is the single call site P2-T2 hooks health recording into.~~ ✅ `asyncio.wait_for` above the adapter's own timeout — the backstop `keel/providers/cohere.py` already pointed at. `timeout_ms: None` (the mock, per §5.2) means *no* gateway deadline and is awaited unwrapped, never `wait_for(None)`.
- **Composition decision:** the executor holds the router, so ingress makes one call. §4 draws them as separate participants and they stay separate objects — but Phase 3's failover loop and half-open re-gating both belong *inside* the executor, and the alternative would have straddled them across the ingress boundary P1-T7 is about to build.
- **The one place the injected clock does not reach.** `wait_for` measures event-loop time, and no amount of `Clock` gets it to measure anything else: `ManualClock.sleep` advances time and returns on the same tick, so racing an attempt against `clock.sleep(timeout)` finishes both together *and* double-advances the clock. So the four timeout tests use `SystemClock` and a fake adapter awaiting a real `asyncio.sleep(50ms)` against a 5ms deadline. NFR-2 still holds — nothing calls `time.sleep`, and the whole real-time cost is under 0.1s. Everything else in the file stays on `ManualClock`.
- **Beyond the bullet, and why:** on timeout the executor synthesizes the `ProviderResult` the adapter never got to return, classed `TIMEOUT` and tagged `provider_error_type="KeelExecutorTimeout"`. `wait_for` cancels the attempt, and both adapters use `except Exception` precisely so `CancelledError` passes through — so nothing below has described the failure and the executor must. The tag exists because a provider-side timeout arrives as LiteLLM's `Timeout` carrying *that* name, and "they were slow" versus "we were impatient" has to be answerable from one log line.

**Done when:** ~~table-driven tests over `(request_class) → ordered candidates` for all three shipped classes.~~ ✅ Met — 32 tests (267 total, 1 skipped), suite 5.7s. The expected candidate lists are transcribed by hand rather than read back from the config, so the two must agree — same posture as the §5.4 table. Two tests assert the *absence* of behaviour and are meant to fail in Phase 3 rather than survive it: a `citations`-tagged request still sees `mock_chaos`, which lacks that capability, and a `SERVER_ERROR` whose own `retry_elsewhere` is `True` is returned rather than failed over. Pinning those makes Phase 3 a visible test edit instead of a silent change of meaning.

### ~~P1-T7 — FastAPI app and the ingress endpoint~~ ✅ **DONE**
**2.0h (est. 1.5h) · FR-1.1**

The 0.5h overrun is **ADR 0006** — the status mapping the task bullets never mention. See below.

- ~~`keel/api/app.py` — app factory with a lifespan that calls `load_config()` once and fails the process on `ConfigError` (NFR-4). Config, clock, and registry live on app state.~~ ✅ One `AppContext` frozen dataclass under one state key, narrowed by `isinstance` in one accessor: `State.__getattr__` returns `Any`, so four keys would mean four unchecked reads per request. The lifespan has **no `try`** — a `ConfigError` escaping the ASGI startup event is what makes uvicorn exit non-zero, so NFR-4 is satisfied by writing no error handling at all.
- ~~`POST /v1/chat/completions` — OpenAI-compatible, so adoption is a base-URL and key change.~~ ✅ No pydantic body model — the payload is opaque pass-through — and for the same reason the route is annotated `-> Response`. **FastAPI infers a `response_model` from the return annotation**, so `-> dict[str, Any]` would re-serialize the provider's body through a generated model and drop both unknown fields and the response headers. It reads as a tightening and is a regression; a test sends an unknown key through to pin it.
- ~~`GET /healthz` — liveness, for the compose healthcheck in P2-T6.~~ ✅ Touches nothing. The P2-T6 healthcheck *restarts the container* when it fails, so a probe that called a provider would turn a provider outage into a restart loop — the exact failure the gateway exists to absorb.
- ~~Response headers `X-Keel-Provider` and `X-Keel-Attempts` (§4). `X-Keel-Cost-Micros` waits for Phase 4.~~ ✅ Both are set on the **failure** path too — a 503 that will not say who it tried is useless in the demo those headers exist for. `X-Keel-Attempts` is a named constant, not a new field on the frozen per-attempt `ProviderResult`; the executor's existing seam comment is where the real count begins in Phase 3.
- **Beyond the bullet, and why (1):** `keel/api/app.py` is the first code in the repo to honour `KEEL_CONFIG_PATH`. It shipped in `.env.example` from P1-T1 and nothing read it — `ProviderCredentials` explicitly ignores it. Precedence is explicit argument → env → default, with a blank value treated as absent for the same reason a blank `COHERE_API_KEY` is.
- **Beyond the bullet, and why (2):** a `ProviderResult` carrying an error is not an HTTP response, and nothing said what status it becomes. §4's literal answer is 503, but that tells an OpenAI-SDK client to retry a `CONTENT_FILTER` rejection forever — and FR-1.1's whole premise is clients arriving with retry policies already written. The status is looked up from the `ErrorClass` instead (**ADR 0006**), and the split is not arbitrary: **the two classes that map to 400 are exactly the two D7 keeps out of the breaker**, because "not evidence about provider health" and "not worth retrying" are the same fact read twice.
- **Beyond the bullet, and why (3):** `stream: true` is rejected with a 400 naming FR-1.6 rather than passed through, since the alternative is handing it to an adapter with no streaming path and returning one non-streamed body to a client parsing for SSE. Checked *after* `build_envelope`, so FR-1.3's report-everything-at-once promise is untouched.

**Done when:** ~~a `TestClient` smoke test sends the same body against two configs — one preferring `mock_chaos`, one preferring `cohere_primary` with a stubbed adapter — and gets the same response shape with a different `X-Keel-Provider`.~~ ✅ **Met — M1 exit criterion cleared.** 51 tests (318 total, 1 deselected), suite 7.0s. The registry in those tests is built by the *real* `build_registry` so `mock_chaos` is a genuine `MockAdapter` and the ADR 0004 check runs; only the Cohere entry is swapped. Two tests are meant to be edited in Phase 3 rather than survive it — the attempts header pinned at `1`, and the absent `X-Keel-Cost-Micros` — and one asserts the route table is exactly two endpoints, so `/metrics` (P2-T4) and `/chaos` (Phase 6) cannot arrive unnoticed.

**Phase 1 total: 12.25h** — 2.25h over: 0.75h the P1-T2 body extension, 1.0h ADR 0004 and the config trim in P1-T5, 0.5h ADR 0006. Under the 15h tripwire.

---

## 3. Phase 2 — Health tracking and observability (M2)

**Exit criterion:** drive load through mocks with a configured error rate and watch success rate, latency percentiles, and error taxonomy move on a Grafana board that was provisioned from version control, not clicked together.

### ~~P2-T1 — Error normalization, per provider~~ ✅ **DONE**
**2.5h (est. 2.0h) · FR-2.4**

The 0.5h overrun is **ADR 0007** and the live-capture script the task bullets never mention. See below.

- ~~Fill in `keel/providers/errors.py`: map each adapter's raw failures onto the §5.4 taxonomy. HTTP 429, `ThrottlingException`, and `RateLimitError` all become `RATE_LIMIT` — the breaker must not see one concept as three.~~ ✅ **but in `keel/providers/normalize.py`, not `errors.py`** — **ADR 0007**. `errors.py` fixes the *vocabulary* and is imported transitively by the health window, the metrics catalogue, and the breaker; none of those should have to read a table about LiteLLM's exception tree to find out what `RATE_LIMIT` means. It is untouched by this task. `keel/providers/cohere.py` keeps `normalize_litellm_error` as a thin delegate supplying the Cohere identity, so the adapter and its 200 lines of existing tests are unchanged.
- ~~`tests/fixtures/providers/{cohere,azure,bedrock}/*.json` — captured error responses replayed offline (§7) … **mark the hand-written ones as unverified inside the fixture file itself**.~~ ✅ 12 fixtures. `verified` is a **required** field cross-validated against `source`, so the two cannot disagree and neither can be forgotten — `extra="forbid"` plus a model validator means a hand-written fixture that omits it fails to parse. Discovery is a glob, so a new file is a new test case with no registration step; a separate non-parametrized guard asserts the corpus is non-empty and covers all three families, because `parametrize` over an empty list yields zero tests and a *green* suite.
- ~~Unmapped provider exceptions default to `SERVER_ERROR` and emit a `WARNING` naming the exception type.~~ ✅ Unchanged from P1-T5, but now warned **before** refinement, so a class that refinement rescues still reports the type-table gap rather than hiding it.
- ~~**One fixture is already known and worth capturing first** … Fixing it means matching on the wrapped message or status rather than the exception type alone.~~ ✅ Fixed, and the cause is worse than "LiteLLM does not raise `AuthenticationError`". `_map_cohere_exception` has **no 401 branch and no 429 branch**: a 401 satisfies its outer `hasattr(status_code)` guard, matches no inner arm, falls out of the chain without raising, and lands on `exception_type`'s generic `APIConnectionError`. **Matching on status is therefore impossible** — the wrapper carries a synthetic `500`, not the real `401` — so the rule matches on message alone.
- **Beyond the bullet, and why (1):** message matching is the classic way an error map rots, so it is gated rather than trusted. **A refinement rule may only replace `SERVER_ERROR`** and can never override a specifically-typed classification (**ADR 0007**). That turns the layer's whole safety argument into one property a test asserts over the corpus, instead of a judgement call per pattern — and it fixes the *direction* of failure: because a rule can only add information, a LiteLLM reword makes it stop firing and land back on today's `SERVER_ERROR`, never on a new misclassification.
- **Beyond the bullet, and why (2):** a real mapping gap found while pinning the table. `litellm.exceptions.OpenAIError` derives from `openai.OpenAIError`, which sits *above* `openai.APIError` — so it was the one member of `litellm.LITELLM_EXCEPTION_TYPES` an `APIError`-terminated table missed, falling to the unmapped default. A row was added, and a test now iterates LiteLLM's own published catalogue so the next addition fails CI rather than arriving as a production `WARNING`.
- **Beyond the bullet, and why (3):** `scripts/capture_error_fixtures.py`, run deliberately and never by CI. A green replay proves the mapping still turns *this* text into *that* class; it can never prove Cohere still emits that text, because fixtures are frozen strings. This is the only thing that checks the other direction, and the §5 tripwire is why it is a script rather than a test.
- **The known cost, recorded rather than hidden.** LiteLLM raises `ContentPolicyViolationError` only for bodies containing both `invalid_request_error` and `content_policy_violation`; Azure's `ResponsibleAIPolicyViolation` body contains neither, so an Azure content filter classes `BAD_REQUEST`. Correcting it needs a `BAD_REQUEST → CONTENT_FILTER` refinement, which the P2-T1 invariant forbids. D7 excludes both classes, so no breaker decision changes — only the M2 panel's label, for a provider with no adapter until Phase 4 (ADR 0004). The Azure fixture pins the **current** behaviour and says why, so widening the invariant fails that fixture and forces the decision into the open.

**Done when:** ~~every fixture replays to its expected `ErrorClass` in a parametrized test.~~ ✅ Met — 65 tests (383 total, 2 skipped), suite 5.7s → 7.9s. Verified end-to-end through the real HTTP stack: a bad key now returns `error.keel.error_class: "auth_failure"` where it returned `"server_error"`, with the status still 503 (ADR 0006 maps status from the class, and both classes land there) and the message still naming `APIConnectionError`, so the raw cause stays diagnosable from one line.

### ~~P2-T2 — Redis bucketed sliding window~~ ✅ **DONE**
**2.75h (est. 2.0h) · FR-3.1, FR-3.2, D3**

The 0.75h overrun is **ADR 0008** and `keel/redis.py`, neither of which the task bullets mention. This is the first task to open a Redis connection, so the connection posture had to be decided before any of the four bullets could be written — see below.

- ~~`keel/health/window.py` — key schema exactly as §5.5: `keel:health:{provider}:{bucket_epoch}` as a HASH of `ok` plus one field per error class, TTL 2× window.~~ ✅ **All seven classes get a field, not just the four D7 counts toward the breaker.** §5.5 elided the list as `...` and this task had to settle it: the M2 error-rate panel must split *across taxonomy classes*, and `MockAdapter`'s default mix carries `CONTENT_FILTER` precisely because it does not trip a circuit. Deciding what counts is the breaker's job in Phase 3; a recorder that filtered would leave that panel unable to show the distinction it exists to show. §5.5 now names all eight fields rather than leaving each reader to re-decide.
- ~~Bucket index derived from the injected clock and `breaker.bucket_seconds`. A read merges the last `window_seconds / bucket_seconds` buckets — 12 with the shipped config.~~ ✅ Nothing hard-codes 5 s or 12; a test drives a non-shipped 30 s / 10 s geometry so a constant sneaking in fails rather than passing by coincidence. `config.py`'s divisibility validator is trusted rather than re-checked (NFR-4).
- ~~Writes are one pipelined `HINCRBY` + `EXPIRE`: O(1) per request, bounded memory.~~ ✅ Transactional, which the bullet does not say and which matters: with `transaction=False` a connection lost between the two leaves a counted key with **no TTL**, and a provider serving one request before going quiet strands it forever — the bounded-memory half of D3 quietly undone. **That flag is not test-covered and the code says so**: `fakeredis` cannot drop a connection mid-pipeline, so an atomicity test would pass either way. Confirmed by mutation, and recorded in `record`'s docstring rather than in a test that would prove nothing.
- ~~Hook the recording call into `keel/routing/executor.py` from P1-T6 (D-C). Recording must not block the response path.~~ ✅ One line, as that module's docstring promised. Both branches are covered by it — a returned provider failure and the synthesized gateway timeout — and the timeout one matters most, since an attempt nobody waited for is exactly what Phase 3 needs counted.
- **Beyond the bullet, and why (1): ADR 0008.** Nothing in the PRD, the technical design, or ADRs 0001–0007 says what happens when Redis is unreachable, and this is the task that has to answer it. Two house postures disagree — NFR-4 and ADR 0004 say fail fast at startup, `/healthz` says absorb the failure — and they are reconciled by noticing the cases are not the same shape. A missing credential means the gateway **can never serve a request**; a missing Redis means it can serve **every** request and merely cannot remember how they went. So: no startup ping, a failed write logs a `WARNING` and returns, and a failed *read* returns `None` for **unknown** — never a zero-filled window, or a Redis outage would present every provider as flawless at the moment health data matters most. Phase 3's breaker must therefore hold on `None`, which is a constraint this task places on work not yet written.
- **Beyond the bullet, and why (2):** the guard lives *inside* `HealthWindow`, not around the call in the executor. That keeps the hook the single line D-C promised, and it means Phase 3's breaker — a second caller, written later, by someone reading a method signature rather than a call site — cannot forget to repeat a guard it never saw. Both methods are time-boxed at 250 ms so a Redis that accepts a command and then stops answering becomes a dropped write rather than a hung request; `CancelledError` is deliberately not caught, or the executor could no longer cancel an attempt.
- **The known cost, measured rather than assumed.** Verified end to end against a machine with no Redis: the gateway starts, `/healthz` and `POST /v1/chat/completions` both return 200 with `X-Keel-Provider` set, and each dropped write logs one `WARNING` naming the provider and field. But the request took **330 ms** — `redis-py` retries a refused connection rather than failing on the first `ECONNREFUSED`, so the 250 ms box was doing all the work and every failure arrived as a bare `TimeoutError` with an empty message. Socket deadlines below that box brought it to **~220 ms** and made the log line say `"Timeout connecting to server"`, which matters more, since until P2-T4 that line is the *only* evidence health data is being lost. It is still far past S5's 15 ms p95 budget: **"a Redis outage costs an observation, not a request" is true of correctness, not of latency.** The real fix is to stop asking a Redis that just refused us — which is a breaker over our own dependency, and §5's tripwire forbids building one during P2-T2. Recorded in ADR 0008 and left for P2-T4, where a dropped-write counter makes it visible enough to justify.
- **Beyond the bullet, and why (3):** `keel/redis.py`, top level for the same reason `keel/clock.py` is (D-B) — §5.5 gives Redis four jobs across three packages. It carries `RedisSettings`, which is the first code in the repo to read `REDIS_URL`; `.env.example` has shipped it since P1-T1 with nothing consuming it. Blank counts as absent, the same rule `ProviderCredentials` and `_resolve_config_path` already apply, and for the same reason. `create_app` gains a `redis` injection point beside `registry` and `clock`, without which every test in `tests/test_app.py` would open a socket to localhost (NFR-2).

**Done when:** ~~`fakeredis` + `ManualClock` tests show counts entering the current bucket, the window rolling as time advances, and old buckets falling out of the merged view. Assert the 5s edge staleness explicitly.~~ ✅ Met — 34 tests (417 total, 2 skipped), suite 7.9s → 8.1s. The D3 edge is pinned at **both** ends: an event at `t=0.0` survives the full 60 s, and one at `t=4.9` is evicted at `t=60.0` having lived only 55.1 s — up to one bucket *early*, which is the direction the trade actually runs. Verified by mutation that the tests bite: widening the merge range by one bucket fails five of them, including the D3 pair. The §5.5 key is transcribed by hand as a literal string rather than rebuilt from the module, same posture as the §5.4 truth table.

### ~~P2-T3 — Latency reservoir and percentiles~~ ✅ **DONE**
**1.75h (est. 1.5h) · FR-3.1**

The 0.25h overrun is the `HealthWindow` → `HealthTracker` rename and its ripple through two modules and three test files — the consequence of the composition decision below, not of the task bullets.

- ~~`keel/health/latency.py` — capped reservoir per bucket at `keel:latency:{provider}:{bucket_epoch}` (LIST, `LPUSH` + `LTRIM` to 200, TTL 2× window).~~ ✅ Pure functions only: the key, the staging, the parsing, and the percentile math. Nothing in it opens a connection, executes a pipeline, or catches a Redis error — `HealthTracker` does all three, once, inside the guard ADR 0008 already put there.
- ~~`keel/health/snapshot.py` — `ProviderHealth` carrying success rate, error counts by class, and p50/p95/p99 computed in-process across merged buckets.~~ ✅ One constructor, `from_window(counts, samples)`, so a snapshot can never disagree with the window it came from. `sample_count` is carried alongside `total` because they legitimately differ under the cap, and a breaker should know how much evidence a percentile rests on.
- ~~These percentiles are approximate … **the README's latency numbers come from the Prometheus histograms in P2-T4, not from here** — say so in the module docstring.~~ ✅ Said, in both new modules.
- **Beyond the bullet, and why (1): one Redis round trip, not two.** The latency write is *staged onto the pipeline the counts already open* rather than issued by a peer recorder. A peer would have duplicated ~40 lines of bucket and guard machinery, and — sharper — **doubled the cost ADR 0008 measured**: two independent 250 ms time boxes instead of one, turning a ~220 ms Redis-down request into ~440 ms. Riding the same transaction also means a bucket's count and its sample land together or not at all. `HealthWindow` became `HealthTracker` as a consequence, which moves *toward* §5.5's own title for the component rather than away from it. The one-round-trip property is asserted by a counting stub, because one write and two produce identical Redis state and differ only in round trips — verified by mutation that splitting the write fails that test.
- **Beyond the bullet, and why (2): nearest rank, not interpolation.** Every percentile returned is a latency some request actually experienced. `statistics.quantiles` reports the p50 of `[10,20,30,40]` as `25.0` — a number no attempt took — and raises below two samples, a case this hits constantly. Since these drive a threshold comparison rather than a published SLA, interpolation buys precision nothing consumes. Pinned by a test that fails if the method is ever swapped.
- **The known cost, measured rather than assumed.** `LPUSH` + `LTRIM` is a **recency cap, not reservoir sampling** — it keeps the newest 200, not 200 drawn uniformly — so §5.5's word "reservoir" implies a property the implementation does not have. Measured against a lognormal spread with a 3 s median: 0.0% error at or under the cap, **-1.1% on p95 at 2.5× oversampling and -0.9% at 10×**. It *understates*, so a breaker reading these trips marginally later on latency and never earlier — the safe direction for a false signal. §5.5 now says all of this instead of "e.g. 200 samples".
- **The open question, named rather than settled.** `latency_budget_p95_ms` is per **request class**; `keel:latency:{provider}:{bucket}` has no class dimension. One provider serving `classification` (800 ms) and `batch_enrichment` (60000 ms) therefore has **one p95 and two verdicts** on the same evidence at the same instant, and §5.6's `p95 > class budget` does not say which it means. Adding the dimension would triple the keys and thin each bucket's samples — making the percentiles worse in the name of making them correct — and deciding a breaker rule before the breaker exists is the FR-3.4 inversion this phase is ordered against. So P2-T3 records the per-provider figure and **writes the gap into §5.6 and `snapshot.py`**, with a test asserting the shape has no class dimension so Phase 3 must move the key schema, the trip condition, and that shape together or not at all.

**Done when:** ~~a known sample distribution yields the expected percentiles, `LTRIM` caps at 200 under 10k writes, and an empty window returns `None` rather than `0.0`.~~ ✅ Met — 81 tests (470 total, 2 skipped), suite 8.1s → 12.7s. Percentiles were checked against an independently computed ground truth over 600 lognormal samples and matched exactly at p50, p95, and p99. Three flavours of "unknown" are distinguished and tested: Redis unreadable → `None`; reachable but idle → zeros with `success_rate is None`; counts without samples → real counts, `None` percentiles. Four mutations confirm the tests bite — dropping `LTRIM`, interpolating, returning `1.0` at zero volume, and splitting the write into two pipelines each fail at least one test. The Redis-down path was re-checked end to end: still 200, still one `WARNING` per attempt (**not two**), still ~0.23 s, unchanged from P2-T2.

### ~~P2-T4 — Prometheus exporter~~ ✅ **DONE**
**2.5h (est. 2.0h) · FR-3.3**

The 0.5h overrun is **ADR 0009** and the label cap it decides. §6 specifies eleven metric names and their labels and nothing else — no buckets, no `outcome` values, no registry strategy — so five gaps had to be closed before the three bullets could be written.

- ~~`keel/observability/metrics.py` — the **full §6 catalogue**, defined now even where the producer arrives later … Declaring them now means the Grafana panels in P2-T6 exist and are simply flat, rather than being built twice.~~ ✅ **Declaring is not enough, and that was worth finding.** A labelled metric nothing has produced exports its `# TYPE` line and **no series at all**, so a panel over `keel_breaker_state` would read "No data" rather than the flat zero the bullet promises. The provider-keyed series are therefore *primed* at startup from the config's provider names — the only label sets knowable before their producers exist. `from_state`/`winner`/`attempt_type` are outcomes rather than configuration and stay absent, because inventing their combinations would export series claiming things had happened.
- ~~`keel/observability/middleware.py` — `keel_gateway_overhead_seconds`, measured as wall clock minus provider time.~~ ✅ Raw ASGI rather than `BaseHTTPMiddleware`, which wraps every request in a task group and two queues — measurable overhead added by the thing measuring overhead, which is the quantity under test. The route hands off the class and the provider time through `request.state`, since middleware sees a `Request` and a `Response` and can reach neither the envelope nor the result.
- ~~`GET /metrics` on the gateway.~~ ✅ Unauthenticated and on the main port, because every document describing it says so and the M2 verification step is literally `curl localhost:8080/metrics`. Renders through the catalogue so the content type travels with the body — prometheus-client 0.26 serves `version=1.0.0`, not the `0.0.4` older examples show.
- **Beyond the bullet, and why (1): ADR 0009, and a live memory-exhaustion vector.** §6 labels `keel_requests_total` with `tenant` and `feature`, and `keel/api/envelope.py` bounds neither — the only rule is "not blank". `prometheus_client` never evicts a series, so **every distinct header value is permanently retained memory**: measured at **706 bytes per series**, 13.5 MB and a 4.7 MB scrape body at 20k tenants. On an endpoint with no authentication (§10 says tenant identity "is asserted by the caller and not verified"), that is remote memory exhaustion from one header, against NFR-5's "no unbounded memory growth". Nothing in the PRD, the design, or ADRs 0001–0008 mentions cardinality at all. The first 64 distinct values of each label are now admitted and the rest recorded as `other`, warning once. **Verified through the real HTTP surface:** 200 requests with distinct random tenants produced exactly 65 label values and a 23 KB scrape body, where uncapped it would have been 200 and climbing.
- **Beyond the bullet, and why (2): the default buckets cannot measure S5.** §6 says the overhead metric exists "specifically so S5 can be measured rather than asserted", and S5 is p95 ≤ 15 ms — but the library's defaults step **10 ms straight to 25 ms**, so `histogram_quantile` interpolates across the threshold and 12 ms is indistinguishable from 24 ms. The metric could not answer the question it was created for. 15 ms is now an exact bucket edge. Duration buckets run to 120 s for the mirror reason: the defaults stop at 10 s while `batch_enrichment`'s budget is 60 s, so every call slow enough to matter would share the `+Inf` bucket.
- **Beyond the bullet, and why (3): two clocks, so the subtraction can go negative.** Overhead is `perf_counter` wall clock minus `ProviderResult.latency_ms`, and ADR 0001 wants the histograms timing independently of the injected `Clock` — a metric timed off a `ManualClock` would read exactly zero in every test and *pass*. The cost is that under `ManualClock` the two diverge violently: `MockAdapter` at `latency_ms=3000` advances injected time 3 s while ~1 ms of real time passes, giving **-2.999 s**. `prometheus_client` accepts a negative observation **silently** — counting it in every bucket and adding it to `_sum` — so one such request would corrupt the S5 histogram for the life of the process with nothing raised and nothing logged. Clamped at zero in both the middleware and the catalogue, the same guard `Executor._elapsed_ms` already applies to the NTP-step version.
- **Beyond the bullet, and why (4):** `keel_health_writes_dropped_total{provider}` — ADR 0008 asked P2-T4 for it by name ("until P2-T4 exports a counter the only evidence is a `WARNING` on stdout"). It is **not** a §6 row, so it is declared in a separately-named extension set and the catalogue test asserts §6 completeness *and* that anything beyond §6 is a declared extension. `HealthTracker` takes a plain callback rather than a catalogue, so `keel/health/` keeps its zero dependencies on `keel/observability/`.
- **The gaps §6 left, closed and recorded.** The label is `class` (the table) not `request_class` (§5.10's prose, which disagrees); `outcome` is exactly `{ok, error}`, enumerated nowhere until now; the registry is per-app, because module-level collectors raise `DuplicateTimeseries` on the second of the thirty-odd apps `tests/test_app.py` builds.

**Done when:** ~~every metric in the §6 table appears in `/metrics` with the exact labels from that table — one test that iterates the catalogue, so a typo in a label name fails CI instead of quietly breaking a dashboard query.~~ ✅ Met — 46 tests (516 total, 2 skipped), suite 12.6s. The §6 table is transcribed by hand as literal tuples and checked against the live registry, same posture as the §5.4 truth table and the §5.5 key. Four mutations confirm the tests bite: dropping the clamp, using default buckets, removing the label cap, and typoing `class` to `request_class` each fail at least one test (the last fails 32).

**S5, measured for the first time — and breaching.** 30 requests through the real gateway with **no Redis running**: mean overhead **153 ms**, with all 30 in the 100–250 ms bucket and **zero** under 15 ms. That is ADR 0008's predicted cost finally visible as a number rather than a paragraph — every request pays the ~150 ms Redis connect timeout, and `keel_health_writes_dropped_total{provider="mock_chaos"}` read 30. The metric is doing exactly its job by showing the breach. **S5 remains unmeasured against a healthy stack** and should be re-read in P2-T6 with Redis up, where the expectation is single-digit milliseconds.

### ~~P2-T5 — Structured logging~~ ✅ **DONE**
**1.5h (est. 1.0h) · FR-7.3**

The 0.5h overrun is `LogSettings` and the capture harness the three bullets never mention. Neither is optional in practice — see below.

- ~~`keel/observability/logging.py` — structlog, JSON to stdout, `request_id` bound once at ingress and carried through every attempt via contextvars.~~ ✅ Plus `KEEL_LOG_LEVEL` and `KEEL_LOG_FORMAT` (`json`|`console`), a `pydantic-settings` model shaped exactly like `RedisSettings`. The bullet says "JSON to stdout" and nothing about configurability, but an operator with no way to reach `DEBUG` has to edit code during an incident. A typo in either variable **fails at startup** rather than being guessed at: `logging` resolves an unknown level name to `0`, which admits every record, so `KEEL_LOG_LEVEL=INF0` would silently turn debug logging *on* in production.
- ~~Every line for a request carries `request_id`, `tenant`, `feature`, `request_class`, `provider`, `attempt`, `outcome`, `error_class`.~~ ✅ Bound in two stages, and the split is the point: `request_id` comes from the raw header **before** anything can reject the request, and the envelope fields are added only once `build_envelope` succeeds. Binding after validation would have left every 400 uncorrelated — the one line a client quotes in a support ticket.
- ~~**Never log request or response bodies.**~~ ✅ Pinned by a sentinel string driven through the whole HTTP path and asserted absent from the entire captured buffer, rather than per line — the failure this guards against is a future `payload=` kwarg on any of the three call sites, or a body rendered into a traceback.
- **Beyond the bullet, and why (1): the four existing loggers are bridged, not rewritten.** ADR 0008 promised P2-T5 would put the dropped-health-write warnings "into structured JSON", and the obvious reading — convert `keel/health/window.py` to structlog — would have undone something P2-T4 built deliberately: `HealthTracker` takes a plain callback rather than a `MetricsCatalogue` precisely so `keel/health/` keeps **zero dependencies** on `keel/observability/`. `ProcessorFormatter`'s `foreign_pre_chain` carries `merge_contextvars`, so all ten stdlib lines across four modules arrive as JSON carrying the request context with **not one of those files edited**. Two of the call sites — `window._merge` and `latency.parse_samples` — are module-level functions with no instance to inject a logger into, so nothing else would have reached them anyway.
- **Beyond the bullet, and why (2): third-party loggers are held at `WARNING`.** `httpx` emits one `INFO` line per request, so every provider call would log a second line saying less than `provider_attempt` already does — doubling the volume of the busiest log in the system. Raised back to the configured level when that level is `DEBUG`, because someone asking for debug logging is usually asking about exactly that layer.
- **Beyond the bullet, and why (3): `configure_logging` removes only its own handler.** The lifespan calls it once per app and the suite builds thirty-odd apps, so it must be idempotent — but the obvious `root.handlers = [handler]` would throw away pytest's `caplog` handler for any test that both captures logs and builds an app. **No test does both today**, and mutation confirms all six `caplog.text` assertions still pass with the destructive version, which is exactly why the property is pinned by a test now rather than discovered when the two habits first meet.
- **The honest gap, recorded rather than dressed up.** `clear_contextvars()` at ingress is **not load-bearing today** and the code says so. Measured: every request already starts with an empty context, because the ASGI server runs each in its own task and a binding made inside never escapes it — deleting the line leaves the whole suite green. It stays for Phase 5's deferred worker, which drains a queue in one long-lived task where successive jobs *do* share a context and a mislabelled job is worse than an unlabelled one. Same posture as the untested `transaction=True` flag in `keel/health/window.py`.

**Done when:** ~~a two-attempt request emits lines sharing one `request_id`, asserted by capturing structlog output.~~ ✅ **Met, and the criterion had to be adapted honestly.** Phase 1 invokes candidate 1 and stops, and `tests/test_executor.py` *actively pins* that candidate 2 is never called — so no single request can make two attempts yet. Correlation is asserted two ways instead: two `Executor.execute` calls under one bound context (the shape Phase 3's loop will produce), and — better — one real HTTP request against a dead Redis emitting **two lines from two different modules**, the executor's `provider_attempt` and `window.py`'s dropped-write warning, sharing one `request_id`. That second one is a genuine multi-line correlation available today and is precisely the diagnostic ADR 0008 says a reviewer needs.

26 tests (542 total, 2 skipped), suite 12.9s. `structlog.testing.capture_logs` is deliberately **not** the tool: in structlog 26 it clears the whole processor chain, so contextvars are invisible unless passed back explicitly, and it never sees stdlib records at all — half of what this task is about. The harness swaps a buffer under the **real** handler instead, so what is asserted is what production writes. Five mutations confirm the tests bite: dropping `merge_contextvars` fails 8, resetting `root.handlers` fails the caplog guard, logging the payload fails the sentinel test, binding after `build_envelope` fails 4, and the level validator rejects four spellings of a typo.

**Verified end to end against a live uvicorn with no Redis running** — the ADR 0008 case. One request produced the dropped-write warning from the untouched `keel.health.window` carrying `request_id`, `tenant`, `feature`, and `request_class`; `provider_attempt` and `request_completed` sharing that id; a 400 carrying the id from the header and correctly carrying **no** envelope fields, since it never got one. Nine JSON lines, zero malformed, and the prompt text absent from all of them. Uvicorn's own startup and access lines render as JSON through the same handler.

### ~~P2-T6 — Compose stack, Prometheus, provisioned Grafana~~ ✅ **DONE** (with one gap, named below)
**4.0h (est. 2.5h) · NFR-1, S8, C3**

Sized 1.0h above the roadmap's 1.5h: it absorbs the Phase 0 compose carry-over (C3) and the load driver the M2 exit criterion needs but the roadmap never budgeted. The further 1.5h is **ADR 0010**, the demo config, and `tests/test_deploy_assets.py` — none of which the bullets mention, and the first two of which the M2 exit turned out to be impossible without.

**Two blockers the task card did not anticipate, found before any file was written.**

1. **The load run would have gone to Cohere.** Every request class in `config/keel.yaml` lists `cohere_primary` first and Phase 1's executor invokes candidate 1 and stops — so `--rps 20 --duration 120` is 2400 live calls against an NFR-3 budget of EUR 75, and §5's own tripwire says to move all load generation to the mock. Verified separately that the shipped config also raises `ConfigError` with no `COHERE_API_KEY`, so a reviewer without a Cohere account could not start the stack at all, which is S8 unmet on the first command. Both are fixed by `deploy/keel.demo.yaml`, a mock-only config that compose points `KEEL_CONFIG_PATH` at: it builds a registry with **empty credentials**, and `tests/test_deploy_assets.py` asserts that property so a future edit cannot quietly reintroduce a key requirement.
2. **Nothing could change the mock's error rate at runtime.** `build_registry` never passes a `MockChaosState`, so a running mock sits at `error_rate=0.0, latency_ms=50.0` forever and `--error-rate 0.4` had nowhere to go. Settled as **ADR 0010** — see the chaos bullet below.

- ~~`deploy/Dockerfile` — gateway image.~~ ✅ `python:3.12-slim`, non-root, dependency layer separated from source. Slim rather than alpine because LiteLLM's tree carries compiled wheels that musl either rebuilds from source or fails on. **Measured: `litellm` alone is ~104 MB installed and the dependency set ~137 MB**, so the image lands near half a gigabyte — the single largest input to S8, and the reason the layer ordering is what it is. Copies `config/` and `deploy/keel.demo.yaml` explicitly, because `packages = ["keel"]` means neither is in the wheel.
- ~~`docker-compose.yml` — `keel-gateway:8080`, `redis:7`, `prometheus:9090`, `grafana:3000` … healthchecks on every service with `depends_on: condition: service_healthy`.~~ ✅ **but at the repo root, not under `deploy/`.** NFR-1 and S8 both spell the bare command `docker compose up`, which resolves only a file in the working directory; a compose file under `deploy/` would have made the one command two requirements name into the wrong command. Everything it *reads* still lives in `deploy/`, so §8's description of that directory stays true. The gateway healthcheck runs a one-line `python` request rather than curl, which the slim image does not carry. Redis is deliberately **not** published to the host.
- ~~`deploy/prometheus/prometheus.yml` — scrape interval ≤ 5s, matching the bucket width.~~ ✅ 5 s, and **not restated**: `tests/test_deploy_assets.py` reads `breaker.bucket_seconds` out of `config/keel.yaml` and asserts the interval still fits inside it, so the two cannot drift. No `alerting:` block — alerting is out of scope for every phase, and an empty one pointing at nothing logs a connection error every evaluation interval, in exactly the logs a reviewer reads.
- ~~datasource and dashboard provider as version-controlled files.~~ ✅ The datasource carries a **fixed UID** (`keel-prometheus`) because the dashboard's panels reference it by UID; letting Grafana generate one would leave every panel reading "Datasource not found" — a whole broken board from one omitted field. A test asserts the declared UID and the referenced UIDs are the same set.
- ~~`deploy/grafana/dashboards/keel-health.json` — the four panels Phase 2 can actually fill … anonymous viewer access on (S8).~~ ✅ Plus a fifth: `keel_health_writes_dropped_total`. ADR 0008's whole argument is that a clean-looking board may be clean because nothing was recorded, and that counter is the only thing that distinguishes the two — a board without it invites exactly the wrong conclusion. Rate windows are 30 s rather than 1 m, because the done-when is movement within 30 seconds; a test asserts every window still holds at least two scrapes.
- ~~`scripts/loadgen.py` — small async driver … with a flag to change the mock's error rate mid-run.~~ ✅ **The open sequencing question is closed: the minimal chaos endpoint landed here — ADR 0010.** `POST /chaos/{provider}`, four scalars, registered *only* when `KEEL_CHAOS_ENABLED` is set. The gate is not decoration: §10 records that the gateway has no authentication of any kind, so an always-on endpoint that makes a provider fail 100% of the time is a denial-of-service control with a REST interface. Registered conditionally rather than registered-and-403ing, so the route table stays a fact about configuration and `tests/test_app.py` pins it in both directions.
- **Beyond the bullet, and why (1): the driver paces, it does not loop.** A sequential send-and-await caps throughput at one request per round trip — about 0.33 rps against the M2 run's 3-second mock — so the requested rate would have been silently unreachable and the panels would have shown a trickle. Requests are spawned on an absolute wall-clock schedule and awaited at the end, which puts roughly `rps × latency` in flight (60 for the M2 profile) and keeps a slow spawn from pushing every later request further behind. The connection pool is sized to match, or requests queue on a socket instead of on the gateway and the achieved rate collapses without saying why.
- **Beyond the bullet, and why (2): tenant and feature are fixed strings.** ADR 0009 caps client-supplied labels at 64 distinct values and folds the rest into `other`. A driver inventing a tenant per request would have filled the demo board with one meaningless series and taught a reviewer the opposite of what the panel is for.
- **Beyond the bullet, and why (3): the first bound body model in the gateway.** `ChaosRequest` is it — the ingress route deliberately has none, since the payload is opaque pass-through. That made it the first thing capable of returning FastAPI's own bare validation body, which would have been a **second error shape** in a gateway whose ADR 0003 promises one body for every 4xx. A `RequestValidationError` handler now renders those in the Keel envelope at 400, matching what `_decode_body` already did by hand.

The remaining three panels — circuit state timeline, failover annotations, queue depth and cost — land in Phase 6 with the full seven-panel board. Building them now would show flat lines meaning "no breaker" rather than "no trips", which is a worse lie than an absent panel; a test asserts they are absent.

**Done when:** on a clean machine, `docker compose up` → `python scripts/loadgen.py --rps 20 --error-rate 0.4` → all four panels move within 30 seconds, and the whole path from clone to moving dashboard is under five minutes. **This is the M2 exit criterion.**

**Met in part, and the part that is not is stated plainly.** 54 tests (597 total, 2 skipped), suite 14.1s. Everything that does not require a container is verified:

- **The four panels have moving data, measured through the real stack.** `scripts/loadgen.py` was run against a live `uvicorn` on the demo config: 240 requests at 19.5 rps achieved, and `/metrics` scraped before and after shows all four panel queries moving — `keel_requests_total` split by outcome, `keel_provider_errors_total` split across **five** taxonomy classes, the duration histogram, and the overhead histogram. The observed error rate was 36.7% against a requested 40%.
- **Overhead came back at 152 ms mean**, with no Redis running — matching P2-T4's independently measured 153 ms almost exactly, which is a useful cross-check on both. **S5 therefore remains unmeasured against a healthy stack** and is the first thing to read when the stack is run for real.
- `tests/test_deploy_assets.py` checks the assets as files: services, ports, healthchecks, `service_healthy` dependencies, the Redis persistence flag, the build context, the `.dockerignore` rules that keep `.env` out of a layer and the demo config *in* the build, and — the one that earns its keep — **every metric name and every grouping label in the dashboard against a live `MetricsCatalogue`**. A PromQL typo renders as an empty panel, never an error, and demo day is the wrong time to find one. Three mutations confirm it bites: a typo'd metric name fails 2 tests, a typo'd label fails 1, and slowing the scrape interval to 15 s fails the bucket-width check.

**The gap: Docker is not installed on the development machine** — no CLI, no daemon, no Desktop, and no local Redis, Prometheus, or Grafana. So `docker compose up` has **never been run**. Unverified, and needing a machine with Docker: that the images pull and the build succeeds, that the healthchecks pass and the `depends_on` ordering holds, that Prometheus actually reaches the gateway, that Grafana renders the board, and the **S8 cold-start timing**, which is the one number this task exists to produce. An honest 7 minutes is worth more than a restated 5.

**Phase 2 total: 15.0h** — 5.0h over the roadmap's 10.0h: 1.0h the P2-T6 carry-over, 0.5h ADR 0007 and the capture script in P2-T1, 0.75h ADR 0008 and `keel/redis.py` in P2-T2, 0.25h the `HealthTracker` rename in P2-T3, 0.5h ADR 0009 and the label cap in P2-T4, 0.5h `LogSettings` and the capture harness in P2-T5, 1.5h ADR 0010, the demo config, and `tests/test_deploy_assets.py` in P2-T6. Under the 16.5h tripwire, with 1.5h of headroom that the unrun Docker verification may yet consume.

---

## 4. Documentation updates as work lands

Not a separate phase. Each of these ships in the same commit as the code that makes it true.

| Change | Trigger |
|---|---|
| ~~`docs/adr/0003-the-client-error-body-nests-keel-detail-inside-the-openai-error-envelope.md`~~ ✅ | With P1-T2 |
| ~~Technical design §5.3 — `capabilities()` returns `frozenset`; `ProviderResult` invariants and failure-as-return-value~~ ✅ | With P1-T3 |
| ~~Technical design §5.1 — name the `x_keel` body extension; `request_class` is `str` not enum; `capabilities` is `frozenset`~~ ✅ | With P1-T2 |
| ~~`docs/adr/0002-the-mock-provider-is-an-in-process-adapter.md`~~ ✅ | Before P1-T4 |
| ~~Technical design §8 — remove `mock-provider:9001` from the deployment diagram~~ ✅ | With ADR 0002 |
| ~~`docs/adr/0004-the-registry-refuses-to-build-a-provider-it-cannot-serve.md`~~ ✅ | With P1-T5 |
| ~~Technical design §5.2 — note that the shipped config carries two providers until Phase 4~~ ✅ | With ADR 0004 |
| ~~`docs/adr/0006-upstream-failures-map-to-http-status-by-normalized-error-class.md`~~ ✅ | With P1-T7 |
| ~~ADR 0003 — note that `error.keel` is extensible; upstream errors add `provider` and `error_class`~~ ✅ | With ADR 0006 |
| ~~`docs/adr/0007-message-refinement-may-only-replace-a-server-error.md`~~ ✅ | With P2-T1 |
| ~~`docs/adr/0008-a-failed-health-write-is-dropped-not-raised.md`~~ ✅ | With P2-T2 |
| ~~Technical design §5.5 — name all eight health-hash fields; §8 repo layout — add `keel/redis.py`~~ ✅ | With ADR 0008 |
| ~~Technical design §5.5 — nearest-rank percentiles, and the cap is a recency cap with a measured bias~~ ✅ | With P2-T3 |
| ~~Technical design §5.6 — the p95 trigger compares a per-provider figure against a per-class budget (open for Phase 3)~~ ✅ | With P2-T3 |
| ~~`docs/adr/0009-client-supplied-metric-labels-are-capped.md`~~ ✅ | With P2-T4 |
| ~~Technical design §6 — bucket sets, `outcome` values, the `class` spelling, the extension metric, and series priming~~ ✅ | With ADR 0009 |
| ~~Technical design §8 repo layout — add `keel/clock.py`, `keel/api/app.py`, `scripts/`~~ ✅ | End of Phase 1 |
| ~~README "Status" placeholder → what works today~~ ✅ (Phase 1) | End of each phase |
| ~~Technical design §6.1 — the log line schema, the field table, and the `request_class` vs `class` split~~ ✅ | With P2-T5 |
| ~~Technical design §8 repo layout — name `metrics.py`, `middleware.py`, and `logging.py` under `observability/`~~ ✅ | With P2-T5 |
| ~~`.env.example` — `KEEL_LOG_LEVEL` and `KEEL_LOG_FORMAT`~~ ✅ | With P2-T5 |
| ~~`docs/adr/0010-the-chaos-endpoint-lands-early-behind-an-env-gate.md`~~ ✅ | With P2-T6 |
| ~~Technical design §8 — compose at the repo root, the demo config, Redis persistence, single replica, and the `keel-worker` node that is still Phase 5~~ ✅ | With P2-T6 |
| ~~Technical design §7 — the Load row names `scripts/loadgen.py`, not Locust/k6; the compose stack is not a test dependency~~ ✅ | With P2-T6 |
| ~~README "Quickstart" placeholder → real commands~~ ✅ | P2-T6 |
| ~~README Status → Phase 2 complete; repo-layout tree refreshed~~ ✅ | P2-T6 |
| ~~`.env.example` — `KEEL_CHAOS_ENABLED`, and the demo-config note on `KEEL_CONFIG_PATH`~~ ✅ | With P2-T6 |

The README quickstart placeholder already says to fill it in during Phase 2 — P2-T6 is where that happens. Leave the measured-results table alone: it is filled from the Phase 6 run, with real numbers or not at all.

---

## 5. Tripwires for these two phases

| Tripwire | Response |
|---|---|
| Mock adapter (P1-T4) exceeds 2.0h | Freeze at current capability immediately. Top-ranked PRD risk |
| Either phase exceeds 1.5× budget (15h / 16.5h) | Apply the roadmap §4 descope ladder rather than extending the phase |
| ~~Tempted to add the breaker "while I'm in here"~~ — **held for the whole phase** | Spent, and it paid: metrics, correlated logs, and a provisioned board all exist, so Phase 3 is written against inputs somebody can watch. FR-3.4 satisfied in the order it asks for |
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
docker compose up -d          # at the repo root — NFR-1 and S8 both spell this command
python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.0
python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.4 --latency-ms 3000
```

No `.env` and no API key are needed: compose points `KEEL_CONFIG_PATH` at the mock-only `deploy/keel.demo.yaml`, so the stack starts with no credentials and the load run costs nothing.

Open Grafana at `localhost:3000`. The error-rate panel must climb to roughly 40%, split across taxonomy classes, and the p95 panel must climb toward 3s — both within about one window (60s). Confirm `curl localhost:8080/metrics` lists every metric from technical design §6.

Then confirm the logs (§6.1): `docker compose logs keel-gateway` must be one JSON object per line, and picking any `request_id` out of it must bring back that request's whole story — the `provider_attempt` line and the `request_completed` line at minimum, plus any health-write warning the same request caused. No line may contain a prompt or a completion.

Finally, time a cold start from `git clone` on a clean machine against the S8 five-minute target. If it misses, record the real number. An honest 7 minutes is worth more than a restated target. Expect the ~137 MB dependency set — `litellm` alone is ~104 MB — to dominate it.

**None of the Docker steps in this section have been run.** The machine P2-T6 was built on has no Docker, no daemon, and no local Redis, Prometheus, or Grafana, so the compose stack is written and statically asserted but never started. The two numbers still missing are the S8 cold start and **S5 against a healthy stack** — P2-T4 and P2-T6 both measured ~150 ms of overhead with Redis *down*, and single-digit milliseconds is the expectation once it is up. Whoever runs it first should record both, and treat a green `pytest` as evidence about the files rather than about the containers.
