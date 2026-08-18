"""Time as an injected dependency.

Every time-dependent behaviour in Keel — health window rolling, breaker cooldown
expiry, hedge triggers, retry backoff — reads time through this protocol rather
than calling :mod:`time` directly. That is what lets the test suite advance time
explicitly instead of waiting for it, so the breaker state machine is exercised
in milliseconds and deterministically (NFR-2, TECHNICAL-DESIGN.md §7).

``now()`` returns wall-clock epoch seconds rather than a monotonic counter, and
that is a deliberate trade. Health bucket keys
(``keel:health:{provider}:{bucket_epoch}``, §5.5) and breaker ``opened_at``
timestamps live in Redis and are read by two processes — the gateway and the
deferred worker (§3). A monotonic counter is only comparable within the process
that started it, so it cannot key state that two processes share.

The cost of that choice is that an NTP step moves the clock underneath a
window. The exposure is bounded: these timestamps are compared against windows
measured in tens of seconds and are recomputed continuously rather than
accumulated, so a step perturbs one window rather than corrupting a running
total.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol

__all__ = ["Clock", "ManualClock", "SystemClock"]


def _reject_negative(seconds: float, method: str) -> None:
    """Guard both implementations identically.

    A negative duration almost always means an expired deadline was not
    clamped — ``deadline - clock.now()`` after the deadline passed. Raising
    surfaces that at its source. ``asyncio.sleep`` would silently treat it as
    zero, and a ``ManualClock`` would have to run time backwards to honour it.
    """
    if seconds < 0:
        raise ValueError(
            f"{method}() received {seconds} seconds; time does not run backwards. "
            f"Clamp at the call site: max(0.0, deadline - clock.now())."
        )


class Clock(Protocol):
    """The time interface every component depends on. Never import ``time``."""

    def now(self) -> float:
        """Wall-clock epoch seconds. Safe to persist and compare across processes."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds``. Raises ``ValueError`` if it is negative."""
        ...


class SystemClock:
    """Real time. The only implementation that reaches production."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        _reject_negative(seconds, "SystemClock.sleep")
        await asyncio.sleep(seconds)


class ManualClock:
    """Time that only moves when the test moves it.

    ``sleep()`` advances the clock by its full duration and returns immediately
    rather than waiting. A component under test that sleeps for a 30 second
    breaker cooldown therefore observes 30 seconds having passed, while the test
    finishes in microseconds — which is the entire point of the abstraction.

    Because ``sleep()`` moves time as a side effect, a test drives the clock two
    ways: implicitly, by letting the code under test sleep, and explicitly, via
    :meth:`advance` for time that passes while nothing is waiting.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move time forward without anything having awaited it."""
        _reject_negative(seconds, "ManualClock.advance")
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        _reject_negative(seconds, "ManualClock.sleep")
        self._now += seconds
        # Yield to the loop so concurrently scheduled tasks resume and observe
        # the new time. Without this a manual sleep is a bare assignment, and
        # anything testing concurrent behaviour — hedging, half-open probe
        # admission — would never interleave.
        await asyncio.sleep(0)


if TYPE_CHECKING:
    # Static conformance: fails `mypy --strict` if either implementation drifts
    # from the protocol. Cheaper than discovering the drift at a call site.
    _system_clock_is_a_clock: Clock = SystemClock()
    _manual_clock_is_a_clock: Clock = ManualClock()
