"""Fixture replay for the per-provider error mapping (P2-T1, FR-2.4, §7).

§7 asks for "fixture responses captured from each real provider, replayed
offline". This is that harness. Each file under ``tests/fixtures/providers/``
describes one provider failure completely enough to rebuild it here, and the
parametrized replay below asserts it still normalizes to the class it was
captured as — which is the task's stated done-when.

**Fixtures are built into real exceptions, not passed through LiteLLM's own
mapper.** ``litellm.exception_type`` works offline, but it is the thing under
test: the bad-key bug this task fixes *is* one of its mis-mappings, so replaying
through it would assert LiteLLM's current opinion rather than Keel's. It also
raises rather than returning, and appends ``traceback.format_exc()`` to its
message, so the text it produces is not stable enough to assert against.

**What a green run here does and does not prove.** It proves the mapping still
turns *this* text into *that* class. It cannot prove a provider still emits that
text — fixtures are frozen strings, and only the ``real_provider`` capture
script re-checks that. Two of the guards below exist because of that limit: the
catalogue test fails when LiteLLM adds an exception type, and every fixture
records the versions it was captured against.

No network, no Redis, no credentials (NFR-2).
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import subprocess
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from keel.config import AdapterName
from keel.providers.errors import ErrorClass
from keel.providers.normalize import (
    _REFINEMENTS,
    _classify_by_type,
    _error_map,
    matching_refinement,
    normalize_provider_error,
)

_FIXTURE_ROOT: Final = Path(__file__).parent / "fixtures" / "providers"

# A floor, not a count. Adding a fixture must never mean editing this number;
# the guard exists so a broken glob fails loudly rather than passing with zero
# cases, which is exactly what `parametrize` over an empty list would do.
_MINIMUM_FIXTURES: Final = 8

# The three directories the P2-T1 bullet names. Asserted as a set so deleting a
# provider's corpus fails here rather than silently shrinking coverage.
_EXPECTED_FAMILIES: Final = frozenset(
    {AdapterName.COHERE, AdapterName.AZURE_OPENAI, AdapterName.BEDROCK}
)

# Where a fixture's `module` name is actually looked up. A closed two-entry map
# rather than a dotted import path in the file, so a fixture cannot name an
# arbitrary module.
_MODULES: Final[Mapping[str, str]] = {"litellm": "litellm.exceptions", "openai": "openai"}


# --------------------------------------------------------------------------
# The fixture schema
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    """Unknown keys are errors, values are frozen — the repo's house style."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpSpec(_Strict):
    """The wire response to materialize, for exceptions that carry one.

    Separate from ``kwargs`` because an ``httpx.Response`` is not JSON. Which
    keyword it lands under is *derived* from the constructor signature rather
    than declared here: ``PermissionDeniedError`` requires ``response`` while
    ``openai.APITimeoutError`` requires ``request``, and a fixture author would
    get that wrong where :mod:`inspect` cannot.
    """

    status_code: int
    url: str


class ExceptionSpec(_Strict):
    module: Literal["litellm", "openai"]
    type: str
    kwargs: Mapping[str, Any] = {}
    http: HttpSpec | None = None


class Expectation(_Strict):
    error_class: ErrorClass
    status_code: int | None
    provider_error_type: str
    message_contains: str


class ErrorFixture(_Strict):
    """One captured or hand-written provider failure."""

    id: str
    provider: AdapterName
    source: Literal["captured", "handwritten"]
    verified: bool
    captured_at: date | None = None
    captured_with: Mapping[str, str] | None = None
    note: str
    exception: ExceptionSpec
    expect: Expectation

    @model_validator(mode="after")
    def _source_and_verified_agree(self) -> ErrorFixture:
        """Two fields that must not be able to disagree.

        ``verified`` is the flag the P2-T1 bullet insists lives in the file and
        is what a reader greps for; ``source`` is what a Phase 4 re-capture pass
        filters on. Letting them drift would make one of the two a lie.
        """
        if (self.source == "captured") is not self.verified:
            raise ValueError(
                f"fixture {self.id!r}: source={self.source!r} contradicts verified="
                f"{self.verified}. A captured fixture is verified; a hand-written one is not."
            )
        if self.source == "captured" and self.captured_with is None:
            raise ValueError(
                f"fixture {self.id!r}: a captured fixture must record captured_with, or "
                f"nobody can tell which library version it was true for."
            )
        return self

    def build(self) -> Exception:
        """Rebuild the exception this fixture describes, offline.

        Constructed by **keyword only**: LiteLLM's sibling exceptions disagree on
        positional order — ``Timeout(message, model, llm_provider)`` against
        ``RateLimitError(message, llm_provider, model)`` — so anything positional
        would silently mislabel the provider on half the corpus.
        """
        module = importlib.import_module(_MODULES[self.exception.module])
        exception_type = getattr(module, self.exception.type, None)
        if exception_type is None or not (
            isinstance(exception_type, type) and issubclass(exception_type, BaseException)
        ):
            raise AssertionError(
                f"fixture {self.id!r} names {self.exception.module}.{self.exception.type}, "
                f"which is not an exception class in this version of the library"
            )

        kwargs = dict(self.exception.kwargs)
        parameters = inspect.signature(exception_type.__init__).parameters

        if self.exception.http is not None:
            request = httpx.Request("POST", self.exception.http.url)
            if "response" in parameters:
                kwargs["response"] = httpx.Response(
                    self.exception.http.status_code, request=request
                )
            elif "request" in parameters:
                kwargs["request"] = request

        # openai's APIStatusError family takes `body` as a required keyword.
        if "body" in parameters and "body" not in kwargs:
            kwargs["body"] = None

        built = exception_type(**kwargs)
        assert isinstance(built, Exception)
        return built


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _load_corpus() -> tuple[tuple[ErrorFixture, ...], tuple[tuple[Path, str], ...]]:
    """Every fixture, plus every file that failed to parse.

    Failures are *returned* rather than raised: one malformed file must not stop
    the other eleven cases from running, or a typo in one fixture hides whatever
    regression the rest would have caught.
    """
    fixtures: list[ErrorFixture] = []
    failures: list[tuple[Path, str]] = []

    for path in sorted(_FIXTURE_ROOT.glob("*/*.json")):
        try:
            fixtures.append(ErrorFixture.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValidationError, ValueError, OSError) as exc:
            failures.append((path, str(exc)))

    return tuple(fixtures), tuple(failures)


_FIXTURES, _FAILURES = _load_corpus()


def _cases() -> list[Any]:
    return [pytest.param(fixture, id=fixture.id) for fixture in _FIXTURES]


# --------------------------------------------------------------------------
# Guards on the corpus itself. These must exist whether or not anything loaded:
# `parametrize` over an empty list yields zero tests and a green suite.
# --------------------------------------------------------------------------


def test_the_fixture_corpus_is_discoverable_and_populated() -> None:
    """A broken glob must fail here rather than pass silently with no cases."""
    assert _FIXTURE_ROOT.is_dir(), f"fixture root is missing: {_FIXTURE_ROOT}"
    assert len(_FIXTURES) >= _MINIMUM_FIXTURES


def test_every_named_provider_family_has_fixtures() -> None:
    """The three directories P2-T1 names. Deleting one is a visible failure."""
    assert {fixture.provider for fixture in _FIXTURES} == _EXPECTED_FAMILIES


@pytest.mark.parametrize(
    ("path", "error"),
    [pytest.param(p, e, id=p.name) for p, e in _FAILURES],
)
def test_every_fixture_file_parses(path: Path, error: str) -> None:
    pytest.fail(f"{path} did not parse as an ErrorFixture:\n{error}")


def test_fixture_ids_are_unique() -> None:
    """Duplicate ids make two parametrized cases indistinguishable in a report."""
    ids = [fixture.id for fixture in _FIXTURES]

    assert len(ids) == len(set(ids))


def test_every_error_class_appears_somewhere_in_the_corpus() -> None:
    """A taxonomy class with no fixture is a mapping nobody is checking.

    Same posture as the import-time completeness guard in
    ``keel/providers/errors.py``: the gap fails loudly rather than being noticed
    later from a flat dashboard panel.
    """
    covered = {fixture.expect.error_class for fixture in _FIXTURES}

    assert covered == set(ErrorClass), f"no fixture for: {sorted(set(ErrorClass) - covered)}"


@pytest.mark.parametrize("fixture", _cases())
def test_a_fixture_message_does_not_already_carry_the_litellm_prefix(
    fixture: ErrorFixture,
) -> None:
    """LiteLLM prepends ``litellm.<Class>: `` inside its own ``__init__``.

    A fixture that stores the *full* captured line therefore double-prefixes —
    and a ``message_contains`` assertion still passes, so the mistake is
    invisible without this test. Store the inner message.
    """
    message = str(fixture.exception.kwargs.get("message", ""))

    assert not message.startswith("litellm."), (
        f"fixture {fixture.id!r} stores a message that already carries LiteLLM's prefix; "
        f"store the inner message instead or it will be prefixed twice"
    )


# --------------------------------------------------------------------------
# The replay. This is what the task's "done when" asks for.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _cases())
def test_every_fixture_replays_to_its_expected_error_class(fixture: ErrorFixture) -> None:
    normalized = normalize_provider_error(fixture.build(), provider=fixture.provider)

    assert normalized.error_class is fixture.expect.error_class


@pytest.mark.parametrize("fixture", _cases())
def test_every_fixture_replays_to_its_expected_status_code(fixture: ErrorFixture) -> None:
    """Asserted separately so a status regression names itself.

    Folded into the class assertion it would surface as a confusing taxonomy
    failure instead of the `_status_code` change it actually is.
    """
    normalized = normalize_provider_error(fixture.build(), provider=fixture.provider)

    assert normalized.status_code == fixture.expect.status_code


@pytest.mark.parametrize("fixture", _cases())
def test_every_fixture_names_the_raw_exception_it_came_from(fixture: ErrorFixture) -> None:
    """Never the refined class — a log line must show what actually arrived."""
    normalized = normalize_provider_error(fixture.build(), provider=fixture.provider)

    assert normalized.provider_error_type == fixture.expect.provider_error_type
    assert fixture.expect.message_contains in normalized.message


# --------------------------------------------------------------------------
# The refinement invariant. This is the layer's entire safety argument.
# --------------------------------------------------------------------------


def test_refinement_never_overrides_a_specifically_typed_class() -> None:
    """A typed result wins over any message rule, however well the text matches.

    This is the regression the ``SERVER_ERROR``-only gate exists to prevent. A
    ``BadRequestError`` whose body happens to quote an auth message is still the
    client's fault, and turning it into an ``AUTH_FAILURE`` would move it across
    a D7 line on the strength of a substring.
    """
    from litellm import exceptions as lle

    exc = lle.BadRequestError(
        message='CohereException - {"message":"Incorrect API key provided: sk-x"}',
        model="command-a",
        llm_provider="cohere",
    )

    assert _classify_by_type(exc) is ErrorClass.BAD_REQUEST
    normalized = normalize_provider_error(exc, provider=AdapterName.COHERE)

    assert normalized.error_class is ErrorClass.BAD_REQUEST


def test_refinement_only_ever_upgrades_a_server_error() -> None:
    """The gate, asserted across the whole corpus rather than on one example."""
    moved = 0

    for fixture in _FIXTURES:
        exc = fixture.build()
        by_type = _classify_by_type(exc)
        refined = normalize_provider_error(exc, provider=fixture.provider).error_class

        if by_type is ErrorClass.SERVER_ERROR:
            moved += refined is not by_type
        else:
            assert refined is by_type, (
                f"{fixture.id}: refinement replaced {by_type.value} with {refined.value}; "
                f"only server_error may be refined"
            )

    # Without this the test would also pass on a corpus that never refines
    # anything, which would make the assertion above vacuous.
    assert moved, "no fixture exercises refinement at all"


def test_no_refinement_moves_a_breaker_excluded_class_into_a_breaker_counted_one() -> None:
    """The D7 property, asserted over the table rather than per rule.

    ``SERVER_ERROR -> AUTH_FAILURE`` — counted to excluded — is the whole point
    of this layer, so the allowed direction is "same or fewer". The forbidden
    one is the reverse: a rule that made an excluded class start counting would
    mean a typo'd API key could open a breaker, which is exactly what ADR 0004
    and D7 exist to prevent.
    """
    # The only class a rule may replace, so the only exposure it starts from.
    assert ErrorClass.SERVER_ERROR.counts_toward_breaker is True

    for provider, rules in _REFINEMENTS.items():
        for rule in rules:
            assert rule.to.counts_toward_breaker <= ErrorClass.SERVER_ERROR.counts_toward_breaker, (
                f"{provider.value}: a rule yielding {rule.to.value} would widen breaker "
                f"exposure rather than narrow it"
            )


def test_every_refinement_rule_is_exercised_by_at_least_one_fixture() -> None:
    """A rule no fixture reaches is a rule nobody can show still works.

    The strongest anti-rot guard here: without it, dead patterns accumulate
    quietly as providers reword their errors.
    """
    exercised = set()
    for fixture in _FIXTURES:
        exc = fixture.build()
        rule = matching_refinement(_classify_by_type(exc), exc, fixture.provider)
        if rule is not None:
            exercised.add(id(rule))

    declared = {id(rule) for rules in _REFINEMENTS.values() for rule in rules}

    assert exercised == declared, "some refinement rule has no fixture reaching it"


def test_refinement_is_a_no_op_without_a_provider() -> None:
    """``provider=None`` must be exactly the type table, for every fixture."""
    for fixture in _FIXTURES:
        exc = fixture.build()

        assert normalize_provider_error(exc).error_class is _classify_by_type(exc)


def test_the_mock_adapter_has_no_refinement_rules() -> None:
    """Mock failures are synthesized from a chosen class (ADR 0002).

    Reinterpreting their text would make the M2 load run exercise this table
    instead of the health window it exists to fill.
    """
    assert AdapterName.MOCK not in _REFINEMENTS


def test_the_bad_key_is_classed_out_of_the_breaker() -> None:
    """The P1-T7 bug, stated as the two facts that together make it a bug.

    Before P2-T1 this classed ``server_error``, which D7 counts — so a wrong API
    key would trip the Phase 3 breaker on a provider that was never unhealthy.
    """
    from litellm import exceptions as lle

    exc = lle.APIConnectionError(
        message='CohereException - {"message":"Incorrect API key provided: sk-x"}',
        llm_provider="cohere",
        model="command-a",
    )

    normalized = normalize_provider_error(exc, provider=AdapterName.COHERE)

    assert normalized.error_class is ErrorClass.AUTH_FAILURE
    assert normalized.counts_toward_breaker is False
    assert normalized.provider_error_type == "APIConnectionError"


def test_a_benign_server_error_is_not_refined() -> None:
    """The negative control: ordinary 500s must stay ordinary 500s."""
    from litellm import exceptions as lle

    for message in (
        "CohereException - internal server error",
        "CohereException - upstream connect error or disconnect/reset before headers",
        "CohereException - the api_key field is required",
    ):
        exc = lle.APIConnectionError(message=message, llm_provider="cohere", model="command-a")

        assert (
            normalize_provider_error(exc, provider=AdapterName.COHERE).error_class
            is ErrorClass.SERVER_ERROR
        ), f"a refinement pattern is loose enough to match: {message!r}"


# --------------------------------------------------------------------------
# Version-churn tripwires
# --------------------------------------------------------------------------


def test_the_whole_litellm_exception_catalogue_is_mapped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every class LiteLLM publishes must find a row without hitting the default.

    ``litellm.LITELLM_EXCEPTION_TYPES`` is the library's own list, so this fails
    the day an upgrade adds a type — which is the moment to decide what it means
    for the breaker, rather than discovering it from a WARNING in production.
    """
    import litellm

    caplog.set_level(logging.WARNING, logger="keel.providers.normalize")

    rows = _error_map()

    for exception_type in litellm.LITELLM_EXCEPTION_TYPES:
        found = any(issubclass(exception_type, mapped) for mapped, _ in rows)

        assert found, f"{exception_type.__name__} matches no row in the §5.4 map"

    assert caplog.text == ""


def test_importing_the_normalizer_does_not_import_litellm() -> None:
    """The 4.5s import stays lazy, and that is measured rather than trusted.

    Run in a subprocess because by the time this file executes, the fixtures
    above have already imported litellm into this interpreter.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import keel.providers.normalize, sys; print('litellm' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


# --------------------------------------------------------------------------
# The schema's own guards
# --------------------------------------------------------------------------


def test_a_fixture_that_claims_to_be_captured_must_be_verified() -> None:
    """The plan bullet's requirement, enforced rather than trusted."""
    payload = json.loads(
        (_FIXTURE_ROOT / "cohere" / "auth-incorrect-api-key.json").read_text(encoding="utf-8")
    )
    payload["verified"] = False

    with pytest.raises(ValidationError, match="contradicts verified"):
        ErrorFixture.model_validate(payload)


def test_an_unknown_fixture_key_is_rejected() -> None:
    """``extra="forbid"`` means a renamed field fails loudly, not silently."""
    payload = json.loads(
        (_FIXTURE_ROOT / "bedrock" / "throttling-exception.json").read_text(encoding="utf-8")
    )
    payload["expected_class"] = "rate_limit"

    with pytest.raises(ValidationError):
        ErrorFixture.model_validate(payload)


def test_at_least_one_fixture_is_a_real_capture() -> None:
    """Hand-written fixtures alone would only assert our own guesses."""
    assert any(fixture.verified for fixture in _FIXTURES)
