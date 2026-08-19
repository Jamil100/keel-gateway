# 0008 — A failed health write is dropped, not raised

**Status:** Accepted
**Date:** 2026-08-19
**Relates to:** FR-3.1, FR-3.2, FR-3.4, NFR-2, NFR-4, S2, S5 · TECHNICAL-DESIGN.md §1 principle 5, §5.5, §5.6, §10 · ADR 0001, ADR 0004 · PHASE-2-PLAN.md P2-T2, D-C · `keel/health/window.py`, `keel/redis.py`

## Context

P2-T2 is the first code in Keel to open a Redis connection. `REDIS_URL` has shipped in `.env.example` since P1-T1 with nothing reading it, and until now the gateway had no runtime dependency beyond the provider APIs it exists to call.

That makes this the moment a question has to be answered that the design documents never do. `PRD.md`, `TECHNICAL-DESIGN.md`, and ADRs 0001–0007 say what Redis is *for* — health windows, breaker state, the deferred queue, idempotency keys (§5.5) — and §10 concedes that "Redis is a single point of failure in this topology". None of them say what the gateway should do when it cannot reach it.

Two established postures in this codebase pull in opposite directions, and both have good arguments.

**Fail fast.** NFR-4 wants configuration problems found at startup, not at first request. ADR 0004 applies it hard: `build_registry` refuses to return a partial registry, because a provider with no credentials fails 100% of its traffic as `AUTH_FAILURE`, which D7 keeps out of the breaker — a total outage with every dashboard green. By that logic the lifespan should ping Redis and refuse to start.

**Absorb the failure.** The gateway's entire purpose is to keep serving when something it depends on degrades. `/healthz` already refuses to touch config, registry, or providers, and says why: the P2-T6 compose healthcheck restarts the container when the probe fails, so a probe with dependencies turns an upstream blip into a restart loop.

The deciding observation is that the two cases are not the same shape. A missing provider credential means the gateway **can never serve a request**. A missing Redis means it can serve **every** request and merely cannot remember how they went. Refusing to start on the second is not fail-fast; it is inventing an outage.

There is a second, quieter question underneath. Health data flows in both directions — `record` on the way out of every attempt, `read` on the way into Phase 3's breaker — and the safe answer is not the same for both.

## Decision

**A health write that fails is dropped. A health read that fails returns `None`, meaning *unknown*.** Neither ever raises. The guard lives inside `HealthWindow`, not at its call sites.

Concretely, four parts:

**1. Redis is never pinged at startup.** `create_redis_client` builds a client and opens no socket; `redis-py` connects on first command. An unreachable Redis therefore does not stop the process, and a Redis that dies mid-flight does not take the gateway with it. This is a deliberate departure from ADR 0004's posture, for the reason above: the failure modes are not analogous.

**2. `record` swallows `RedisError`, `OSError`, and `TimeoutError`, logs a `WARNING` naming the provider and the field, and returns.** Design principle 5 says work not required to produce the response happens off the critical path; recording is exactly that. `CancelledError` is deliberately *not* caught — it derives from `BaseException`, and swallowing it would break the executor's ability to cancel an attempt that runs through this code.

**3. `read` returns `None` on the same failures, and a zero-filled `WindowCounts` when Redis answers with an empty window.** The distinction is the point. P2-T3 states the same rule for percentiles — "a provider with no traffic is *unknown*, not perfect" — and it matters more here: if a Redis outage read as zeros, `min_requests_in_window` would be unmet for every provider at once and Phase 3's breaker would hold every circuit closed while believing it had evidence. Returning `None` makes "we could not find out" a thing the breaker must handle explicitly rather than a healthy-looking number.

**4. Both calls are time-boxed at `REDIS_TIMEOUT_SECONDS` (0.25 s).** A refused connection fails on its own; a Redis that accepts the command and then stops answering would otherwise hang the request. S5 allows 15 ms of p95 gateway overhead and a local pipelined round trip is well under a millisecond, so this bound is nowhere near a normal cost — it exists purely to convert a hang into a drop.

The guard sits inside `HealthWindow` rather than around the call in `keel/routing/executor.py` so that the executor's hook stays the single line D-C promised, and so Phase 3's breaker — a second caller, written later, by someone reading the method signature and not the call site — cannot forget to repeat a guard it never saw.

## Consequences

**Health data can be lost silently.** This is the real cost and it is not small. A Redis that is up but rejecting writes produces a gateway that looks entirely healthy while its window quietly empties, and until P2-T4 exports a counter the only evidence is a `WARNING` on stdout. The mitigation is deliberate and scheduled rather than assumed: P2-T5 puts those lines into structured JSON, and P2-T4 should give the drop a metric so it is alertable. **Until then, a reviewer reading a suspiciously clean dashboard should check the logs before believing it.**

**While Redis is unreachable, every request pays the timeout — and that breaks S5.** This is the sharpest cost and it was measured, not predicted. With the gateway running against a machine with nothing on 6379, a request that should add single-digit milliseconds took **330 ms**, because `redis-py` retries a refused connection rather than failing on the first `ECONNREFUSED` and the 250 ms outer box was doing all the work. Adding socket deadlines below that box (`keel/redis.py`) brought it to **~220 ms** and, more usefully, turned an anonymous `TimeoutError` into `"Timeout connecting to server"`. It is still an order of magnitude past S5's 15 ms p95 budget.

The decision stands, because the alternative on the table is worse: a request that fails outright rather than one that is slow. But "a Redis outage costs an observation, not a request" is **only true of correctness, not of latency**, and this ADR should not be read as claiming otherwise.

The real fix is to stop asking a Redis that has just refused us — a short-circuit that fails the write locally for a few seconds after a failure and retries periodically, which is the breaker pattern §5.6 already describes, pointed at our own dependency. It is deliberately **not** built here: PHASE-2-PLAN §5 has an explicit tripwire against adding breaker logic during P2-T2, and a breaker over Redis written before the one over providers would be the same reactive-before-observable mistake FR-3.4 rules out. It belongs with P2-T4, which is where a dropped-write counter makes the problem visible enough to justify it.

**Phase 3's breaker must handle a `None` read.** It cannot treat unknown as healthy — that would reintroduce the failure this decision avoids one layer up — and it cannot treat unknown as unhealthy, which would trip every breaker in the fleet the moment Redis hiccuped. The correct behaviour is to hold: make no transition, admit traffic as currently gated. That is a constraint this ADR places on work not yet written, and it belongs in P3's task card.

**Restart-persistence (FR-3.2) is weaker than it reads.** Counters survive a gateway restart, as required. They do not survive a Redis restart without persistence configured, and the compose stack in P2-T6 should say which it uses rather than leaving it to the image default.

**The `min_requests_in_window` floor absorbs partial loss by accident.** A window with dropped writes is sparse, sparse windows fail the volume floor, and the breaker holds rather than acting on nonsense. ADR 0001 already noted this same accidental protection for clock steps. It is worth naming as luck rather than design, because a future change to that floor would remove a safety net nobody remembered was load-bearing.

**Revisit if Redis stops being a single point of failure.** §10 already flags it as one. If Keel ever runs multiple gateway instances against a replicated Redis, "unknown" becomes rarer and more suspicious, and fail-fast on startup becomes affordable again because the dependency is no longer a coin flip.

## Alternatives considered

**Ping Redis in the lifespan and refuse to start.** The most tempting, because it matches ADR 0004 and NFR-4 and this codebase has a strong fail-fast habit. Rejected because it converts a Redis restart into a gateway outage, and because health data is not required to serve a request — the check would enforce a dependency that does not exist. A weaker variant, *ping at startup but degrade afterwards*, was also considered: it catches a typo'd `REDIS_URL` at deploy time, which is a real benefit, but it makes the compose stack's startup order load-bearing and buys a diagnostic that the first `WARNING` line already provides.

**Fire-and-forget: `asyncio.create_task` around the write.** The literal reading of P2-T2's "recording must not block the response path", and rejected on three counts. A Redis stall would build unbounded pending tasks against NFR-5. Writes in flight at shutdown are lost, silently. And every test would have to drain the loop before asserting, which makes the ordering untestable rather than merely fast. Awaiting a sub-millisecond pipelined write costs less than the machinery to avoid it, and it has the property that P2-T4's `keel_gateway_overhead_seconds` will *measure* the real cost rather than letting it hide in an untracked task.

**Let the exception propagate and handle it at the ingress.** Would make the loss visible, which is the one thing the chosen option is bad at. Rejected because the visibility arrives as a 500 to a client whose request already succeeded — the gateway reporting its own bookkeeping problem as the caller's failure. It also puts the decision in `keel/api/app.py`, which would leave the deferred worker (§3, a second caller with no HTTP surface) with no answer at all.

**Guard at the call site instead of inside `HealthWindow`.** Keeps the method honest about what it does, and would have been defensible. Rejected because there will be more call sites than one — Phase 3's breaker reads, P2-T3 writes latency — and a contract that each caller must remember to wrap is a contract that will eventually be missed, in the module whose entire job is to keep working when something else is broken.
