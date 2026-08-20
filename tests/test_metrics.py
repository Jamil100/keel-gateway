"""Tests for the Prometheus catalogue (P2-T4, FR-3.3, TECHNICAL-DESIGN.md §6).

Two things are pinned here, and the second is the one that matters most.

**The §6 table, transcribed by hand.** Metric names and label names are wire-visible
API: P2-T6's Grafana panels hard-code them, and a typo does not fail anything at
runtime — it produces an empty panel that looks like "no traffic". So the table is
written out below as literal tuples and checked against what the registry actually
exposes, the same posture `tests/test_provider_errors.py` takes with the §5.4
truth table and `tests/test_health_window.py` with the §5.5 key. The two must
agree, and disagreeing must be loud.

**The label cap (ADR 0009).** `tenant` and `feature` are client-supplied strings
that `keel/api/envelope.py` never bounds, and `prometheus_client` never evicts a
series. Unbounded distinct values are therefore unbounded memory — measured at
706 bytes retained per series — reachable from one header on an endpoint with no
authentication. The cap is asserted as a *bound* rather than spot-checked: ten
thousand distinct values must still leave the series count flat.

No Redis, no network, no app — the catalogue is a plain object over its own
registry (NFR-2).
"""

from __future__ import annotations

import logging

import pytest
from prometheus_client import Counter, Gauge, Histogram

from keel.api.envelope import RequestEnvelope
from keel.observability.metrics import (
    DURATION_BUCKETS_SECONDS,
    EXTENSION_METRIC_NAMES,
    LABEL_CAP,
    OTHER_LABEL,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OVERHEAD_BUCKETS_SECONDS,
    MetricsCatalogue,
)
from keel.providers.base import ProviderResult
from keel.providers.errors import ErrorClass, NormalizedError

# The §6 table, transcribed by hand from docs/TECHNICAL-DESIGN.md rather than read
# back from the module under test. `class` is the table's spelling; §5.10's prose
# says `request_class` for the cost counter and the two disagree — P2-T4's brief
# points at the table, so the table is what is pinned.
SECTION_6_CATALOGUE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("keel_requests_total", "counter", ("tenant", "feature", "class", "provider", "outcome")),
    ("keel_request_duration_seconds", "histogram", ("class", "provider")),
    ("keel_gateway_overhead_seconds", "histogram", ("class",)),
    ("keel_provider_errors_total", "counter", ("provider", "error_class")),
    ("keel_breaker_state", "gauge", ("provider",)),
    ("keel_breaker_transitions_total", "counter", ("provider", "from_state", "to_state")),
    ("keel_failover_events_total", "counter", ("from_provider", "to_provider", "class")),
    ("keel_hedge_attempts_total", "counter", ("class", "winner")),
    ("keel_queue_depth", "gauge", ()),
    ("keel_queue_job_age_seconds", "histogram", ()),
    (
        "keel_cost_micros_total",
        "counter",
        ("tenant", "feature", "class", "provider", "attempt_type"),
    ),
)

PROVIDERS = ("cohere_primary", "mock_chaos")


def catalogue(providers: tuple[str, ...] = PROVIDERS) -> MetricsCatalogue:
    return MetricsCatalogue(providers=providers)


def families(cat: MetricsCatalogue) -> dict[str, tuple[str, tuple[str, ...]]]:
    """What the registry exposes, as `{name: (type, declared_labels)}`.

    Read from `registry.collect()` and the collector objects rather than by parsing
    the rendered text, because a labelled metric that nothing has produced yet has
    **no samples at all** — and half the §6 catalogue is exactly that until Phases
    3 to 5 arrive. Parsing text could therefore only check the metrics that already
    have data, which is the opposite of what this task is for.

    Counter families are named without their `_total` suffix by
    `prometheus_client` (the suffix belongs to the sample), so it is added back to
    compare against §6, which lists the sample name.
    """
    collectors = {
        value._name: value
        for value in vars(cat).values()
        if isinstance(value, Counter | Gauge | Histogram)
    }
    exposed: dict[str, tuple[str, tuple[str, ...]]] = {}
    for metric in cat.registry.collect():
        name = f"{metric.name}_total" if metric.type == "counter" else metric.name
        exposed[name] = (metric.type, tuple(collectors[metric.name]._labelnames))
    return exposed


def envelope(tenant: str = "acme", feature: str = "support-summary") -> RequestEnvelope:
    return RequestEnvelope(
        request_id="req-1",
        tenant=tenant,
        feature=feature,
        request_class="interactive_chat",
        capabilities=frozenset(),
        deferrable=False,
        idempotency_key=None,
        payload={},
        received_at=0.0,
    )


def success(provider: str = "mock_chaos", latency_ms: float = 250.0) -> ProviderResult:
    return ProviderResult.success(
        provider=provider, response={"id": "x"}, latency_ms=latency_ms
    )


def failure(error_class: ErrorClass, provider: str = "mock_chaos") -> ProviderResult:
    return ProviderResult.failure(
        provider=provider,
        latency_ms=40.0,
        error=NormalizedError(error_class=error_class, message="injected"),
    )


# --------------------------------------------------------------------------
# The §6 catalogue
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "metric_type", "labels"),
    SECTION_6_CATALOGUE,
    ids=[row[0] for row in SECTION_6_CATALOGUE],
)
def test_every_section_6_metric_is_exported_with_its_exact_labels(
    name: str, metric_type: str, labels: tuple[str, ...]
) -> None:
    """A typo in a label name fails here rather than emptying a Grafana panel.

    Nothing at runtime notices a mislabelled metric — the gateway serves traffic
    identically — so the only way this is caught before a demo is a test that
    compares against a hand-written copy of the table.
    """
    exported = families(catalogue())

    assert name in exported, f"{name} is in §6 but not exported"
    actual_type, actual_labels = exported[name]
    assert actual_type == metric_type
    assert set(actual_labels) == set(labels)


def test_the_catalogue_exports_section_6_and_declared_extensions_and_nothing_else() -> None:
    """Both directions. A missing row and a stray metric are different bugs.

    The §6 check above would pass if a twelfth metric quietly appeared; this is
    what stops the catalogue drifting away from the document without anyone
    deciding to let it.
    """
    exported = set(families(catalogue()))
    expected = {row[0] for row in SECTION_6_CATALOGUE} | set(EXTENSION_METRIC_NAMES)

    assert exported == expected


def test_the_dropped_write_counter_is_declared_an_extension_not_a_section_6_row() -> None:
    """ADR 0008 asked P2-T4 for it; §6 does not list it. Both facts are recorded.

    Kept honest by naming it out loud rather than letting it blend into the table:
    a reader comparing `/metrics` against §6 should be able to tell which line is
    the document's and which is ours.
    """
    assert "keel_health_writes_dropped_total" in EXTENSION_METRIC_NAMES
    assert "keel_health_writes_dropped_total" not in {row[0] for row in SECTION_6_CATALOGUE}


def test_the_transcribed_table_has_all_eleven_rows() -> None:
    """Guards the parametrization itself: a truncated table would silently test less."""
    assert len(SECTION_6_CATALOGUE) == 11
    assert len({row[0] for row in SECTION_6_CATALOGUE}) == 11


# --------------------------------------------------------------------------
# Flat, not absent (the P2-T6 panels)
# --------------------------------------------------------------------------


def test_provider_keyed_series_exist_before_anything_produces_them() -> None:
    """Declaring a labelled metric exports no series at all until it is touched.

    That is the difference between a Phase 3 panel reading a flat zero and one
    reading "No data", and the P2-T4 brief is explicit that these are declared now
    so the panels are "simply flat" rather than built twice.
    """
    body, _ = catalogue().render()
    text = body.decode("utf-8")

    for provider in PROVIDERS:
        assert f'keel_breaker_state{{provider="{provider}"}} 0.0' in text


def test_metrics_with_no_producer_are_declared_even_with_no_series() -> None:
    """`# TYPE` lines exist for the Phase 3/4/5 metrics, so a scraper knows the name."""
    body, _ = catalogue().render()
    text = body.decode("utf-8")

    for name in ("keel_failover_events_total", "keel_cost_micros_total", "keel_queue_depth"):
        assert f"# TYPE {name.removesuffix('_total')} " in text or f"# TYPE {name} " in text


def test_the_render_content_type_is_the_one_the_library_declares() -> None:
    """Not hardcoded: prometheus-client 0.26 serves `version=1.0.0`, not `0.0.4`."""
    _, content_type = catalogue().render()

    assert content_type.startswith("text/plain")
    assert "version=" in content_type


# --------------------------------------------------------------------------
# Buckets (decision C — S5 must be readable)
# --------------------------------------------------------------------------


def test_fifteen_milliseconds_is_an_exact_overhead_bucket_edge() -> None:
    """The whole reason these buckets are not the library defaults.

    S5 is "p95 <= 15 ms added" and §6 says this metric exists "specifically so S5
    can be measured rather than asserted". The default buckets step 10 ms straight
    to 25 ms, so `histogram_quantile` interpolates across the threshold and 12 ms
    is indistinguishable from 24 ms — the metric could not answer the question it
    was created for.
    """
    assert 0.015 in OVERHEAD_BUCKETS_SECONDS

    cat = catalogue()
    cat.observe_overhead("interactive_chat", 0.012)
    body, _ = cat.render()

    assert 'keel_gateway_overhead_seconds_bucket{class="interactive_chat",le="0.015"} 1.0' in (
        body.decode("utf-8")
    )


def test_the_duration_buckets_reach_the_slowest_configured_budget() -> None:
    """`batch_enrichment` allows 60 s; the library defaults stop at 10 s.

    With the defaults every call slower than 10 s lands in `+Inf` together, and the
    p95 panel flattens exactly where the M2 run drives it.
    """
    assert max(DURATION_BUCKETS_SECONDS) >= 60.0
    assert 60.0 in DURATION_BUCKETS_SECONDS


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_an_attempt_records_duration_in_seconds_not_milliseconds() -> None:
    """`latency_ms` is milliseconds at the source; the metric name says seconds."""
    cat = catalogue()

    cat.observe_attempt(envelope(), success(latency_ms=2500.0))

    text = cat.render()[0].decode("utf-8")
    assert (
        'keel_request_duration_seconds_sum{class="interactive_chat",'
        'provider="mock_chaos"} 2.5'
    ) in text


def test_a_failed_attempt_records_its_taxonomy_class() -> None:
    """The M2 error-rate panel's data source, split by class rather than by status.

    ADR 0006 is explicit that the metrics are labelled by `ErrorClass` and not by
    HTTP status "precisely so the dashboards never depend on this mapping".
    """
    cat = catalogue()

    cat.observe_attempt(envelope(), failure(ErrorClass.RATE_LIMIT))

    text = cat.render()[0].decode("utf-8")
    assert 'keel_provider_errors_total{error_class="rate_limit",provider="mock_chaos"} 1.0' in text


def test_a_successful_attempt_records_no_provider_error() -> None:
    cat = catalogue()

    cat.observe_attempt(envelope(), success())

    assert "keel_provider_errors_total{" not in cat.render()[0].decode("utf-8")


@pytest.mark.parametrize(
    ("ok", "expected"),
    [(True, OUTCOME_OK), (False, OUTCOME_ERROR)],
    ids=["ok", "error"],
)
def test_a_request_records_its_outcome(ok: bool, expected: str) -> None:
    """`outcome` is enumerated nowhere in the docs; these two values are the definition."""
    cat = catalogue()

    cat.observe_request(envelope=envelope(), provider="mock_chaos", ok=ok)

    assert f'outcome="{expected}"' in cat.render()[0].decode("utf-8")


def test_overhead_is_clamped_at_zero() -> None:
    """A negative observation is accepted silently by the library and poisons `_sum`.

    The middleware clamps too; this is the second guard, so a future caller cannot
    corrupt the S5 histogram by subtracting two clocks without thinking about it.
    """
    cat = catalogue()

    cat.observe_overhead("interactive_chat", -2.999)

    text = cat.render()[0].decode("utf-8")
    assert 'keel_gateway_overhead_seconds_sum{class="interactive_chat"} 0.0' in text


def test_a_dropped_health_write_is_counted_per_provider() -> None:
    """ADR 0008's ask: make silent Redis data loss visible as something other than a log."""
    cat = catalogue()

    cat.observe_dropped_health_write("mock_chaos")
    cat.observe_dropped_health_write("mock_chaos")

    text = cat.render()[0].decode("utf-8")
    assert 'keel_health_writes_dropped_total{provider="mock_chaos"} 2.0' in text


# --------------------------------------------------------------------------
# The label cap (ADR 0009)
# --------------------------------------------------------------------------


def test_distinct_tenants_pass_through_up_to_the_cap() -> None:
    cat = catalogue()

    for index in range(LABEL_CAP):
        cat.observe_request(envelope=envelope(tenant=f"tenant-{index}"), provider="p", ok=True)

    text = cat.render()[0].decode("utf-8")
    assert 'tenant="tenant-0"' in text
    assert f'tenant="tenant-{LABEL_CAP - 1}"' in text
    assert f'tenant="{OTHER_LABEL}"' not in text


def test_tenants_past_the_cap_collapse_into_one_series(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The overflow is folded, and said out loud once.

    Once rather than per request: the overflow case is exactly the one where
    something is generating values in a loop, and a line per request would turn a
    metrics problem into a disk problem.
    """
    cat = catalogue()
    for index in range(LABEL_CAP):
        cat.observe_request(envelope=envelope(tenant=f"tenant-{index}"), provider="p", ok=True)

    with caplog.at_level(logging.WARNING, logger="keel.observability.metrics"):
        cat.observe_request(envelope=envelope(tenant="the-65th"), provider="p", ok=True)
        cat.observe_request(envelope=envelope(tenant="the-66th"), provider="p", ok=True)

    text = cat.render()[0].decode("utf-8")
    assert f'tenant="{OTHER_LABEL}"' in text
    assert 'tenant="the-65th"' not in text
    assert caplog.text.count("reached its cap") == 1, "warned once, not per request"


def test_the_series_count_is_bounded_under_a_flood() -> None:
    """The NFR-5 property, asserted as a bound rather than spot-checked.

    Without the cap this is remote memory exhaustion: 706 bytes retained per
    distinct value, on an unauthenticated endpoint, driven by a header. Ten
    thousand distinct tenants must leave at most `LABEL_CAP` real values plus the
    one `other` bucket.
    """
    cat = catalogue()

    for index in range(10_000):
        cat.observe_request(envelope=envelope(tenant=f"t{index}"), provider="p", ok=True)

    text = cat.render()[0].decode("utf-8")
    tenant_values = {
        line.split('tenant="', 1)[1].split('"', 1)[0]
        for line in text.splitlines()
        if line.startswith("keel_requests_total{")
    }

    assert len(tenant_values) <= LABEL_CAP + 1
    assert OTHER_LABEL in tenant_values


def test_tenant_and_feature_are_capped_independently() -> None:
    """One busy dimension must not consume the other's budget."""
    cat = catalogue()

    for index in range(LABEL_CAP + 10):
        cat.observe_request(
            envelope=envelope(tenant=f"t{index}", feature="stable"), provider="p", ok=True
        )

    text = cat.render()[0].decode("utf-8")
    assert 'feature="stable"' in text
    assert f'feature="{OTHER_LABEL}"' not in text, "features were never over their own cap"


def test_a_capped_tenant_keeps_counting_rather_than_being_dropped() -> None:
    """Folded into `other`, not discarded — the request volume stays true."""
    cat = catalogue()
    for index in range(LABEL_CAP):
        cat.observe_request(envelope=envelope(tenant=f"t{index}"), provider="p", ok=True)

    for _ in range(5):
        cat.observe_request(envelope=envelope(tenant="overflow"), provider="p", ok=True)

    text = cat.render()[0].decode("utf-8")
    other_line = next(
        line for line in text.splitlines()
        if line.startswith("keel_requests_total{") and f'tenant="{OTHER_LABEL}"' in line
    )
    assert other_line.endswith(" 5.0")
