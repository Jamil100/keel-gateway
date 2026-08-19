# 0007 — Message refinement may only replace a server error

**Status:** Accepted
**Date:** 2026-08-19
**Relates to:** FR-2.4, FR-3.4 · TECHNICAL-DESIGN.md §5.4, §7, D7 · ADR 0004, ADR 0006 · PHASE-2-PLAN.md P2-T1 · `keel/providers/normalize.py`

## Context

P1-T5 shipped a best-effort error map: an ordered `isinstance` table over LiteLLM's exception classes, most-specific-first. It was labelled best-effort for a reason, and P2-T1 found the reason.

The P1-T7 live check sent a deliberately bogus `COHERE_API_KEY` and got back `error_class: server_error`. Reading LiteLLM 1.97.0's source explains it exactly. `_map_cohere_exception` matches auth only on the literal strings `"invalid api token"` and `"No API key provided."`, and its status arm is:

```python
elif hasattr(original_exception, "status_code"):
    if original_exception.status_code == 400 or ... == 498: raise BadRequestError(...)
    elif original_exception.status_code == 408: raise Timeout(...)
    elif original_exception.status_code == 500: raise InternalServerError(...)
```

A 401 satisfies the outer `hasattr` guard, matches no inner branch, falls out of the chain without raising, and never reaches the function's final `else`. Control returns to `exception_type`, which ends at a generic `raise APIConnectionError(message=f"{original_exception}\n{traceback}")`. The result is exactly the captured line:

```
litellm.APIConnectionError: CohereException - {"message":"Incorrect API key provided: ..."}
```

carrying a synthetic `status_code` of 500. Cohere throttling takes the same path, because that status arm has no 429 branch either.

This is not a misconfiguration and not something the isinstance table can reach. It is an upstream bug, on someone else's schedule, and its consequence is specific: **D7 excludes `AUTH_FAILURE` from the breaker and includes `SERVER_ERROR`.** Left alone, a wrong API key trips the Phase 3 breaker on a provider that is perfectly healthy — the gateway blaming an upstream for our own typo. That is the failure ADR 0004 exists to prevent at startup, arriving instead at request time.

Fixing it means reading the message, not just the type. And reading messages is how error mapping normally rots: a rule written against today's prose quietly starts matching the wrong thing, or stops matching at all, and nobody notices until a dashboard is wrong or a breaker misfires.

## Decision

**Two decisions, one file.**

**1. The mapping lives in `keel/providers/normalize.py`, not in `keel/providers/errors.py`.**

The P2-T1 task bullet says to fill in `errors.py`. It is the wrong home. That module fixes the §5.4 *vocabulary* — the seven classes and the truth table saying what each means to the breaker — and it is imported transitively by the health window, the metrics catalogue, and the breaker. None of those should have to read a table about LiteLLM's exception tree to find out what `RATE_LIMIT` means. `errors.py` is untouched by this task; the mapping moves to a sibling and `keel/providers/cohere.py` keeps `normalize_litellm_error` as a thin delegate that supplies the Cohere identity.

**2. A message refinement rule may only replace `SERVER_ERROR`.**

Classification runs the type table first. Only when it produces `SERVER_ERROR` — the least informative answer, and the one the connection-level wrapper produces — are the provider's message rules consulted. A rule can never override a specifically-typed classification.

Rules are scoped per `AdapterName` and deliberately narrow. Cohere's auth rule matches `incorrect api key provided`, `invalid api token`, or `no api key provided` — each an assertion about the key's *validity*, not a mention of keys. A bare `api key` would match a `BadRequestError` complaining that `api_key` is a required parameter. Two of the three are the exact literals LiteLLM's own mapper treats as its auth signal, so we share fate with upstream rather than guessing independently.

No rule keys on status. The observed status on the wrapping `APIConnectionError` is 500, not the real 401 — gating on status would break the case the rule exists to fix.

## Consequences

**The gate turns the whole layer's safety into one assertable property.** "Refinement may only replace `SERVER_ERROR`" is a single test over the whole corpus, rather than a judgement call per rule about whether this pattern is tight enough. `tests/test_provider_normalize.py` asserts it, and separately asserts that no rule moves a breaker-excluded class into a breaker-counted one.

**It also fixes the direction of failure.** Because a rule can only *add* information, a LiteLLM release that rewords a message makes the rule stop firing and land back on today's `SERVER_ERROR`. That is a regression to the status quo — never a crash, never a new misclassification. The cost is that the failure is silent, which is why every rule must be exercised by a fixture and the corpus records the library versions it was captured against.

**The cost, and it is a real one: Azure content filters stay mislabelled.** LiteLLM raises `ContentPolicyViolationError` only when a body contains both `invalid_request_error` and `content_policy_violation`. Azure's `ResponsibleAIPolicyViolation` body contains neither, so an Azure content filter arrives as a plain `BadRequestError` and classes `BAD_REQUEST`. Correcting that needs a `BAD_REQUEST → CONTENT_FILTER` refinement, which this decision forbids. The consequence is confined: D7 excludes both classes from the breaker, so no circuit decision changes — only the M2 taxonomy panel's label, for a provider that has no adapter until Phase 4 (ADR 0004). `tests/fixtures/providers/azure/content-filter-responsible-ai.json` pins the *current* behaviour and explains why, so the day someone widens the invariant that fixture fails and forces the decision into the open rather than letting it happen by accident.

**A refined error keeps LiteLLM's wrong status code.** A bad key normalizes to `AUTH_FAILURE` while still reporting `status_code: 500`. ADR 0006 already establishes this field drives nothing — the client's HTTP status is looked up from the `ErrorClass` — so the wrong number is inert. Recovering the real status would mean parsing LiteLLM's message format, which is the churn surface this design exists to keep small.

**Fixtures cannot prove what they look like they prove.** A green replay shows the mapping still turns *this* text into *that* class. It cannot show a provider still emits that text. Only `scripts/capture_error_fixtures.py`, run deliberately at a phase gate, checks the other direction. The suite must not be read as evidence that the mapping is current.

**One bug found in passing.** `litellm.exceptions.OpenAIError` derives from `openai.OpenAIError`, which sits *above* `openai.APIError` — so it was the one member of `litellm.LITELLM_EXCEPTION_TYPES` an `APIError`-terminated table missed, falling to the unmapped default. A row was added, and a test now iterates LiteLLM's own published catalogue so the next addition fails CI instead of arriving as a production `WARNING`.

## Alternatives considered

**Replay fixtures through `litellm.exception_type`.** It works offline, in under 10 ms, with no network — I checked. Rejected because it is the thing under test: the bug being fixed *is* one of its mis-mappings, so fixtures replayed through it would assert LiteLLM's current opinion rather than Keel's, and the next `pip install -U litellm` would rewrite the expected values. It also raises rather than returning, appends `traceback.format_exc()` to its message so the text is non-deterministic, prints a feedback banner unless a module global is mutated, and reads ambient LiteLLM config. And it does not even remove hand-authorship — its input must be a duck-typed provider exception, so you hand-write a fake SDK exception to avoid hand-writing a LiteLLM one.

**Run message rules first, for any exception.** More reach for wrapped errors generally. Rejected because it inverts the risk: a genuine TCP reset whose text happened to quote a request body could be reclassified across a D7 line on the strength of a substring. The gate exists precisely so the type table always wins where it has an opinion.

**Parse the wrapped provider JSON and classify from structured data.** More faithful than substring matching, and it would recover the real status code. Rejected for now: it means owning a parser for LiteLLM's message format — `CohereException - {...}`, `Error code: 401`, `BedrockException: Rate Limit Error - {...}` — which is a larger and more brittle surface than three tight substrings, for a benefit ADR 0006 already made inert. Worth revisiting in Phase 4 when three providers' formats are in hand rather than one.

**An explicit transition allow-list** — `{(SERVER_ERROR, AUTH_FAILURE), (SERVER_ERROR, RATE_LIMIT), (BAD_REQUEST, CONTENT_FILTER), ...}` — generalizing the gate instead of fixing it at one source class. This is the natural way to fix the Azure case, and it keeps the invariant reviewable. Rejected for P2-T1 only because Azure has no adapter, so the pair it would exist to serve cannot be captured or exercised yet; adding machinery ahead of the evidence is what the §5 scope tripwires are for. It is the first thing to reach for in Phase 4, and the shape of `_REFINABLE` was left as a frozenset so widening it is a one-line, one-test change.
