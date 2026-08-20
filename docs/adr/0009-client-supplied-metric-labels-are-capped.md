# 0009 — Client-supplied metric labels are capped

**Status:** Accepted
**Date:** 2026-08-20
**Relates to:** FR-3.3, FR-6.2, FR-7.1, NFR-5 · TECHNICAL-DESIGN.md §5.1, §6, §10 · PHASE-2-PLAN.md P2-T4 · `keel/observability/metrics.py`

## Context

§6 gives `keel_requests_total` the labels `tenant, feature, class, provider, outcome`, and `keel_cost_micros_total` the same two client-facing ones. Three of those are bounded by construction: `class` is validated against `config.request_classes`, `provider` is a config key, and `outcome` is a two-value enum.

`tenant` and `feature` are not bounded by anything. §5.1's envelope table describes each as simply "Cost and metric dimension", and `keel/api/envelope.py` enforces exactly one rule on them — that the value is not blank after stripping. No length limit, no character set, no allow-list. They are whatever the caller puts in `X-Keel-Tenant` and `X-Keel-Feature`.

`prometheus_client` creates a new child series per distinct label combination and **never evicts one**. So every distinct header value is permanently retained gateway memory plus a permanently larger scrape body.

Measured, rather than assumed:

| Distinct tenant values | Retained | Scrape body |
|---|---|---|
| 20 000 | 13.5 MB | 4.7 MB |

That is **706 bytes per series**, growing without bound, on an endpoint with no authentication of any kind — §10 already records "tenant identity is asserted by the caller and not verified". A loop sending a random `X-Keel-Tenant` header is therefore remote memory exhaustion against a gateway whose NFR-5 says it "adds no unbounded memory growth under sustained load". It also degrades Prometheus itself, since the scrape body grows with the attack.

Nothing in the PRD, the technical design, or ADRs 0001–0008 mentions cardinality at all. This is a gap in the design rather than a disagreement with it.

The constraint that makes this awkward is that the labels are genuinely wanted. FR-6.2 asks for cost "dimensioned by tenant, feature, and request class", and FR-7.1's dashboard includes "cost per hour by tenant and by feature". Dropping the dimension would satisfy NFR-5 by abandoning two requirements.

## Decision

**The metrics layer admits the first `LABEL_CAP` (64) distinct values of each client-supplied label and records everything after as the literal `other`.**

The cap lives in `keel/observability/metrics.py`, applies to `tenant` and `feature`, and is reached through `MetricsCatalogue.cap_tenant` / `cap_feature` so Phase 4's cost counter reuses it rather than reinventing it.

Three details carry the weight:

**It is first-seen-wins, not least-recently-used.** LRU would keep the busiest tenants, which is nicer to look at. But it also means a series can disappear and later reappear from zero, and a counter that resets is a lie to every `rate()` query over it. A stable admitted set plus one honest overflow bucket keeps every series monotonic.

**Overflow is folded, not dropped.** A request from an uncapped tenant still increments `keel_requests_total{tenant="other"}`. Total request volume stays true; only the attribution is lost.

**It warns once, naming the value.** The overflow case is by definition the one where something is generating values in a loop, so a line per request would turn a metrics problem into a disk problem.

64 is chosen against reality rather than theory: the shipped config has one tenant. The bounded worst case is 64 tenants x 64 features x 3 classes x 2 providers x 2 outcomes, about 49k series and roughly 35 MB — and, unlike today, it cannot grow past that however the gateway is used.

## Consequences

**This deviates from §6 as written, and the deviation is visible in the data.** A dashboard filtered to a specific tenant will silently show nothing if that tenant landed in `other`. Anyone reading a per-tenant panel needs to know the bucket exists, which is why §6 now documents it and why the log line names the first value that overflowed.

**Verified end to end, not just in unit tests.** 200 requests with distinct random `X-Keel-Tenant` headers through the real HTTP surface produced exactly **65** tenant label values — 64 real plus `other` — a 23 KB scrape body, and one warning. Without the cap the same traffic produces 200 series and keeps going.

**It is a mitigation, not authentication.** A caller can still consume all 64 slots with junk and push every real tenant into `other`, destroying attribution for as long as the process lives. That is a denial of *observability*, not of service, and the honest fix is authenticating the tenant claim — which PRD §3 puts out of scope ("Production-grade multi-tenancy … tenant isolation, quota enforcement per tenant"). Recorded so nobody mistakes this for a security control.

**The cap is process-lifetime and never resets.** A gateway restart clears it. In a long-running process the admitted set is whatever arrived first, which for a legitimate deployment is the right answer and for an abused one is not.

**Phase 4 must use it.** `keel_cost_micros_total` carries the same two labels, so a cost engine that passes raw header values would reintroduce the whole problem on a different metric. The capper is exposed as a public method for exactly that reason, and its docstring says so.

**A better fix exists and is not this one.** Validating `tenant` against a configured set at ingress would bound the label *and* reject nonsense at the door with a clear 400, which is strictly better observability and strictly better behaviour. It needs a tenant registry in config, a schema change, and a decision about what to do with unknown tenants in a gateway that today accepts all of them — more than P2-T4 can carry, and a change to the request contract rather than to the metrics. Worth revisiting if real multi-tenancy ever comes into scope.

## Alternatives considered

**Ship §6 exactly as written and document the risk.** Rejected: a known, trivially reachable memory-exhaustion vector is not something to leave live and describe in a limitations section. The gateway is a resilience demonstrator; shipping it with an unbounded input feeding unbounded memory undercuts the premise.

**Drop `tenant` and `feature` from the metrics.** Trivially bounded and the smallest diff. Rejected because it abandons FR-6.2 and half of FR-7.1's dashboard, and because the same two labels return on `keel_cost_micros_total` in Phase 4 — the problem would have to be solved then anyway, with less context.

**An allow-list in `config/keel.yaml`.** Explicit, operator-controlled, and reviewable in version control, which is genuinely better on every axis except cost. Rejected for P2-T4 on scope: it needs a new config model, entries in the shipped YAML, re-anchored cases in `tests/test_config.py`, and a §5.2 note — the same ripple that put P1-T5 an hour over budget. The cap is a smaller change that removes the unbounded-memory property today, and an allow-list can be layered on top later without changing any metric's shape.

**Hashing unbounded values into a fixed bucket space.** Bounds cardinality with no admission policy and no `other` bucket. Rejected because the resulting labels are unreadable, collisions silently merge unrelated tenants, and a dashboard showing `tenant="a3f9"` is worse than one showing `tenant="other"` — at least the second is honest about what it does not know.
