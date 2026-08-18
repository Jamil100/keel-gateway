"""The request envelope, and the boundary that builds one or rejects the request.

Every request becomes a :class:`RequestEnvelope` before anything routes it
(TECHNICAL-DESIGN.md §5.1). The envelope carries the metadata the OpenAI schema
does not: who is asking (``tenant``), what for (``feature``), under which
routing policy (``request_class``), and what the request semantically needs
(``capabilities``). Without it the gateway cannot attribute cost (S4, FR-6.2),
cannot pick a preference list (FR-1.4), and cannot filter on capability
(FR-1.5) — which is why untagged traffic is rejected rather than served
(FR-1.3).

Metadata arrives in ``X-Keel-*`` headers, with an ``x_keel`` object in the
request body as the fallback for clients that cannot set headers (§5.1). The
header wins on conflict. Two deliberate departures from the §5.1 table, both
following the code that already exists:

* ``request_class`` is a ``str`` checked against ``config.request_classes``,
  not an enum. Classes are config-defined per design principle 1 — adding one
  must not require touching code.
* ``capabilities`` is a ``frozenset``, matching ``ProviderConfig.capabilities``.

``deferrable`` is derived from the class's config and is never taken from the
client. A caller cannot promote its own request into the durable queue.

This module imports no web framework, so the whole validation boundary is
exercised with plain dicts and no HTTP (NFR-2). ``keel/api/app.py`` (P1-T7)
supplies the real headers and body.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from keel.api.errors import FieldProblem, ProblemCode, raise_for
from keel.clock import Clock
from keel.config import KeelConfig, RequestClassConfig

__all__ = [
    "BODY_EXTENSION_KEY",
    "HEADER_CAPABILITIES",
    "HEADER_CLASS",
    "HEADER_FEATURE",
    "HEADER_IDEMPOTENCY_KEY",
    "HEADER_REQUEST_ID",
    "HEADER_TENANT",
    "RequestEnvelope",
    "build_envelope",
]

HEADER_TENANT: Final = "X-Keel-Tenant"
HEADER_FEATURE: Final = "X-Keel-Feature"
HEADER_REQUEST_ID: Final = "X-Keel-Request-Id"
HEADER_CLASS: Final = "X-Keel-Class"
HEADER_CAPABILITIES: Final = "X-Keel-Capabilities"
HEADER_IDEMPOTENCY_KEY: Final = "X-Keel-Idempotency-Key"

BODY_EXTENSION_KEY: Final = "x_keel"

# Envelope field -> the header that carries it. Declaration order is the order
# problems are reported in, so a client and a test see a stable list rather than
# whatever order a set happened to iterate in.
_REQUIRED: Final[tuple[tuple[str, str], ...]] = (
    ("tenant", HEADER_TENANT),
    ("feature", HEADER_FEATURE),
    ("request_id", HEADER_REQUEST_ID),
    ("request_class", HEADER_CLASS),
)

_OPTIONAL: Final[tuple[tuple[str, str], ...]] = (
    ("capabilities", HEADER_CAPABILITIES),
    ("idempotency_key", HEADER_IDEMPOTENCY_KEY),
)

# What the body extension object accepts. `deferrable` is pointedly absent: it
# is derived from config, so a client naming it gets an error rather than a
# silent override.
_EXTENSION_KEYS: Final[frozenset[str]] = frozenset(field for field, _ in _REQUIRED + _OPTIONAL)


class RequestEnvelope(BaseModel):
    """One validated request, as §5.1 defines it.

    Frozen for the same reason the config models are: this is decided once at
    ingress and then read by the router, executor, health recorder, and cost
    engine. Nothing downstream may edit it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    """Correlation key across attempts, logs, and metrics. Any non-empty string."""

    tenant: str
    feature: str
    request_class: str
    capabilities: frozenset[str]

    deferrable: bool
    """Derived from the request class's config, never asserted by the client."""

    idempotency_key: str | None

    payload: dict[str, Any]
    """The provider-bound body, with the ``x_keel`` extension already removed."""

    received_at: float
    """From the injected clock. Never ``time.time()`` (ADR 0001)."""


def _problem(field: str, header: str | None, code: ProblemCode, message: str) -> FieldProblem:
    return FieldProblem(field=field, header=header, code=code, message=message)


def _split_extension(
    body: Mapping[str, Any], problems: list[FieldProblem]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate the provider-bound payload from the Keel metadata extension.

    The extension must be *removed* from the payload, not merely read. This is
    the asymmetry §5.1 points at when it prefers headers: a header survives
    request-body pass-through untouched, a body field does not. Leaving
    ``x_keel`` in place would forward an unknown top-level field to the provider
    and earn a 400 for a request the client wrote correctly.
    """
    payload = {key: value for key, value in body.items() if key != BODY_EXTENSION_KEY}

    raw = body.get(BODY_EXTENSION_KEY)
    if raw is None:
        return payload, {}

    if not isinstance(raw, dict):
        problems.append(
            _problem(
                BODY_EXTENSION_KEY,
                None,
                ProblemCode.INVALID,
                f"must be a JSON object mapping envelope fields to values; "
                f"got {type(raw).__name__}",
            )
        )
        # Carry on with an empty extension. The headers may still supply
        # everything, and the client deserves the whole problem list at once.
        return payload, {}

    extension: dict[str, Any] = raw
    for key in extension:
        if key in _EXTENSION_KEYS:
            continue
        detail = (
            "is derived from the request class's configuration and cannot be set by a client"
            if key == "deferrable"
            else f"is not an envelope field; accepted fields are {sorted(_EXTENSION_KEYS)}"
        )
        problems.append(
            _problem(f"{BODY_EXTENSION_KEY}.{key}", None, ProblemCode.UNKNOWN_FIELD, detail)
        )

    return payload, extension


def _resolve(
    field: str,
    header: str,
    headers: Mapping[str, str],
    extension: Mapping[str, Any],
    problems: list[FieldProblem],
) -> str | None:
    """Read one string field: header first, then the body extension.

    A blank or whitespace-only value is the same client mistake as an absent
    one, so both return ``None`` and collapse to a single ``missing`` code.
    """
    raw = headers.get(header.lower())
    if raw is not None and raw.strip():
        return raw.strip()

    if field not in extension:
        return None

    value = extension[field]
    if not isinstance(value, str):
        problems.append(
            _problem(
                f"{BODY_EXTENSION_KEY}.{field}",
                header,
                ProblemCode.INVALID,
                f"must be a string; got {type(value).__name__}",
            )
        )
        return None

    return value.strip() or None


def _resolve_capabilities(
    headers: Mapping[str, str],
    extension: Mapping[str, Any],
    problems: list[FieldProblem],
) -> frozenset[str]:
    """Optional (FR-1.5) — absent means "no constraint", never an error.

    Accepts the comma-separated header form, and in the body either the same
    string or a JSON list. Empty segments are dropped rather than becoming
    empty-string capabilities that no provider can ever satisfy.
    """
    raw: Any = headers.get(HEADER_CAPABILITIES.lower())
    if raw is None or not raw.strip():
        raw = extension.get("capabilities")

    if raw is None:
        return frozenset()

    if isinstance(raw, str):
        return frozenset(tag.strip() for tag in raw.split(",") if tag.strip())

    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return frozenset(tag.strip() for tag in raw if tag.strip())

    problems.append(
        _problem(
            f"{BODY_EXTENSION_KEY}.capabilities",
            HEADER_CAPABILITIES,
            ProblemCode.INVALID,
            f"must be a comma-separated string or a list of strings; got {type(raw).__name__}",
        )
    )
    return frozenset()


def build_envelope(
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    config: KeelConfig,
    clock: Clock,
) -> RequestEnvelope:
    """Build a validated envelope, or raise a ``KeelError`` listing every problem.

    Nothing here short-circuits on the first failure. A client fixing headers
    one round-trip at a time is a bad first impression of the gateway, so the
    whole problem list is collected and reported together (FR-1.3).
    """
    problems: list[FieldProblem] = []

    # Header lookup is case-insensitive: HTTP says so, and it lets a test pass a
    # plain dict where production passes Starlette's Headers mapping.
    lowered = {name.lower(): value for name, value in headers.items()}

    payload, extension = _split_extension(body, problems)

    values: dict[str, str | None] = {
        field: _resolve(field, header, lowered, extension, problems)
        for field, header in _REQUIRED + _OPTIONAL
        if field != "capabilities"
    }
    capabilities = _resolve_capabilities(lowered, extension, problems)

    for field, header in _REQUIRED:
        if values[field] is None:
            problems.append(
                _problem(
                    field,
                    header,
                    ProblemCode.MISSING,
                    f"required; supply the {header} header or "
                    f"{BODY_EXTENSION_KEY}.{field} in the request body",
                )
            )

    request_class = values["request_class"]
    class_config = config.request_classes.get(request_class) if request_class else None

    if request_class is not None and class_config is None:
        problems.append(
            _problem(
                "request_class",
                HEADER_CLASS,
                ProblemCode.UNKNOWN_REQUEST_CLASS,
                f"{request_class!r} is not a configured request class; "
                f"known classes are {sorted(config.request_classes)}",
            )
        )

    idempotency_key = values["idempotency_key"]
    # Checked only when the class is known. With an unknown class we cannot know
    # whether it is deferrable, and inventing a second problem from a guess sends
    # the client chasing a requirement that may not exist.
    if class_config is not None and class_config.deferrable and idempotency_key is None:
        problems.append(
            _problem(
                "idempotency_key",
                HEADER_IDEMPOTENCY_KEY,
                ProblemCode.MISSING,
                f"request class {request_class!r} is deferrable, so an idempotency key is "
                f"required to stop a replay duplicating a side effect (FR-5.3); supply the "
                f"{HEADER_IDEMPOTENCY_KEY} header or {BODY_EXTENSION_KEY}.idempotency_key",
            )
        )

    raise_for(problems, values["request_id"])

    # raise_for returned, so every required field resolved and the class exists.
    return RequestEnvelope(
        request_id=_expect(values["request_id"]),
        tenant=_expect(values["tenant"]),
        feature=_expect(values["feature"]),
        request_class=_expect(request_class),
        capabilities=capabilities,
        deferrable=_expect_class(class_config).deferrable,
        idempotency_key=idempotency_key,
        payload=payload,
        received_at=clock.now(),
    )


def _expect(value: str | None) -> str:
    """Narrow a resolved-and-validated field for mypy. Never fires at runtime."""
    if value is None:  # pragma: no cover - raise_for would have raised first
        raise RuntimeError("required field passed validation but is unset")
    return value


def _expect_class(value: RequestClassConfig | None) -> RequestClassConfig:
    """As :func:`_expect`, for the resolved request class config."""
    if value is None:  # pragma: no cover - raise_for would have raised first
        raise RuntimeError("request class passed validation but is unset")
    return value
