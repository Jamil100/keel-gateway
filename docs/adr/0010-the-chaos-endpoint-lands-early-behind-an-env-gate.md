# 0010 — The chaos endpoint lands early, behind an env gate

**Status:** Accepted
**Date:** 2026-08-20
**Relates to:** FR-7.2, NFR-1, NFR-3, S8 · TECHNICAL-DESIGN.md §3, §10 · PHASE-2-PLAN.md P2-T6 · ADR 0002 · `keel/api/app.py`, `keel/providers/mock.py`

## Context

FR-7.2 — "chaos control endpoint that injects configurable error rates and latency into a chosen provider on demand" — is scheduled for Phase 6, and the roadmap budgets 1.5h for it there. P2-T6 nevertheless has to answer a question that cannot wait, and the task card says so in as many words: *"that flag needs a way to reach `MockChaosState`, but the chaos API (FR-7.2) is scheduled for Phase 6. Either a minimal chaos endpoint lands early here, or loadgen sets the rate at gateway startup only. Decide when P2-T6 starts."*

The forcing constraint is that **`MockChaosState` is unreachable from outside the process.** `build_registry` constructs `MockAdapter(name=..., clock=..., capabilities=...)` and never passes a `state`, so a running gateway's mock sits permanently at its defaults — `error_rate=0.0`, `latency_ms=50.0`. The object is mutable and `validate_assignment=True`, and `tests/test_executor.py` already writes to it directly, but only in-process. Over HTTP there is nothing.

That collides with the M2 exit criterion, which is two load profiles against one gateway:

```bash
python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.0
python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.4 --latency-ms 3000
```

Without a runtime hook, `--error-rate` has nowhere to go. The alternatives are all worse than they look:

- **Startup-only environment variables.** The two runs then need a container restart between them, which breaks the documented one-command flow, resets the health window the panels are reading, and makes "watch the error rate climb" into "watch the stack come back up".
- **Put the knobs in `config/keel.yaml`.** `ProviderConfig` is `extra="forbid"` and `_check_mock_takes_no_model` rejects anything beyond the mock's existing fields, so this is a schema change to the routing config — and FR-2.3 says that file describes *routing behaviour*. Chaos is neither routing nor per-machine deployment wiring; it is a runtime instruction.
- **Skip the demo.** The M2 exit criterion is the phase.

The counter-pressure is real and is about safety, not scope. §10 records that the gateway has **no authentication of any kind**: "static API keys only; tenant identity is asserted by the caller and not verified". An always-on endpoint that makes a provider fail 100% of the time, reachable by anyone who can reach the port, is a denial-of-service control with a REST interface. `/metrics` is already unauthenticated, but reading counters and degrading traffic are not the same exposure.

There is also a tripwire pointing the other way. PHASE-2-PLAN §5 lists "tempted to add the breaker while I'm in here" and the P1-T7 route-table test exists specifically so that *"`/metrics` (P2-T4) and `/chaos` (Phase 6) cannot arrive unnoticed"*. Landing this route makes that test fail by design. The decision should therefore be recorded rather than absorbed.

## Decision

**A minimal `POST /chaos/{provider}` lands in P2-T6, and it is registered only when `KEEL_CHAOS_ENABLED` is set.**

**1. Registered conditionally, not registered-and-refusing.** `create_app` takes `chaos_enabled: bool | None`; `None` reads the environment, which is off unless the value is one of `1/true/yes/on`. When disabled the route does not exist and a request gets FastAPI's own 404 — which tells a caller the same thing a 403 would and tells a scanner less. It also keeps the route table a fact about configuration rather than a runtime branch, so `tests/test_app.py` can assert both directions: three routes by default, exactly one more when enabled.

An unrecognised value means **off**. This is deliberately unlike `KEEL_LOG_LEVEL`, which raises on a typo (§6.1): a mistyped log level should stop a gateway from starting, and a mistyped chaos flag should stop it from exposing a failure injector. Both fail in the safe direction; the safe direction differs.

**2. Four scalars, and nothing else.** `error_rate`, `latency_ms`, `latency_sigma`, `seed`. No per-tenant behaviour, no scheduling, no error-class mix, no connection-level faults (ADR 0002 already records that the in-process mock cannot produce those). Phase 6 extends this route; it does not replace it.

**3. Every field optional, and omitted fields left alone.** The request model is `ChaosRequest`, not `MockChaosState` itself. That model has a default for every field, so binding it directly would make "set the error rate to 0.4" also reset latency, the class mix, and the seed — a footgun in the middle of a run, and exactly what loadgen does when it flips a rate mid-flight.

**4. The bounds stay in `MockChaosState`.** It is `validate_assignment=True`, so `error_rate = 5.0` raises at the assignment; the endpoint catches that and renders pydantic's own message in the ADR 0003 error body. The range is written down once, in the model that owns it.

**5. `reseed()` fires only when the caller names `seed`.** Restarting the RNG streams is what re-running the same scenario needs, and is precisely what a mid-run error-rate change must not do — otherwise every flip would rewind the failure sequence and the two M2 runs could not be compared, which is the property the three-stream seeding in P1-T4 exists to provide.

**6. Naming a non-mock provider is a `409`, not a quiet success.** A real provider cannot be told to fail 40% of the time. Answering `200` to a control that does nothing would let a chaos demo appear to work while changing nothing at all — the worst available outcome for a feature whose entire purpose is demonstrating behaviour under failure.

## Consequences

**The route table test changes, as intended.** `tests/test_app.py` was written to be edited here. It still asserts three routes for the default app, and a second test asserts that enabling chaos adds exactly `/chaos/{provider}` and nothing else.

**Phase 6 inherits a working endpoint and a smaller task.** FR-7.2's remaining surface is the error-class mix, whatever the demo script needs, and the README's chaos commands. The measurement run reuses this rather than reinventing it, which is what the task card anticipated.

**The gateway now has an endpoint that must not be exposed.** Off by default is a mitigation, not a fix. A deployment that sets `KEEL_CHAOS_ENABLED` and publishes port 8080 to an untrusted network has handed out a failure injector. That is acceptable for a demonstrator whose §10 already says "no real authn … disqualifying for production", and it is one more item on the list that a production hardening pass would have to address. It is recorded here so that pass has something to find.

**It is one more thing that is true of the demo stack and not of the shipped config.** `docker-compose.yml` sets `KEEL_CHAOS_ENABLED=true` and points `KEEL_CONFIG_PATH` at the mock-only `deploy/keel.demo.yaml`. Both are demo-stack facts. Someone reading `config/keel.yaml` and `.env.example` alone would not know the endpoint exists, which is why `.env.example` carries it commented out with the reasoning rather than omitting it.

**A second gateway replica would break it.** ADR 0002 already records that chaos state is per-process; this endpoint is what makes that consequence reachable, since a chaos call would retune one replica and leave the others serving the old rate. `docker-compose.yml` says so at the service definition and does not scale the gateway.

## Rejected alternatives

**Wait for Phase 6 and restart the gateway between load profiles.** Honest to the roadmap and fatal to the demo: the M2 exit criterion is a single command producing a moving dashboard, and a restart in the middle empties the health window the dashboard is reading.

**Always register the route and return 403 when disabled.** Slightly simpler code, and it leaks that the endpoint exists. It also makes the route table constant, which sounds like a virtue but removes the thing that makes the P1-T7 test able to assert the gate at all.

**Put chaos behind a shared secret instead of an env flag.** A header check would be a few lines, and it would be the only authentication in the entire gateway — an inconsistency that reads as security where there is none. §10's position is that this build has no authn; adding a token to one endpoint muddies that statement without changing the exposure of the other three.
