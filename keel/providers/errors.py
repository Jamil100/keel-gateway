"""The normalized provider error taxonomy (TECHNICAL-DESIGN.md §5.4, FR-2.4).

Providers signal the same condition differently: an HTTP 429, a
``ThrottlingException``, and a ``RateLimitError`` are one concept. The breaker
must not treat them as three, so every provider failure is mapped onto one of
seven classes before anything downstream sees it.

This module fixes the **taxonomy** and nothing else. The per-provider mapping —
which raw exception becomes which class — lands in P2-T1 with captured fixtures
to replay against. Fixing the vocabulary first means the breaker, the metrics
catalogue, and the executor can all be written against a stable set of names
while the mapping is still being learned.

**Not to be confused with** :mod:`keel.api.errors`. That module is the
*client-facing* fault vocabulary at the ingress boundary and never reaches the
breaker. This one describes *provider* behaviour and is the breaker's only
input. Merging them would let a malformed client request look like provider
degradation.

The ``value`` of each member is wire-visible and therefore stable API: it is
the ``error_class`` label on ``keel_provider_errors_total`` (§6) and the source
of the per-class field names in the health hash (§5.5). Renaming one silently
breaks every dashboard query and orphans the counters already in Redis.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

__all__ = ["ErrorClass", "NormalizedError"]


class ErrorClass(StrEnum):
    """The seven normalized failure modes of §5.4."""

    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILURE = "auth_failure"
    CONTENT_FILTER = "content_filter"
    BAD_REQUEST = "bad_request"

    @property
    def counts_toward_breaker(self) -> bool:
        """Whether this failure is evidence that the *provider* is unhealthy.

        The three ``False`` rows are the ones that matter (D7). Counting auth
        failures as degradation means one bad API key opens every breaker at
        once and the gateway declares a total outage over a typo. Counting
        content filters means a policy-violating prompt is retried across every
        provider until all of them trip.
        """
        return _TAXONOMY[self].counts_toward_breaker

    @property
    def retry_elsewhere(self) -> bool:
        """Whether another provider is worth trying for the same request.

        ``BAD_REQUEST`` is the client's fault and fails identically everywhere.
        ``AUTH_FAILURE`` is our configuration, not this provider's. For
        ``CONTENT_FILTER`` the provider behaved correctly, so retrying is a
        policy decision — defaulted off, and revisited if a tenant asks for it.
        """
        return _TAXONOMY[self].retry_elsewhere


class _Semantics(BaseModel):
    """One row of the §5.4 truth table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    counts_toward_breaker: bool
    retry_elsewhere: bool


# The §5.4 truth table, transcribed once. Both properties above read from here
# rather than from a condition, so the table is the thing a reviewer checks and
# `tests/test_provider_errors.py` asserts it row by row.
_TAXONOMY: Final[dict[ErrorClass, _Semantics]] = {
    ErrorClass.RATE_LIMIT: _Semantics(counts_toward_breaker=True, retry_elsewhere=True),
    ErrorClass.TIMEOUT: _Semantics(counts_toward_breaker=True, retry_elsewhere=True),
    ErrorClass.SERVER_ERROR: _Semantics(counts_toward_breaker=True, retry_elsewhere=True),
    ErrorClass.QUOTA_EXHAUSTED: _Semantics(counts_toward_breaker=True, retry_elsewhere=True),
    ErrorClass.AUTH_FAILURE: _Semantics(counts_toward_breaker=False, retry_elsewhere=False),
    ErrorClass.CONTENT_FILTER: _Semantics(counts_toward_breaker=False, retry_elsewhere=False),
    ErrorClass.BAD_REQUEST: _Semantics(counts_toward_breaker=False, retry_elsewhere=False),
}

# An eighth class added without a row here would raise `KeyError` at the first
# request that hit it — during an incident, inside the breaker. Failing at
# import instead makes "you added a class, now decide what it means for the
# breaker" a five-second problem. Same posture as NFR-4 for config.
if set(_TAXONOMY) != set(ErrorClass):
    missing = sorted(member.value for member in ErrorClass if member not in _TAXONOMY)
    raise RuntimeError(
        f"ErrorClass members missing from the §5.4 truth table: {missing}. "
        f"Every class must declare counts_toward_breaker and retry_elsewhere."
    )


class NormalizedError(BaseModel):
    """One provider failure, in Keel's vocabulary rather than the provider's."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_class: ErrorClass

    message: str
    """Human-readable, for logs. Never shown to a client verbatim."""

    provider_error_type: str | None = None
    """The raw exception or error type this was mapped from, e.g. ``RateLimitError``.

    Kept so a mapping gap is diagnosable from a log line. P2-T1 defaults
    unmapped exceptions to ``SERVER_ERROR`` and warns naming this value — a
    catch-all that records what it caught, rather than one that hides it.
    """

    status_code: int | None = None
    """Provider HTTP status where there was one. ``None`` for SDK-level failures."""

    @property
    def counts_toward_breaker(self) -> bool:
        """Delegates to :class:`ErrorClass` — one truth table, read from everywhere."""
        return self.error_class.counts_toward_breaker

    @property
    def retry_elsewhere(self) -> bool:
        return self.error_class.retry_elsewhere
