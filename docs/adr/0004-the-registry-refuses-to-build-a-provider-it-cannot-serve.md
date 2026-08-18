# 0004 — The registry refuses to build a provider it cannot serve

**Status:** Accepted
**Date:** 2026-08-18
**Relates to:** FR-2.1, FR-2.2, FR-2.3, FR-2.6, NFR-4 · TECHNICAL-DESIGN.md §5.2, §5.3, D7 · PHASE-2-PLAN.md P1-T5

## Context

Writing `keel/providers/registry.py` turned three constraints that each look reasonable into a set that cannot all hold at once.

| | Constraint | Source |
|---|---|---|
| 1 | The shipped `config/keel.yaml` declares four providers: `cohere_primary`, `azure_fallback`, `bedrock_fallback`, `mock_chaos` | §5.2, and the file itself |
| 2 | A missing credential must fail at **startup**, not at the first request | NFR-4, P1-T5 |
| 3 | An offline test builds the registry from that exact shipped file | P1-T5 "Done when" |

Only Cohere access exists today (PRD C1). `.env.example` keeps the Azure and AWS credential blocks commented out, and no adapter for either exists — `AdapterName` declares `azure_openai` and `bedrock` because the design specifies them, but Phase 4 is where FR-2.6 lands. So a registry built eagerly from the shipped config must construct two adapters that have no implementation and no credentials, and constraint 2 says it must not paper over that.

`.env.example` already anticipated the collision without resolving it:

> Commented out until the quota request is approved. Uncomment in Phase 4, and remove `azure_fallback` from every preference list in config/keel.yaml until then — a preference entry whose credentials are absent is a failover target that does not exist.

The shipped config never did that removal. The result was a repository whose own committed configuration could not have started the gateway — which nothing noticed, because until P1-T5 there was no code that tried.

The reason this matters more than it first appears is **D7**. A missing credential does not surface at request time as an obvious configuration fault; it surfaces as an `AUTH_FAILURE`, and D7 deliberately excludes that class from the breaker so that one bad API key cannot open every circuit at once. That exclusion is correct, and it has a consequence: a gateway that discovered credentials lazily would fail 100% of its traffic with every breaker closed, every error-rate panel reading "not provider degradation", and every dashboard green. The startup check is what stops the gateway's own misconfiguration from wearing a provider's clothes.

## Decision

`build_registry()` **raises `ConfigError` for any provider entry it cannot fully construct**, and returns nothing rather than a partial registry. Two cases qualify: an adapter with no implementation yet (`azure_openai`, `bedrock`), and an implemented adapter whose credential is unset or blank.

Every problem is collected and reported in one error rather than the first one found, matching what `load_config` and `build_envelope` already do — a deployment with two mistakes should cost one restart to diagnose, not two.

`config/keel.yaml` is trimmed to match: the `azure_fallback` and `bedrock_fallback` blocks are commented out in place, and all three preference lists become `[cohere_primary, mock_chaos]`. FR-2.2 is satisfied exactly as it anticipates — *"Mock providers satisfy this initially; real fallbacks replace them as access lands."*

`mock_chaos` gives up its `citations` capability in the same edit. That is not tidying. `citations` was the capability only Cohere offered, and the asymmetry between it and the two fallbacks is the entire argument for the §5.7 capability filter (D2) — the correctness bug wearing a resilience costume. Standing both fallbacks down would have left the shipped config perfectly symmetric, and Phase 3's filter with nothing to demonstrate against. Moving the asymmetry onto `mock_chaos` keeps it alive at the cost of one line. The mock never enforces capabilities, so nothing behaves differently today.

## Consequences

**The shipped config no longer matches §5.2's example.** §5.2 continues to document the four-provider end state, with a note pointing here. Two files describing the same thing differently is how documentation starts lying, so the divergence is stated in the config file, in §5.2, and here — not left for a reader to discover by diffing.

**Restoring a fallback in Phase 4 is a three-part edit, and all three parts are required.** Uncomment the provider block, uncomment the credentials in `.env`, and put the entry back in the preference lists. Doing only the first now fails startup with a message naming the adapter and this ADR, which is the intended behaviour rather than a rough edge — but it does mean the restoration cannot be done incrementally.

**Six anchors in `tests/test_config.py` moved.** Those tests mutate the real shipped file rather than a fixture, which is what makes them catch config drift; the cost is that trimming the config edits the test suite too. Two cases that targeted the `azure_fallback` block now mutate `mock_chaos` and `cohere_primary` instead, and one needed a two-part mutation because `_check_target_naming` reports a missing `deployment` before it reports a surplus `model`.

**A preference list is now two entries deep instead of three**, so Phase 3's failover chain is exercised one hop shallower than the design intends until Azure or Bedrock lands. A second mock entry would restore the depth cheaply if that turns out to matter — several config entries may share one adapter — and this ADR does not rule it out.

**Startup is now the only place a credential problem can appear**, which means a credential that is *rotated* while the gateway runs is not detected until the next restart. Acceptable: that failure arrives as a genuine `AUTH_FAILURE` from the provider, which is what the class is for, and D7's exclusion is correct in exactly that case. The startup check is about the gateway never having had a usable key, not about keys that stop working.

## Alternatives considered

**Skip unbuildable providers with a `WARNING`.** The shipped config stays untouched and no tests move — by far the cheapest option. Rejected because a skipped provider silently shortens a preference list, so failover has one fewer target than the operator believes it has and nothing says so until the primary is already down. `KeelConfig._check_preferences_reference_known_providers` exists to prevent that exact failure at the config layer; allowing the registry to reintroduce it one layer down would make that validator decorative. A warning in a startup log is not a control.

**Build placeholder adapters for the unimplemented ones**, returning a normalized failure on every call. The config stays whole and the preference chain keeps its declared depth. Rejected on two counts: it is code that exists only to be deleted in Phase 4, and it makes a provider that *cannot* be called indistinguishable on the M2 dashboard from one that is merely failing — polluting the error-rate panel that Phase 2's exit criterion is measured on.

**Check credentials lazily, at first use.** Rejected on NFR-4 directly, and on the D7 argument above: this is the option that produces a fully green dashboard during a total outage.

**Validate credentials inside `keel/config.py`.** Superficially tidy, since config validation already fails at startup. Rejected because it would put secrets in the module that parses a committed file, and because the credential a provider needs is a property of its *adapter*, which `config.py` deliberately knows nothing about beyond a dispatch key.
