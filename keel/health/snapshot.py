"""One provider's health, counts and latency together, at one instant.

This is the object Phase 3's breaker reads. :mod:`keel.health.window` merges the
counts, :mod:`keel.health.latency` merges the samples, and this composes the two
into the single view §5.6 needs to evaluate both of its trip conditions —
``error_rate > threshold`` and ``p95 > class budget`` — without asking two
questions that could be answered from two different moments.

**Nothing consumes this yet, deliberately.** FR-3.4 orders health tracking ahead
of anything that reacts to it, and PHASE-2-PLAN §5's tripwire is explicit that the
breaker does not arrive until P2-T4 and P2-T6 make these inputs visible. This task
supplies the input and stops.

**The percentiles here are approximate and are not the ones to quote.** See
:mod:`keel.health.latency` — the README's latency numbers come from P2-T4's
Prometheus histograms.

Unknown is not zero
-------------------

Three distinct "no answer" cases, and collapsing any two of them would hand the
breaker a confident wrong number:

===============================  ==========================================
Redis unreadable                 :meth:`HealthTracker.snapshot` returns ``None``
Reachable, no traffic            ``total == 0``, ``success_rate`` and every
                                 percentile ``None``
Counts present, no samples       counts real, percentiles ``None``
===============================  ==========================================

The first two are ADR 0008's rule for the window, restated. The third is new here
and is genuinely reachable: an older build wrote counts without samples, or a
pipeline applied partially. It matters because "this provider is failing and we do
not know how slow it is" must not read as "this provider is failing and is
infinitely fast".

The per-class gap, stated rather than hidden
--------------------------------------------

``ProviderHealth`` has **no request-class dimension**, because §5.5's key schema
has none: ``keel:latency:{provider}:{bucket_epoch}``. But
``latency_budget_p95_ms`` is configured **per request class** — 800 ms for
``classification`` and 60000 ms for ``batch_enrichment`` in the shipped config.

So one provider serving both classes has **one p95 and two verdicts**. At 1200 ms
it is failing ``classification``'s budget and sitting comfortably inside
``batch_enrichment``'s, at the same instant, on the same evidence. Nothing in the
PRD, the technical design, or §11's Q3 resolution ("window is global, latency
*budgets* are per class") says which answer §5.6's ``p95 > class budget`` wants.

That is left open on purpose. Resolving it means either giving the key a class
dimension — tripling the keys and thinning each bucket's samples, making the
percentiles worse in the name of improving them — or deciding a rule the breaker
does not yet exist to need. Phase 3 owns it; this docstring and the §5.6 note
exist so it is met as a stated decision rather than discovered while writing the
trigger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from keel.health.latency import percentile
from keel.providers.errors import ErrorClass

if TYPE_CHECKING:
    # Import-time cycle, broken deliberately and in this direction.
    # ``window.py`` builds a ``ProviderHealth`` and so must import this module at
    # runtime; this module only needs ``WindowCounts`` to *annotate* one method
    # parameter, and `from __future__ import annotations` makes every annotation a
    # string. Pydantic validates fields, not method arguments, so nothing here
    # needs the real class at runtime. The dependency that matters — Redis access
    # depends on data shapes, never the reverse — points the way it should.
    from keel.health.window import WindowCounts

__all__ = ["ProviderHealth"]


class ProviderHealth(BaseModel):
    """What one provider did over the merged window: outcomes and latency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str

    ok: Annotated[int, Field(ge=0)]
    """Successful attempts in the window."""

    errors: Mapping[ErrorClass, int]
    """All seven classes, zero-filled — never partial.

    Carried whole rather than pre-summed because D7 splits them: three classes are
    not evidence about provider health at all, and only the breaker may decide
    which of these rows it is allowed to count. A recorder or a snapshot that
    summed them here would make that decision on its behalf, invisibly.
    """

    sample_count: Annotated[int, Field(ge=0)]
    """Latency samples the percentiles were computed from.

    Not the same as :attr:`total`, and the gap is informative rather than a bug.
    ``LTRIM`` caps each bucket at ``SAMPLE_CAP``, so a provider taking more than
    that per bucket has fewer samples than attempts — and the breaker should know
    how much evidence a percentile rests on before acting on it.
    """

    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    """``None`` when there were no samples. Never ``0.0`` — see the module docstring."""

    @property
    def total(self) -> int:
        """Every attempt in the window, however it ended.

        What ``breaker.min_requests_in_window`` is compared against in Phase 3 —
        the volume floor that stops two failures out of three reading as a 67%
        error rate.
        """
        return self.ok + sum(self.errors.values())

    @property
    def success_rate(self) -> float | None:
        """Successes over attempts, or ``None`` when there were no attempts.

        ``None`` rather than ``1.0``. A provider with no traffic is *unknown, not
        perfect*: returning a perfect score for an idle provider is exactly the
        reading that would make a breaker keep a dead circuit closed, and it is
        the same rule ADR 0008 fixes one layer down.
        """
        attempts = self.total
        if attempts == 0:
            return None
        return self.ok / attempts

    @property
    def error_rate(self) -> float | None:
        """The complement of :attr:`success_rate`, and ``None`` for the same reason.

        **Not the number §5.6's threshold compares against.** This counts every
        class; the breaker's rate counts only those whose ``counts_toward_breaker``
        is true (D7). Provided because the M2 error-rate panel wants the whole
        picture, and named so the two cannot be confused.
        """
        rate = self.success_rate
        if rate is None:
            return None
        return 1.0 - rate

    @classmethod
    def from_window(
        cls, counts: WindowCounts, samples: Sequence[float]
    ) -> ProviderHealth:
        """Compose merged counts and merged samples into one view.

        The single constructor, so the snapshot can never disagree with the window
        about what happened — the counts are carried across rather than recomputed.

        ``samples`` is sorted once here and read three times, rather than each
        percentile sorting its own copy.
        """
        ordered = sorted(samples)
        return cls(
            provider=counts.provider,
            ok=counts.ok,
            errors=counts.errors,
            sample_count=len(ordered),
            p50_ms=percentile(ordered, 50),
            p95_ms=percentile(ordered, 95),
            p99_ms=percentile(ordered, 99),
        )
