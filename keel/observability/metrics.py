"""The Prometheus metrics catalogue (FR-3.3, TECHNICAL-DESIGN.md §6).

Every metric in the §6 table is declared here, **including the ones nothing
produces yet**. `keel_breaker_*` and `keel_failover_events_total` wait for Phase
3, `keel_hedge_attempts_total` for §5.8, `keel_queue_*` for Phase 5, and
`keel_cost_micros_total` for Phase 4. Declaring them now costs a few lines and
means the P2-T6 Grafana panels are built once and simply sit flat, rather than
being built, discovered empty, and built again.

This is the first module that makes any of the health data *visible*. FR-3.4
ordered it that way deliberately — P2-T2 and P2-T3 recorded outcomes and
latencies to Redis and nothing read them back — and §5's tripwire keeps Phase 3's
breaker waiting until the board in P2-T6 exists.

**Label values that come from clients are capped (ADR 0009).** §6 gives
``keel_requests_total`` the labels ``tenant`` and ``feature``, and
``keel/api/envelope.py`` accepts both as arbitrary strings from
``X-Keel-Tenant`` and ``X-Keel-Feature``. ``prometheus_client`` never evicts a
series, so unbounded label values are unbounded memory — measured at 706 bytes
retained per distinct value, with a 4.7 MB scrape body at 20k tenants. See
:class:`_LabelCapper`.

**Bucket boundaries are chosen, not defaulted.** The library's default set jumps
10 ms to 25 ms, which straddles S5's 15 ms target and makes the one metric §6
says exists "specifically so S5 can be measured" unable to measure it. See
:data:`OVERHEAD_BUCKETS_SECONDS`.

**One registry per app, injected.** The collectors below are instance attributes
rather than module globals because ``prometheus_client`` raises
``DuplicateTimeseries`` when the same name is registered twice, and
``tests/test_app.py`` builds thirty-odd apps in one session. It also gives each
test its own counters, matching how a fresh ``FakeRedis`` is already handed to
each app.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from keel.api.envelope import RequestEnvelope
    from keel.providers.base import ProviderResult

__all__ = [
    "DURATION_BUCKETS_SECONDS",
    "EXTENSION_METRIC_NAMES",
    "LABEL_CAP",
    "OTHER_LABEL",
    "OVERHEAD_BUCKETS_SECONDS",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "MetricsCatalogue",
]

logger = logging.getLogger(__name__)

# --- Label vocabulary -------------------------------------------------------

OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
"""The two values of the ``outcome`` label on ``keel_requests_total``.

**Enumerated here because no document enumerates them.** §6 names the label and
stops. Two values, mirroring ``ProviderResult.ok``, and deliberately not a third
for "which kind of error" — that split belongs to
``keel_provider_errors_total{error_class}``, which ADR 0006 says the dashboards
should read precisely so they never depend on the HTTP status mapping.
"""

LABEL_CAP: Final = 64
OTHER_LABEL: Final = "other"
"""The bound on client-supplied label values, and what overflow becomes (ADR 0009).

64 is generous against reality — the shipped config has one tenant — and small
enough that the worst case is bounded and unremarkable: 64 tenants x 64 features
x 3 classes x 2 providers x 2 outcomes is about 49k series, roughly 35 MB, and it
cannot grow past that however the gateway is abused.
"""

_BREAKER_CLOSED: Final = 0.0
"""The 0-closed / 1-half-open / 2-open encoding is fixed by the §6 table."""

# --- Buckets ----------------------------------------------------------------

OVERHEAD_BUCKETS_SECONDS: Final = (
    0.001, 0.0025, 0.005, 0.0075, 0.010, 0.015, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0,
)
"""Buckets for ``keel_gateway_overhead_seconds``, with **15 ms as an exact edge**.

That edge is the entire point. S5 is "p95 <= 15 ms added" and §6 says this metric
exists "specifically so S5 can be measured rather than asserted" — but the
library's default buckets step 10 ms straight to 25 ms, so ``histogram_quantile``
interpolates across the threshold and cannot tell 12 ms from 24 ms. With 0.015 as
a boundary, S5 is read directly off one bucket rather than inferred from a
straight line drawn through it.

The tail runs to 1 s because ADR 0008 measured ~220 ms of overhead per request
while Redis is unreachable. That breaches S5 by design and is recorded; these
buckets are what let the breach be *seen* rather than swallowed by ``+Inf``.
"""

DURATION_BUCKETS_SECONDS: Final = (
    0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
)
"""Buckets for ``keel_request_duration_seconds`` — provider time, so seconds-scale.

The defaults stop at 10 s. ``batch_enrichment``'s ``latency_budget_p95_ms`` is
60 s, so with defaults every slow call — the ones a latency budget exists to
notice — would land in ``+Inf`` together and the p95 panel would flatten exactly
where the M2 run drives it (a 3 s median with a long right tail).
"""

QUEUE_AGE_BUCKETS_SECONDS: Final = (
    1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 21600.0,
)
"""Buckets for ``keel_queue_job_age_seconds``. Phase 5 fills it.

Minutes-to-hours, because a deferrable job's age is a queue-drain measure rather
than a request latency. Declared now with the rest of the catalogue; revisit when
Phase 5 has a real drain rate to compare against.
"""

# --- What is, and is not, in §6 ---------------------------------------------

EXTENSION_METRIC_NAMES: Final = frozenset({"keel_health_writes_dropped_total"})
"""Metrics this gateway exports that the §6 catalogue does not list.

Named as a set rather than left implicit so ``tests/test_metrics.py`` can assert
two different things: that every §6 row exists, and that anything *beyond* §6 is
a deliberate extension rather than an accident. Without the second check a
twelfth metric could be added with nobody noticing it had left the catalogue.
"""


class _LabelCapper:
    """Bounds a client-supplied label to :data:`LABEL_CAP` distinct values (ADR 0009).

    The failure this prevents is specific and reachable from one header.
    ``tenant`` and ``feature`` are whatever the caller puts in ``X-Keel-Tenant``
    and ``X-Keel-Feature`` — ``keel/api/envelope.py`` checks only that they are
    non-blank — and ``prometheus_client`` retains every distinct label
    combination for the lifetime of the process. Measured: **706 bytes per
    series**, and a 4.7 MB scrape body at 20k tenants. So a loop sending random
    headers is remote memory exhaustion, against NFR-5's "no unbounded memory
    growth under sustained load", with no authentication needed because there is
    none (§10).

    First-seen-wins rather than any cleverer eviction. A least-recently-used
    policy would keep the *busiest* tenants, which is nicer, but it also means a
    label's series can vanish and a counter appear to reset — and a counter that
    resets is a lie to every ``rate()`` query over it. Admitting a stable set and
    folding the rest into one honest bucket keeps every series monotonic.
    """

    def __init__(self, label: str, cap: int = LABEL_CAP) -> None:
        self._label = label
        self._cap = cap
        self._seen: set[str] = set()
        self._warned = False

    def __call__(self, value: str) -> str:
        if value in self._seen:
            return value
        if len(self._seen) < self._cap:
            self._seen.add(value)
            return value

        if not self._warned:
            # Once, not per request: the overflow case is exactly the one where
            # something is generating values in a loop, and a log line per
            # request would turn a metrics problem into a disk problem.
            self._warned = True
            logger.warning(
                "metric label %r reached its cap of %d distinct values; %r and every "
                "later unseen value are recorded as %r (ADR 0009). This usually means "
                "a client is sending unbounded %s values.",
                self._label,
                self._cap,
                value,
                OTHER_LABEL,
                self._label,
            )
        return OTHER_LABEL

    @property
    def distinct(self) -> int:
        """How many real values have been admitted. For tests and diagnostics."""
        return len(self._seen)


class MetricsCatalogue:
    """The §6 catalogue, bound to one registry.

    Built once per app in the lifespan and reachable from ``AppContext``. Holds
    no state beyond the collectors and the two label caps.
    """

    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        providers: Iterable[str] = (),
    ) -> None:
        """
        :param registry: the collector registry. A fresh one per app by default,
            because ``prometheus_client`` raises ``DuplicateTimeseries`` on a
            second registration of the same name and the test suite builds many
            apps in one session.
        :param providers: the configured provider names, used to pre-create the
            provider-keyed series so they read **flat zero rather than "no data"**
            before anything has produced them. See :meth:`_prime`.
        """
        self.registry = registry if registry is not None else CollectorRegistry()

        self._tenants = _LabelCapper("tenant")
        self._features = _LabelCapper("feature")

        # --- §6, in table order -------------------------------------------------
        # The label spelled `class` follows the §6 table, not §5.10's prose, which
        # calls it `request_class`; the two disagree and P2-T4's brief points at
        # the table. It is a Python keyword, so every call site must spell it
        # `.labels(**{"class": ...})` rather than as a keyword argument.

        self.requests_total = Counter(
            "keel_requests_total",
            "Requests served, by tenant, feature, class, provider and outcome.",
            ["tenant", "feature", "class", "provider", "outcome"],
            registry=self.registry,
        )
        self.request_duration_seconds = Histogram(
            "keel_request_duration_seconds",
            "Provider call duration in seconds.",
            ["class", "provider"],
            buckets=DURATION_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self.gateway_overhead_seconds = Histogram(
            "keel_gateway_overhead_seconds",
            "Request wall clock minus provider time, in seconds (S5).",
            ["class"],
            buckets=OVERHEAD_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self.provider_errors_total = Counter(
            "keel_provider_errors_total",
            "Provider failures by normalized error class (design section 5.4).",
            ["provider", "error_class"],
            registry=self.registry,
        )

        # Phase 3 (§5.6) fills the next three. Flat until then, on purpose.
        self.breaker_state = Gauge(
            "keel_breaker_state",
            "Circuit state per provider: 0 closed, 1 half-open, 2 open.",
            ["provider"],
            registry=self.registry,
        )
        self.breaker_transitions_total = Counter(
            "keel_breaker_transitions_total",
            "Circuit state transitions.",
            ["provider", "from_state", "to_state"],
            registry=self.registry,
        )
        self.failover_events_total = Counter(
            "keel_failover_events_total",
            "Requests rerouted from one provider to another.",
            ["from_provider", "to_provider", "class"],
            registry=self.registry,
        )

        # §5.8 hedging, also Phase 3.
        self.hedge_attempts_total = Counter(
            "keel_hedge_attempts_total",
            "Hedged attempts, labelled by which leg won.",
            ["class", "winner"],
            registry=self.registry,
        )

        # Phase 5, the deferred queue (§5.9). Both are unlabelled in §6.
        self.queue_depth = Gauge(
            "keel_queue_depth",
            "Deferred jobs currently enqueued.",
            registry=self.registry,
        )
        self.queue_job_age_seconds = Histogram(
            "keel_queue_job_age_seconds",
            "Age of deferred jobs at the moment they are drained, in seconds.",
            buckets=QUEUE_AGE_BUCKETS_SECONDS,
            registry=self.registry,
        )

        # Phase 4, the cost engine (§5.10). Note it carries the same two
        # client-supplied labels as `keel_requests_total`, so when Phase 4 starts
        # producing it, it must route them through `cap_tenant`/`cap_feature`
        # below rather than passing raw header values.
        self.cost_micros_total = Counter(
            "keel_cost_micros_total",
            "Attributed spend in micros.",
            ["tenant", "feature", "class", "provider", "attempt_type"],
            registry=self.registry,
        )

        # --- Beyond §6: extensions, declared as such ----------------------------
        # ADR 0008 left this here by name: a health write that fails is dropped,
        # and "until P2-T4 exports a counter the only evidence is a WARNING on
        # stdout". A dashboard that looks clean because nothing was recorded is
        # the failure this makes visible.
        self.health_writes_dropped_total = Counter(
            "keel_health_writes_dropped_total",
            "Health observations lost because Redis could not be written (ADR 0008).",
            ["provider"],
            registry=self.registry,
        )

        self.prime(providers)

    def prime(self, providers: Iterable[str]) -> None:
        """Create the provider-keyed series up front, at zero.

        Public and safe to call again: the app factory builds the catalogue
        before the config is loaded (so the middleware has something to hold),
        and the lifespan primes it once the provider names are known.

        Declaring a labelled metric exports its ``# HELP`` and ``# TYPE`` lines but
        **no series** — a labelled family is empty until something calls
        ``.labels(...)`` on it. That is the difference between a Grafana panel
        reading a flat zero and one reading "No data", and the P2-T4 brief is
        explicit that the Phase 3–5 metrics are declared now so those panels "exist
        and are simply flat, rather than being built twice".

        Only the series whose label sets are knowable at startup can be primed:
        ``provider`` comes from the validated config. ``from_state``/``to_state``,
        ``winner``, and ``attempt_type`` are outcomes rather than configuration, and
        inventing their combinations would export series that misrepresent what has
        happened. Those stay absent until their producer arrives.

        Priming a counter also fixes a subtler thing: ``rate()`` needs a prior
        sample, so a counter whose first-ever observation creates the series has no
        rate at the moment it first matters.
        """
        for provider in providers:
            self.breaker_state.labels(provider=provider).set(_BREAKER_CLOSED)
            self.health_writes_dropped_total.labels(provider=provider)

    # --- Recording ----------------------------------------------------------

    def observe_attempt(self, envelope: RequestEnvelope, result: ProviderResult) -> None:
        """Record one provider attempt. Called from the executor (D-C).

        Per-*attempt* rather than per-request, which is why it lives beside
        ``HealthTracker.record`` rather than at the HTTP boundary: Phase 3's
        failover loop makes several attempts for one request, and a duration
        histogram that only saw the last one would hide exactly the slow attempt
        that caused the failover.

        ``latency_ms`` is milliseconds at the source and seconds here, because
        Prometheus convention is base units and the metric name says ``_seconds``.
        """
        self.request_duration_seconds.labels(
            **{"class": envelope.request_class, "provider": result.provider}
        ).observe(result.latency_ms / 1000.0)

        if result.error is not None:
            self.provider_errors_total.labels(
                provider=result.provider, error_class=result.error.error_class.value
            ).inc()

    def observe_request(
        self,
        *,
        envelope: RequestEnvelope,
        provider: str,
        ok: bool,
    ) -> None:
        """Record one served request. Called from the ingress route.

        Separate from :meth:`observe_attempt` because §6 counts requests once
        while attempts may be several. In Phase 1 they coincide; the split is what
        keeps that a coincidence rather than an assumption.
        """
        self.requests_total.labels(
            **{
                "tenant": self.cap_tenant(envelope.tenant),
                "feature": self.cap_feature(envelope.feature),
                "class": envelope.request_class,
                "provider": provider,
                "outcome": OUTCOME_OK if ok else OUTCOME_ERROR,
            }
        ).inc()

    def observe_overhead(self, request_class: str, overhead_seconds: float) -> None:
        """Record gateway overhead for one request. Called from the middleware.

        Negative input is clamped to zero rather than rejected — see the middleware
        for why it can be negative at all. Clamping here as well as there means the
        histogram cannot be poisoned by a future second caller: ``prometheus_client``
        accepts a negative observation silently, counts it in every bucket, and
        leaves ``_sum`` permanently wrong.
        """
        self.gateway_overhead_seconds.labels(**{"class": request_class}).observe(
            max(0.0, overhead_seconds)
        )

    def observe_dropped_health_write(self, provider: str) -> None:
        """One health observation lost to Redis (ADR 0008). Not a §6 metric."""
        self.health_writes_dropped_total.labels(provider=provider).inc()

    # --- Label capping, exposed for Phase 4 ---------------------------------

    def cap_tenant(self, value: str) -> str:
        """Bound a tenant label (ADR 0009). Phase 4's cost counter must use this too."""
        return self._tenants(value)

    def cap_feature(self, value: str) -> str:
        """Bound a feature label (ADR 0009)."""
        return self._features(value)

    # --- Exposition ---------------------------------------------------------

    def render(self) -> tuple[bytes, str]:
        """The scrape body and its content type.

        ``generate_latest`` returns ``bytes``, and the content type in
        prometheus-client 0.26 is ``version=1.0.0`` rather than the ``0.0.4`` older
        examples show — both are reasons to render through here rather than
        hand-assembling a response at the route.
        """
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
