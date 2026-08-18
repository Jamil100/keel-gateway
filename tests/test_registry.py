"""Tests for the provider registry (NFR-4, FR-2.3, ADR 0004).

The registry's job is to be the last place a configuration mistake can hide.
Everything it refuses, it refuses at startup — because the alternative is worse
than it sounds: a missing credential surfaces at request time as an
``AUTH_FAILURE``, which D7 deliberately excludes from the breaker, so a lazily
built registry would fail 100% of traffic with every breaker closed and every
dashboard green. Several tests below exist to pin exactly that.

Credentials are injected rather than read from the environment, so these tests
neither depend on nor are broken by whatever happens to be exported in the shell
running them (NFR-2). No network, no Redis, no real time.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from keel.api.envelope import RequestEnvelope
from keel.clock import ManualClock
from keel.config import ConfigError, KeelConfig, load_config
from keel.providers.cohere import CohereAdapter
from keel.providers.credentials import ProviderCredentials
from keel.providers.mock import MockAdapter
from keel.providers.registry import build_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"

CREDENTIALS = ProviderCredentials(cohere_api_key="test-key")


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


def build(config: KeelConfig, credentials: ProviderCredentials = CREDENTIALS) -> dict[str, object]:
    return dict(build_registry(config=config, clock=ManualClock(), credentials=credentials))


def envelope() -> RequestEnvelope:
    return RequestEnvelope(
        request_id="req-1",
        tenant="acme",
        feature="support-summary",
        request_class="interactive_chat",
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload={"model": "keel", "messages": [{"role": "user", "content": "hi"}]},
        received_at=0.0,
    )


# --------------------------------------------------------------------------
# The "Done when" — the shipped config builds, and maps to the right classes
# --------------------------------------------------------------------------


def test_the_shipped_config_builds_a_registry() -> None:
    """P1-T5's exit condition, offline: every entry becomes the adapter it names."""
    config = load_config(SHIPPED_CONFIG)

    registry = build(config)

    assert {name: type(adapter) for name, adapter in registry.items()} == {
        "cohere_primary": CohereAdapter,
        "mock_chaos": MockAdapter,
    }


def test_every_configured_provider_gets_an_adapter() -> None:
    """A registry with a silently missing entry is a preference list with a hole in it."""
    config = load_config(SHIPPED_CONFIG)

    registry = build(config)

    assert set(registry) == set(config.providers)


def test_each_adapter_is_named_for_its_config_key_not_its_adapter() -> None:
    """`X-Keel-Provider`, metrics, and health all key on the entry, not the adapter."""
    config = load_config(SHIPPED_CONFIG)

    registry = build_registry(config=config, clock=ManualClock(), credentials=CREDENTIALS)

    for name, adapter in registry.items():
        assert adapter.name == name


def test_capabilities_are_threaded_through_from_config() -> None:
    """The §5.7 filter reads these; an adapter built without them is invisible to it."""
    config = load_config(SHIPPED_CONFIG)

    registry = build_registry(config=config, clock=ManualClock(), credentials=CREDENTIALS)

    assert registry["cohere_primary"].capabilities() == config.providers[
        "cohere_primary"
    ].capabilities
    assert "citations" not in registry["mock_chaos"].capabilities()


# --------------------------------------------------------------------------
# Credentials fail at startup, not at the first request (NFR-4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "credentials",
    [
        pytest.param(ProviderCredentials(cohere_api_key=None), id="unset"),
        pytest.param(ProviderCredentials(cohere_api_key=""), id="empty"),
        pytest.param(ProviderCredentials(cohere_api_key="   "), id="whitespace"),
    ],
)
def test_a_missing_cohere_credential_fails_the_build(credentials: ProviderCredentials) -> None:
    """The empty case is the one that matters: `.env.example` ships `COHERE_API_KEY=`.

    "Copied the template and forgot to fill it in" is the likeliest way this is
    wrong in practice, and a present-but-blank key must not read as configured.
    """
    config = load_config(SHIPPED_CONFIG)

    with pytest.raises(ConfigError) as excinfo:
        build(config, credentials)

    assert "COHERE_API_KEY" in str(excinfo.value)
    assert "cohere_primary" in str(excinfo.value)


def test_a_config_with_no_cohere_entry_needs_no_credential(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """The CI and `loadgen` path: mock-only traffic must not require a real key."""
    mock_only = base_text.replace(
        "preference: [cohere_primary, mock_chaos]", "preference: [mock_chaos]"
    ).replace("  cohere_primary:\n    adapter: cohere\n", "  disabled_cohere:\n    adapter: mock\n")
    # Strip the fields the mock adapter refuses, so the edit stays a valid config.
    mock_only = mock_only.replace("    model: command-a\n", "", 1)

    config = load_config(write_config(mock_only))
    registry = build(config, ProviderCredentials(cohere_api_key=None))

    assert set(registry) == {"disabled_cohere", "mock_chaos"}


# --------------------------------------------------------------------------
# Adapters with no implementation are refused, not skipped (ADR 0004)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "target"),
    [
        pytest.param("azure_openai", "    deployment: gpt-4o\n", id="azure"),
        pytest.param("bedrock", "    model: anthropic.claude-sonnet-4\n", id="bedrock"),
    ],
)
def test_an_unimplemented_adapter_fails_the_build(
    base_text: str, write_config: Callable[[str], Path], adapter: str, target: str
) -> None:
    """Skipping it would shorten a preference list behind the operator's back.

    That is the same failure `KeelConfig`'s cross-reference validator already
    exists to prevent — a failover target the operator believes in and the
    gateway does not have.
    """
    mutated = base_text.replace(
        "  mock_chaos:\n    adapter: mock\n",
        f"  new_fallback:\n    adapter: {adapter}\n{target}"
        f"    pricing: {{ input_per_mtok: 1.0, output_per_mtok: 1.0 }}\n"
        f"  mock_chaos:\n    adapter: mock\n",
        1,
    )
    assert mutated != base_text, "mutation did not change the file; the anchor text moved"

    config = load_config(write_config(mutated))

    with pytest.raises(ConfigError) as excinfo:
        build(config)

    assert "new_fallback" in str(excinfo.value)
    assert adapter in str(excinfo.value)


def test_every_problem_is_reported_at_once(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """One restart should be enough to see everything that is wrong.

    Same posture as `load_config` and `build_envelope`: a deployment with two
    mistakes must not take two restarts to diagnose.
    """
    mutated = base_text.replace(
        "  mock_chaos:\n    adapter: mock\n",
        "  azure_fallback:\n    adapter: azure_openai\n    deployment: gpt-4o\n"
        "    pricing: { input_per_mtok: 1.0, output_per_mtok: 1.0 }\n"
        "  mock_chaos:\n    adapter: mock\n",
        1,
    )
    config = load_config(write_config(mutated))

    with pytest.raises(ConfigError) as excinfo:
        build(config, ProviderCredentials(cohere_api_key=None))

    message = str(excinfo.value)
    assert "COHERE_API_KEY" in message
    assert "azure_fallback" in message


def test_a_failed_build_returns_nothing_rather_than_a_partial_registry() -> None:
    """Half a registry is worse than none: it routes some traffic and drops the rest."""
    config = load_config(SHIPPED_CONFIG)

    with pytest.raises(ConfigError):
        build(config, ProviderCredentials(cohere_api_key=None))


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


async def test_the_cohere_adapter_receives_the_configured_model_and_timeout() -> None:
    """Config reaches the provider call, not just the constructor.

    Asserted by substituting the adapter's collaborator and invoking it, rather
    than by reading private state: what matters is the request that would go out
    on the wire, which is the thing a wiring mistake actually corrupts.
    """
    config = load_config(SHIPPED_CONFIG)
    registry = build_registry(config=config, clock=ManualClock(), credentials=CREDENTIALS)

    adapter = registry["cohere_primary"]
    assert isinstance(adapter, CohereAdapter)

    sent: dict[str, object] = {}

    async def record(**kwargs: object) -> dict[str, object]:
        sent.update(kwargs)
        return {"id": "chatcmpl-x", "object": "chat.completion"}

    adapter._acompletion = record  # noqa: SLF001 - substituting a collaborator
    result = await adapter.invoke(envelope())

    assert result.ok
    assert sent["model"] == "cohere/command-a"
    assert sent["api_key"] == "test-key"
    assert sent["timeout"] == 30.0


async def test_the_registry_shares_one_clock_with_every_adapter() -> None:
    """Health windows and breaker cooldowns are only comparable on one clock (ADR 0001).

    Observed through the mock's response, which stamps `created` from whatever
    clock it was handed — so a registry that quietly built its own would show it.
    """
    config = load_config(SHIPPED_CONFIG)
    clock = ManualClock(start=500.0)

    registry = build_registry(config=config, clock=clock, credentials=CREDENTIALS)
    result = await registry["mock_chaos"].invoke(envelope())

    assert result.response is not None
    assert result.response["created"] == 500
