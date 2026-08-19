"""The Redis connection, built once at startup.

Top level rather than inside :mod:`keel.health`, for the same reason
:mod:`keel.clock` is (decision D-B). §5.5 gives Redis four jobs — health windows,
breaker state, the deferred queue, and idempotency keys — and they live in three
different packages, so the client belongs to none of them.

**This module does not shadow the ``redis`` package.** Python resolves imports
absolutely, so ``from redis.asyncio import Redis`` below reaches the installed
library rather than back into this file. It reads like a footgun and is not one.

``REDIS_URL`` has sat in `.env.example` since P1-T1 with nothing reading it. This
is the first code that does — which is also why the connection posture had to be
decided here rather than inherited. See **ADR 0008**: the client is built lazily
and never pinged at startup, because a gateway that refuses to start without Redis
turns a Redis restart into a gateway outage, and health data is not required to
serve a request.
"""

from __future__ import annotations

from typing import Final

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_REDIS_URL",
    "SOCKET_TIMEOUT_SECONDS",
    "RedisSettings",
    "create_redis_client",
]

DEFAULT_REDIS_URL: Final = "redis://localhost:6379/0"
"""Matches the value `.env.example` ships, so the two cannot drift apart."""

CONNECT_TIMEOUT_SECONDS: Final = 0.15
SOCKET_TIMEOUT_SECONDS: Final = 0.15
"""Socket-level deadlines, and the inner half of a two-layer bound.

``HealthWindow`` wraps every call in its own 250 ms box, and these sit underneath
it — the same shape as the executor's ``wait_for`` sitting above the adapter's own
timeout, and not redundant for the same reason: the inner one frees a connection,
the outer one frees the request.

Measured rather than guessed, and the measurement is the argument. Against a
machine with nothing listening on 6379, ``redis-py`` with no socket deadline takes
**4.1 s** to give up — Windows retries a refused connection rather than failing on
the first ``ECONNREFUSED``. The outer box does keep the request safe, but it fires
first and every failure then arrives as a bare ``TimeoutError`` with an empty
message. With these set, the same failure takes 0.16 s and says *"Timeout
connecting to server"*. That matters more than the tenth of a second: until P2-T4
exports a counter, a log line is the **only** evidence that health data is being
dropped (ADR 0008), and an anonymous timeout is not evidence of anything.

Both must stay below ``HealthWindow.REDIS_TIMEOUT_SECONDS`` or the outer box wins
again and the diagnosis goes back to being blank. 150 ms leaves a local Redis —
which answers in well under a millisecond — around two orders of magnitude of
headroom, and a false trip costs one observation, never a request.
"""


class RedisSettings(BaseSettings):
    """The Redis URL, from the environment or from `.env` when one is present.

    Deliberately not part of :class:`keel.config.KeelConfig`. `config/keel.yaml`
    is committed and describes *routing behaviour*; a connection string is
    per-machine deployment wiring, and FR-2.3 draws that line. Inside docker
    compose the host is the service name rather than localhost, which is exactly
    the kind of difference that must not live in a committed file.

    Separate from :class:`keel.providers.credentials.ProviderCredentials` for a
    smaller reason: that model's docstring already declares ``REDIS_URL`` "not
    this model's business", and a settings class that reads everything in `.env`
    would make every consumer depend on every secret.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `.env` also carries KEEL_CONFIG_PATH and the provider credentials.
        extra="ignore",
    )

    redis_url: str = DEFAULT_REDIS_URL
    """From ``REDIS_URL``. Blank falls back to the default — see below."""

    @field_validator("redis_url", mode="after")
    @classmethod
    def _blank_is_default(cls, value: str) -> str:
        """Treat an empty or whitespace-only value as absent.

        Same rule, and the same reason, as ``ProviderCredentials`` for a blank
        ``COHERE_API_KEY`` and ``_resolve_config_path`` for a blank
        ``KEEL_CONFIG_PATH``: `.env.example` ships assignments that people copy
        and then do not fill in. Without this, ``REDIS_URL=`` becomes an empty
        connection string that fails at ``from_url`` with a message about a URL
        rather than about the variable that is actually wrong.
        """
        return value.strip() or DEFAULT_REDIS_URL


def create_redis_client(settings: RedisSettings | None = None) -> Redis:
    """Build the client. Opens no socket — ``redis-py`` connects on first command.

    That laziness is what lets the app lifespan construct this without making
    Redis a startup dependency (ADR 0008), and it is why ``create_app`` stays
    I/O-free at import.

    ``decode_responses=True`` is load-bearing rather than convenient. It makes
    ``HGETALL`` hand back ``str`` field names, so they compare directly against
    :class:`keel.providers.errors.ErrorClass` values with no decode step for a
    future caller to forget — and a forgotten decode compares ``b"ok"`` against
    ``"ok"``, silently reads every bucket as empty, and shows a healthy provider
    with no traffic.
    """
    resolved = settings if settings is not None else RedisSettings()
    return Redis.from_url(
        resolved.redis_url,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
    )
