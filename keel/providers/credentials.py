"""Provider credentials, read from the environment once at startup.

Separate from :mod:`keel.config` on purpose. `config/keel.yaml` is committed and
describes *routing behaviour*; credentials are per-machine secrets that must
never enter it (FR-2.3 draws that line, and §8 draws `.env` as a separate input
to the gateway). The two are validated at the same moment — process start — but
they come from different places and have different blast radii when wrong.

Variable names follow LiteLLM's conventions because the adapter layer sits above
LiteLLM (D4); renaming them would buy a translation step and nothing else.

Only credentials with an adapter to consume them are declared. Azure and Bedrock
are absent until Phase 4 brings adapters that could use them — a settings field
for a provider that cannot be built is a promise the registry cannot keep.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ProviderCredentials"]


class ProviderCredentials(BaseSettings):
    """Secrets from the environment, or from `.env` when one is present.

    Constructed once in the app lifespan and handed to
    :func:`keel.providers.registry.build_registry`, which is what makes the
    startup check and the eventual provider call read the *same* value. Letting
    LiteLLM pick the key out of the environment on its own would leave those two
    reads independent, so a value that changed in between would pass validation
    and still fail at request time.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `.env` also carries KEEL_CONFIG_PATH and REDIS_URL, which are not
        # credentials and are not this model's business.
        extra="ignore",
    )

    cohere_api_key: str | None = None
    """From ``COHERE_API_KEY``. ``None`` when absent *or* blank — see below."""

    @field_validator("cohere_api_key", mode="after")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        """Collapse an empty or whitespace-only value to ``None``.

        Load-bearing rather than tidy. `.env.example` ships ``COHERE_API_KEY=``
        with nothing after it, so "copied the template and forgot to fill it in"
        is the single most likely way this is wrong in practice. Without this,
        that value is a present-but-useless string: it passes the registry's
        startup check and fails on every request instead — which is exactly the
        NFR-4 failure the check exists to prevent.
        """
        if value is None:
            return None
        return value.strip() or None
