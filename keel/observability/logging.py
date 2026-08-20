"""Structured logging: one JSON object per line, correlated by ``request_id``.

FR-7.3 asks for "structured request logs with correlation by ``request_id``
across retries and failovers". This module is the whole of the configuration
side: it decides the processor chain, the renderer, and the handler, and it is
the only place in ``keel`` that touches the root logger.

**This module does not shadow the stdlib ``logging`` package.** It sits beside
``keel/observability/metrics.py``, which does ``import logging`` at module
scope. Python resolves imports absolutely, so that reaches the standard library
rather than back into this file — the same footgun, and the same non-problem, as
``keel/redis.py`` next to the ``redis`` package.

**The four existing loggers are bridged, not rewritten.** ``keel.health.window``,
``keel.health.latency``, ``keel.observability.metrics``, and
``keel.providers.normalize`` between them emit ten stdlib ``WARNING``/``DEBUG``
lines, and none of them change. ``ProcessorFormatter`` renders those *foreign*
records through the same processor chain as structlog's own, so they arrive as
JSON carrying whatever request context was bound at ingress — with no import of
this module anywhere in ``keel/health/``.

That last part is load-bearing rather than tidy. P2-T4 gave ``HealthTracker`` a
plain callback instead of a ``MetricsCatalogue`` specifically so ``keel/health/``
keeps zero dependencies on ``keel/observability/``; the breaker and the executor
both import the health window, and neither should drag the exporter in behind it.
Converting those modules to structlog would close that seam to buy nothing.

It is also what **ADR 0008** asked this task for by name. A dropped health write
is logged and swallowed, and until now the resulting ``WARNING`` said which
provider went unrecorded but not which request paid for it::

    {"event": "health write dropped for provider 'mock_chaos' (field 'ok'): ...",
     "level": "warning", "logger": "keel.health.window",
     "request_id": "req-1", "tenant": "acme", "timestamp": "..."}

Two of those call sites — ``window._merge`` and ``latency.parse_samples`` — are
module-level functions with no instance to inject a logger into and no request in
scope. Contextvars reach them anyway, which is the second reason to bridge.

**Never log request or response bodies.** PRD §2.1 lists "PII detection and
redaction in request logging" as an explicit non-goal, so there is nothing
downstream that would scrub a prompt. The safe default when redaction does not
exist is to log no payload at all — not the messages, not the completion, not a
truncated preview. ``tests/test_logging.py`` pins that with a sentinel rather
than leaving it to discipline.

**Field spelling, and a deliberate disagreement with §6.** The log field is
``request_class``, matching ``RequestEnvelope`` and TECHNICAL-DESIGN §5.1. The
*metric label* for the same value is ``class``, because §6's table is
authoritative there (P2-T4 settled it). Two namespaces, two spellings, both
correct in their own; a reader comparing a log line to a PromQL query needs to
know that up front.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

import structlog
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "FORMAT_CONSOLE",
    "FORMAT_JSON",
    "LogSettings",
    "configure_logging",
    "get_logger",
]

FORMAT_JSON: Final = "json"
FORMAT_CONSOLE: Final = "console"

DEFAULT_LOG_LEVEL: Final = "INFO"
DEFAULT_LOG_FORMAT: Final = FORMAT_JSON

_HANDLER_MARKER: Final = "_keel_log_handler"
"""Marks the handler this module installs, so re-running replaces only its own.

Re-running is normal: the lifespan calls ``configure_logging`` once per app, and
the test suite builds thirty-odd apps in a session. Without the marker each one
would leave another handler behind and every line would be emitted once per app
ever built.

``root.handlers = [...]`` would solve that and introduce a worse problem.
pytest's ``caplog`` fixture installs its capture handler on the root logger for
the duration of a test, and six tests in this suite assert on ``caplog.text``; an
app built *inside* one of them would throw that handler away and the assertion
would fail for a reason having nothing to do with what it tests. No test does
both today — which is exactly why the property is pinned by one in
``tests/test_logging.py`` rather than left to be discovered later, when the two
habits finally meet in the same test.
"""

_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")
"""Uvicorn installs its own handlers and sets ``propagate = False``.

Left alone, the gateway would emit two log formats at once: JSON from everything
in ``keel``, and uvicorn's colourised text for startup and access lines. Clearing
those handlers and letting the records propagate to root sends both through this
module's formatter, so a log aggregator sees one shape.
"""

_CHATTY_LOGGERS: Final = ("httpx", "httpcore", "litellm", "LiteLLM")
"""Third-party loggers held at ``WARNING`` unless the gateway is set to ``DEBUG``.

Not tidiness. ``httpx`` logs one ``INFO`` line per request, so every provider call
would emit a second line saying less than ``provider_attempt`` already says —
doubling the volume of the busiest log in the system and pushing the line a
reader actually wants further from the one before it. LiteLLM is chattier still.

Raised back to the configured level when that level is ``DEBUG``, because
somebody who asked for debug logging is usually asking about exactly this layer.
"""


class LogSettings(BaseSettings):
    """Log level and renderer, from the environment or from `.env`.

    Deliberately not part of :class:`keel.config.KeelConfig`, for the reason
    FR-2.3 draws the line: `config/keel.yaml` is committed and describes *routing
    behaviour*, while how loudly a particular machine logs is deployment wiring.
    A developer wanting ``DEBUG`` locally must not have to edit a file that
    changes how requests are routed.

    Same shape as :class:`keel.redis.RedisSettings`, including the blank-is-absent
    rule below.
    """

    model_config = SettingsConfigDict(
        env_prefix="KEEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        # `.env` also carries REDIS_URL and the provider credentials, and the
        # KEEL_ prefix means KEEL_CONFIG_PATH would otherwise bind to a field
        # this model does not have.
        extra="ignore",
    )

    log_level: str = DEFAULT_LOG_LEVEL
    """From ``KEEL_LOG_LEVEL``. One of the standard level names."""

    log_format: str = DEFAULT_LOG_FORMAT
    """From ``KEEL_LOG_FORMAT``: ``json`` for deployment, ``console`` for a terminal."""

    @field_validator("log_level", mode="after")
    @classmethod
    def _known_level(cls, value: str) -> str:
        """Reject a level name the stdlib does not know, at startup (NFR-4).

        A blank value falls back to the default, the same rule ``RedisSettings``
        applies to ``REDIS_URL`` and ``ProviderCredentials`` to a blank key:
        `.env.example` ships assignments that people copy and do not fill in.

        A *typo* is different from a blank and is not forgiven. ``logging``
        resolves an unknown name to level ``0``, which admits every record at
        every level — so ``KEEL_LOG_LEVEL=INF0`` would silently turn on debug
        logging in production rather than failing. Raising here means the
        lifespan surfaces it at startup, where NFR-4 wants configuration errors.
        """
        normalized = value.strip().upper() or DEFAULT_LOG_LEVEL
        known = logging.getLevelNamesMapping()
        if normalized not in known:
            raise ValueError(
                f"unknown log level {value!r}; expected one of "
                f"{', '.join(sorted(n for n in known if n != 'NOTSET'))}"
            )
        return normalized

    @field_validator("log_format", mode="after")
    @classmethod
    def _known_format(cls, value: str) -> str:
        """Same blank-is-absent rule, and the same refusal to guess at a typo."""
        normalized = value.strip().lower() or DEFAULT_LOG_FORMAT
        if normalized not in (FORMAT_JSON, FORMAT_CONSOLE):
            raise ValueError(
                f"unknown log format {value!r}; expected {FORMAT_JSON!r} or {FORMAT_CONSOLE!r}"
            )
        return normalized


def _shared_processors() -> list[structlog.typing.Processor]:
    """The chain both structlog's own records and foreign stdlib ones go through.

    One chain rather than two, so a line's shape does not depend on whether it
    came from ``structlog`` or from ``logging``. ``merge_contextvars`` is first
    and is the entire correlation mechanism: it folds whatever ingress bound into
    every event dict, including those from modules that have never heard of this
    one.

    Identical regardless of the renderer, which is what makes
    ``cache_logger_on_first_use`` safe below — the JSON/console choice lives in
    the handler's formatter, not here, so a logger cached under one setting still
    renders correctly under another.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def _renderer(log_format: str) -> structlog.typing.Processor:
    """The terminal processor: machine-readable by default, human-readable on request."""
    if log_format == FORMAT_CONSOLE:
        # `format_exc_info` above has already turned any exception into an
        # `exception` string, which ConsoleRenderer prints beneath the line.
        return structlog.dev.ConsoleRenderer()
    # `sort_keys=False` so `event` stays where the chain put it rather than being
    # alphabetised behind `attempt` — a human tailing JSON still reads the event
    # name first.
    return structlog.processors.JSONRenderer(sort_keys=False)


def configure_logging(settings: LogSettings | None = None) -> None:
    """Install the handler and point structlog at it. Safe to call repeatedly.

    Idempotent because it has to be: the app factory is called thirty-odd times in
    one test session, and each ``TestClient`` context runs the lifespan that calls
    this. Re-running removes only the handler this function installed
    (:data:`_HANDLER_MARKER`) and leaves every other one alone.

    Called first in the lifespan, before ``load_config``, so that a ``ConfigError``
    escaping startup (NFR-4) is itself rendered in the configured format rather
    than being the one message a log aggregator cannot parse.
    """
    resolved = settings if settings is not None else LogSettings()
    level = logging.getLevelNamesMapping()[resolved.log_level]
    shared = _shared_processors()

    structlog.configure(
        processors=[
            # Drops records below the level before the rest of the chain runs, so
            # a suppressed DEBUG line costs a comparison rather than a timestamp
            # and a JSON encode.
            structlog.stdlib.filter_by_level,
            *shared,
            # Hands off to the stdlib handler below rather than rendering here.
            # Everything reaches stdout through one handler and one formatter,
            # which is what lets foreign records share this chain.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # What a record from `keel.health.window` and friends goes through on
            # its way in. `merge_contextvars` sits inside `shared`, which is why
            # an untouched `logger.warning(...)` comes out carrying `request_id`.
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _renderer(resolved.log_format),
            ],
        )
    )
    setattr(handler, _HANDLER_MARKER, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    for name in _CHATTY_LOGGERS:
        logging.getLogger(name).setLevel(level if level <= logging.DEBUG else logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A logger for one module, named as the stdlib one would be.

    The explicit return type is the point of the wrapper. ``structlog.get_logger``
    is annotated as returning ``Any``, and ``mypy --strict`` then accepts any
    misspelled method on the result without complaint; ``structlog.stdlib``'s
    ``BoundLogger`` is a real type with real methods.

    Binding happens through contextvars rather than through this object, so a
    module-level ``logger = get_logger(__name__)`` is correct and there is nothing
    per-request to thread through a constructor.
    """
    return structlog.stdlib.get_logger(name)
