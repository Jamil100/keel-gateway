"""The Cohere adapter: one provider call, made through LiteLLM (FR-2.1, D4).

Cohere is the primary provider and everything else is a fallback (PRD §1), so
this is the adapter the gateway is built around. It **wraps** LiteLLM rather
than being replaced by it (D4): the library handles request and response shape
translation, and this module owns the three things the library cannot own for
us — the normalized error taxonomy (§5.4), the injected clock (ADR 0001), and
the ``ProviderResult`` contract that a failure is a *return value, not an
exception*.

Like every adapter, this one knows nothing about Redis, health, or breakers.
Recording the outcome belongs to the executor and only the executor (D-C).

**Error mapping here is best-effort.** P2-T1 replaces it with captured fixtures
replayed offline (§7). :func:`normalize_litellm_error` is public precisely so
those fixtures have a stable entry point to replay against, and the unmapped
default already has the shape P2-T1 commits to: ``SERVER_ERROR`` plus a warning
naming the exception type, so a mapping gap surfaces rather than hiding inside
a catch-all.

**LiteLLM is imported lazily, and that is measured rather than assumed.**
``import litellm`` costs about 4.5 seconds. At module scope that lands on every
test collection and on the S8 five-minute cold start, for a dependency that the
mock-only path — which is the whole M2 load run — never touches.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel

from keel.api.envelope import RequestEnvelope
from keel.clock import Clock
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.errors import ErrorClass, NormalizedError

__all__ = ["CohereAdapter", "CompletionFn", "normalize_litellm_error"]

_LOGGER: Final = logging.getLogger(__name__)

# LiteLLM addresses a provider by prefixing the model. In litellm 1.97 both
# `cohere/` and `cohere_chat/` resolve to CohereV2ChatConfig — the v2 chat API —
# so the plain prefix is correct and matches AdapterName.COHERE's value.
_LITELLM_PREFIX: Final = "cohere"

CompletionFn = Callable[..., Awaitable[Any]]
"""The shape of ``litellm.acompletion``.

Injected rather than reached for, so an offline test substitutes a fake without
monkeypatching a module global — the same posture as :class:`~keel.clock.Clock`.
"""


async def _default_acompletion(**kwargs: Any) -> Any:
    """Call the real LiteLLM, importing it on first use rather than at import."""
    import litellm

    return await litellm.acompletion(**kwargs)


# --------------------------------------------------------------------------
# Error mapping (best-effort; P2-T1 replaces this with captured fixtures)
# --------------------------------------------------------------------------


@cache
def _error_map() -> tuple[tuple[type[BaseException], ErrorClass], ...]:
    """The §5.4 mapping, **ordered most-specific-first and matched by isinstance**.

    Two facts about LiteLLM's exceptions shape this table, both verified against
    litellm 1.97.0 / openai 2.54.0 rather than assumed:

    **The root of the tree is ``openai.APIError``, not ``litellm.APIError``.**
    LiteLLM defines exceptions that subclass the OpenAI SDK's same-named ones,
    and its own ``APIError`` is a *sibling* of the rest rather than their
    ancestor — ``isinstance(litellm.RateLimitError(...), litellm.APIError)`` is
    ``False``. A table that used ``litellm.APIError`` as its catch-all would
    therefore catch almost nothing, and every real provider failure would fall
    through to the unmapped default. So the catch-all rows name the OpenAI
    classes, and the LiteLLM rows above them exist only where LiteLLM adds a
    distinction the SDK does not have.

    **Order is load-bearing.** Every row below sits above at least one ancestor
    that would otherwise swallow it:

    * ``Timeout`` subclasses ``openai.APITimeoutError`` → ``APIConnectionError``
      → ``APIError``. Below any of them a timeout normalizes to
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
        # --- the true root, so necessarily last ---
        (openai.APIError, ErrorClass.SERVER_ERROR),
    )


def _classify(exc: Exception) -> ErrorClass:
    for exception_type, error_class in _error_map():
        if isinstance(exc, exception_type):
            return error_class

    # The P2-T1 contract: default to SERVER_ERROR, but name what was caught, so
    # a mapping gap is one grep away rather than invisible.
    _LOGGER.warning(
        "unmapped provider exception %s normalized to %s; add it to the §5.4 map",
        type(exc).__name__,
        ErrorClass.SERVER_ERROR.value,
    )
    return ErrorClass.SERVER_ERROR


def _status_code(exc: Exception) -> int | None:
    """The provider's HTTP status, when the exception carried a plausible one."""
    raw = getattr(exc, "status_code", None)
    # bool is an int subclass, and a value outside the HTTP range is a library
    # sentinel rather than a real response.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 100 <= raw <= 599 else None


def normalize_litellm_error(exc: Exception) -> NormalizedError:
    """Map one LiteLLM failure onto the §5.4 taxonomy.

    Public because P2-T1's captured fixtures replay against exactly this
    function. Never raises: an adapter that failed to call a provider must not
    then fail to describe the failure.
    """
    return NormalizedError(
        error_class=_classify(exc),
        # str() on some LiteLLM exceptions is empty. The type name is a poor
        # message, but a better one than nothing at all in a log line.
        message=str(exc) or type(exc).__name__,
        provider_error_type=type(exc).__name__,
        status_code=_status_code(exc),
    )


# --------------------------------------------------------------------------
# Response extraction
# --------------------------------------------------------------------------


def _as_openai_dict(raw: Any) -> dict[str, Any]:
    """Narrow LiteLLM's return value to a plain OpenAI-shaped mapping.

    Narrowed explicitly rather than trusted. ``acompletion`` is typed loosely
    enough to return a stream wrapper, and a wrapper reaching the executor would
    fail far from its cause.
    """
    if isinstance(raw, BaseModel):
        return raw.model_dump()
    if isinstance(raw, Mapping):
        return dict(raw)
    raise TypeError(
        f"provider returned {type(raw).__name__}, which is not an OpenAI-shaped "
        f"completion; streaming is deferred to v1.1 (D8)"
    )


def _token_count(value: Any) -> int | None:
    """One usage figure, or ``None`` when the provider reported no usable one.

    Never coerces to ``0``. ``ProviderResult`` is explicit that absent usage is
    *unknown*, or a provider that omits it silently becomes the cheapest one in
    the Phase 4 cost report.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _token_counts(response: Mapping[str, Any]) -> tuple[int | None, int | None]:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    return _token_count(usage.get("prompt_tokens")), _token_count(usage.get("completion_tokens"))


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class CohereAdapter:
    """A ``ProviderAdapter`` that calls Cohere through LiteLLM."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str,
        clock: Clock,
        capabilities: frozenset[str] = frozenset(),
        timeout_ms: int | None = None,
        acompletion: CompletionFn | None = None,
    ) -> None:
        self.name = name
        """The provider's **config key** (``cohere_primary``), not the adapter name (``cohere``).

        Several config entries may share one adapter, and metrics, health, and
        the ``X-Keel-Provider`` header all key on the entry.
        """

        self._model = model
        self._api_key = api_key
        self._clock = clock
        self._capabilities = capabilities
        self._timeout_ms = timeout_ms
        self._acompletion = acompletion if acompletion is not None else _default_acompletion

    def capabilities(self) -> frozenset[str]:
        """What config says this provider can do. The adapter never enforces it.

        The §5.7 capability filter is the router's job and it reads config, not
        adapters. An adapter that also filtered would be a second, divergent
        source of the same truth.
        """
        return self._capabilities

    def _build_request(self, envelope: RequestEnvelope) -> dict[str, Any]:
        """The provider-bound payload, with the four things Keel decides overridden."""
        request = dict(envelope.payload)

        # The client's `model` is discarded. Keel picks the target from config —
        # that is the whole premise of the gateway, and honouring a client's
        # choice here would make the preference list advisory.
        request["model"] = f"{_LITELLM_PREFIX}/{self._model}"

        # Passed explicitly rather than left to LiteLLM's own environment
        # lookup, so the registry's startup check and this call read one value.
        request["api_key"] = self._api_key

        # D8: streaming is deferred to v1.1. Forced off rather than trusted,
        # since a `stream: true` slipping through returns an async generator and
        # breaks every layer above. Rejecting it with a 400 is the ingress's job
        # in P1-T7; this is the backstop, not the error message.
        request["stream"] = False

        if self._timeout_ms is not None:
            # Config and operators speak milliseconds; LiteLLM takes seconds.
            # Passed down so the socket actually closes. P1-T6's executor-level
            # timeout sits above this as a backstop, not as a replacement.
            request["timeout"] = self._timeout_ms / 1000.0

        return request

    def _elapsed_ms(self, started: float) -> float:
        # Clamped because the injected clock is wall-clock (ADR 0001): an NTP
        # step mid-call would otherwise produce a negative latency, which
        # ProviderResult rejects — turning a successful call into a crash.
        return max(0.0, (self._clock.now() - started) * 1000.0)

    async def invoke(self, envelope: RequestEnvelope) -> ProviderResult:
        """Call Cohere once, and report what happened either way.

        Every failure is a returned :class:`ProviderResult`, never a raised
        exception — including a malformed success. Failure is the case the
        gateway exists to handle, so it must not unwind past the executor that
        has to record it (D-C).
        """
        started = self._clock.now()
        try:
            raw = await self._acompletion(**self._build_request(envelope))
            response = _as_openai_dict(raw)
        except Exception as exc:
            # `except Exception` deliberately does not catch CancelledError,
            # which in 3.11 derives from BaseException: a cancelled request is
            # the caller giving up, not the provider failing, and recording it
            # as provider health would be a lie.
            return ProviderResult.failure(
                provider=self.name,
                latency_ms=self._elapsed_ms(started),
                error=normalize_litellm_error(exc),
            )

        prompt_tokens, completion_tokens = _token_counts(response)
        return ProviderResult.success(
            provider=self.name,
            response=response,
            latency_ms=self._elapsed_ms(started),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


if TYPE_CHECKING:
    # Static conformance, same idiom as keel/clock.py and keel/providers/mock.py:
    # fails `mypy --strict` if CohereAdapter drifts from the protocol. Phrased as
    # a function because constructing one would need a real Clock.
    def _cohere_is_an_adapter(adapter: CohereAdapter) -> ProviderAdapter:
        return adapter
