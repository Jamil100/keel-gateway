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

This module deliberately imports no web framework. It raises and renders; the
FastAPI exception handler that turns a :class:`KeelError` into a response is
registered in ``keel/api/app.py`` (P1-T7).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

__all__ = [
    "EnvelopeValidationError",
    "FieldProblem",
    "KeelError",
    "MalformedRequestError",
    "ProblemCode",
    "raise_for",
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
