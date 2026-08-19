"""Capture real Cohere error fixtures for the P2-T1 replay corpus.

Run deliberately; **nothing in CI runs this** (NFR-2, and the §5 tripwire caps
Cohere spend at EUR 10 across Phases 1-2). It provokes a small set of failures
against the live API and writes each one to
``tests/fixtures/providers/cohere/`` in the schema
``tests/test_provider_normalize.py`` replays, marked ``"verified": true``.

    export COHERE_API_KEY=...
    python scripts/capture_error_fixtures.py            # show what would be written
    python scripts/capture_error_fixtures.py --write    # write the files

Every probe here is a *failure* by construction, so the spend is a handful of
rejected requests rather than completions. The bad-key probe costs nothing at
all — it never authenticates.

**Why this exists rather than a test.** A green replay suite proves the mapping
still turns known text into a known class; it can never prove Cohere still emits
that text, because the fixtures are frozen strings. Re-running this at a phase
gate is the only thing that checks the other direction. Fixtures it overwrites
should be reviewed in the diff, not accepted blind: a changed message is exactly
the signal worth noticing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
_FIXTURE_DIR: Final = _REPO_ROOT / "tests" / "fixtures" / "providers" / "cohere"
_MODEL: Final = "command-a"

# `litellm.__version__` does not exist; the distribution metadata is the only
# way to record what a capture was true for.
_LIBRARIES: Final = ("litellm", "openai")


@dataclass(frozen=True, slots=True)
class Probe:
    """One deliberate way to make Cohere fail."""

    name: str
    note: str
    api_key: str | None
    payload: Mapping[str, Any]


def _probes(api_key: str) -> tuple[Probe, ...]:
    return (
        Probe(
            name="auth-incorrect-api-key",
            note=(
                "A deliberately bogus COHERE_API_KEY. LiteLLM's _map_cohere_exception has "
                "no 401 branch, so this arrives as APIConnectionError with a synthetic "
                "status 500 rather than as AuthenticationError; the type table alone "
                "yields server_error, which D7 counts toward the breaker. This is the bug "
                "P2-T1 exists to fix."
            ),
            api_key="sk-deliberately-invalid-key",
            payload={"messages": [{"role": "user", "content": "hi"}]},
        ),
        Probe(
            name="not-found-unknown-model",
            note="A model string Cohere does not serve.",
            api_key=api_key,
            payload={"messages": [{"role": "user", "content": "hi"}]},
        ),
        Probe(
            name="context-window-exceeded",
            note=(
                "A prompt past the model's context window. Costs nothing to serve: "
                "Cohere rejects it before generating."
            ),
            api_key=api_key,
            payload={"messages": [{"role": "user", "content": "word " * 400_000}]},
        ),
    )


async def _capture(probe: Probe, *, model: str) -> dict[str, Any] | None:
    """Run one probe and render the failure as a fixture, or ``None`` if it succeeded."""
    import litellm

    try:
        await litellm.acompletion(
            model=f"cohere/{model}",
            api_key=probe.api_key,
            stream=False,
            **probe.payload,
        )
    except Exception as exc:  # noqa: BLE001 - capturing whatever the provider does is the point
        return _as_fixture(probe, exc, model=model)

    return None


def _inner_message(exc: Exception) -> str:
    """Strip the ``litellm.<Class>: `` prefix LiteLLM adds in its own ``__init__``.

    Storing the full line would double-prefix on replay, and a
    ``message_contains`` assertion would still pass — so the mistake would be
    invisible. ``tests/test_provider_normalize.py`` guards against it too; this
    is the half that stops it being written in the first place.
    """
    text = str(exc)
    prefix = f"litellm.{type(exc).__name__}: "
    return text[len(prefix) :] if text.startswith(prefix) else text


def _as_fixture(probe: Probe, exc: Exception, *, model: str) -> dict[str, Any]:
    from keel.config import AdapterName
    from keel.providers.normalize import normalize_provider_error

    normalized = normalize_provider_error(exc, provider=AdapterName.COHERE)
    message = _inner_message(exc)

    return {
        "id": f"cohere-{probe.name}",
        "provider": "cohere",
        "source": "captured",
        "verified": True,
        "captured_at": date.today().isoformat(),
        "captured_with": {name: version(name) for name in _LIBRARIES},
        "note": probe.note,
        "exception": {
            "module": "litellm",
            "type": type(exc).__name__,
            "kwargs": {"message": message, "llm_provider": "cohere", "model": model},
        },
        "expect": {
            # Recorded as observed. If a capture disagrees with the committed
            # fixture, that is the finding — do not paper over it in the diff.
            "error_class": normalized.error_class.value,
            "status_code": normalized.status_code,
            "provider_error_type": normalized.provider_error_type,
            "message_contains": message[:40],
        },
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write files instead of printing")
    parser.add_argument("--model", default=_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("COHERE_API_KEY", "").strip()
    if not api_key:
        print("COHERE_API_KEY is unset or blank; nothing to capture.", file=sys.stderr)
        return 1

    for probe in _probes(api_key):
        model = "no-such-model-v9" if probe.name == "not-found-unknown-model" else args.model
        fixture = await _capture(probe, model=model)
        if fixture is None:
            print(f"{probe.name}: SUCCEEDED unexpectedly - no error to capture", file=sys.stderr)
            continue

        rendered = json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"
        if args.write:
            path = _FIXTURE_DIR / f"{probe.name}.json"
            path.write_text(rendered, encoding="utf-8")
            print(f"wrote {path}")
        else:
            print(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
