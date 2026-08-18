"""Turning `config/keel.yaml` into a set of live adapters, at startup.

One function, called once from the app lifespan (P1-T7). It reads
``KeelConfig.providers``, dispatches on ``AdapterName``, and returns the adapter
for every configured entry — keyed by the **entry name**, not the adapter name,
because several entries may share one adapter and everything downstream
(metrics, health, ``X-Keel-Provider``) keys on the entry.

**Everything that can be wrong is wrong before the first request** (NFR-4). That
is a sharper requirement here than it looks. A missing ``COHERE_API_KEY``
surfaces at request time as an ``AUTH_FAILURE``, and D7 deliberately excludes
that class from the breaker — so a gateway that discovered credentials lazily
would fail 100% of its traffic while every breaker stayed closed and every
dashboard stayed green. Checking at startup is what stops the gateway's own
misconfiguration from wearing a provider's clothes.

The registry also refuses to build an adapter that does not exist yet
(**ADR 0004**). ``azure_openai`` and ``bedrock`` are declared in ``AdapterName``
because the design specifies them, but no implementation lands until Phase 4.
Skipping them silently would shorten a preference list behind the operator's
back, which is the exact failure ``KeelConfig``'s cross-reference validator
already exists to prevent.
"""

from __future__ import annotations

from typing import assert_never

from keel.clock import Clock
from keel.config import AdapterName, ConfigError, KeelConfig, ProviderConfig
from keel.providers.base import ProviderAdapter
from keel.providers.cohere import CohereAdapter
from keel.providers.credentials import ProviderCredentials
from keel.providers.mock import MockAdapter

__all__ = ["build_registry"]

# Config key -> the environment variable that carries its credential. Named here
# rather than inlined so the error message can quote the variable an operator
# actually has to set, not the pydantic field name.
_COHERE_API_KEY_VAR = "COHERE_API_KEY"


def _unimplemented(name: str, entry: ProviderConfig) -> str:
    return (
        f"providers.{name}: adapter {entry.adapter.value!r} has no implementation yet "
        f"(Phase 4, FR-2.6; see docs/adr/0004-*.md). Remove the entry and every "
        f"reference to it in a preference list until the adapter and its credentials land."
    )


def _missing_credential(name: str) -> str:
    return (
        f"providers.{name}: adapter 'cohere' requires {_COHERE_API_KEY_VAR}, which is "
        f"unset or empty. Set it in the environment or in .env (see .env.example). "
        f"Failing now rather than at the first request is deliberate: a missing key "
        f"would otherwise surface as an AUTH_FAILURE, which is excluded from the "
        f"breaker (D7), so every request would fail with every dashboard green."
    )


def _expect_model(name: str, entry: ProviderConfig) -> str:
    """Narrow ``model`` for mypy. ``ProviderConfig`` already guarantees it is set."""
    if entry.model is None:  # pragma: no cover - _check_target_naming would have raised
        raise ConfigError(f"providers.{name}: adapter {entry.adapter.value!r} requires 'model'")
    return entry.model


def build_registry(
    *,
    config: KeelConfig,
    clock: Clock,
    credentials: ProviderCredentials | None = None,
) -> dict[str, ProviderAdapter]:
    """Build one adapter per configured provider, or raise ``ConfigError``.

    ``credentials`` defaults to reading the environment and ``.env``. It is
    injectable so the test suite never depends on — or is broken by — whatever
    happens to be exported in the shell running it (NFR-2).

    Raises ``ConfigError`` listing **every** problem rather than the first.
    A deployment with two mistakes should take one restart to diagnose, not two;
    this is the same posture ``load_config`` and ``build_envelope`` already take.
    """
    resolved = credentials if credentials is not None else ProviderCredentials()

    registry: dict[str, ProviderAdapter] = {}
    problems: list[str] = []

    for name, entry in config.providers.items():
        match entry.adapter:
            case AdapterName.COHERE:
                if resolved.cohere_api_key is None:
                    problems.append(_missing_credential(name))
                    continue
                registry[name] = CohereAdapter(
                    name=name,
                    model=_expect_model(name, entry),
                    api_key=resolved.cohere_api_key,
                    clock=clock,
                    capabilities=entry.capabilities,
                    timeout_ms=entry.timeout_ms,
                )

            case AdapterName.MOCK:
                # No credential, no network, no timeout: the mock is in-process
                # (ADR 0002) and its latency is injected rather than waited on.
                registry[name] = MockAdapter(
                    name=name,
                    clock=clock,
                    capabilities=entry.capabilities,
                )

            case AdapterName.AZURE_OPENAI | AdapterName.BEDROCK:
                problems.append(_unimplemented(name, entry))

            case _:  # pragma: no cover - unreachable while AdapterName is exhaustive
                # Not defensive programming: this makes adding a fifth AdapterName
                # a `mypy --strict` failure rather than a KeyError during an
                # incident. Same posture as the §5.4 completeness guard in
                # keel/providers/errors.py.
                assert_never(entry.adapter)

    if problems:
        raise ConfigError(
            "Cannot build the provider registry:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )

    return registry
