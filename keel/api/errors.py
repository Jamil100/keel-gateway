"""The one error body every 4xx and 5xx uses.

A client that has to fix its headers one round-trip at a time is a bad first
impression of the gateway, so a rejection lists *every* problem it found, not
the first (FR-1.3). The shape follows the OpenAI error envelope because the
public surface is OpenAI-compatible (FR-1.1): an existing SDK client surfaces
``error.message`` with no special handling, while everything Keel-specific
lives under ``error.keel`` where a machine can read it.

```json
{"error": {"message": "...", "type": "invalid_request_error",
           "code": "missing_metadata",
           "keel": {"request_id": "...", "fields": [...]}}}
```

**These codes are not the §5.4 error taxonomy.** ``ErrorClass`` (P1-T3,
``keel/providers/errors.py``) describes *provider* behaviour and feeds the
breaker. :class:`ProblemCode` describes *client* fault at the gateway boundary
and never reaches the breaker. Merging the two vocabularies would let a
malformed client request look like provider degradation.

**Two vocabularies meet here, and they do not merge.** :class:`ProblemCode`
describes *client* fault at the gateway boundary. ``ErrorClass`` describes
*provider* behaviour and feeds the breaker. The :class:`UpstreamError` family
below is the translation layer between them: it renders an ``ErrorClass`` as an
HTTP status without ever letting a taxonomy value become a ``ProblemCode``. The
taxonomy value does reach the client, but under its own labelled key
(``error.keel.error_class``), never as ``error.code``.

This module deliberately imports no web framework. It raises and renders; the
FastAPI exception handler that turns a :class:`KeelError` into a response is
registered in ``keel/api/app.py`` (P1-T7). It also imports nothing from
``keel.providers.base`` — that module imports ``keel.api.envelope``, which
imports this one, so reaching for ``ProviderResult`` here would close an import
cycle. :func:`upstream_error_for` takes the ``NormalizedError`` instead and the
caller unwraps the result.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict

from keel.providers.errors import ErrorClass, NormalizedError

__all__ = [
    "ChaosUnsupportedError",
    "EnvelopeValidationError",
    "FieldProblem",
    "KeelError",
    "MalformedRequestError",
    "ProblemCode",
    "UnknownProviderError",
    "UpstreamBadRequestError",
    "UpstreamError",
    "UpstreamRateLimitError",
    "UpstreamTimeoutError",
    "UpstreamUnavailableError",
    "raise_for",
    "upstream_error_for",
]


class ProblemCode(StrEnum):
    """Why one field was rejected. A closed set, so a client can branch on it."""

    MISSING = "missing"
    """Required, and absent or blank."""

    UNKNOWN_REQUEST_CLASS = "unknown_request_class"
    """Syntactically fine, but not a class this deployment has configured."""

    INVALID = "invalid"
    """Present with a value of the wrong shape."""

    UNKNOWN_FIELD = "unknown_field"
    """Supplied a field the envelope does not accept."""


# Problems that mean the request was *built* wrong rather than *tagged* wrong.
# Their presence changes the top-level code, so a client can tell "I sent
# nonsense" apart from "I forgot a header".
_STRUCTURAL_CODES = frozenset({ProblemCode.INVALID, ProblemCode.UNKNOWN_FIELD})


class FieldProblem(BaseModel):
    """One thing wrong with one field.

    ``header`` names the header that would have supplied it, so the fix is in
    the response rather than in the documentation. It is ``None`` for problems
    with no header form — anything inside the request-body extension object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    header: str | None
    code: ProblemCode
    message: str


def _summarize(problems: Sequence[FieldProblem]) -> str:
    """Human sentence for ``error.message``.

    An OpenAI SDK client surfaces this string and nothing else, so it names the
    offending fields rather than just counting them.
    """
    if not problems:
        return "Request rejected."
    noun = "problem" if len(problems) == 1 else "problems"
    named = ", ".join(problem.field for problem in problems)
    return f"Request rejected: {len(problems)} {noun} ({named})."


class KeelError(Exception):
    """Base for every error rendered to a client.

    Subclasses fix the status code and the machine-readable top-level code.
    ``422`` (no capable provider, §5.7) and ``503`` (all candidates exhausted,
    §4) land here in later phases; the shape is already able to carry them.
    """

    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "invalid_request"
    error_type: ClassVar[str] = "invalid_request_error"

    def __init__(
        self,
        message: str,
        *,
        fields: Sequence[FieldProblem] = (),
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.fields: tuple[FieldProblem, ...] = tuple(fields)
        self.request_id = request_id

    def to_body(self) -> dict[str, Any]:
        """The JSON body, ready to serialize. Field order is stable."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "keel": {
                    # Echoed so a client can correlate a rejection with the
                    # request it sent. None when the request id was itself the
                    # thing missing.
                    "request_id": self.request_id,
                    "fields": [problem.model_dump(mode="json") for problem in self.fields],
                },
            }
        }


class EnvelopeValidationError(KeelError):
    """Required metadata is absent or names something that does not exist."""

    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "missing_metadata"


class MalformedRequestError(KeelError):
    """The request itself is built wrong — bad shapes, or fields we do not accept."""

    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "invalid_request"


class UnknownProviderError(KeelError):
    """A route named a provider this deployment has not configured.

    ``404`` rather than ``400``: the provider name is a path segment, so the
    resource genuinely does not exist. Used by the chaos endpoint (ADR 0010);
    nothing on the request path takes a provider from the client.
    """

    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "unknown_provider"


class ChaosUnsupportedError(KeelError):
    """The provider exists but is not a mock, so it cannot be retuned.

    ``409`` rather than ``400`` or ``404``: the request is well-formed and the
    resource exists — it is the provider's *state* that makes the operation
    impossible, and no edit to the body would fix it. A real provider cannot be
    told to fail 40% of the time, and answering ``200`` to a control that does
    nothing would make a chaos demo silently lie (ADR 0010).
    """

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "chaos_unsupported"


def raise_for(problems: Sequence[FieldProblem], request_id: str | None) -> None:
    """Raise the right error for a collected problem list, or return if empty.

    One call site, one response: every problem found during envelope
    construction is reported together. The choice of class is driven by what is
    in the list rather than by where the caller happened to notice the trouble.
    """
    if not problems:
        return
    error_class: type[KeelError] = (
        MalformedRequestError
        if any(problem.code in _STRUCTURAL_CODES for problem in problems)
        else EnvelopeValidationError
    )
    raise error_class(_summarize(problems), fields=problems, request_id=request_id)


class UpstreamError(KeelError):
    """A provider was reached — or waited for — and did not serve the request.

    Not a client fault, so ``fields`` stays empty: a :class:`FieldProblem` names
    something the caller can fix in its own request, and there is nothing here it
    could edit. ADR 0003 keeps the key present and empty rather than omitting it,
    so a client parses one shape for every error.

    The provider and its §5.4 class are reported under ``error.keel`` where a
    machine can read them, which is what makes a 429 from the gateway
    distinguishable from a 429 the gateway itself imposed in a later phase.
    """

    error_type: ClassVar[str] = "api_error"

    def __init__(
        self,
        error: NormalizedError,
        *,
        provider: str,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            f"Upstream provider {provider!r} failed ({error.error_class.value}): {error.message}",
            request_id=request_id,
        )
        self.provider = provider
        self.error_class = error.error_class

    def to_body(self) -> dict[str, Any]:
        body = super().to_body()
        keel = body["error"]["keel"]
        keel["provider"] = self.provider
        # The taxonomy value, under its own key. Deliberately not `error.code`:
        # that field is the ProblemCode namespace and mixing the two would let a
        # provider's behaviour look like a client mistake to anything branching
        # on it.
        keel["error_class"] = self.error_class.value
        return body


class UpstreamBadRequestError(UpstreamError):
    """The provider rejected the payload itself. Resending it unchanged will not help."""

    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "upstream_bad_request"


class UpstreamRateLimitError(UpstreamError):
    """The provider is throttling us, or the account is out of quota."""

    status_code: ClassVar[int] = 429
    code: ClassVar[str] = "upstream_rate_limit"


class UpstreamTimeoutError(UpstreamError):
    """The provider did not answer inside the deadline — its own, or the gateway's."""

    status_code: ClassVar[int] = 504
    code: ClassVar[str] = "upstream_timeout"


class UpstreamUnavailableError(UpstreamError):
    """No candidate served the request. §4's "503 with normalized error"."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "upstream_unavailable"


# Transcribed by hand from the §5.4 taxonomy rather than derived from it, the same
# posture as `tests/test_provider_errors.py`: the two must agree, and a change to
# either has to be a deliberate edit to both.
#
# The two 400s are worth reading twice. `BAD_REQUEST` and `CONTENT_FILTER` are
# exactly the classes D7 excludes from the breaker, and for the same underlying
# reason — neither says anything about provider health. The status mapping and the
# breaker rule agree because they are reading the same fact, not by coincidence.
_UPSTREAM_BY_CLASS: Final[dict[ErrorClass, type[UpstreamError]]] = {
    ErrorClass.BAD_REQUEST: UpstreamBadRequestError,
    ErrorClass.CONTENT_FILTER: UpstreamBadRequestError,
    ErrorClass.RATE_LIMIT: UpstreamRateLimitError,
    ErrorClass.QUOTA_EXHAUSTED: UpstreamRateLimitError,
    ErrorClass.TIMEOUT: UpstreamTimeoutError,
    ErrorClass.AUTH_FAILURE: UpstreamUnavailableError,
    ErrorClass.SERVER_ERROR: UpstreamUnavailableError,
}

# Refuse to import rather than raise `KeyError` inside a live request. An eighth
# error class is a decision about what the gateway tells its callers, and it
# should be made at a keyboard, not discovered during an incident. Same guard the
# taxonomy module uses for `counts_toward_breaker`.
_UNMAPPED: Final[frozenset[ErrorClass]] = frozenset(ErrorClass) - frozenset(_UPSTREAM_BY_CLASS)
if _UNMAPPED:  # pragma: no cover - the table above is complete
    raise RuntimeError(
        f"no HTTP status is defined for error class(es) "
        f"{sorted(cls.value for cls in _UNMAPPED)}; add a row to "
        f"_UPSTREAM_BY_CLASS in keel/api/errors.py before the taxonomy grows"
    )


def upstream_error_for(
    error: NormalizedError, *, provider: str, request_id: str | None = None
) -> UpstreamError:
    """The rendered error for a provider failure, keyed on its normalized class.

    Takes the ``NormalizedError`` rather than the ``ProviderResult`` that carried
    it, so this module never imports ``keel.providers.base`` and the import cycle
    described in the module docstring stays open.
    """
    return _UPSTREAM_BY_CLASS[error.error_class](error, provider=provider, request_id=request_id)
