# Build Plan — Phases 1–2 (Milestones M1 and M2)

**Owner:** Jamil
**Status:** Draft v1.0 — in progress
**Last updated:** 2026-08-19
**Related documents:** `PRD.md`, `TECHNICAL-DESIGN.md`, `ROADMAP.md`

**Progress:** 14.75h of 23.25h. ✅ P1-T1 … P1-T7 — **Phase 1 complete, M1 met** · ✅ P2-T1 · ⬜ P2-T2 … P2-T6. Next up: **P2-T2 — Redis bucketed sliding window.**
Completed work is struck through and marked ✅ DONE. Unmarked items are outstanding.

---

## 0. Where the repo is, and what this covers

Phase 0 / M0 is complete apart from three carry-over items — ~~C1~~ and ~~C2~~ have since landed with P1-T1; only C3 remains. In place today: the three design documents, `keel/config.py` with the full pydantic schema and cross-reference validation, `tests/test_config.py` mutating the shipped `config/keel.yaml`, `pyproject.toml` with the dependency set chosen, empty package directories, and — as of P1-T1 — `keel/clock.py`, `.env.example`, and `.github/workflows/ci.yml`. P1-T2 adds `keel/api/envelope.py` and `keel/api/errors.py`; P1-T3 adds `keel/providers/base.py` and `keel/providers/errors.py`; P1-T4 adds `keel/providers/mock.py`; P1-T5 adds `keel/providers/cohere.py`, `keel/providers/credentials.py`, and `keel/providers/registry.py`; P1-T6 adds `keel/routing/router.py` and `keel/routing/executor.py`; P1-T7 adds `keel/api/app.py` and the `UpstreamError` family in `keel/api/errors.py`. P2-T1 adds `keel/providers/normalize.py`, `tests/fixtures/providers/`, and `scripts/capture_error_fixtures.py`.

Not yet built: health tracking, metrics, the compose stack.

This document covers roadmap **Phases 1–2 (Milestones M1 and M2)**, ~22.75h at ~10h/week. Ordering is fixed by design principle 2 and FR-3.4: **observe before you react.** No breaker, no capability filter, no failover here — those are Phase 3.

Three Phase 0 exit items never landed and are folded in, sized at ~1.5h total:

| # | Carry-over | Where it lands |
|---|---|---|
| ~~C1~~ | ~~`.env.example` (`.gitignore` already references it)~~ | P1-T1 — ✅ **DONE** |
| ~~C2~~ | ~~CI running the test suite (`.github/workflows/ci.yml`)~~ | P1-T1 — ✅ **DONE** |
| C3 | `docker compose up` starts a stack | P2-T6 — deferred from P0, since nothing existed to compose until Redis and Prometheus do — ⬜ open |

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
- `scripts/loadgen.py` — small async driver sending tagged traffic through the gateway at a set RPS against `mock_chaos`, with a flag to change the mock's error rate mid-run. **Open sequencing question:** that flag needs a way to reach `MockChaosState`, but the chaos API (FR-7.2) is scheduled for Phase 6. Either a minimal chaos endpoint lands early here, or loadgen sets the rate at gateway startup only. Decide when P2-T6 starts — the cheap option is a single `POST /chaos/{provider}` since the state object is already runtime-mutable and validated on assignment. This is what makes the M2 exit demonstrable, and the Phase 6 measurement run reuses it rather than reinventing it.

The remaining three panels — circuit state timeline, failover annotations, queue depth and cost — land in Phase 6 with the full seven-panel board.

**Done when:** on a clean machine, `docker compose up` → `python scripts/loadgen.py --rps 20 --error-rate 0.4` → all four panels move within 30 seconds, and the whole path from clone to moving dashboard is under five minutes. **This is the M2 exit criterion.**

**Phase 2 total: 11.5h** — 1.5h over the roadmap's 10.0h: 1.0h the P2-T6 carry-over, 0.5h ADR 0007 and the capture script in P2-T1. Under the 16.5h tripwire.

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
| ~~Technical design §8 repo layout — add `keel/clock.py`, `keel/api/app.py`, `scripts/`~~ ✅ | End of Phase 1 |
| ~~README "Status" placeholder → what works today~~ ✅ (Phase 1) | End of each phase |
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
