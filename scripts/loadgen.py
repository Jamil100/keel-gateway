"""Drive tagged traffic through the gateway at a set rate, for the M2 dashboard.

This is what makes the M2 exit criterion demonstrable: it sends load through
`mock_chaos`, retunes the mock mid-flight, and gives the four Grafana panels
something to move for.

    docker compose up -d
    python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.0
    python scripts/loadgen.py --rps 20 --duration 120 --error-rate 0.4 --latency-ms 3000

**Why a hand-rolled driver rather than Locust or k6.** TECHNICAL-DESIGN §7 names
those, and they would be the right answer for a load *test*. This is not one — it
is a demo fixture. It has to set `X-Keel-*` metadata on every request, drive the
chaos endpoint between phases, and run from `python scripts/...` with no extra
install inside the S8 five-minute budget. `httpx` is already a dependency; Locust
is not, and adding it would cost more S8 seconds than the driver costs lines.

**It targets the mock, and that is a spend decision.** Every preference list in
`config/keel.yaml` starts with `cohere_primary`, and Phase 1's executor invokes
candidate 1 and stops — so pointed at that config, a 20 rps × 120 s run is 2400
live Cohere calls against an NFR-3 budget of EUR 75 total. The compose stack
points `KEEL_CONFIG_PATH` at `deploy/keel.demo.yaml`, which is mock-only, and
`--provider` names the mock for the chaos calls. Running this against a
Cohere-preferring gateway spends real money; `--no-chaos` is the flag that lets
you do it deliberately.

**Tenant and feature are fixed strings, not generated.** ADR 0009 caps
client-supplied metric labels at 64 distinct values and folds the rest into
`other`. A driver that invented a tenant per request would fill the demo board
with one meaningless series and teach a reviewer the wrong thing about the
gateway.

Nothing here is imported by the gateway and nothing in CI runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

_DEFAULT_URL: Final = "http://localhost:8080"
_DEFAULT_PROVIDER: Final = "mock_chaos"
_DEFAULT_CLASS: Final = "interactive_chat"
_DEFAULT_TENANT: Final = "acme"
_DEFAULT_FEATURE: Final = "support-summary"

_ENDPOINT: Final = "/v1/chat/completions"

# Generous, and deliberately not derived from --latency-ms. The gateway may be
# slower than the mock's median (a Redis that is down adds ~220 ms per request,
# ADR 0008), and a client timeout that fired would be recorded here as a failure
# the gateway never caused.
_TIMEOUT_MARGIN_SECONDS: Final = 30.0

_PROMPT: Final = "Summarise this support ticket in one sentence."


@dataclass(slots=True)
class Outcome:
    """What one run produced. Client-side only — the dashboard is the real record."""

    statuses: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0

    @property
    def sent(self) -> int:
        return sum(self.statuses.values())

    def percentile(self, fraction: float) -> float | None:
        """Nearest rank, matching `keel/health/latency.py`.

        The same choice and the same reason: every number reported is a latency
        some request actually saw, rather than an interpolation between two that
        nobody did.
        """
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        index = max(0, min(len(ordered) - 1, round(fraction * len(ordered) + 0.5) - 1))
        return ordered[index]


def _headers(*, tenant: str, feature: str, request_class: str) -> dict[str, str]:
    """The §5.1 metadata. A missing one is a 400 listing every field it wanted."""
    return {
        "X-Keel-Tenant": tenant,
        "X-Keel-Feature": feature,
        # Fresh per request: this is the correlation key FR-7.3 threads through
        # every log line, so reusing one would make the logs unreadable at 20 rps.
        "X-Keel-Request-Id": str(uuid.uuid4()),
        "X-Keel-Class": request_class,
        "Content-Type": "application/json",
    }


async def _apply_chaos(
    client: httpx.AsyncClient,
    *,
    provider: str,
    error_rate: float | None,
    latency_ms: float | None,
    latency_sigma: float | None,
    seed: int | None,
) -> None:
    """Retune the mock before the run (ADR 0010).

    A 404 here almost always means `KEEL_CHAOS_ENABLED` is unset, which is the
    default — so the message says that rather than printing a bare status.
    """
    body: dict[str, Any] = {
        name: value
        for name, value in (
            ("error_rate", error_rate),
            ("latency_ms", latency_ms),
            ("latency_sigma", latency_sigma),
            ("seed", seed),
        )
        if value is not None
    }
    if not body:
        return

    response = await client.post(f"/chaos/{provider}", json=body)
    if response.status_code == 404:
        raise SystemExit(
            f"chaos endpoint not found for provider {provider!r}. The gateway registers it "
            f"only when KEEL_CHAOS_ENABLED is set (ADR 0010) — docker-compose.yml sets it. "
            f"Use --no-chaos to drive load without retuning the mock."
        )
    if response.status_code != 200:
        raise SystemExit(f"chaos call failed: {response.status_code} {response.text}")

    print(f"chaos: {provider} <- {body}")


async def _one_request(
    client: httpx.AsyncClient, outcome: Outcome, *, headers: dict[str, str]
) -> None:
    """Send one request and record what came back. Never raises.

    A transport failure is recorded as an outcome rather than propagated: one
    refused connection must not end a 120-second run, and "the gateway stopped
    answering" is itself a result worth seeing in the summary.
    """
    started = time.perf_counter()
    try:
        response = await client.post(
            _ENDPOINT,
            headers=headers,
            json={"model": "keel", "messages": [{"role": "user", "content": _PROMPT}]},
        )
    except httpx.HTTPError as exc:
        outcome.statuses[type(exc).__name__] += 1
        return
    outcome.latencies_ms.append((time.perf_counter() - started) * 1000.0)
    outcome.statuses[str(response.status_code)] += 1


async def _drive(
    client: httpx.AsyncClient,
    *,
    rps: float,
    duration: float,
    headers_for: Callable[[], dict[str, str]],
) -> Outcome:
    """Fire requests on a wall-clock schedule, not one after another.

    A sequential loop would cap throughput at `1 / latency` — about 0.33 rps
    against the M2 run's 3-second mock — so the requested rate would be silently
    unreachable and the dashboard would show a flat trickle. Requests are spawned
    on a schedule instead and awaited at the end, which means roughly
    `rps × latency` are in flight at once (60 for the M2 profile).

    The schedule is absolute rather than a `sleep(1/rps)` accumulation, so a slow
    spawn does not push every later request further behind.
    """
    outcome = Outcome()
    tasks: list[asyncio.Task[None]] = []
    interval = 1.0 / rps

    outcome.started = time.perf_counter()
    deadline = outcome.started + duration
    index = 0

    while True:
        target = outcome.started + index * interval
        if target >= deadline:
            break
        now = time.perf_counter()
        if target > now:
            await asyncio.sleep(target - now)
        tasks.append(asyncio.create_task(_one_request(client, outcome, headers=headers_for())))
        index += 1

    if tasks:
        await asyncio.gather(*tasks)
    outcome.finished = time.perf_counter()
    return outcome


def _report(outcome: Outcome, *, rps: float) -> None:
    elapsed = outcome.finished - outcome.started
    achieved = outcome.sent / elapsed if elapsed > 0 else 0.0

    print()
    print(f"sent            {outcome.sent} in {elapsed:.1f}s")
    print(f"requested rate  {rps:.1f}/s")
    print(f"achieved rate   {achieved:.1f}/s")

    for status, count in sorted(outcome.statuses.items()):
        share = 100.0 * count / outcome.sent if outcome.sent else 0.0
        print(f"  {status:<18} {count:>6}  {share:5.1f}%")

    if outcome.latencies_ms:
        print(
            f"client latency  p50 {outcome.percentile(0.50):.0f} ms  "
            f"p95 {outcome.percentile(0.95):.0f} ms  "
            f"p99 {outcome.percentile(0.99):.0f} ms  "
            f"mean {statistics.fmean(outcome.latencies_ms):.0f} ms"
        )
    print()
    print("These are client-side numbers. The authoritative ones are the Prometheus")
    print("histograms behind the Grafana board (TECHNICAL-DESIGN.md §6).")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rps", type=float, default=20.0, help="requests per second")
    parser.add_argument("--duration", type=float, default=120.0, help="seconds to run")
    parser.add_argument("--error-rate", type=float, default=None, help="mock failure fraction")
    parser.add_argument("--latency-ms", type=float, default=None, help="mock median latency")
    parser.add_argument("--latency-sigma", type=float, default=None, help="lognormal shape")
    parser.add_argument("--seed", type=int, default=None, help="restart the mock's RNG streams")
    parser.add_argument("--url", default=_DEFAULT_URL)
    parser.add_argument("--provider", default=_DEFAULT_PROVIDER, help="which mock to retune")
    parser.add_argument("--class", dest="request_class", default=_DEFAULT_CLASS)
    parser.add_argument("--tenant", default=_DEFAULT_TENANT)
    parser.add_argument("--feature", default=_DEFAULT_FEATURE)
    parser.add_argument(
        "--no-chaos",
        action="store_true",
        help="skip the chaos call; required when driving a gateway without it enabled",
    )
    args = parser.parse_args()

    if args.rps <= 0 or args.duration <= 0:
        print("--rps and --duration must both be positive.", file=sys.stderr)
        return 1

    timeout = httpx.Timeout(_TIMEOUT_MARGIN_SECONDS + (args.latency_ms or 0.0) / 1000.0)
    # The connection pool has to hold everything in flight, or requests queue on
    # a connection instead of on the gateway and the achieved rate collapses to
    # the pool size without saying why.
    in_flight = max(10, int(args.rps * 5))
    limits = httpx.Limits(max_connections=in_flight, max_keepalive_connections=in_flight)

    async with httpx.AsyncClient(base_url=args.url, timeout=timeout, limits=limits) as client:
        if not args.no_chaos:
            await _apply_chaos(
                client,
                provider=args.provider,
                error_rate=args.error_rate,
                latency_ms=args.latency_ms,
                latency_sigma=args.latency_sigma,
                seed=args.seed,
            )

        print(f"driving {args.url} at {args.rps}/s for {args.duration}s ...")

        def headers_for() -> dict[str, str]:
            return _headers(
                tenant=args.tenant,
                feature=args.feature,
                request_class=args.request_class,
            )

        outcome = await _drive(
            client, rps=args.rps, duration=args.duration, headers_for=headers_for
        )

    _report(outcome, rps=args.rps)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
