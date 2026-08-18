"""Resolving a request class into an ordered list of providers to try.

The router answers one question — *given this envelope, which providers, in
what order?* — and TECHNICAL-DESIGN.md §5.7 gives it three steps to get there:
filter by capability, order by the class's preference list, then gate on
breaker state. **Only the middle step exists in Phase 1.**

That is deliberate rather than unfinished. Design principle 2 and FR-3.4 fix
the order of construction: health tracking must be complete and *visible*
before anything reacts to it, because a breaker whose inputs cannot be seen is
guesswork. So Phase 1 ships the seam and Phase 3 fills it, and the two absent
steps are marked below by name rather than left for a later reader to rediscover
from the design document.

**The router reads configuration, never adapters.** Capabilities live on
``ProviderConfig`` as well as on the adapter, and ``CohereAdapter.capabilities``
is explicit that enforcement is the router's job precisely so there is one
source of that truth rather than two that can disagree.

``KeelConfig`` has already guaranteed everything this module would otherwise
have to re-check: ``preference`` is non-empty (``min_length=1``), free of
duplicates (``_check_preference_unique``), and every name in it keys
``config.providers`` (``_check_preferences_reference_known_providers``). Those
are startup guarantees (NFR-4), so nothing here re-validates them at request
time.
"""

from __future__ import annotations

from keel.api.envelope import RequestEnvelope
from keel.config import KeelConfig, RequestClassConfig

__all__ = ["Router"]


class Router:
    """Turns one envelope into the ordered candidate list the executor walks."""

    def __init__(self, *, config: KeelConfig) -> None:
        self._config = config

    def candidates(self, envelope: RequestEnvelope) -> tuple[str, ...]:
        """The providers to try, best first, as config entry names.

        Entry names (``cohere_primary``) rather than adapter names (``cohere``):
        several entries may share one adapter, and the registry, metrics,
        health, and ``X-Keel-Provider`` all key on the entry.

        Returned as a tuple because the result is a decision, not a working
        list — the executor consumes it and Phase 3's failover loop iterates it,
        but neither may edit it.
        """
        class_config = _expect_class(
            self._config.request_classes.get(envelope.request_class),
            envelope.request_class,
        )

        # --- Phase 3 seam 1 of 3: capability filter (§5.7, D2) ------------------
        # Drop every provider whose `ProviderConfig.capabilities` does not cover
        # `envelope.capabilities`. It belongs *here*, above the preference
        # ordering, and that ordering is the single most load-bearing detail in
        # §5.7: a `citations` request must never see a provider that cannot
        # produce citations, however healthy it is or however high it sits in
        # the list. Filtering afterwards would let Cohere degrading turn into a
        # silent semantic downgrade that still returns 200. An empty result here
        # is the §5.7 `422` (or an enqueue, when the class is deferrable), which
        # is why the executor guards for an empty tuple it cannot yet receive.

        candidates = tuple(class_config.preference)

        # --- Phase 3 seam 2 of 3: breaker gate (§5.6, §5.7) --------------------
        # Drop providers whose breaker is OPEN, and admit a HALF_OPEN one only
        # when this request is drawn as a probe. Reads `keel/health/` — which is
        # why it cannot land before P2-T2 gives the breaker inputs to read.

        # --- Phase 3 seam 3 of 3: no candidate survives -------------------------
        # `deferrable` classes enqueue (§5.9); interactive ones get the `422`.
        # Both need `keel/api/errors.py` subclasses that do not exist yet — its
        # `KeelError` docstring already reserves the shape.

        return candidates


def _expect_class(value: RequestClassConfig | None, name: str) -> RequestClassConfig:
    """Narrow the resolved class config for mypy, and say so if it is absent.

    ``build_envelope`` rejects an unknown ``request_class`` before an envelope
    exists, so this cannot fire on the ingress path. It fires only when an
    envelope built against one config is routed with another — a wiring mistake
    that deserves a sentence rather than a bare ``KeyError`` from a dict index.
    Same idiom as ``keel/api/envelope.py``'s ``_expect_class``.
    """
    if value is None:
        raise RuntimeError(
            f"request class {name!r} is not configured; the envelope was built against a "
            f"different KeelConfig than the router holds"
        )
    return value
