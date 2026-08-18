# PRD — Self-Healing LLM Gateway

**Codename:** Keel *(rename freely — used here so docs can reference the system by name)*
**Owner:** Jamil
**Status:** Draft v1.0
**Last updated:** 2026-08-17
**Related documents:** `TECHNICAL-DESIGN.md`

---

## 1. Context

Enterprises adopting LLMs at scale hit the same operational wall: model calls are scattered across application code, each team integrates a provider SDK directly, and nobody can answer three basic questions — *what does this cost per team*, *what happens when the provider degrades*, and *how do we move workloads between clouds without a rewrite*.

A gateway solves this by becoming the single seam every model call passes through. Keel is that seam, built around a specific opinion: **Cohere is the primary provider, and everything else is a fallback.** That reflects the real posture of an enterprise that has standardized on Cohere for data-residency or sovereignty reasons but still needs an answer for "what if Cohere is unavailable."

### 1.1 Why this project, specifically

This document has a second audience beyond the notional enterprise user: engineering reviewers at AI labs evaluating candidates for Forward Deployed Engineer roles. That audience shapes several requirements that a pure internal tool would not have — the chaos harness, the demo video, the honesty of the measured numbers. This is stated openly rather than hidden, because a reviewer reading this document should understand the project's intent.

The build targets a capability gap in the author's portfolio: existing work (a hybrid graph+vector RAG system over EU AI Act content) demonstrates *building with* LLMs. This project demonstrates *operating* them — failure modes, observability, cost control, and graceful degradation. Those are the FDE-relevant skills.

---

## 2. Goals

| # | Goal | Why it matters |
|---|---|---|
| G1 | Every model call in a fleet routes through one service with no per-app SDK changes | Adoption cost is the single biggest barrier to a gateway landing in a real org |
| G2 | Provider degradation is detected and routed around automatically, then healed automatically | This is the "self-healing" claim; without half-open recovery it is just a kill switch |
| G3 | Every request is attributable to a tenant, feature, and request class, with cost attached | Cost attribution is the primary commercial justification for a gateway |
| G4 | Failover never silently downgrades response semantics | Cohere-specific capabilities have no equivalent at fallback providers; a silent swap is a correctness bug, not a resilience feature |
| G5 | The whole system runs locally with one command and can be broken on demand | Reviewers clone repos; unprovable failover logic is worthless |

### 2.1 Non-goals

Explicitly out of scope for v1, to protect the timeline:

- Production-grade multi-tenancy (real authn/authz, tenant isolation, quota enforcement per tenant)
- Horizontal scaling, Kubernetes deployment, or high-availability of the gateway itself
- Semantic caching, prompt caching, or response deduplication
- PII detection and redaction in request logging
- Fine-tuning, evaluation harnesses, or model quality comparison
- A UI beyond Grafana dashboards
- Streaming responses (deferred to v1.1 — see §8)

Items in this list are candidates for a stretch phase *after* the demo video exists, never before.

---

## 3. Users

**U1 — Platform engineer (primary).** Owns the shared AI infrastructure for several product teams. Needs a way to enforce policy centrally and to survive a provider incident without paging every application team. Judges Keel on whether it reduces the number of people who get woken up.

**U2 — Application developer (adopter).** Wants to call a model. Does not want to learn a new SDK, and will route around the gateway if it adds friction or latency. Judges Keel on whether the migration is a base-URL change.

**U3 — Engineering manager / FinOps (beneficiary).** Needs to know which team and which feature is spending the money, and what a failover event costs. Judges Keel on whether the numbers exist at all.

**U4 — Technical reviewer (meta-audience).** Evaluating the author's engineering judgement. Judges Keel on whether the failure handling is real, the trade-offs are documented, and the claims are measured rather than asserted.

---

## 4. Success criteria

The project is done when all of the following are demonstrably true, measured against the local stack with the chaos harness active.

| ID | Criterion | Target |
|---|---|---|
| S1 | Availability sustained through a simulated primary-provider outage | ≥ 99% of interactive requests return a valid response |
| S2 | Time from provider degradation onset to traffic fully rerouted | p95 ≤ 2× the health-window length |
| S3 | Automatic recovery after the provider returns, with no operator action | Breaker closes within 3 successful half-open probe cycles |
| S4 | Cost attribution completeness | 100% of served requests carry tenant, feature, and request class; cost computed per request |
| S5 | Gateway latency overhead, excluding provider time | p95 ≤ 15 ms added |
| S6 | Deferrable work survives total provider unavailability | 0 lost jobs, 0 duplicated side effects across a full outage-and-replay cycle |
| S7 | Capability-safe failover | 0 instances of a capability-tagged request being served by a provider that cannot satisfy the tag |
| S8 | Cold start for a reviewer | `docker compose up` to healthy dashboard in ≤ 5 minutes on a clean machine |

**Resume claim this unlocks (to be filled with real measured values, not estimates):**
> Built a multi-provider LLM gateway with per-provider circuit breaking and capability-aware failover; sustained __% availability through simulated provider outages while attributing cost per tenant, feature, and request class.

---

## 5. Requirements

Priority: **P0** = required for the project to be considered complete. **P1** = strongly wanted, cut only under schedule pressure. **P2** = stretch, only after the demo exists.

### 5.1 Ingress and compatibility

| ID | Priority | Requirement |
|---|---|---|
| FR-1.1 | P0 | Expose an OpenAI-compatible chat completions endpoint so adoption is a base-URL and key change |
| FR-1.2 | P0 | Accept and validate request metadata: `tenant`, `feature`, `request_id` |
| FR-1.3 | P0 | Reject requests missing required metadata with a clear 4xx, rather than serving untagged traffic |
| FR-1.4 | P0 | Accept a `request_class` (e.g. `interactive_chat`, `classification`, `long_form`, `batch_enrichment`) that drives routing policy |
| FR-1.5 | P1 | Accept capability tags declaring what the request semantically requires (e.g. `citations`, `tool_use`, `structured_output`) |
| FR-1.6 | P2 | Support streaming responses end to end, including mid-stream failover semantics |

### 5.2 Provider abstraction

| ID | Priority | Requirement |
|---|---|---|
| FR-2.1 | P0 | Support Cohere as primary provider, configured as the default target for all request classes |
| FR-2.2 | P0 | Support at least two additional targets. Mock providers satisfy this initially; real fallbacks replace them as access lands |
| FR-2.3 | P0 | Provider set, preference order, and per-class policy are configuration, not code |
| FR-2.4 | P0 | Normalize heterogeneous provider errors into one internal taxonomy: rate limit, timeout, server error, content filter, auth failure, quota exhausted, bad request |
| FR-2.5 | P1 | Declare per-provider capabilities in config so the router can filter targets by capability tags |
| FR-2.6 | P1 | Support Azure OpenAI and AWS Bedrock as concrete fallback providers |

### 5.3 Health tracking

| ID | Priority | Requirement |
|---|---|---|
| FR-3.1 | P0 | Maintain a rolling window per provider: success rate, p50/p95/p99 latency, error counts by taxonomy class |
| FR-3.2 | P0 | Persist counters in Redis so a gateway restart does not reset the view of provider health |
| FR-3.3 | P0 | Expose Prometheus metrics covering request volume, outcome, latency, provider selection, and breaker state |
| FR-3.4 | P0 | Health tracking must be complete and observable *before* failover logic is built |

### 5.4 Failover

| ID | Priority | Requirement |
|---|---|---|
| FR-4.1 | P0 | Per-provider circuit breaker with closed, open, and half-open states |
| FR-4.2 | P0 | Trip on error rate above threshold within the window, or p95 latency above the class budget |
| FR-4.3 | P0 | On trip, route to the next eligible provider in the preference list for that request class |
| FR-4.4 | P0 | Half-open probes send a small share of traffic to a recovering provider; success closes the breaker, failure reopens it immediately |
| FR-4.5 | P1 | Preference lists differ per request class — a cheap classification call and a long-form generation call must not fail over identically |
| FR-4.6 | P1 | A request whose capability tags cannot be satisfied by any healthy provider must queue or fail explicitly, never silently downgrade |
| FR-4.7 | P1 | Hedged requests for latency-sensitive classes: fire a second provider after N ms, take the first response, cancel the loser |
| FR-4.8 | P1 | Hedging is disabled by default and its cost multiplier is documented |

### 5.5 Deferrable work

| ID | Priority | Requirement |
|---|---|---|
| FR-5.1 | P1 | Classify requests as interactive or deferrable at the API boundary; interactive fails fast, deferrable survives an outage |
| FR-5.2 | P1 | Queue deferrable work in Redis with exponential backoff and jitter when all providers are degraded |
| FR-5.3 | P1 | Idempotency keys prevent a retry from duplicating a side effect |
| FR-5.4 | P2 | Expose job status so a caller can poll for a deferred result |

### 5.6 Cost attribution

| ID | Priority | Requirement |
|---|---|---|
| FR-6.1 | P0 | Compute cost per request from a pricing table keyed by provider and model |
| FR-6.2 | P0 | Emit cost as a metric dimensioned by tenant, feature, and request class |
| FR-6.3 | P1 | Surface the cost delta of failover — what routing away from the primary costs per workload class |
| FR-6.4 | P2 | Per-tenant budgets with enforcement (soft warn, hard reject) |

### 5.7 Observability and provability

| ID | Priority | Requirement |
|---|---|---|
| FR-7.1 | P0 | Grafana dashboard with: RPS by provider, error rate, p95 latency, circuit state timeline, failover events, queue depth, cost per hour by tenant and feature |
| FR-7.2 | P0 | Chaos control endpoint that injects configurable error rates and latency into a chosen provider on demand |
| FR-7.3 | P0 | Structured request logs with correlation by `request_id` across retries and failovers |
| FR-7.4 | P0 | A ≤ 90 second screen capture showing healthy traffic → degradation → breaker trip → reroute → recovery, placed at the top of the README |

### 5.8 Non-functional

| ID | Priority | Requirement |
|---|---|---|
| NFR-1 | P0 | Entire stack runs via `docker compose up` with no cloud dependency beyond provider API keys |
| NFR-2 | P0 | Circuit breaker logic is unit-testable against a controllable clock and fault-injecting mock providers — no live API calls in the test suite |
| NFR-3 | P0 | Total spend across the build stays under €75; load testing uses the cheapest available model tier |
| NFR-4 | P1 | Configuration is validated at startup with clear errors, not discovered at first request |
| NFR-5 | P1 | Gateway adds no unbounded memory growth under sustained load |

---

## 6. Constraints and assumptions

**C1 — Provider access is asynchronous.** Only Cohere access exists today. AWS Bedrock model access and Azure OpenAI quota require approval processes with unpredictable turnaround. *Mitigation: the mock provider is a first-class component, not a stopgap. The full failover engine is built and tested against mocks, and real providers are substituted later. A mock remains in the final topology permanently as a controllable failure source.*

**C2 — Cohere capabilities are not portable.** Structured citations, safety modes, and Cohere's tool-use format have no clean equivalent at fallback providers. *Mitigation: capability tags (FR-1.5, FR-2.5, FR-4.6).*

**C3 — Budget.** Real-provider load testing costs money and is bounded by NFR-3. Sustained load is generated against mocks; real providers see low-volume correctness traffic and the demo run only.

**C4 — Rate limits are a first-class error class.** Self-inflicted rate limiting during load tests will trip breakers. This is treated as a legitimate test case rather than a bug to work around.

**C5 — Single operator, ~10 hours per week.** No parallelization. Sequencing must keep the project demoable at the end of every phase.

**C6 — Library support.** Provider normalization depends on a routing library supporting the required models across all three surfaces. Verification of current support is a week-zero task; a gap here changes the technical design, not this PRD.

---

## 7. Risks

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Provider access never lands for one fallback | Medium | Medium | Mocks are permanent citizens; two real providers is sufficient, one real plus mocks still demos the logic |
| Mock provider becomes its own project | High | High | Hard scope cap: latency distribution, error rate, error type, one chaos endpoint. Nothing else |
| Scope creep into stretch goals before the demo exists | High | High | Nothing from §2.1 starts until FR-7.4 is recorded |
| Cost overrun from hedging or load tests | Medium | Low | Hedging off by default, budget alerts at 50/80/100% of €75 |
| Cross-provider error normalization is deeper work than estimated | Medium | Medium | It is scheduled as its own phase alongside real-provider integration, not bundled into failover |
| Availability numbers are unimpressive under honest measurement | Low | Medium | Report measured values. An honest 97.3% with a good explanation beats a fabricated 99.9% |

---

## 8. Future scope (post-v1)

Ordered by expected value, not by ease:

1. Streaming support with defined mid-stream failover semantics
2. Model tiering router — route easy requests to a small model, degrade large→small under pressure
3. Semantic cache layer
4. Per-tenant rate limits and enforced budgets
5. Request logging with PII redaction
6. Same-model-different-cloud topology (Cohere on native API, Bedrock, and Azure simultaneously) as a data-sovereignty variant — the config-driven routing table already permits this

---

## 9. Open questions

| # | Question | Needed by |
|---|---|---|
| Q1 | Which two fallback providers land first, and with which models? | Phase 4 |
| Q2 | Does the OpenAI-compatible request shape cleanly carry Cohere tool-use and citation semantics, or does an extension field become necessary? | Phase 1 |
| Q3 | What is the health window length — fixed, or per request class? | Phase 2 |
| Q4 | Is the deferrable queue a separate worker process or in-process background tasks? | Phase 5 |
| Q5 | Does the demo showcase cross-vendor failover, the sovereignty variant, or both? | Phase 6 |

---

## 10. Document control

This PRD defines *what* is being built and *why*. It does not define implementation. Component design, data models, and interfaces live in `TECHNICAL-DESIGN.md`. Where those documents conflict with this one, this one wins, and the conflict is a signal that this document needs revision.
