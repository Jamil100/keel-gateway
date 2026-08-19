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

**Error mapping now lives in** :mod:`keel.providers.normalize` (P2-T1,
**ADR 0007**), shared with every other LiteLLM-backed adapter and replayed
offline against captured fixtures (§7). :func:`normalize_litellm_error` remains
the public entry point those fixtures use; all it adds is the Cohere identity
that selects the per-provider refinement rules.

**LiteLLM is imported lazily, and that is measured rather than assumed.**
``import litellm`` costs about 4.5 seconds. At module scope that lands on every
test collection and on the S8 five-minute cold start, for a dependency that the
mock-only path — which is the whole M2 load run — never touches.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel

from keel.api.envelope import RequestEnvelope
from keel.clock import Clock
from keel.config import AdapterName
from keel.providers.base import ProviderAdapter, ProviderResult
from keel.providers.errors import NormalizedError
from keel.providers.normalize import normalize_provider_error

__all__ = ["CohereAdapter", "CompletionFn", "normalize_litellm_error"]

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
# Error mapping
# --------------------------------------------------------------------------


def normalize_litellm_error(exc: Exception) -> NormalizedError:
    """Map one Cohere failure onto the §5.4 taxonomy.

    A thin delegate: the table itself lives in :mod:`keel.providers.normalize`,
    shared with every other LiteLLM-backed adapter (**ADR 0007**). What this
    function adds is the one thing the shared module cannot know — that failures
    arriving here came from Cohere, which selects the per-provider refinement
    rules that rescue a bad API key from being reported as a server error.

    Kept as a named, public entry point because it is the seam the captured
    fixtures replay against and the name the adapter and its tests already use.
    Never raises: an adapter that failed to call a provider must not then fail to
    describe the failure.
    """
    return normalize_provider_error(exc, provider=AdapterName.COHERE)


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
