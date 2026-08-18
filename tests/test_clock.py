"""Tests for the injectable clock (NFR-2, TECHNICAL-DESIGN.md §7).

The claim this module has to earn is that a component can sleep for a breaker
cooldown while the test finishes instantly. The determinism tests below assert
that directly, in wall-clock terms, rather than trusting it.

No network, no Redis. The one place real time is allowed is the SystemClock
test, where real time is the subject.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from keel.clock import Clock, ManualClock, SystemClock

# --------------------------------------------------------------------------
# SystemClock
# --------------------------------------------------------------------------


def test_system_clock_reads_wall_clock_epoch() -> None:
    """Epoch seconds, not a monotonic counter — bucket keys are shared state."""
    before = time.time()
    reading = SystemClock().now()
    after = time.time()

    assert before <= reading <= after


async def test_system_clock_sleep_actually_waits() -> None:
    clock = SystemClock()

    start = time.perf_counter()
    await clock.sleep(0.01)
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.005


# --------------------------------------------------------------------------
# ManualClock
# --------------------------------------------------------------------------


def test_manual_clock_starts_where_told_and_stays_there() -> None:
    clock = ManualClock(start=1_000.0)

    assert clock.now() == 1_000.0
    assert clock.now() == 1_000.0  # Idle time does not pass on its own.


def test_manual_clock_advance_moves_time() -> None:
    clock = ManualClock()

    clock.advance(5.0)
    clock.advance(2.5)

    assert clock.now() == 7.5


async def test_manual_clock_sleep_advances_by_its_full_duration() -> None:
    clock = ManualClock()

    await clock.sleep(30.0)

    assert clock.now() == 30.0


async def test_manual_clock_sleep_does_not_consume_real_time() -> None:
    """The load-bearing property: a 1h cooldown must cost a test microseconds."""
    clock = ManualClock()

    start = time.perf_counter()
    await clock.sleep(3_600.0)
    elapsed = time.perf_counter() - start

    assert clock.now() == 3_600.0
    assert elapsed < 0.5


async def test_manual_clock_sleep_yields_to_other_tasks() -> None:
    """Without a yield, hedging and probe-admission tests could never interleave."""
    clock = ManualClock()
    ran: list[str] = []

    async def other() -> None:
        ran.append("other")

    task = asyncio.create_task(other())
    await clock.sleep(1.0)

    assert ran == ["other"], "sleep() returned without letting the loop run"
    await task


async def test_manual_clock_mixes_sleeps_and_advances() -> None:
    clock = ManualClock(start=100.0)

    await clock.sleep(10.0)
    clock.advance(5.0)
    await clock.sleep(0.0)

    assert clock.now() == 115.0


# --------------------------------------------------------------------------
# Negative durations. Same rejection from both implementations, or a test that
# passes against ManualClock would behave differently in production.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clock", [SystemClock(), ManualClock()], ids=["system", "manual"])
async def test_sleep_rejects_negative_duration(clock: Clock) -> None:
    with pytest.raises(ValueError, match="does not run backwards"):
        await clock.sleep(-0.5)


def test_advance_rejects_negative_duration() -> None:
    clock = ManualClock(start=50.0)

    with pytest.raises(ValueError, match="does not run backwards"):
        clock.advance(-1.0)

    assert clock.now() == 50.0, "a rejected advance must not have moved time"


def test_negative_duration_message_names_the_fix() -> None:
    """The message has to be actionable at 3am, like the config errors."""
    with pytest.raises(ValueError) as caught:
        ManualClock().advance(-2.0)

    message = str(caught.value)
    assert "ManualClock.advance" in message
    assert "max(0.0, deadline - clock.now())" in message
