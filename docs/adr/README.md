# Architecture Decision Records

An ADR records one decision: what was decided, what the situation was that forced the decision, and what it costs. It is written when the decision is made and is never rewritten afterwards — a decision that turns out wrong gets a *new* ADR that supersedes the old one, so the reasoning that looked correct at the time stays readable.

## What belongs here, and what does not

The technical design already carries a decision table ([`../TECHNICAL-DESIGN.md`](../TECHNICAL-DESIGN.md) §9, D1–D9) — one row each, decision and rejected alternative. That table is the index of decisions already made before the build started.

An ADR is for a decision that needs more than a table row:

- It was made *during* the build, in response to something learned by writing code
- It reverses or narrows something in §9, or resolves an open question from PRD §9 / technical design §11
- The reasoning is long enough that compressing it to one row would lose the argument

A decision that fits comfortably in the §9 table should stay in the §9 table. Two places recording the same decision is how documentation starts lying.

## Numbering

Files are named `NNNN-short-kebab-case-title.md`, starting at `0001`.

- **Four digits, zero padded.** `0001`, `0002`, … `0042`. Fixed width so the directory sorts chronologically in any file listing.
- **Monotonically increasing, never reused.** If an ADR is withdrawn before acceptance, its number is retired with it. Gaps are fine; a reused number breaks every reference to it.
- **The number is permanent.** The title in the filename may be clarified, the number never changes.
- **Superseding does not renumber.** ADR 0007 replacing ADR 0003 leaves 0003 in place with its status changed to `Superseded by 0007`, and 0003 stays readable exactly as written.

## Status values

| Status | Meaning |
|---|---|
| `Proposed` | Written, not yet acted on |
| `Accepted` | Decided and being implemented |
| `Superseded by NNNN` | Replaced by a later ADR. The record stays; the decision does not |
| `Deprecated` | No longer applies and nothing replaced it — the constraint that forced it is gone |

## Template

```markdown
# NNNN — Title as a decision, not a topic

**Status:** Proposed | Accepted | Superseded by NNNN | Deprecated
**Date:** YYYY-MM-DD
**Relates to:** requirement IDs (FR-x.x, NFR-x, S-x), phase, or design section

## Context

The situation that forces a decision. Constraints, measurements, what was
tried. Written so someone who was not there can tell why this was hard.

## Decision

What was decided, in the active voice.

## Consequences

What this costs, what it rules out, and what has to be revisited if the
constraint that forced it goes away. Include the bad parts — an ADR with
no downsides listed is a decision that was not actually examined.

## Alternatives considered

What else was on the table and why it lost.
```

Title the ADR as the decision itself — "Capability filter runs before preference ordering", not "Routing order". A directory of topics is unreadable; a directory of decisions is a changelog of thinking.
