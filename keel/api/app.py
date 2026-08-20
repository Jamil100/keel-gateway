"""The HTTP surface: one OpenAI-compatible endpoint, one liveness probe.

This is the only module in the gateway that knows what a socket is. Everything
below it — envelope validation, routing, execution, normalization — was built and
tested against plain dicts (NFR-2), and this file is the thin adapter that turns
a real request into those dicts and a ``ProviderResult`` back into a real
response.

**Adoption is a base-URL and key change (FR-1.1).** ``POST /v1/chat/completions``
takes and returns the OpenAI shape, so an existing SDK client points at Keel and
keeps working. The metadata Keel needs and OpenAI does not carry rides in
``X-Keel-*`` headers (§5.1); the payload itself is opaque and is forwarded
untouched.

**Configuration is decided once, at startup (NFR-4).** The lifespan loads the
config and builds the registry, and lets any ``ConfigError`` escape. The process
then never accepts a request against a config it could not validate — which is
the whole argument of ADR 0004, applied one level up: a gateway that starts
"successfully" and fails every request is worse than one that refuses to start.

**Every request is correlated from here (FR-7.3).** ``request_id`` is bound to
contextvars from the raw header before anything can reject the request, and
``tenant``/``feature``/``request_class`` are added as soon as the envelope exists.
Everything below inherits them — the executor's per-attempt line, and the stdlib
warnings from the health tracker and the error normalizer, which know nothing
about any of this. See ``keel/observability/logging.py``.

**What is deliberately absent.** ``X-Keel-Cost-Micros`` waits for the Phase 4
cost engine, because emitting a zero would be a cost claim and a wrong one; the
``422`` for no capable provider (§5.7) and a real attempt count arrive with Phase
3's failover loop; §4's ``202`` deferrable-enqueue branch is Phase 5.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

from keel.api.envelope import HEADER_REQUEST_ID, RequestEnvelope, build_envelope
from keel.api.errors import (
    FieldProblem,
    KeelError,
    MalformedRequestError,
    ProblemCode,
    raise_for,
    upstream_error_for,
)
from keel.clock import Clock, SystemClock
from keel.config import DEFAULT_CONFIG_PATH, KeelConfig, load_config
from keel.health.window import HealthTracker
from keel.observability.logging import configure_logging, get_logger
from keel.observability.metrics import OUTCOME_ERROR, OUTCOME_OK, MetricsCatalogue
from keel.observability.middleware import (
    PROVIDER_SECONDS_ATTR,
    REQUEST_CLASS_ATTR,
    OverheadMiddleware,
)
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.credentials import ProviderCredentials
from keel.providers.registry import build_registry
from keel.redis import create_redis_client
from keel.routing.executor import Executor
from keel.routing.router import Router

__all__ = ["AppContext", "app", "create_app", "keel_error_handler"]

logger = get_logger(__name__)

CONFIG_PATH_ENV: Final = "KEEL_CONFIG_PATH"
STATE_KEY: Final = "keel"

HEADER_PROVIDER: Final = "X-Keel-Provider"
HEADER_ATTEMPTS: Final = "X-Keel-Attempts"

PHASE_1_ATTEMPTS: Final = 1
"""Always one, because ``Executor.execute`` invokes candidate 1 and stops.

A constant rather than a measurement, and honest about being one. When Phase 3's
failover loop starts counting — the seam is already named in
``keel/routing/executor.py`` — this is deleted and the real count travels on the
result. The header does not change shape, so a client reading it today keeps
working.
"""

STREAM_KEY: Final = "stream"

HTTP_OK: Final = 200
"""The success status, for the log line only.

``KeelError.status_code`` supplies the failure statuses; the success path has no
error object to read one from, and Starlette's default is not stated anywhere the
log line could reach.
"""


@dataclass(frozen=True, slots=True)
class AppContext:
    """Everything the request path needs, decided once at startup.

    One ``app.state`` key rather than four. ``State.__getattr__`` returns ``Any``,
    so every attribute read off it is unchecked; funnelling them through one
    dataclass means that happens exactly once, in :func:`_context`.
    """

    config: KeelConfig
    clock: Clock
    registry: Mapping[str, ProviderAdapter]
    executor: Executor
    metrics: MetricsCatalogue
    """The §6 catalogue and its registry, built once per app.

    Per app rather than module-global because ``prometheus_client`` refuses a
    second registration of the same metric name, and the test suite builds many
    apps in one session."""

    tracker: HealthTracker
    """The health recorder. Held here as well as inside the executor because
    Phase 3's breaker and P2-T4's exporter both read it without going through an
    attempt, and one narrowed accessor beats four unchecked ``state`` reads."""


def _context(request: Request) -> AppContext:
    """The startup context, narrowed from ``Any``."""
    context = getattr(request.app.state, STATE_KEY, None)
    if not isinstance(context, AppContext):  # pragma: no cover - the lifespan sets it
        raise RuntimeError(
            "the app has no startup context, so the lifespan did not run. In a test, use "
            "`with TestClient(app) as client:` rather than a bare `TestClient(app)`."
        )
    return context


def _resolve_config_path(override: str | Path | None) -> Path:
    """Explicit argument, then ``KEEL_CONFIG_PATH``, then the shipped default.

    Read at startup rather than at import, so the module-level :data:`app` does
    not freeze whatever the environment happened to hold when Python imported
    this file.

    A blank value counts as absent, matching ``ProviderCredentials`` and for the
    same reason: ``.env.example`` ships empty assignments, and "copied the
    template, did not fill it in" must not become ``Path("")``. Note that
    ``.env`` reaches this only through a real export — pydantic-settings reads
    that file for credentials, not for ``os.environ`` at large — which compose's
    ``env_file:`` does in P2-T6 and a bare dev shell does not.
    """
    if override is not None:
        return Path(override)
    from_env = os.environ.get(CONFIG_PATH_ENV, "").strip()
    return Path(from_env) if from_env else DEFAULT_CONFIG_PATH


def _json_type_name(value: object) -> str:
    """The JSON name for a decoded value, so the error speaks the client's language."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _reject_body(request: Request, detail: str, cause: BaseException | None) -> NoReturn:
    """Raise the 400 for a body that never had a chance of being an envelope."""
    raise MalformedRequestError(
        "Request rejected: the request body must be a JSON object.",
        fields=[FieldProblem(field="body", header=None, code=ProblemCode.INVALID, message=detail)],
        # Best effort. The header may itself be missing, in which case
        # `error.keel.request_id` is null and the client correlates by time —
        # the same fallback ADR 0003 already describes.
        request_id=request.headers.get(HEADER_REQUEST_ID),
    ) from cause


async def _decode_body(request: Request) -> Mapping[str, Any]:
    """The body as a JSON object, or a 400 explaining why it is not one.

    This is the one guard that has to sit *above* ``build_envelope`` rather than
    inside it. That function's signature requires a ``Mapping`` and it calls
    ``.items()`` immediately, so a bare JSON array would surface as a 500 for
    what is plainly a client mistake.

    FR-1.3's "report every problem at once" is knowingly narrowed here, and the
    narrowing is honest: a body that is not an object cannot be searched for an
    ``x_keel`` extension, so there are no header problems to collect alongside
    this one. A client that sent an array gets the single sentence that matters.
    """
    try:
        decoded: Any = await request.json()
    except ValueError as exc:
        # Starlette hands the bytes straight to `json.loads`, so an empty body
        # arrives here too rather than as `None`. Caught as `ValueError` — the
        # base of `JSONDecodeError` — so swapping the serializer cannot quietly
        # turn a client mistake back into a 500.
        _reject_body(request, f"could not be parsed as JSON ({exc})", exc)

    if not isinstance(decoded, dict):
        _reject_body(request, f"must be a JSON object; got {_json_type_name(decoded)}", None)

    return decoded


def _reject_streaming(envelope: RequestEnvelope) -> None:
    """Refuse ``stream: true`` until FR-1.6 lands, rather than half-serving it.

    Checked *after* the envelope is built, not before, so FR-1.3's promise is
    untouched: a client with missing headers hears about all of them first and
    only then about streaming. Passing this through instead would hand
    ``stream: true`` to an adapter with no streaming path and return a single
    non-streamed body to a client parsing for SSE — a wrong answer dressed as a
    right one.
    """
    if envelope.payload.get(STREAM_KEY) is not True:
        return
    raise_for(
        [
            FieldProblem(
                field=STREAM_KEY,
                header=None,
                code=ProblemCode.INVALID,
                message=(
                    "streaming responses are not supported yet (FR-1.6, Phase 2); "
                    "omit `stream` or set it to false"
                ),
            )
        ],
        envelope.request_id,
    )


def _expect_response(result: ProviderResult) -> dict[str, Any]:
    """Narrow a successful result's body for mypy.

    ``ProviderResult`` enforces exactly one of ``response``/``error``, but that
    is a runtime validator and invisible to a type checker. Same ``_expect``
    idiom as ``envelope.py`` and ``registry.py``.
    """
    if result.response is None:  # pragma: no cover - the xor validator forbids it
        raise RuntimeError("a successful ProviderResult carried no response")
    return result.response


def _log_completed(result: ProviderResult, status_code: int) -> None:
    """One line per served request, on the success and the upstream-failure path alike.

    Distinct from the executor's ``provider_attempt`` line and deliberately so:
    §6 counts requests once while attempts may be several, and Phase 3's failover
    loop turns that from a coincidence into a difference. The split here is what
    keeps ``keel_requests_total`` and this line describing the same thing.

    Logged on the failure branch for the same reason ``X-Keel-Provider`` is set
    there — a 503 that will not say which provider it tried is useless in an
    incident, and the exception handler below has no result to read a name from.

    The correlation fields are absent on purpose: ingress bound them to
    contextvars, so adding them here would duplicate what ``merge_contextvars``
    already folds in, and the two copies could disagree.
    """
    logger.info(
        "request_completed",
        provider=result.provider,
        attempts=PHASE_1_ATTEMPTS,
        outcome=OUTCOME_OK if result.ok else OUTCOME_ERROR,
        status_code=status_code,
        error_class=result.error.error_class.value if result.error is not None else None,
    )


def _render(error: KeelError, headers: Mapping[str, str] | None = None) -> JSONResponse:
    """The single place a ``KeelError`` becomes bytes."""
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_body(),
        headers=dict(headers) if headers else None,
    )


async def keel_error_handler(request: Request, exc: Exception) -> Response:
    """Render any ``KeelError`` as the one client error body (ADR 0003).

    Typed on ``Exception`` rather than ``KeelError`` because Starlette's
    ``ExceptionHandler`` alias is ``Callable[[Request, Exception], ...]`` and
    parameter types are contravariant — the narrower annotation is rejected by
    ``mypy --strict`` at the registration call. The narrowing happens here
    instead, and costs one branch.

    Status and body both come from the exception, so §5.7's ``422`` and any later
    subclass need no change here. Starlette walks the MRO when dispatching, so
    registering the base class covers all of them.
    """
    if not isinstance(exc, KeelError):  # pragma: no cover - registered per class
        raise exc

    # Field *names* and problem *codes* only — never the offending values, which
    # are client input and may carry anything (PRD §2.1 rules out redaction, so
    # the safe amount to log is none). WARNING rather than ERROR: a rejected
    # request is the gateway working, not failing.
    logger.warning(
        "request_rejected",
        status_code=exc.status_code,
        problems=[problem.code.value for problem in exc.fields],
        problem_fields=[problem.field for problem in exc.fields],
    )
    return _render(exc)


def create_app(
    *,
    config_path: str | Path | None = None,
    clock: Clock | None = None,
    registry: Mapping[str, ProviderAdapter] | None = None,
    credentials: ProviderCredentials | None = None,
    redis: Redis | None = None,
    metrics: MetricsCatalogue | None = None,
) -> FastAPI:
    """Build the gateway app. Does no I/O — every read happens in the lifespan.

    :param config_path: overrides ``KEEL_CONFIG_PATH`` and the shipped default.
    :param clock: defaults to ``SystemClock()``, and that default matters beyond
        tidiness: ``MockAdapter`` stamps ``created = int(clock.now())``, so a
        ``ManualClock`` in production would date every completion to epoch 0.
    :param registry: a pre-built adapter set, for tests that need a specific
        adapter without a network. When omitted the real ``build_registry`` runs
        inside the lifespan, so the ADR 0004 startup guarantee applies.
    :param credentials: passed to ``build_registry``; ignored when ``registry``
        is supplied.
    :param metrics: a pre-built §6 catalogue. Supply one to read counters back
        in a test; otherwise the lifespan builds a fresh registry per app.
    :param redis: a pre-built client, for tests that must not touch a socket.
        When omitted the lifespan builds one from ``REDIS_URL`` and closes it on
        shutdown; an injected client is left open, because it belongs to the
        caller and a test may want to read the health keys back after the
        ``TestClient`` block exits. Without this parameter every test in
        ``tests/test_app.py`` would attempt a real connection to localhost, which
        NFR-2 rules out.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # First, before anything that can fail. `load_config` below raises on a
        # bad config and that exception is deliberately left to escape (NFR-4) —
        # so the one message an operator most needs to read is emitted by a
        # logger that has already been configured, rather than being the single
        # line their aggregator cannot parse.
        configure_logging()

        # No try/except, deliberately (NFR-4). A ConfigError raised here escapes
        # the ASGI startup event: uvicorn logs "Application startup failed" and
        # exits non-zero, and `TestClient.__enter__` re-raises it. Catching it to
        # serve 500s instead would reintroduce exactly the lazy discovery ADR
        # 0004 argues against — a green process failing every request.
        resolved_clock: Clock = clock if clock is not None else SystemClock()
        config = load_config(_resolve_config_path(config_path))
        adapters: Mapping[str, ProviderAdapter] = (
            registry
            if registry is not None
            else build_registry(config=config, clock=resolved_clock, credentials=credentials)
        )

        # Deliberately not pinged (ADR 0008). `from_url` opens no socket, so an
        # unreachable Redis does not stop the gateway starting — unlike a missing
        # credential, which means it could never serve a request at all. Health
        # data is not required to answer one, and coupling the two would turn a
        # Redis restart into a gateway outage.
        owns_redis = redis is None
        client = create_redis_client() if redis is None else redis

        # The catalogue itself is built below, before the middleware stack is
        # assembled. Priming happens here instead, because it needs the provider
        # names and the config is deliberately not read until startup: it makes the
        # provider-keyed series read a flat zero rather than "no data" before Phase
        # 3 produces them, which is the whole reason P2-T4 declares them up front.
        catalogue.prime(config.providers)

        tracker = HealthTracker(
            redis=client,
            breaker=config.breaker,
            clock=resolved_clock,
            on_write_dropped=catalogue.observe_dropped_health_write,
        )

        setattr(
            app.state,
            STATE_KEY,
            AppContext(
                config=config,
                clock=resolved_clock,
                registry=adapters,
                executor=Executor(
                    router=Router(config=config),
                    config=config,
                    registry=adapters,
                    clock=resolved_clock,
                    tracker=tracker,
                    metrics=catalogue,
                ),
                metrics=catalogue,
                tracker=tracker,
            ),
        )

        yield

        # --- Shutdown ---------------------------------------------------------
        # The mock is in-process (ADR 0002) and Cohere's transport belongs to
        # LiteLLM, so the Redis pool is the only thing here. Closed only when this
        # lifespan built it: an injected client belongs to whoever passed it.
        if owns_redis:
            await client.aclose()

    # Built here, not in the lifespan: Starlette assembles the middleware stack on
    # the first request and forbids additions after that, so the overhead
    # middleware needs the catalogue now. Construction is pure in-memory — no
    # config, no socket — so `create_app` stays I/O-free (the module-level `app`
    # depends on that). The provider names it primes with arrive at startup.
    catalogue = metrics if metrics is not None else MetricsCatalogue()

    app = FastAPI(
        title="Keel",
        summary="A self-healing LLM gateway with an OpenAI-compatible surface.",
        lifespan=lifespan,
    )
    app.add_exception_handler(KeelError, keel_error_handler)

    # Outermost by construction, so the wall clock it measures includes every
    # other layer — which is what S5 means by gateway overhead.
    app.add_middleware(OverheadMiddleware, metrics=catalogue)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        """The OpenAI-compatible ingress (FR-1.1, §5.1).

        There is no pydantic body model here, on purpose. The payload is opaque
        pass-through: the gateway lifts ``x_keel`` out of it and forwards the
        rest verbatim, so a model declared here would silently drop whatever
        field OpenAI adds next and turn a working client into a broken one.
        Validating the payload is the provider's job; validating the *envelope*
        is ours.

        For the same reason the return annotation is ``Response`` and must stay
        that way. FastAPI infers a ``response_model`` from the return annotation,
        so ``-> dict[str, Any]`` would re-serialize the provider's body through a
        generated model and drop both the unknown fields and these headers. It
        looks like a tightening and is a regression.
        """
        context = _context(request)

        # Bound before anything that can reject the request, and from the raw
        # header rather than the envelope, because `build_envelope` below raises
        # on a request missing metadata and there would be no envelope to read.
        # A 400 is exactly the line a client most needs correlated, so binding
        # only after validation would lose the ones that matter most.
        #
        # `clear_contextvars` first — and it is not load-bearing today, which the
        # code should say rather than imply. Measured: every request already
        # starts with an empty context, because the ASGI server runs each one in
        # its own task and a binding made inside never escapes it. Deleting this
        # line leaves the entire suite passing.
        #
        # It stays for the caller that does not exist yet. Phase 5's deferred
        # worker drains a queue in one long-lived task, where successive jobs
        # *do* share a context, and a job inheriting the previous job's tenant
        # would be mislabelled rather than unlabelled — wrong, and looking right.
        # Same posture as the `transaction=True` flag in `keel/health/window.py`:
        # a guard whose absence no test can currently detect is recorded here
        # rather than in a test that would pass either way.
        clear_contextvars()
        bind_contextvars(request_id=request.headers.get(HEADER_REQUEST_ID))

        body = await _decode_body(request)

        envelope = build_envelope(
            # Starlette's `Headers` is a case-insensitive `Mapping[str, str]`,
            # which is exactly what `build_envelope` documents accepting.
            headers=request.headers,
            body=body,
            config=context.config,
            clock=context.clock,
        )

        # Everything below — the executor's per-attempt line, and any warning
        # from the health tracker or the normalizer, which are stdlib loggers
        # that know nothing about this — inherits these four fields.
        bind_contextvars(
            tenant=envelope.tenant,
            feature=envelope.feature,
            request_class=envelope.request_class,
        )

        _reject_streaming(envelope)

        result = await context.executor.execute(envelope)

        # The handoff to the overhead middleware, which sees only a Request and a
        # Response and so can reach neither the envelope nor the result. Set on
        # both branches below by being set here, before either is taken — an
        # upstream failure consumed gateway time exactly like a success did.
        #
        # `setattr` with the shared constants rather than literal attribute names,
        # so the writer here and the reader there cannot drift apart into a handoff
        # that silently reads empty and records no overhead at all.
        setattr(request.state, REQUEST_CLASS_ATTR, envelope.request_class)
        setattr(request.state, PROVIDER_SECONDS_ATTR, result.latency_ms / 1000.0)

        context.metrics.observe_request(
            envelope=envelope, provider=result.provider, ok=result.ok
        )

        # Set on the failure path too. A 503 that will not say which provider it
        # tried is useless in the demo these headers exist for (§4), and the
        # exception handler has no result to read a provider name from — which is
        # why the failure branch renders inline rather than raising.
        headers = {HEADER_PROVIDER: result.provider, HEADER_ATTEMPTS: str(PHASE_1_ATTEMPTS)}

        if result.error is not None:
            error = upstream_error_for(
                result.error, provider=result.provider, request_id=envelope.request_id
            )
            _log_completed(result, error.status_code)
            return _render(error, headers)

        _log_completed(result, HTTP_OK)
        return JSONResponse(content=_expect_response(result), headers=headers)

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness. The process is up and the event loop is turning — nothing more.

        It touches no config, no registry, no provider, and (from P2-T2) no
        Redis, and that restraint is the point. The P2-T6 compose healthcheck
        *restarts the container* when this fails, so a probe that called a
        provider would turn a provider outage into a restart loop — precisely the
        failure this gateway exists to absorb. Deciding what to do about a
        degraded provider is a routing question, and §5.6's breaker owns it.

        One guarantee it does give for free: no route answers until the lifespan
        has completed, so a 200 here means ``load_config`` and ``build_registry``
        both succeeded. Readiness in the fuller sense — Redis reachable, queue
        drained — is a different endpoint, and nothing needs it until there is
        something for it to answer.
        """
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    async def metrics_endpoint(request: Request) -> Response:
        """The Prometheus scrape endpoint (FR-3.3, §6).

        Unauthenticated and on the main port, because that is what every document
        describing it says: §8 draws one `PR -->|scrape| GW` edge, the compose
        stack publishes one gateway port, and the M2 verification step is literally
        `curl localhost:8080/metrics`. The gateway has no authentication at all
        (§10 records that as a known limitation), so adding some here would be an
        undocumented departure that also breaks that verification command.

        Reads through :meth:`MetricsCatalogue.render` rather than calling
        ``generate_latest`` here, so the content type travels with the body — it is
        ``version=1.0.0`` in prometheus-client 0.26, not the ``0.0.4`` that older
        examples show, and a scraper given the wrong one may refuse the payload.
        """
        body, content_type = _context(request).metrics.render()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
"""The module-level ASGI app, for ``uvicorn keel.api.app:app`` and the P2-T6 image.

Construction is I/O-free — the config file, ``KEEL_CONFIG_PATH``, and the
credentials are all read inside the lifespan — so importing this module neither
touches the filesystem nor can fail on a bad config. It fails at *startup*, which
is where NFR-4 wants it.
"""
