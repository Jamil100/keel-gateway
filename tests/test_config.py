"""Tests for configuration loading and startup validation (NFR-4).

Every case here is a mutation of the real ``config/keel.yaml``. Testing
against the shipped file rather than a hand-written fixture means these
tests fail if the file drifts out of the schema, which is the drift that
matters — a config the gateway cannot load is a gateway that will not start.

No network, no Redis, no clock dependency (NFR-2).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from keel.config import AdapterName, ConfigError, KeelConfig, load_config

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


# --------------------------------------------------------------------------
# The shipped file
# --------------------------------------------------------------------------


def test_shipped_config_loads() -> None:
    config = load_config(SHIPPED_CONFIG)
    assert isinstance(config, KeelConfig)


def test_shipped_config_matches_design_section_5_2() -> None:
    """Assert the values TECHNICAL-DESIGN.md §5.2 specifies, not just that it parses."""
    config = load_config(SHIPPED_CONFIG)

    # Two providers, not §5.2's four: azure_fallback and bedrock_fallback are
    # stood down until Phase 4 because no adapter can build them (ADR 0004).
    assert set(config.providers) == {"cohere_primary", "mock_chaos"}
    assert set(config.request_classes) == {
        "interactive_chat",
        "classification",
        "batch_enrichment",
    }

    cohere = config.providers["cohere_primary"]
    assert cohere.adapter is AdapterName.COHERE
    assert cohere.model == "command-a"
    assert cohere.capabilities == frozenset({"citations", "tool_use", "structured_output"})
    assert cohere.timeout_ms == 30000

    # The fallback cannot serve citations. This is the asymmetry the whole
    # capability filter exists for (§5.7) — if it ever becomes symmetric by
    # accident, the failover story loses its point. It rode on azure_fallback
    # and bedrock_fallback until ADR 0004 stood both down; mock_chaos carries it
    # now, so that Phase 3's filter still has something real to bite on.
    assert "citations" in config.providers["cohere_primary"].capabilities
    assert "citations" not in config.providers["mock_chaos"].capabilities

    # §5.2 omits timeout_ms and hedge in exactly these two places.
    assert config.providers["mock_chaos"].timeout_ms is None
    assert config.request_classes["batch_enrichment"].hedge is None

    assert config.request_classes["batch_enrichment"].deferrable is True
    assert config.request_classes["interactive_chat"].deferrable is False
    assert config.request_classes["classification"].latency_budget_p95_ms == 800

    breaker = config.breaker
    assert breaker.window_seconds == 60
    assert breaker.bucket_seconds == 5
    assert breaker.min_requests_in_window == 20
    assert breaker.error_rate_threshold == 0.30
    assert breaker.open_cooldown_seconds == 30
    assert breaker.half_open_probe_ratio == 0.10
    assert breaker.half_open_successes_to_close == 3


def test_every_preference_entry_is_a_defined_provider() -> None:
    config = load_config(SHIPPED_CONFIG)
    for class_config in config.request_classes.values():
        for provider_name in class_config.preference:
            assert provider_name in config.providers


def test_cohere_is_primary_for_every_class() -> None:
    """FR-2.1: Cohere is the default target for all request classes."""
    config = load_config(SHIPPED_CONFIG)
    for name, class_config in config.request_classes.items():
        assert class_config.preference[0] == "cohere_primary", name


def test_config_is_immutable() -> None:
    """``frozen=True`` holds after load, so nothing mutates config at request time."""
    config = load_config(SHIPPED_CONFIG)
    with pytest.raises(ValidationError, match="frozen"):
        config.breaker.window_seconds = 999  # type: ignore[misc]


# --------------------------------------------------------------------------
# Rejections. Each asserts the message is specific enough to act on.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(
            lambda s: s.replace(
                "preference: [cohere_primary, mock_chaos]",
                "preference: [cohere_primary, mock_chas]",
                1,
            ),
            "undefined provider(s) ['mock_chas']",
            id="preference-references-unknown-provider",
        ),
        pytest.param(
            lambda s: s.replace(
                "preference: [cohere_primary, mock_chaos]",
                "preference: [cohere_primary, cohere_primary]",
                1,
            ),
            "repeats provider(s)",
            id="preference-repeats-a-provider",
        ),
        pytest.param(
            lambda s: s.replace("preference: [cohere_primary, mock_chaos]", "preference: []", 1),
            "at least 1 item",
            id="preference-empty",
        ),
        pytest.param(
            lambda s: s.replace("adapter: cohere\n", "adapter: coherre\n", 1),
            "Input should be 'cohere', 'azure_openai', 'bedrock' or 'mock'",
            id="unknown-adapter",
        ),
        pytest.param(
            lambda s: s.replace("timeout_ms: 30000", "timout_ms: 30000", 1),
            "Extra inputs are not permitted",
            id="misspelled-key-is-not-silently-ignored",
        ),
        # Both azure cases now retarget a live entry rather than the stood-down
        # azure_fallback block (ADR 0004): mock_chaos names neither a model nor
        # a deployment, and cohere_primary names a model.
        pytest.param(
            lambda s: s.replace("    adapter: mock\n", "    adapter: azure_openai\n", 1),
            "adapter 'azure_openai' requires 'deployment'",
            id="azure-needs-a-deployment",
        ),
        pytest.param(
            # Needs *both* fields present: the validator checks for a missing
            # deployment first, so an entry with only a model never reaches the
            # branch this case is here to cover.
            lambda s: s.replace(
                "    adapter: cohere\n    model: command-a\n",
                "    adapter: azure_openai\n    model: command-a\n    deployment: gpt-4o\n",
                1,
            ),
            "adapter 'azure_openai' addresses a 'deployment', not a 'model'",
            id="azure-does-not-address-a-model",
        ),
        pytest.param(
            lambda s: s.replace(
                "    adapter: mock\n", "    adapter: mock\n    model: some-model\n", 1
            ),
            "adapter 'mock' takes neither",
            id="mock-addresses-nothing",
        ),
        pytest.param(
            lambda s: s.replace("bucket_seconds: 5", "bucket_seconds: 7"),
            "must be a whole multiple of bucket_seconds",
            id="window-not-a-multiple-of-bucket",
        ),
        pytest.param(
            lambda s: s.replace("error_rate_threshold: 0.30", "error_rate_threshold: 30"),
            "less than or equal to 1",
            id="error-rate-threshold-above-one",
        ),
        pytest.param(
            lambda s: s.replace("half_open_probe_ratio: 0.10", "half_open_probe_ratio: 0"),
            "greater than 0",
            id="probe-ratio-zero-would-never-recover",
        ),
        pytest.param(
            lambda s: s.replace("latency_budget_p95_ms: 800", "latency_budget_p95_ms: -1"),
            "greater than 0",
            id="negative-latency-budget",
        ),
        pytest.param(
            lambda s: s.replace("input_per_mtok: 2.50", "input_per_mtok: -2.50", 1),
            "greater than or equal to 0",
            id="negative-price",
        ),
        pytest.param(
            lambda s: s + "\nunexpected_section:\n  foo: bar\n",
            "Extra inputs are not permitted",
            id="unknown-top-level-section",
        ),
        pytest.param(
            lambda s: s.replace("providers:", "providers: [oops", 1),
            "not valid YAML",
            id="malformed-yaml",
        ),
    ],
)
def test_invalid_config_is_rejected(
    base_text: str,
    write_config: Callable[[str], Path],
    mutate: Callable[[str], str],
    expected: str,
) -> None:
    mutated = mutate(base_text)
    assert mutated != base_text, "mutation did not change the file; the anchor text moved"

    path = write_config(mutated)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert expected in str(excinfo.value)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_empty_file(write_config: Callable[[str], Path]) -> None:
    with pytest.raises(ConfigError, match="is empty"):
        load_config(write_config(""))


def test_top_level_is_not_a_mapping(write_config: Callable[[str], Path]) -> None:
    with pytest.raises(ConfigError, match="must contain a mapping"):
        load_config(write_config("- a\n- b\n"))


def test_error_lists_every_problem_not_just_the_first(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """An operator fixing config one error per restart is an operator wasting an hour."""
    broken = base_text.replace("min_requests_in_window: 20", "min_requests_in_window: -20")
    broken = broken.replace("open_cooldown_seconds: 30", "open_cooldown_seconds: -30")

    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(broken))

    message = str(excinfo.value)
    assert "min_requests_in_window" in message
    assert "open_cooldown_seconds" in message


def test_validation_errors_do_not_leak_pydantic_types(
    base_text: str, write_config: Callable[[str], Path]
) -> None:
    """Callers catch ConfigError. A ValidationError escaping is an API break."""
    path = write_config(base_text.replace("adapter: cohere\n", "adapter: nonsense\n", 1))
    with pytest.raises(ConfigError):
        load_config(path)
