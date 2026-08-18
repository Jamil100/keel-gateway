"""Configuration schema and startup validation.

One validated YAML file is the source of truth for routing behaviour (FR-2.3).
It is validated when the process starts, not when the first request arrives:
a misspelled provider name in a preference list is a deployment error, and
finding it at request time means finding it during an incident (NFR-4).

Schema follows TECHNICAL-DESIGN.md §5.2. This module validates structure and
cross-references only. It contains no routing, health, or breaker logic.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "AdapterName",
    "BreakerConfig",
    "ConfigError",
    "HedgeConfig",
    "KeelConfig",
    "PricingConfig",
    "ProviderConfig",
    "RequestClassConfig",
    "load_config",
]

DEFAULT_CONFIG_PATH = Path("config/keel.yaml")


class ConfigError(Exception):
    """Configuration is unusable. Raised at startup, never at request time."""


class AdapterName(StrEnum):
    """Adapter implementations that can back a provider entry (§5.3).

    Closed set: an adapter name is a dispatch key to code that must exist, so
    an unknown value is a typo rather than an extension point. Providers
    themselves stay open — several entries may share one adapter.
    """

    COHERE = "cohere"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    MOCK = "mock"


class _Strict(BaseModel):
    """Base for every config model: unknown keys are errors, values are frozen.

    ``extra="forbid"`` is the point. A silently ignored ``timout_ms`` key
    reads as configured behaviour that never takes effect.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class PricingConfig(_Strict):
    """Per-million-token rates used by the cost engine (§5.10, FR-6.1).

    Rates are maintained by hand and drift. They are declared here so cost is
    computed from version-controlled values rather than guessed.
    """

    input_per_mtok: Annotated[float, Field(ge=0.0)]
    output_per_mtok: Annotated[float, Field(ge=0.0)]


class HedgeConfig(_Strict):
    """Hedged-request policy for one request class (§5.8, FR-4.7, FR-4.8).

    Disabled by default because hedging roughly doubles spend on hedged calls.
    """

    enabled: bool = False
    after_ms: Annotated[int, Field(gt=0)]


class ProviderConfig(_Strict):
    """One routable provider target.

    ``model`` and ``deployment`` are alternative ways to name the thing being
    called: Azure OpenAI addresses a deployment, other adapters address a
    model. The mock adapter addresses neither.
    """

    adapter: AdapterName
    model: str | None = None
    deployment: str | None = None
    capabilities: frozenset[str] = frozenset()
    pricing: PricingConfig
    # §5.2 omits timeout_ms for the mock provider, so it cannot be required.
    # None means "no gateway-imposed timeout"; the adapter's own default applies.
    timeout_ms: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _check_target_naming(self) -> ProviderConfig:
        if self.adapter is AdapterName.AZURE_OPENAI:
            if self.deployment is None:
                raise ValueError("adapter 'azure_openai' requires 'deployment'")
            if self.model is not None:
                raise ValueError(
                    "adapter 'azure_openai' addresses a 'deployment', not a 'model'"
                )
        elif self.adapter is AdapterName.MOCK:
            if self.model is not None or self.deployment is not None:
                raise ValueError(
                    "adapter 'mock' takes neither 'model' nor 'deployment'"
                )
        else:
            if self.model is None:
                raise ValueError(f"adapter '{self.adapter}' requires 'model'")
            if self.deployment is not None:
                raise ValueError(
                    f"adapter '{self.adapter}' addresses a 'model', not a 'deployment'"
                )
        return self


class RequestClassConfig(_Strict):
    """Routing policy for one request class (§5.2).

    The class name is the map key. Classes are config-defined rather than a
    fixed enum, per design principle 1 — adding a class must not require
    touching the router.
    """

    deferrable: bool
    preference: Annotated[list[str], Field(min_length=1)]
    latency_budget_p95_ms: Annotated[int, Field(gt=0)]
    hedge: HedgeConfig | None = None

    @model_validator(mode="after")
    def _check_preference_unique(self) -> RequestClassConfig:
        duplicates = sorted({p for p in self.preference if self.preference.count(p) > 1})
        if duplicates:
            raise ValueError(f"preference list repeats provider(s): {duplicates}")
        return self


class BreakerConfig(_Strict):
    """Circuit breaker thresholds, global across providers (§5.6).

    Per design decision on PRD Q3: the health window is global; latency
    budgets are per request class and live on RequestClassConfig.
    """

    window_seconds: Annotated[int, Field(gt=0)]
    bucket_seconds: Annotated[int, Field(gt=0)]
    min_requests_in_window: Annotated[int, Field(gt=0)]
    error_rate_threshold: Annotated[float, Field(gt=0.0, le=1.0)]
    open_cooldown_seconds: Annotated[int, Field(gt=0)]
    half_open_probe_ratio: Annotated[float, Field(gt=0.0, le=1.0)]
    half_open_successes_to_close: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _check_window_divides_into_buckets(self) -> BreakerConfig:
        # The window is the union of whole buckets (§5.5). A window that is not
        # a multiple of the bucket width has an edge bucket counted partially,
        # which quietly skews every error rate the breaker reads.
        if self.window_seconds % self.bucket_seconds != 0:
            raise ValueError(
                f"window_seconds ({self.window_seconds}) must be a whole multiple of "
                f"bucket_seconds ({self.bucket_seconds})"
            )
        if self.bucket_seconds > self.window_seconds:
            raise ValueError("bucket_seconds cannot exceed window_seconds")
        return self


class KeelConfig(_Strict):
    """The whole validated configuration file."""

    providers: Annotated[dict[str, ProviderConfig], Field(min_length=1)]
    request_classes: Annotated[dict[str, RequestClassConfig], Field(min_length=1)]
    breaker: BreakerConfig

    @model_validator(mode="after")
    def _check_preferences_reference_known_providers(self) -> KeelConfig:
        # The failure this prevents: a typo in a preference list silently
        # shortens the candidate chain, so failover has one fewer target than
        # the operator believes it has — and nothing says so until the primary
        # is already down.
        known = set(self.providers)
        errors: list[str] = []
        for class_name, class_config in self.request_classes.items():
            unknown = [p for p in class_config.preference if p not in known]
            if unknown:
                errors.append(
                    f"request_classes.{class_name}.preference references undefined "
                    f"provider(s) {unknown}; defined providers are {sorted(known)}"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _format_validation_error(path: Path, error: ValidationError) -> str:
    """Render a ValidationError as something readable at 3am."""
    lines = [f"Invalid configuration in {path}:"]
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "<root>"
        lines.append(f"  - {location}: {issue['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> KeelConfig:
    """Load and validate the configuration file.

    Call once at process startup, before the server accepts traffic. Raises
    ConfigError with an actionable message on any problem; never returns a
    partially valid config (NFR-4).
    """
    path = Path(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read configuration file {path}: {exc}") from exc

    try:
        parsed: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Configuration file {path} is not valid YAML:\n{exc}") from exc

    if parsed is None:
        raise ConfigError(f"Configuration file {path} is empty")
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"Configuration file {path} must contain a mapping at the top level, "
            f"got {type(parsed).__name__}"
        )

    try:
        return KeelConfig.model_validate(parsed)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc
