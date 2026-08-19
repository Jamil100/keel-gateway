"""Mapping raw provider failures onto the §5.4 taxonomy (P2-T1, FR-2.4).

:mod:`keel.providers.errors` fixes the **vocabulary** — seven ``ErrorClass``
members and the truth table that says what each one means to the breaker. This
module is the other half: which raw failure becomes which class. They are
separate files on purpose. The taxonomy is imported transitively by the health
window, the metrics catalogue, and the breaker; none of those should have to
read a table about LiteLLM's exception tree to find out what ``RATE_LIMIT``
means (**ADR 0007**).

Classification runs in two stages, and the second is strictly subordinate to the
first:

1. **The type table** (:func:`_error_map`) — an ordered ``isinstance`` match,
   most-specific-first. This is the whole mapping for every provider that
   reports failures with a distinct exception type, which is nearly all of them.
2. **Refinement** (:data:`_REFINEMENTS`) — per-provider message rules that may
   only replace :attr:`ErrorClass.SERVER_ERROR`, never a specifically-typed
   result. See :func:`_refine` for why that gate is the whole design.

**LiteLLM is imported lazily and that is measured, not assumed.**
``import litellm`` costs about 4.5 seconds. Nothing at module scope here touches
it: the type table is built inside a cached function, and the refinement table
references only :mod:`re`, ``AdapterName`` and ``ErrorClass``. By the time an
error needs normalizing, the call that failed has already paid the import.

**Rejected: replaying fixtures through ``litellm.exception_type``.** It works
offline, but it is the thing under test — the P1-T7 bug *is* one of its
mis-mappings — so fixtures replayed through it would assert LiteLLM's current
opinion rather than Keel's. It also raises rather than returns, appends
``traceback.format_exc()`` to its message (so the text is non-deterministic),
and prints a feedback banner unless a module global is mutated. Fixtures build
the exception directly instead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Final

from keel.config import AdapterName
from keel.providers.errors import ErrorClass, NormalizedError

__all__ = ["normalize_provider_error"]

_LOGGER: Final = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Stage 1 — the type table
# --------------------------------------------------------------------------


@cache
def _error_map() -> tuple[tuple[type[BaseException], ErrorClass], ...]:
    """The §5.4 mapping, **ordered most-specific-first and matched by isinstance**.

    Three facts about LiteLLM's exceptions shape this table, all verified
    against litellm 1.97.0 / openai 2.54.0 rather than assumed:

    **The root of the tree is ``openai.APIError``, not ``litellm.APIError``.**
    LiteLLM defines exceptions that subclass the OpenAI SDK's same-named ones,
    and its own ``APIError`` is a *sibling* of the rest rather than their
    ancestor — ``isinstance(litellm.RateLimitError(...), litellm.APIError)`` is
    ``False``. A table that used ``litellm.APIError`` as its catch-all would
    therefore catch almost nothing, and every real provider failure would fall
    through to the unmapped default. So the catch-all rows name the OpenAI
    classes, and the LiteLLM rows above them exist only where LiteLLM adds a
    distinction the SDK does not have.

    **``openai.APIError`` is not quite the outermost ancestor.**
    ``openai.OpenAIError`` sits above it, and ``litellm.exceptions.OpenAIError``
    derives from that rather than from ``APIError`` — so it is the one member of
    ``litellm.LITELLM_EXCEPTION_TYPES`` an ``APIError``-terminated table misses.
    Both rows are kept: ``APIError`` because it is the row that carries the
    reasoning above, ``OpenAIError`` beneath it because it is the row that
    actually terminates the tree.

    **Order is load-bearing.** Every row below sits above at least one ancestor
    that would otherwise swallow it:

    * ``Timeout`` subclasses ``openai.APITimeoutError`` -> ``APIConnectionError``
      -> ``APIError``. Below any of them a timeout normalizes to
      ``SERVER_ERROR`` — and §5.4 separates the two precisely because latency
      budgets treat them differently.
    * ``ContentPolicyViolationError`` subclasses ``BadRequestError``. Below it,
      every content filter reads as ``BAD_REQUEST``. Both are excluded from the
      breaker (D7) so the breaker would not notice — but the M2 taxonomy panel,
      whose whole job is showing that split, would be wrong.

    ``BudgetExceededError`` is the one exception outside the tree entirely: it
    subclasses plain ``Exception``, so without its own row no catch-all reaches
    it and quota exhaustion would arrive as an unmapped ``SERVER_ERROR``.

    Cached because it is built from lazily imported modules. By the time any
    error needs normalizing, the call that failed has already imported them.
    """
    import openai
    from litellm import exceptions as lle

    return (
        # --- LiteLLM's own distinctions, which the OpenAI SDK does not draw ---
        (lle.Timeout, ErrorClass.TIMEOUT),
        (lle.BudgetExceededError, ErrorClass.QUOTA_EXHAUSTED),
        (lle.ContentPolicyViolationError, ErrorClass.CONTENT_FILTER),
        (lle.ContextWindowExceededError, ErrorClass.BAD_REQUEST),
        (lle.ServiceUnavailableError, ErrorClass.SERVER_ERROR),
        (lle.BadGatewayError, ErrorClass.SERVER_ERROR),
        # --- the OpenAI hierarchy every LiteLLM exception actually derives from ---
        (openai.APITimeoutError, ErrorClass.TIMEOUT),
        (openai.RateLimitError, ErrorClass.RATE_LIMIT),
        (openai.AuthenticationError, ErrorClass.AUTH_FAILURE),
        (openai.PermissionDeniedError, ErrorClass.AUTH_FAILURE),
        (openai.BadRequestError, ErrorClass.BAD_REQUEST),
        (openai.UnprocessableEntityError, ErrorClass.BAD_REQUEST),
        (openai.NotFoundError, ErrorClass.BAD_REQUEST),
        (openai.ConflictError, ErrorClass.BAD_REQUEST),
        (openai.InternalServerError, ErrorClass.SERVER_ERROR),
        (openai.APIConnectionError, ErrorClass.SERVER_ERROR),
        # --- the roots, so necessarily last, outermost of the two last of all ---
        (openai.APIError, ErrorClass.SERVER_ERROR),
        (openai.OpenAIError, ErrorClass.SERVER_ERROR),
    )


def _classify_by_type(exc: Exception) -> ErrorClass:
    for exception_type, error_class in _error_map():
        if isinstance(exc, exception_type):
            return error_class

    # Default to SERVER_ERROR, but name what was caught, so a mapping gap is one
    # grep away rather than invisible. Warned here, before refinement, so a class
    # that refinement later rescues still reports the type-table gap.
    _LOGGER.warning(
        "unmapped provider exception %s normalized to %s; add it to the §5.4 map",
        type(exc).__name__,
        ErrorClass.SERVER_ERROR.value,
    )
    return ErrorClass.SERVER_ERROR


# --------------------------------------------------------------------------
# Stage 2 — per-provider refinement
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Refinement:
    """One message rule: what it yields, what it matches, and why it exists."""

    to: ErrorClass
    pattern: re.Pattern[str]
    why: str


# The gate that makes this layer safe. A rule may replace SERVER_ERROR and
# nothing else — see `_refine`.
_REFINABLE: Final[frozenset[ErrorClass]] = frozenset({ErrorClass.SERVER_ERROR})

_COHERE_AUTH: Final = _Refinement(
    to=ErrorClass.AUTH_FAILURE,
    # Each alternative asserts something about the *validity of the key*, rather
    # than merely mentioning one. A bare "api key" would match a BadRequestError
    # complaining that `api_key` is a required parameter, and would match any 500
    # whose body echoed the request. Two of the three are the exact literals
    # LiteLLM's own `_map_cohere_exception` treats as its auth signal, so we
    # share fate with upstream rather than guessing separately.
    pattern=re.compile(
        r"incorrect api key provided|invalid api token|no api key provided",
        re.IGNORECASE,
    ),
    why=(
        "litellm's _map_cohere_exception has no 401 branch: a 401 satisfies its "
        "hasattr(status_code) guard, matches no inner arm, and falls through to "
        "the generic APIConnectionError. A wrong key therefore arrives classed "
        "SERVER_ERROR, which D7 counts toward the breaker, so the gateway would "
        "trip a breaker on a provider that is perfectly healthy."
    ),
)

_COHERE_RATE_LIMIT: Final = _Refinement(
    to=ErrorClass.RATE_LIMIT,
    pattern=re.compile(r"you are being rate limited|rate limit exceeded", re.IGNORECASE),
    why=(
        "the same missing-branch bug as the auth rule: that status arm has no 429 "
        "either, so Cohere throttling also arrives as APIConnectionError. Both "
        "classes count toward the breaker (D7), so a misfire here changes a "
        "dashboard label rather than a circuit decision."
    ),
)

# No status gate on any rule. The observed status on the wrapping
# APIConnectionError is 500, not the provider's real 401 or 429 — a LiteLLM
# synthetic — so gating on status would break the exact case these rules exist
# to fix.
#
# `AdapterName.MOCK` is deliberately absent. Mock failures are synthesized from
# a chosen ErrorClass (ADR 0002), so reinterpreting their text would make the M2
# load run exercise this table instead of the health window.
#
# Azure and Bedrock have no rules yet, only fixtures: their entries land in
# Phase 4 with the adapters, against errors captured rather than guessed.
_REFINEMENTS: Final[Mapping[AdapterName, tuple[_Refinement, ...]]] = {
    AdapterName.COHERE: (_COHERE_AUTH, _COHERE_RATE_LIMIT),
}


def matching_refinement(
    error_class: ErrorClass, exc: Exception, provider: AdapterName
) -> _Refinement | None:
    """The rule that would fire, or ``None``. Public so tests can measure coverage.

    A rule with no fixture exercising it is a rule nobody can show still works,
    so ``tests/test_provider_normalize.py`` asserts the corpus reaches every one.
    """
    if error_class not in _REFINABLE:
        return None
    for rule in _REFINEMENTS.get(provider, ()):
        if rule.pattern.search(str(exc)):
            return rule
    return None


def _refine(error_class: ErrorClass, exc: Exception, provider: AdapterName) -> ErrorClass:
    """Upgrade a ``SERVER_ERROR`` when the provider's own text says more.

    **The invariant: refinement may only replace ``SERVER_ERROR``.** It can never
    override a specifically-typed classification. That confines message sniffing
    to the single case that motivated it — a connection-level wrapper that
    swallowed a real provider error — and reduces the whole layer's blast radius
    to one property a test can assert, rather than a judgement call per rule.

    It also fixes the direction of failure. Because a rule can only *add*
    information, a LiteLLM release that rewords a message makes the rule stop
    firing and lands back on today's ``SERVER_ERROR``: a regression to the
    status quo, never a crash and never a new misclassification.
    """
    rule = matching_refinement(error_class, exc, provider)
    if rule is None:
        return error_class

    _LOGGER.debug(
        "refined %s to %s for provider %s: %s",
        error_class.value,
        rule.to.value,
        provider.value,
        rule.why,
    )
    return rule.to


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------


def _status_code(exc: Exception) -> int | None:
    """The provider's HTTP status, when the exception carried a plausible one."""
    raw = getattr(exc, "status_code", None)
    # bool is an int subclass, and a value outside the HTTP range is a library
    # sentinel rather than a real response.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 100 <= raw <= 599 else None


def normalize_provider_error(
    exc: Exception, *, provider: AdapterName | None = None
) -> NormalizedError:
    """Map one provider failure onto the §5.4 taxonomy.

    ``provider`` selects the refinement rules; ``None`` means the type table
    alone. Never raises: an adapter that failed to call a provider must not then
    fail to describe the failure.

    ``status_code`` is reported as the exception carried it and is **not**
    corrected by refinement — a refined bad-key error keeps LiteLLM's synthetic
    ``500``. ADR 0006 already establishes that this field drives nothing: the
    client's HTTP status is looked up from the ``ErrorClass``. Recovering the
    provider's real status would mean parsing LiteLLM's message format, which is
    the churn surface this module exists to keep small.
    """
    error_class = _classify_by_type(exc)
    if provider is not None:
        error_class = _refine(error_class, exc, provider)

    return NormalizedError(
        error_class=error_class,
        # str() on some LiteLLM exceptions is empty. The type name is a poor
        # message, but a better one than nothing at all in a log line.
        message=str(exc) or type(exc).__name__,
        # The raw exception, never the refined class. One log line should show
        # both what arrived and what Keel decided to call it.
        provider_error_type=type(exc).__name__,
        status_code=_status_code(exc),
    )
