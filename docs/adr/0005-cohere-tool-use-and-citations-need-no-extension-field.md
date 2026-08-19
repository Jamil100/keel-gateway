# 0005 — Cohere tool-use and citations need no extension field

**Status:** Accepted
**Date:** 2026-08-18
**Relates to:** PRD §9 Q2, TECHNICAL-DESIGN.md §11 Q2, §5.1, §5.2 · ROADMAP.md Phase 0 (spike, gate G0) · `keel/providers/cohere.py`

## Context

Q2 was flagged as the highest-risk unknown in the design (ROADMAP Phase 0: a dedicated 1.5h spike, with gate G0 — *"if the Q2 spike shows the OpenAI-compatible shape cannot carry Cohere semantics, update the technical design §5.1 before Phase 1 begins"*). It was never run as its own step: Phase 2 shipped `keel/providers/cohere.py` with `citations` and `tool_use` already declared as capability tags (§5.2's example, `config/keel.yaml`) on the assumption that LiteLLM's OpenAI-shaped response carries both. This ADR is that spike, run retroactively against the exact LiteLLM version `cohere.py`'s own error-mapping docstring already targets and verified (1.97.0, confirmed installed), by reading `CohereV2ChatConfig` (`litellm/llms/cohere/chat/v2_transformation.py`) and its OpenAI base class directly rather than trusting either library's documentation.

Two things had to be checked independently — tool-use round-trips in both directions (request and response), citations only in the response direction (Cohere has no request-side citation concept):

**Tool-use.** `map_openai_params` forwards OpenAI `tools` into Cohere's `tools` param unchanged (v2_transformation.py:161-162), and `transform_request` is a bare pass-through to `OpenAIGPTConfig.transform_request` because Cohere's v2 chat endpoint is already OpenAI-shaped (line 178, confirmed by the class's own docstring). On the response side, Cohere's `message.tool_calls` is converted into standard OpenAI `ChatCompletionToolCallChunk` objects on `message.tool_calls` (lines 223-238). Both directions need no extension field.

But `tool_choice` is listed as a supported param (`get_supported_openai_params`, line 130) and silently is not one: `map_openai_params` has no branch for it (lines 135-165), so a client's `tool_choice` never enters `optional_params`. `OpenAIGPTConfig.transform_request` builds the outgoing HTTP body as `{"model": ..., "messages": ..., **optional_params}` (gpt_transformation.py:435-439) — a key missing from `optional_params` is missing from the request Cohere receives. A caller who sends `tool_choice: "required"`, or names a specific function, gets it accepted at ingress and dropped before Cohere ever sees it, with no error anywhere in the path.

**Citations.** Cohere's native `message.citations` (`start`/`end`/`text` + `sources[]`, each source a full document object) is translated into OpenAI's `message.annotations` field, type `url_citation` (v2_transformation.py:212-220, 291-352) — the same field OpenAI itself uses for web-search citations, so again no extension field is needed. One Cohere citation with N sources becomes N annotations sharing one start/end span. The translation is lossy, though: only `title` and `url` survive per source (falling back to a synthetic `source:{id}` when Cohere supplies no URL); `snippet` and any other document metadata Cohere returns is discarded inside `_translate_citations_to_openai_annotations`. `annotations` is a properly declared field on `litellm.Message` (`types/utils.py:1241`) and is deleted when empty, so a response with no citations stays clean.

`CohereAdapter._as_openai_dict` (`keel/providers/cohere.py`) does a bare `model_dump()`/`dict(raw)` with no field-specific handling, so both `tool_calls` and `annotations` already flow into `ProviderResult.response` for free — nothing in the adapter needs to change for the response side of either capability.

## Decision

No extension field. The OpenAI-compatible shape is accepted as sufficient to carry Cohere's `tool_use` and `citations` capability tags (§5.2), for litellm 1.97.0's Cohere v2 transformation, as measured above rather than assumed. §5.1 is not revised; gate G0 does not fire.

`tool_choice` is recorded as a known, separate gap — not folded into "tool-use works" as if it were covered by this decision. It is out of scope today because no request class constrains tool choice yet, and is deferred to whichever phase first needs forced tool selection.

## Consequences

**Citation display loses per-source metadata below title/url.** If M2 or the demo wants to show a citation snippet, it is not present in `annotations` — recovering it would mean parsing Cohere's raw response before LiteLLM's transformation runs, which is exactly the provider-specific special-casing a `ProviderAdapter` exists to normalize away (§5.3). Cheaper to accept the loss than to reopen that boundary for one provider.

**`tool_choice` is silently unsupported for Cohere**, and *silently* is the expensive part — no 400, no warning, behavior indistinguishable from Cohere choosing freely. If a request class ever declares a hard requirement on forced tool use, the §5.7 capability filter has no tag for it today (`tool_choice` is not `tool_use`), and the router has no way to route around a provider that can't honor it.

**The spike ran after Phase 2 code shipped, not during Phase 0 as G0 specifies.** The outcome happens to match what Phase 2 already assumed, so nothing gets rebuilt — but that is the spike confirming a bet already placed, not the bet being placed after the spike. Recorded so the gap between "the gate blocks the phase" and "the gate actually ran" doesn't repeat unnoticed on the next open question.

## Alternatives considered

**Add a Cohere-specific extension field carrying the untranslated response** (e.g., stash the raw JSON body alongside the OpenAI-shaped one). Rejected: neither gap found needs it. Tool-use is complete; citations lose detail but keep the core span-plus-source data an M2 display needs. An extension field for a hypothetical future need is exactly the kind of unverified assumption Q2 existed to prevent adding.

**Fix `tool_choice` by post-processing the payload in `CohereAdapter._build_request`** to inject it past LiteLLM's filtering. Rejected for now: no request class exercises forced tool choice (checked — no `tool_choice` reference anywhere in `keel/` or `tests/`), so this would be speculative code against a requirement that doesn't exist yet. Named as a gap instead, matching the project's own pattern for `normalize_litellm_error`'s unmapped default (P2-T1) — surface it precisely so it's one grep away, don't paper over it with code nothing has asked for.

**Vendor a patched `CohereV2ChatConfig`** to fix `tool_choice` upstream-style. Rejected as disproportionate to a gap nothing currently exercises, and it would put Keel in the business of tracking a forked file against every LiteLLM upgrade for a three-line documented limitation.
