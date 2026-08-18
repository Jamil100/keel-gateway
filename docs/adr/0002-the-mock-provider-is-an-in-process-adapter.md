# 0002 — The mock provider is an in-process adapter, not a container

**Status:** Accepted
**Date:** 2026-08-18
**Relates to:** FR-2.2, FR-7.2, NFR-1, NFR-2, S8 · TECHNICAL-DESIGN.md §2, §3, §5.3, §8, D1 · PHASE-2-PLAN.md D-A, P1-T4

## Context

That Keel has a mock provider is not what this record decides. D1 already settled that it is permanent rather than scaffolding — real providers refuse to break on cue during a demo, and NFR-2 forbids live API calls in the test suite. PRD constraint C1 goes further: only Cohere access exists today, so the entire failover engine is built and tested against mocks and real providers are substituted later.

Implementing it forced a question the documents answer two different ways. **They disagree about what the mock is.**

| Where | What it says |
|---|---|
| §8 deployment diagram | `MK["mock-provider:9001"]`, a compose service, with `GW --> MK` |
| §5.3 | "The mock **adapter** is a permanent component" — described alongside the other adapters |
| §3 container diagram | `AD4["Mock adapter"]`, inside the "Provider adapters" subgraph, with `CHAOS --> AD4` |
| ROADMAP Phase 1 | "Mock **adapter** with configurable latency, error rate, error class" |

Three of the four describe an in-process adapter; one draws a container listening on a port. The diagram is the outlier, and it was drawn before the adapter protocol existed. Writing `keel/providers/mock.py` meant picking one, because they produce genuinely different code: a container needs an HTTP client, a serialization boundary, a health check, and a place in the compose dependency graph, and none of that exists for an adapter.

Three things push toward the adapter.

**NFR-2 wants it callable from a unit test with no network.** This is the decisive one. The mock is the fault-injection source for every breaker, hedging, and failover test in the project. As a container, exercising the breaker state machine means either standing up a service in CI or writing a second in-process fake to stand in for the mock — at which point there are two mocks with two behaviours, and the one the tests use is not the one the demo uses. That is precisely the divergence D1 exists to prevent.

**The chaos API mutates gateway-side state either way.** FR-7.2 puts the chaos control endpoint on the gateway, and §3 already draws `CHAOS --> AD4` pointing at the adapter. A container would mean the gateway's chaos endpoint proxying to a second service's chaos endpoint — one more hop, one more failure mode, and no capability the direct call lacks.

**One fewer container helps S8.** `docker compose up` must reach a healthy dashboard in under five minutes on a clean machine. Every service is an image pull, a healthcheck, and a `depends_on` edge.

## Decision

The mock provider is an **in-process `ProviderAdapter`** living at `keel/providers/mock.py`. There is no `mock-provider` container, and `mock-provider:9001` is removed from the §8 deployment diagram — both the node and the now-dangling `GW --> MK` edge.

The mock stays a permanent *logical* component: §2's system-context `P4["Mock provider"]` and §3's `AD4["Mock adapter"]` are unchanged. What goes away is the container, not the component.

Its scope stays hard-capped. §5.3 and the PRD risk register cap it at *latency distribution, error rate, error type, and one chaos endpoint*. P1-T4 implements the first three plus a **seeded RNG**, which occupies the chaos-endpoint slot because the endpoint itself is Phase 6 work (FR-7.2). The cap did not widen; the fourth item is deferred, not traded for something larger.

## Consequences

**The mock cannot simulate connection-level failures.** DNS resolution failure, TCP reset, a half-open socket, TLS handshake failure, a truncated response body — none of these are reachable from an in-process function call. This is the real cost, and it is not small: those are among the more interesting ways a provider fails in production, and Keel's error taxonomy has no way to observe them being produced. What the mock *can* produce is every failure that arrives as a normalized `ErrorClass`, which is the layer the breaker actually reads. If connection-level fault injection is ever needed, it belongs at a different layer — a proxy or a network-level fault injector between the gateway and a real provider — not by reversing this decision.

**`httpx` is no longer needed for the mock.** It stays a dependency for LiteLLM and FastAPI's `TestClient`, but the comment in `pyproject.toml` that justified it as "async HTTP for the mock provider service" is now wrong and is retargeted with this ADR.

**The mock shares a process with the gateway**, so a bug in it — an unbounded loop, a memory leak — takes the gateway with it rather than being isolated behind a port. Acceptable because its scope cap keeps it to a few dozen lines with no I/O and no unbounded allocation, and NFR-5 is asserted against the gateway as a whole regardless.

**Chaos state is per-process.** If the gateway is ever run with multiple replicas, each holds its own `MockChaosState` and a chaos API call reaches exactly one of them. Not a concern for a single-container compose stack; it would need revisiting before any horizontal scaling, and the deferred worker (D6) is a separate process that does not share this state.

## Alternatives considered

**Keep `mock-provider:9001` as a container.** The strongest argument for it is fidelity: it exercises the real HTTP path, so the adapter's serialization, timeout, and connection handling are tested against a socket rather than a function call, and connection-level faults become injectable. Rejected because it makes NFR-2 unreachable without a second in-process fake, and two mocks that can disagree is worse than one mock that cannot simulate a TCP reset. The S8 cold-start cost is real but secondary.

**Both — an in-process adapter for tests and a container for the demo.** Rejected for the reason above, stated plainly: it is the two-mocks problem by construction. The demo would exercise code the test suite never runs, which inverts what the mock is for.

**A container that the adapter talks to only in integration tests.** A narrower version of the same idea, and it survives longer than the others — but it still means the chaos API has two targets with two state stores, and §7's integration layer is already specified as "full compose stack with mock providers only", which the in-process adapter satisfies unchanged.
