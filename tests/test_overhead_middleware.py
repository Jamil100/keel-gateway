"""Tests for the gateway-overhead middleware (P2-T4, S5, §6).

This is the metric §6 says exists "specifically so S5 can be measured rather than
asserted", and it is computed by subtracting one clock from another:

    overhead = perf_counter wall clock - ProviderResult.latency_ms

Those two clocks are deliberately different — ADR 0001 wants the histograms timing
independently of the injected `Clock`, and a metric timed off a `ManualClock` would
read exactly zero in every test and pass. The cost of that choice is that the
subtraction **can go negative**, and hard: `MockAdapter` at `latency_ms=3000`
advances a `ManualClock` three seconds while about a millisecond of real time
passes, giving -2.999 s.

That is the case worth testing above all others, because `prometheus_client`
accepts a negative observation *silently* — it counts it in every bucket and adds
it to `_sum` — so a single such request corrupts the S5 histogram for the life of
the process, with nothing raised and nothing logged.

Driven through a real ASGI app rather than by calling the middleware directly, so
the `scope["state"]` handoff between the route and the middleware is exercised
rather than assumed. No Redis, no network (NFR-2).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, Response

from keel.observability.metrics import MetricsCatalogue
from keel.observability.middleware import (
    PROVIDER_SECONDS_ATTR,
    REQUEST_CLASS_ATTR,
    OverheadMiddleware,
)

CLASS = "interactive_chat"


def build(
    *, provider_seconds: float | None, request_class: str | None = CLASS
) -> tuple[TestClient, MetricsCatalogue]:
    """A minimal app whose one route stashes what the real ingress stashes.

    Deliberately not the real `create_app`: this isolates the middleware's
    arithmetic from routing, Redis, and adapters, so a failure here is unambiguous.
    `tests/test_app.py` covers the wiring end to end.
    """
    metrics = MetricsCatalogue()
    app = FastAPI()
    app.add_middleware(OverheadMiddleware, metrics=metrics)

    @app.get("/served")
    async def served(request: Request) -> Response:
        if request_class is not None:
            setattr(request.state, REQUEST_CLASS_ATTR, request_class)
        if provider_seconds is not None:
            setattr(request.state, PROVIDER_SECONDS_ATTR, provider_seconds)
        return JSONResponse({"ok": True})

    return TestClient(app), metrics


def samples(metrics: MetricsCatalogue) -> dict[str, Any]:
    """The overhead histogram's `_count` and `_sum`, or empty if it has no series."""
    for metric in metrics.registry.collect():
        if metric.name != "keel_gateway_overhead_seconds":
            continue
        return {
            sample.name: sample.value
            for sample in metric.samples
            if sample.name.endswith(("_count", "_sum"))
        }
    return {}  # pragma: no cover - the metric is always declared


# --------------------------------------------------------------------------
# The clamp
# --------------------------------------------------------------------------


def test_provider_time_longer_than_the_request_records_zero_not_a_negative() -> None:
    """The `ManualClock` case, and the one that would silently corrupt the histogram.

    A mock that advances injected time by three seconds inside a request that took
    a millisecond of real time yields an overhead of about -2.999 s. Prometheus
    accepts that without complaint and `_sum` goes permanently wrong, so every S5
    reading afterwards is a lie. Clamped to zero instead.
    """
    client, metrics = build(provider_seconds=3.0)

    assert client.get("/served").status_code == 200

    observed = samples(metrics)
    assert observed["keel_gateway_overhead_seconds_count"] == 1.0
    assert observed["keel_gateway_overhead_seconds_sum"] == 0.0


def test_the_histogram_sum_never_goes_negative_across_many_such_requests() -> None:
    """Asserted as an invariant, not a single case: `_sum` is what S5 is read from."""
    client, metrics = build(provider_seconds=5.0)

    for _ in range(10):
        client.get("/served")

    observed = samples(metrics)
    assert observed["keel_gateway_overhead_seconds_count"] == 10.0
    assert observed["keel_gateway_overhead_seconds_sum"] >= 0.0


def test_a_request_with_no_provider_time_records_a_real_positive_overhead() -> None:
    """With provider time at zero the whole request is overhead, which is the truth.

    Also the sanity check that the middleware is measuring anything at all — a
    clamp that always fired would pass every test above.
    """
    client, metrics = build(provider_seconds=0.0)

    client.get("/served")

    observed = samples(metrics)
    assert observed["keel_gateway_overhead_seconds_count"] == 1.0
    assert observed["keel_gateway_overhead_seconds_sum"] > 0.0


# --------------------------------------------------------------------------
# What is not measured
# --------------------------------------------------------------------------


def test_a_request_that_never_reached_a_provider_is_not_measured() -> None:
    """An envelope 400 is all gateway time, and counting it would corrupt S5.

    S5 is a claim about requests the gateway served. Including rejected ones would
    let a client drag the figure wherever it liked by sending malformed requests —
    the metric would measure the caller rather than the gateway.
    """
    client, metrics = build(provider_seconds=None, request_class=None)

    client.get("/served")

    assert samples(metrics) == {}, "no series at all, rather than a zero"


def test_a_half_filled_handoff_is_not_measured() -> None:
    """Both halves or neither. One without the other is a wiring bug, not a datum.

    Guards against a future edit that sets the class but forgets the provider time
    (or the reverse) and silently starts reporting whole request durations as
    gateway overhead.
    """
    client, metrics = build(provider_seconds=None, request_class=CLASS)

    client.get("/served")

    assert samples(metrics) == {}


def test_an_unknown_path_is_not_measured() -> None:
    """A 404 never reaches a route, so it never stashes and is never counted."""
    client, metrics = build(provider_seconds=0.0)

    assert client.get("/nope").status_code == 404
    assert samples(metrics) == {}


# --------------------------------------------------------------------------
# Labelling
# --------------------------------------------------------------------------


def test_the_overhead_is_labelled_by_request_class() -> None:
    """§6 gives this metric `class` and nothing else — notably no `provider`.

    That is a real constraint on P2-T6: a per-provider overhead panel is not
    possible as specified, and this pins the label set so the dashboard is built
    against what exists.
    """
    client, metrics = build(provider_seconds=0.0, request_class="classification")

    client.get("/served")

    body, _ = metrics.render()
    assert 'keel_gateway_overhead_seconds_count{class="classification"}' in body.decode("utf-8")


def test_a_failing_route_is_still_measured() -> None:
    """Recorded in a `finally`: a request that 500s consumed gateway time like any other.

    Dropping it would flatter the S5 figure at exactly the moment the gateway is
    misbehaving, which is when the number matters most.
    """
    metrics = MetricsCatalogue()
    app = FastAPI()
    app.add_middleware(OverheadMiddleware, metrics=metrics)

    @app.get("/boom")
    async def boom(request: Request) -> Response:
        setattr(request.state, REQUEST_CLASS_ATTR, CLASS)
        setattr(request.state, PROVIDER_SECONDS_ATTR, 0.0)
        raise RuntimeError("gateway bug")

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/boom").status_code == 500

    assert samples(metrics)["keel_gateway_overhead_seconds_count"] == 1.0
