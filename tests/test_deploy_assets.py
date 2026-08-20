"""Tests for the compose stack's version-controlled assets (P2-T6, NFR-1, S8).

None of these start a container. Docker is not a test dependency — PHASE-2-PLAN
§6 requires the suite to run with no network and no Redis (NFR-2), and a suite
that needed a daemon would stop running in CI. What is checked here is everything
about the stack that is knowable from the files themselves, which turns out to be
most of the ways it can be wrong.

**The one that earns its keep is the dashboard check.** A PromQL query naming a
metric that does not exist renders as an empty panel, not an error, and the
cheapest time to discover that is not during a demo. So every metric name and
every label in `keel-health.json` is checked against a live `MetricsCatalogue`
— the same posture as the §5.4 truth table and the §5.5 key, where the thing that
must agree is asserted to agree rather than assumed to.

The rest guard drift between files that have to match: the Prometheus scrape
interval against `breaker.bucket_seconds`, the Grafana datasource UID against the
one the dashboard's panels point at, and the demo config against its promise of
needing no credentials.

What these tests **cannot** tell you: whether the images pull, whether the
healthchecks pass, whether Prometheus actually reaches the gateway, or whether
Grafana renders any of this. That needs a machine with Docker and is recorded as
unverified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from keel.config import load_config
from keel.observability.metrics import MetricsCatalogue
from keel.providers.credentials import ProviderCredentials
from keel.providers.registry import build_registry

REPO_ROOT = Path(__file__).resolve().parent.parent

COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
DEMO_CONFIG = REPO_ROOT / "deploy" / "keel.demo.yaml"
SHIPPED_CONFIG = REPO_ROOT / "config" / "keel.yaml"
PROMETHEUS = REPO_ROOT / "deploy" / "prometheus" / "prometheus.yml"
DATASOURCES = REPO_ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
DASHBOARD_PROVIDER = REPO_ROOT / "deploy" / "grafana" / "provisioning" / "dashboards" / "keel.yml"
DASHBOARD = REPO_ROOT / "deploy" / "grafana" / "dashboards" / "keel-health.json"

GATEWAY_SERVICE = "keel-gateway"
GATEWAY_PORT = 8080

EXPECTED_SERVICES = {GATEWAY_SERVICE, "redis", "prometheus", "grafana"}


def load_yaml(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.name} is not a YAML mapping"
    return parsed


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return load_yaml(COMPOSE)


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    parsed = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def panel_expressions(dashboard: dict[str, Any]) -> list[str]:
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]


# --------------------------------------------------------------------------
# Everything exists and parses
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [COMPOSE, DOCKERFILE, DOCKERIGNORE, DEMO_CONFIG, PROMETHEUS, DATASOURCES,
     DASHBOARD_PROVIDER, DASHBOARD],
    ids=lambda p: str(p.relative_to(REPO_ROOT)).replace("\\", "/"),
)
def test_the_asset_exists_and_is_not_empty(path: Path) -> None:
    assert path.is_file(), f"{path} is missing"
    assert path.read_text(encoding="utf-8").strip(), f"{path} is empty"


def test_the_compose_file_is_at_the_repo_root() -> None:
    """NFR-1 and S8 both spell the bare command `docker compose up`.

    That resolves a file in the working directory, so a compose file under
    `deploy/` would make every documented command wrong. Asserted rather than
    remembered, because "move it into deploy/ for tidiness" is an obvious and
    quiet way to break the one command two requirements name.
    """
    assert COMPOSE.parent == REPO_ROOT
    assert not (REPO_ROOT / "deploy" / "docker-compose.yml").exists()


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------


def test_compose_declares_exactly_the_expected_services(compose: dict[str, Any]) -> None:
    """No `mock-provider` (ADR 0002) and no `keel-worker` (Phase 5).

    Both are drawn in §8's diagram, which describes the end state. Asserting the
    exact set means adding either one is a visible decision — the same anti-scope
    -creep posture as the route-table test in `tests/test_app.py`.
    """
    assert set(compose["services"]) == EXPECTED_SERVICES


def test_every_service_has_a_healthcheck(compose: dict[str, Any]) -> None:
    """"Up" must mean ready, not started.

    Without this Grafana can start before Prometheus and provision a datasource
    it cannot reach, which renders as four empty panels — indistinguishable from
    a broken dashboard, and the first thing a reviewer would see.
    """
    missing = [name for name, spec in compose["services"].items() if "healthcheck" not in spec]
    assert not missing, f"services with no healthcheck: {missing}"


def test_dependencies_wait_for_health_rather_than_start(compose: dict[str, Any]) -> None:
    for name, spec in compose["services"].items():
        for dependency, condition in (spec.get("depends_on") or {}).items():
            assert condition.get("condition") == "service_healthy", (
                f"{name} waits on {dependency} without requiring health"
            )


def test_the_documented_ports_are_published(compose: dict[str, Any]) -> None:
    """The ports every document names: 8080, 9090, 3000 (§8).

    Redis is deliberately absent — nothing outside the compose network needs it,
    and publishing an unauthenticated Redis to the host is a habit worth not
    forming.
    """
    published = {
        name: [str(entry).split(":")[0] for entry in spec.get("ports", [])]
        for name, spec in compose["services"].items()
    }
    assert published[GATEWAY_SERVICE] == [str(GATEWAY_PORT)]
    assert published["prometheus"] == ["9090"]
    assert published["grafana"] == ["3000"]
    assert not published["redis"], "Redis should not be published to the host"


def test_the_gateway_points_at_the_mock_only_demo_config(compose: dict[str, Any]) -> None:
    """The whole reason the stack needs no credentials and the load run is free."""
    environment = compose["services"][GATEWAY_SERVICE]["environment"]
    assert environment["KEEL_CONFIG_PATH"].endswith("keel.demo.yaml")
    assert environment["REDIS_URL"] == "redis://redis:6379/0", "service name, not localhost"
    assert environment["KEEL_LOG_FORMAT"] == "json"
    assert environment["KEEL_CHAOS_ENABLED"] == "true", "loadgen needs the chaos endpoint"


def test_redis_persistence_is_stated_rather_than_inherited(compose: dict[str, Any]) -> None:
    """ADR 0008 asked P2-T6 to say which persistence mode the stack uses.

    FR-3.2 wants health counters to survive a restart, and the image default
    (RDB snapshots) loses the last seconds of the window on an unclean stop —
    which is precisely the window a breaker demo is looking at.
    """
    redis = compose["services"]["redis"]
    assert "--appendonly" in redis["command"]
    assert redis["command"][redis["command"].index("--appendonly") + 1] == "yes"
    assert any("/data" in str(volume) for volume in redis["volumes"])


def test_the_gateway_is_not_scaled(compose: dict[str, Any]) -> None:
    """`MockChaosState` is per-process (ADR 0002).

    A second replica would receive none of loadgen's chaos calls, so half the
    traffic would ignore the demo's configured error rate and the panels would
    show a rate nobody asked for.
    """
    gateway = compose["services"][GATEWAY_SERVICE]
    assert "deploy" not in gateway or "replicas" not in gateway.get("deploy", {})
    assert gateway.get("scale") in (None, 1)


def test_the_build_context_is_the_repo_root(compose: dict[str, Any]) -> None:
    """The image needs `pyproject.toml`, `keel/`, and `config/`, which live above `deploy/`."""
    build = compose["services"][GATEWAY_SERVICE]["build"]
    assert build["context"] == "."
    assert build["dockerfile"] == "deploy/Dockerfile"


def test_the_dockerignore_excludes_the_env_file() -> None:
    """`.env` carries a real provider credential and must never enter a layer."""
    lines = {line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    assert ".env" in lines
    assert ".git" in lines


def test_the_dockerignore_does_not_exclude_the_demo_config() -> None:
    """The Dockerfile copies it by name, so an ignore rule here would break the build.

    A whole-directory `deploy/` exclusion is the obvious tidy-up and would fail
    at `docker build` with a path that does not exist — on someone else's
    machine, since Docker is not available where this is developed.
    """
    lines = {line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    assert "deploy/" not in lines
    assert "deploy" not in lines


# --------------------------------------------------------------------------
# The demo config — the promise the stack rests on
# --------------------------------------------------------------------------


def test_the_demo_config_builds_a_registry_with_no_credentials_at_all() -> None:
    """S8's "clean machine" means a reviewer with no Cohere account.

    `config/keel.yaml` cannot do this: the registry refuses to build a provider
    whose credential is missing (ADR 0004), so a blank key fails startup. The
    demo config names only the mock. If it ever grows a real provider, this fails
    here rather than on a stranger's laptop.
    """
    config = load_config(DEMO_CONFIG)
    registry = build_registry(
        config=config,
        clock=__import__("keel.clock", fromlist=["SystemClock"]).SystemClock(),
        credentials=ProviderCredentials(cohere_api_key=""),
    )
    assert set(registry) == {"mock_chaos"}


def test_every_demo_preference_list_routes_to_the_mock() -> None:
    """Phase 1 invokes candidate 1 and stops, so candidate 1 is the whole routing story.

    A demo config whose first candidate were Cohere would send the M2 run — 2400
    requests — to a paid API, against NFR-3's EUR 75 budget and §5's tripwire.
    """
    config = load_config(DEMO_CONFIG)
    for name, request_class in config.request_classes.items():
        assert request_class.preference == ["mock_chaos"], f"{name} does not route to the mock"


def test_the_demo_config_keeps_the_shipped_capability_gap() -> None:
    """`citations` is absent from the mock in both configs, and must stay absent.

    That asymmetry is the only thing in the repo that makes the §5.7 capability
    filter demonstrable (D2). A demo config that was generous with capabilities
    would quietly remove the gap Phase 3 exists to show.
    """
    demo = load_config(DEMO_CONFIG).providers["mock_chaos"]
    shipped = load_config(SHIPPED_CONFIG).providers["mock_chaos"]
    assert demo.capabilities == shipped.capabilities
    assert "citations" not in demo.capabilities


def test_the_two_configs_agree_on_the_breaker_geometry() -> None:
    """The scrape interval is derived from this, so the two configs must not diverge."""
    assert load_config(DEMO_CONFIG).breaker == load_config(SHIPPED_CONFIG).breaker


# --------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------


def test_the_scrape_interval_matches_the_health_bucket_width() -> None:
    """Read from both files rather than restated, so they cannot drift apart.

    The health window rolls in `bucket_seconds` buckets. A slower scrape makes
    the dashboard lag the gateway's own view of health, and in Phase 3 a breaker
    could trip and recover between two samples.
    """
    prometheus = load_yaml(PROMETHEUS)
    interval = prometheus["global"]["scrape_interval"]
    assert interval.endswith("s")

    bucket_seconds = load_config(SHIPPED_CONFIG).breaker.bucket_seconds
    assert int(interval.removesuffix("s")) <= bucket_seconds


def test_prometheus_scrapes_the_gateway_on_the_documented_path() -> None:
    prometheus = load_yaml(PROMETHEUS)
    (job,) = prometheus["scrape_configs"]

    assert job["metrics_path"] == "/metrics"
    targets = [target for entry in job["static_configs"] for target in entry["targets"]]
    assert targets == [f"{GATEWAY_SERVICE}:{GATEWAY_PORT}"]


def test_the_scrape_target_names_a_real_compose_service(compose: dict[str, Any]) -> None:
    """A typo here scrapes nothing and reports `DOWN` with no other symptom."""
    prometheus = load_yaml(PROMETHEUS)
    (job,) = prometheus["scrape_configs"]
    for entry in job["static_configs"]:
        for target in entry["targets"]:
            host, _, port = target.partition(":")
            assert host in compose["services"], f"{host} is not a compose service"
            assert port == str(GATEWAY_PORT)


def test_the_scrape_timeout_fits_inside_the_interval() -> None:
    """Otherwise a hung gateway stalls the scrape loop instead of failing one scrape."""
    prometheus = load_yaml(PROMETHEUS)
    interval = int(prometheus["global"]["scrape_interval"].removesuffix("s"))
    (job,) = prometheus["scrape_configs"]
    assert int(str(job["scrape_timeout"]).removesuffix("s")) < interval


# --------------------------------------------------------------------------
# Grafana
# --------------------------------------------------------------------------


def test_the_datasource_uid_is_fixed_and_matches_every_panel(dashboard: dict[str, Any]) -> None:
    """Panels refer to the datasource by UID.

    Letting Grafana generate one would leave every panel pointing at a datasource
    that does not exist, which renders as "Datasource not found" on all of them —
    a whole broken board from one omitted field.
    """
    datasources = load_yaml(DATASOURCES)
    (declared,) = datasources["datasources"]
    uid = declared["uid"]

    assert declared["url"] == "http://prometheus:9090", "service name, not localhost"

    referenced = {
        target["datasource"]["uid"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if isinstance(target.get("datasource"), dict)
    }
    assert referenced == {uid}


def test_the_dashboard_provider_path_matches_the_compose_mount(compose: dict[str, Any]) -> None:
    provider = load_yaml(DASHBOARD_PROVIDER)
    (declared,) = provider["providers"]
    path = declared["options"]["path"]

    mounts = [str(volume) for volume in compose["services"]["grafana"]["volumes"]]
    assert any(mount.split(":")[1] == path for mount in mounts if ":" in mount), (
        f"nothing is mounted at {path}"
    )


def test_the_dashboard_has_a_panel_for_every_metric_something_produces(
    dashboard: dict[str, Any],
) -> None:
    """A panel exists exactly when a producer for its metric exists — no earlier.

    This test was written in P2-T6 to be edited, and Phase 3 is the edit. It then
    asserted that `circuit` and `failover` panels were **absent**, because a flat
    line drawn before the breaker existed would have meant "no breaker" rather
    than "no trips" — a worse lie than an absent panel. Phase 3 builds the
    breaker, so `keel_breaker_state` at `0` now honestly reads as *closed* and
    both panels have something true to draw.

    `queue` and `cost` stay absent for exactly the same reason, until Phase 5 and
    Phase 4 produce them. That is what keeps this a rule rather than a snapshot.
    """
    titles = [panel["title"] for panel in dashboard["panels"]]

    assert "RPS by provider" in titles
    assert "Error rate by normalized class" in titles
    assert "p95 latency by provider" in titles
    assert any("overhead" in title.lower() for title in titles)
    assert any("circuit" in title.lower() for title in titles)
    assert any("failover" in title.lower() for title in titles)

    for absent in ("queue", "cost"):
        assert not any(absent in title.lower() for title in titles), (
            f"a {absent!r} panel exists but nothing produces its metric until a later phase"
        )


def test_the_circuit_panel_maps_all_three_states(dashboard: dict[str, Any]) -> None:
    """0/1/2 is the §6 encoding, and a timeline of bare integers is unreadable.

    Without the value mappings the panel renders three indistinguishable numbers
    where the demo needs "Closed → Open → Half-open → Closed" to be legible at a
    glance. This is the panel the FR-7.4 video is built around.
    """
    (panel,) = [p for p in dashboard["panels"] if "circuit" in p["title"].lower()]
    (mapping,) = panel["fieldConfig"]["defaults"]["mappings"]

    assert {key: value["text"] for key, value in mapping["options"].items()} == {
        "0": "Closed",
        "1": "Half-open",
        "2": "Open",
    }


def test_every_metric_the_dashboard_queries_actually_exists() -> None:
    """The test that would otherwise be a demo-day discovery.

    A PromQL query naming a metric that does not exist renders as an empty panel,
    never an error. Checked against a live `MetricsCatalogue` rather than against
    the §6 table, so this stays true if the code and the table ever disagree —
    the panel reads the code.

    `_bucket`, `_sum`, `_count` and `_total` suffixes are Prometheus's own
    rendering of histograms and counters, so they are stripped before comparison.
    """
    exported = {
        metric.name for metric in MetricsCatalogue(providers=["mock_chaos"]).registry.collect()
    }
    queried = {
        name
        for expr in panel_expressions(json.loads(DASHBOARD.read_text(encoding="utf-8")))
        for name in re.findall(r"\bkeel_[a-z_]+\b", expr)
    }
    assert queried, "no keel metrics found in the dashboard — the parser is wrong"

    for name in sorted(queried):
        base = name
        for suffix in ("_bucket", "_sum", "_count"):
            base = base.removesuffix(suffix)
        assert base in exported or base.removesuffix("_total") in exported, (
            f"{name!r} is queried by a panel but no such metric is exported"
        )


def test_every_label_the_dashboard_groups_by_actually_exists() -> None:
    """A `by (provider)` over a metric with no `provider` label silently collapses.

    The panel still renders — it just shows one line labelled `{}` instead of one
    per provider — so this is the second half of the check above and fails for a
    typo like `by (error_klass)`.
    """
    # Declared label names, not sample labels. Only the primed metrics have any
    # samples before traffic arrives (P2-T4), so reading labels off a sample
    # would find nothing for `keel_requests_total` and pass this test vacuously.
    catalogue = MetricsCatalogue(providers=["mock_chaos"])
    labels_by_metric = {
        collector._name: set(collector._labelnames)
        for collector in vars(catalogue).values()
        if hasattr(collector, "_labelnames") and hasattr(collector, "_name")
    }
    # `prometheus_client` stores a Counter's *base* name, so `keel_requests_total`
    # is held as `keel_requests`. The lookup below strips the suffix; this guard
    # makes a change in that behaviour fail loudly rather than emptying the map
    # and passing.
    assert labels_by_metric.get("keel_requests") == {
        "tenant",
        "feature",
        "class",
        "provider",
        "outcome",
    }, "the catalogue was read wrong"
    # `le` is synthesised by Prometheus for histogram buckets and appears on no
    # collector definition.
    synthetic = {"le"}

    expressions = panel_expressions(json.loads(DASHBOARD.read_text(encoding="utf-8")))
    for expr in expressions:
        metrics = re.findall(r"\bkeel_[a-z_]+\b", expr)
        grouped = {
            label.strip()
            for clause in re.findall(r"by\s*\(([^)]*)\)", expr)
            for label in clause.split(",")
            if label.strip()
        } - synthetic
        if not grouped:
            continue

        available: set[str] = set()
        for name in metrics:
            base = name
            for suffix in ("_bucket", "_sum", "_count"):
                base = base.removesuffix(suffix)
            available |= labels_by_metric.get(base, set())
            available |= labels_by_metric.get(base.removesuffix("_total"), set())

        unknown = grouped - available
        assert not unknown, f"{expr!r} groups by {sorted(unknown)}, which no queried metric carries"


def test_the_overhead_panel_marks_the_s5_threshold(dashboard: dict[str, Any]) -> None:
    """15 ms is an exact histogram bucket edge, so S5 is read rather than inferred.

    The threshold line is what makes the panel answer S5 at a glance instead of
    being a latency graph a reader has to interpret.
    """
    (panel,) = [p for p in dashboard["panels"] if "overhead" in p["title"].lower()]
    steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert any(step["value"] == 0.015 for step in steps), "no 15 ms marker on the S5 panel"


def test_the_dashboard_refreshes_fast_enough_for_the_done_when(
    dashboard: dict[str, Any],
) -> None:
    """"All four panels move within 30 seconds" is the P2-T6 done-when.

    A default refresh slower than that would fail the criterion while the data
    behind it was perfectly correct.
    """
    assert dashboard["refresh"].endswith("s")
    assert int(dashboard["refresh"].removesuffix("s")) <= 30


def test_the_rate_windows_hold_at_least_two_scrapes() -> None:
    """`rate()` over a window shorter than two scrape intervals returns nothing.

    The panel renders empty and looks like missing data rather than a query
    mistake, which is the same failure mode as a typo'd metric name and just as
    worth catching here.
    """
    interval = int(load_yaml(PROMETHEUS)["global"]["scrape_interval"].removesuffix("s"))
    windows = {
        int(window)
        for expr in panel_expressions(json.loads(DASHBOARD.read_text(encoding="utf-8")))
        for window in re.findall(r"\[(\d+)s\]", expr)
    }
    assert windows, "no rate windows found — the parser is wrong"
    for window in windows:
        assert window >= 2 * interval, (
            f"a {window}s window over a {interval}s scrape cannot hold two samples"
        )
