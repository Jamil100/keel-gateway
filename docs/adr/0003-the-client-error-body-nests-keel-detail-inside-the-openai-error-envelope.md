# 0003 — The client error body nests Keel detail inside the OpenAI error envelope

**Status:** Accepted
**Date:** 2026-08-18
**Relates to:** FR-1.1, FR-1.2, FR-1.3, S4 · TECHNICAL-DESIGN.md §5.1, §5.4, §5.7 · PHASE-2-PLAN.md P1-T2

## Context

That Keel rejects untagged traffic is not what this record decides. FR-1.3 requires a "clear 4xx" for missing metadata, technical design §5.1 requires "a machine-readable error listing the absent fields", and P1-T2 requires one body shape "used by every 4xx/5xx". Those were settled before any code existed.

Implementing `keel/api/errors.py` forced a question none of them answers: **what does that body actually look like?** The documents specify the properties and never once give a field name. There is no JSON schema, no example, no RFC reference anywhere in the PRD, the technical design, or the roadmap. The shape had to be invented, and inventing it in passing would leave the gateway's most-hit failure surface undesigned.

Three constraints bound the choice:

**The endpoint is OpenAI-compatible, and that is the whole adoption story.** FR-1.1 makes adoption "a base-URL and key change". A client that swaps its base URL keeps its OpenAI SDK — and every one of those SDKs parses errors by reaching for `error.message` and raising with it. A body those SDKs cannot parse produces a stack trace naming a `KeyError` rather than the sentence explaining which header is missing. The rejection path is precisely where a new adopter meets Keel first, so a body that degrades the SDK's own error reporting undoes the compatibility the endpoint exists to provide.

**It must list every problem at once.** A client fixing four headers across four round trips forms a lasting opinion of the gateway. So the body carries a *list*, not a single field name, and the list needs enough structure per entry — which field, which header would supply it, why it was rejected — to be actionable without reading prose.

**One shape has to cover errors that are not about fields at all.** The design already fixes `422` for an unsatisfiable capability set (§5.7) and `503` for exhausted candidates (§4). Those carry no field list. A shape built solely around validation would fork the moment Phase 3 lands.

## Decision

The body is the **OpenAI error envelope with a `keel` extension object nested inside it**:

```json
{
  "error": {
    "message": "Request rejected: 2 problems (tenant, request_class).",
    "type": "invalid_request_error",
    "code": "missing_metadata",
    "keel": {
      "request_id": "req-1",
      "fields": [
        {"field": "tenant", "header": "X-Keel-Tenant", "code": "missing",
         "message": "required; supply the X-Keel-Tenant header or x_keel.tenant in the request body"}
      ]
    }
  }
}
```

An unmodified OpenAI SDK surfaces `error.message`, which names the offending fields, so the default client experience is already useful. Anything that wants to branch reads `error.code` and `error.keel.fields` and never parses prose.

Three supporting choices ship with it:

- **`error.keel.fields` uses a closed `ProblemCode` vocabulary** — `missing`, `unknown_request_class`, `invalid`, `unknown_field` — and it is deliberately **not** the §5.4 `ErrorClass` taxonomy. `ErrorClass` describes provider behaviour and feeds the breaker; these describe client fault at the gateway boundary and must never reach it. Merging the two would let a malformed client request look like provider degradation, which is the same failure mode D7 exists to prevent.
- **`error.keel.request_id` echoes the caller's id, and is `null` when the id was itself the missing field.** A rejection a client cannot correlate with what it sent is half an error message.
- **`keel/api/errors.py` imports no web framework.** It raises and renders; `keel/api/app.py` registers the handler in P1-T7. That is what lets the whole validation boundary be tested with plain dicts and no HTTP (NFR-2).

## Consequences

**The body is now a wire contract, and renaming a key is a breaking change.** `tests/test_errors.py` asserts the shape key-by-key for exactly this reason — a rename must break a test rather than a client.

**Keel is committed to OpenAI's error vocabulary, including its vaguer parts.** `error.type` is `invalid_request_error` because that is what OpenAI uses for the 4xx band; it carries little information on its own, and the real signal lives in `error.code`. If the public surface ever stops being OpenAI-compatible, this envelope becomes an unexplained wrapper and should be revisited with a new ADR.

**Nesting under `error.keel` costs one level of indirection** for the machine-readable half, which is the price of leaving `error.message` where the SDKs look for it.

**Non-field errors carry an empty `fields` list rather than omitting the key.** Slightly redundant on a 503, and it keeps the shape stable so a client parses one structure for every failure it will ever see.

**The `keel` object is extensible, and P1-T7 was the first to extend it.** Upstream failures (ADR 0006) add `provider` and `error_class` alongside `request_id` and `fields`. That is the extension point working as intended — a client parsing the four documented keys is unaffected by a fifth — but it does mean `error.keel` is a superset by error family rather than one fixed record. Two rules keep that from drifting into a free-for-all: a key added for one family is never repurposed by another, and the §5.4 taxonomy value lives under its own `error_class` key and never becomes `error.code`, so the two vocabularies this ADR separates stay separated.

## Alternatives considered

**A flat Keel-native envelope** (`{"code": ..., "message": ..., "fields": [...]}`). Cleaner to read and a level shallower. Rejected because an OpenAI SDK client hits `KeyError: 'error'` and reports a parse failure instead of the reason for the rejection — worst exactly where FR-1.1's adoption promise is being tested.

**RFC 9457 `application/problem+json`** (`type`/`title`/`status`/`detail` plus a custom `fields` member). Standards-correct, and a real argument for a general-purpose API. Rejected because no OpenAI client understands it, so it would trade the adoption story for conformance to a standard nothing in this ecosystem reads. It also duplicates the HTTP status into the body, which invites the two to disagree.

**Returning only the first problem.** Simpler, and matches many frameworks' default validators. Rejected outright by P1-T2: a client fixing headers one round-trip at a time is a bad first impression of the gateway.
