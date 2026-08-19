# 0006 — Upstream failures map to HTTP status by normalized error class

**Status:** Accepted
**Date:** 2026-08-19
**Relates to:** FR-1.1, FR-2.4, FR-3.4 · TECHNICAL-DESIGN.md §4, §5.4, D7 · ADR 0003 · PHASE-2-PLAN.md P1-T7

## Context

P1-T7 put an HTTP endpoint in front of the executor, and immediately hit a question the task bullets never raise. `Executor.execute` returns a failure rather than raising one — that is the P1-T3 contract, deliberately chosen so a provider failure cannot unwind past the one place obliged to record it (D-C). But a `ProviderResult` carrying a `NormalizedError` is not an HTTP response, and something has to decide what status the client sees.

§4 answers this once, in the sequence diagram:

> ```
> else request is interactive
>     E-->>C: 503 with normalized error
> ```

That line is about the *exhausted candidates* case, and in Phase 1 it is trivially true: there is one candidate, so one failure exhausts the list. Reading it literally gives a one-line rule — every non-ok result is a 503 — and `KeelError`'s docstring already reserves 503 for exactly this.

The problem is what that rule does to the two error classes that are not about the provider at all. `BAD_REQUEST` means the provider rejected the payload the client sent. `CONTENT_FILTER` means the provider declined to answer it. Neither will resolve on its own, and neither has anything to do with availability. An OpenAI-SDK client reading a 503 does the obvious thing — backs off and retries — and retries a request that cannot succeed, indefinitely, against a gateway whose only honest answer is "stop sending this."

FR-1.1 is what makes that a real cost rather than a pedantic one. The whole premise is that adoption is a base-URL change, which means clients arrive with retry policies already written against OpenAI's status codes. A gateway that is wire-compatible in its success path and lies in its failure path is compatible in the way that matters least.

The taxonomy needed to do better already exists and is already normalized. §5.4's `ErrorClass` was built in P1-T3 precisely so the breaker sees one concept where three providers report three — and the same normalization is what lets one status table serve every provider Phase 4 adds.

## Decision

An upstream failure's HTTP status is looked up from its `ErrorClass`, in a table transcribed by hand in `keel/api/errors.py`:

| `ErrorClass` | Status | Exception |
|---|---|---|
| `bad_request` | 400 | `UpstreamBadRequestError` |
| `content_filter` | 400 | `UpstreamBadRequestError` |
| `rate_limit` | 429 | `UpstreamRateLimitError` |
| `quota_exhausted` | 429 | `UpstreamRateLimitError` |
| `timeout` | 504 | `UpstreamTimeoutError` |
| `auth_failure` | 503 | `UpstreamUnavailableError` |
| `server_error` | 503 | `UpstreamUnavailableError` |

The split is not four arbitrary buckets. It reproduces a distinction the codebase already makes: **the two classes that map to 400 are exactly the two that D7 excludes from the breaker.** That is not a coincidence to be noted and moved past — it is the same fact read twice. The breaker excludes them because they are not evidence about provider health; the status table refuses to tell the client to retry them for the same reason. If a future edit moves a class across one of those lines, it almost certainly has to move across the other, and the comment in the table says so.

`auth_failure` stays a 503 despite being a gateway fault rather than a provider one, because from the client's side it is indistinguishable from unavailability and there is nothing the client can do about either. ADR 0004 already ensures the common case — an unset key — never reaches a request at all.

The body shape follows ADR 0003 unchanged: the OpenAI error envelope with `error.keel` nested inside, and `fields` present and empty because no `FieldProblem` describes something the caller could edit. Two keys are added under `error.keel` for this family:

```json
{"error": {"message": "Upstream provider 'cohere_primary' failed (rate_limit): ...",
           "type": "api_error", "code": "upstream_rate_limit",
           "keel": {"request_id": "req-1", "fields": [],
                    "provider": "cohere_primary", "error_class": "rate_limit"}}}
```

`error_class` carries the taxonomy value under its own labelled key and **never becomes `error.code`**. ADR 0003 keeps `ProblemCode` and `ErrorClass` as separate vocabularies so a malformed client request cannot look like provider degradation; this table renders one as HTTP without merging it into the other.

The table refuses to import if an `ErrorClass` has no row — the same guard P1-T3 put on `counts_toward_breaker`, and for the same reason. Discovering an unmapped class as a `KeyError` inside a live request means discovering it during an incident.

## Consequences

**Three statuses now mean "the gateway worked and the provider did not", and only one of them is 503.** Anything counting 5xx as a gateway-health signal will undercount, because a 400 from `CONTENT_FILTER` is a completely healthy gateway. The P2-T4 metrics are labelled by `ErrorClass` rather than by status precisely so the dashboards never depend on this mapping.

**§4's diagram is now narrower than the code.** Its 503 remains correct for the exhausted-candidates case it describes; it is simply no longer the only outcome. That divergence is stated here and in the endpoint's comments rather than left for someone to find by diffing.

**Phase 3 has to decide which failure names the status.** With failover, several candidates can fail with different classes, and the last one is not obviously the right one to report — a `TIMEOUT` after a `BAD_REQUEST` probably should not become a 504. The table itself does not change; the choice of *which* `NormalizedError` to hand it does, and that decision belongs with the failover loop.

**A provider's own status code is discarded.** `NormalizedError.status_code` is preserved on the model but does not drive this mapping, because passing an upstream 502 straight through would make the gateway's response depend on which provider happened to be first — the exact coupling normalization exists to remove.

## Alternatives considered

**Every failure is a 503.** The literal reading of §4, and the cheapest thing to build. Rejected on FR-1.1: it tells an SDK client to retry a `CONTENT_FILTER` rejection forever, and the whole value proposition is that existing clients keep working correctly.

**Pass the provider's own HTTP status through.** Maximally faithful to what happened, and it destroys the abstraction — the same logical failure would produce different statuses depending on which provider served, which is what §5.4 normalization exists to prevent. It also has no answer for gateway-side timeouts, which never had an upstream status.

**Defer the table to Phase 3, ship 503 now with a named seam.** Consistent with this repo's habit of deferring rather than guessing. Rejected because the mapping is not made cheaper by waiting — Phase 3 changes *which* error is reported, not what each error means — and shipping M1 with a known-wrong retry signal makes the milestone demo misleading in the one dimension FR-1.1 cares about.
