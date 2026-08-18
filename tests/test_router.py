"""Tests for the static router (P1-T6, FR-1.4, TECHNICAL-DESIGN.md §5.7).

The router is deliberately the least clever component in the system right now,
and most of these tests exist to hold it that way. Two of them assert the
*absence* of behaviour — no capability filtering, no health awareness — because
Phase 3 adds both, and a test that pins today's behaviour turns that into a
visible edit rather than a silent change of meaning.

The expected candidate lists are transcribed by hand rather than read back from
the config, so the shipped `config/keel.yaml` and this file have to agree. Same
posture as the §5.4 truth table in `tests/test_provider_errors.py`: a test that
derives its expectation from the thing under test asserts nothing.

No network, no Redis, no clock at all (NFR-2).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from keel.api.envelope import RequestEnvelope
from keel.config import KeelConfig, load_config
from keel.routing.router import Router

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"


@pytest.fixture
def base_text() -> str:
    return SHIPPED_CONFIG.read_text(encoding="utf-8")


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def _write(text: str) -> Path:
        path = tmp_path / "keel.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write


def envelope(
    request_class: str = "interactive_chat",
    capabilities: frozenset[str] = frozenset(),
) -> RequestEnvelope:
    return RequestEnvelope(
        request_id="req-1",
        tenant="acme",
        feature="support-summary",
        request_class=request_class,
        capabilities=capabilities,
        deferrable=False,
        idempotency_key=None,
        payload={"model": "keel", "messages": [{"role": "user", "content": "hi"}]},
        received_at=0.0,
    )


def router(config: KeelConfig | None = None) -> Router:
    return Router(config=config if config is not None else load_config(SHIPPED_CONFIG))


# --------------------------------------------------------------------------
# The "Done when" — (request_class) -> ordered candidates, all three classes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_class", "expected"),
    [
        pytest.param("interactive_chat", ("cohere_primary", "mock_chaos"), id="interactive_chat"),
        pytest.param("classification", ("cohere_primary", "mock_chaos"), id="classification"),
        pytest.param("batch_enrichment", ("cohere_primary", "mock_chaos"), id="batch_enrichment"),
    ],
)
def test_each_shipped_class_resolves_to_its_preference_list(
    request_class: str, expected: tuple[str, ...]
) -> None:
    """P1-T6's exit condition. Expectations are hand-written, not read from config."""
    assert router().candidates(envelope(request_class)) == expected


def test_every_shipped_class_is_covered_by_the_table_above() -> None:
    """A class added to the config without a row above would otherwise go untested."""
    config = load_config(SHIPPED_CONFIG)

    assert set(config.request_classes) == {
        "interactive_chat",
        "classification",
        "batch_enrichment",
    }


# --------------------------------------------------------------------------
# Order is the whole point
# --------------------------------------------------------------------------


def test_candidates_preserve_preference_order_rather_than_set_order(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Reordering the config reorders the candidates, and nothing else has to change.

    This is design principle 1 made testable — policy is configuration — and it
    is the M1 exit criterion in miniature: the same request reaches a different
    provider because one line of YAML moved.
    """
    reversed_text = base_text.replace(
        "    preference: [cohere_primary, mock_chaos]\n"
        "    latency_budget_p95_ms: 4000\n",
        "    preference: [mock_chaos, cohere_primary]\n"
        "    latency_budget_p95_ms: 4000\n",
        1,
    )
    assert reversed_text != base_text, "mutation did not change the file; the anchor text moved"

    config = load_config(write_config(reversed_text))

    assert router(config).candidates(envelope("interactive_chat")) == (
        "mock_chaos",
        "cohere_primary",
    )
    # The other two classes were not touched, so they must not have moved.
    assert router(config).candidates(envelope("classification")) == (
        "cohere_primary",
        "mock_chaos",
    )


def test_a_single_provider_class_yields_one_candidate(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The mock-only path CI and `loadgen` use; also the shortest possible chain."""
    mock_only = base_text.replace(
        "    preference: [cohere_primary, mock_chaos]\n"
        "    latency_budget_p95_ms: 4000\n",
        "    preference: [mock_chaos]\n    latency_budget_p95_ms: 4000\n",
        1,
    )
    config = load_config(write_config(mock_only))

    assert router(config).candidates(envelope("interactive_chat")) == ("mock_chaos",)


def test_candidates_are_returned_as_a_tuple() -> None:
    """The result is a decision, not a working list; the executor must not edit it."""
    assert isinstance(router().candidates(envelope()), tuple)


def test_the_router_does_not_hand_out_its_config_list(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """A caller mutating the returned list must not rewrite routing policy.

    `KeelConfig` is frozen, but `preference` is a `list` and freezing a pydantic
    model does not freeze the list inside it. The tuple is what makes this safe.
    """
    config = load_config(write_config(base_text))
    candidates = router(config).candidates(envelope())

    assert candidates is not config.request_classes["interactive_chat"].preference


# --------------------------------------------------------------------------
# What the router deliberately does NOT do yet (Phase 3 seams)
# --------------------------------------------------------------------------


def test_capabilities_are_not_filtered_yet() -> None:
    """Pins today's behaviour so Phase 3's §5.7 filter is a visible test edit.

    `mock_chaos` ships deliberately without `citations` and `cohere_primary` has
    it, so this is the exact request that the D2 capability filter exists to
    protect. Today it still sees both providers. That is correct for Phase 1 —
    observe before you react (FR-3.4) — and wrong the moment Phase 3 lands, at
    which point this test should fail and be rewritten rather than quietly keep
    passing.
    """
    candidates = router().candidates(envelope("interactive_chat", frozenset({"citations"})))

    assert candidates == ("cohere_primary", "mock_chaos")


def test_an_unsatisfiable_capability_still_returns_every_candidate() -> None:
    """No provider offers `telepathy`; Phase 3 turns this into a 422, Phase 1 does not."""
    candidates = router().candidates(envelope("interactive_chat", frozenset({"telepathy"})))

    assert candidates == ("cohere_primary", "mock_chaos")


def test_the_router_needs_neither_a_registry_nor_a_clock() -> None:
    """Constructible from config alone — it resolves policy, it does not call anything.

    Health and breaker state arrive in Phase 3. Keeping the constructor this
    narrow is what makes the table above testable without building adapters.
    """
    assert Router(config=load_config(SHIPPED_CONFIG)).candidates(envelope()) == (
        "cohere_primary",
        "mock_chaos",
    )


# --------------------------------------------------------------------------
# Wiring mistakes say so
# --------------------------------------------------------------------------


def test_routing_an_envelope_built_against_another_config_is_an_error() -> None:
    """`build_envelope` cannot produce this; a mis-wired app factory can.

    The message names the class rather than surfacing a bare `KeyError` from a
    dict index, because the fix is at the wiring site and not in the request.
    """
    stray = envelope("a_class_from_another_deployment")

    with pytest.raises(RuntimeError) as excinfo:
        router().candidates(stray)

    assert "a_class_from_another_deployment" in str(excinfo.value)
