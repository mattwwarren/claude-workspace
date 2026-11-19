"""Doc conformance for the capability doctor rows (RFC 0019 H2b, #3744).

Replaces ``tests/test_all_codex_lane_docs.py`` (RFC 0018 D1, #3064) now that
the ``all-codex-lane`` and ``tracker-mcp`` checks are folded into
``harness-capabilities`` and ``review-convergence``. Keeps that file's
still-valid needles (the operator probe doc and the headless-contract
sentence), the all-codex lane prose that still holds, and pins the new check
names, the dogfood checklist, the renamed reasons and the one-release mention
of the retired names. Needles are matched after :func:`_norm`, so line
wrapping does not matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._shared.workflow_text import _norm

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "config" / "CONFIG_REFERENCE.md"
_RUNBOOK = _ROOT / "docs" / "dispatch-runbook.md"
_INSTALL = _ROOT / "docs" / "INSTALL.md"
_EVENTS = _ROOT / "docs" / "events.md"
_HEADLESS = _ROOT / "docs" / "headless-contract.md"
_PROBE_DOC = _ROOT / "docs" / "codex-live-session-smoke.md"

_PROBE_DESCRIPTION = (
    "Run one opt-in Codex CLI create/resume smoke test in an isolated temporary"
    " Git repository; this does not validate CW queue/executor integration."
)
_PROBE_INVOCATION = (
    "CW_CODEX_LIVE_SESSION_SMOKE=1 python scripts/probe_codex_live_session.py"
    " --model gpt-5.6-luna"
)
_RESULT_KEYS = (
    "status",
    "cli_version",
    "model",
    "session_id",
    "create",
    "resume",
    "error_code",
)


def _text(path: Path) -> str:
    return _norm(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "needle",
    [
        "All-codex lane (RFC 0018 D1, #3064)",
        "review.confirm_backend: none",
        "review.confirm_backend: codex",
        "`claude` binary on `PATH`",
        "confirm_backend_unavailable",
        "review-convergence/<client>/<lane>",
        "harness-capabilities/<client>/<lane>/<backend>",
        "harness-capabilities/<client>/<lane>/codex",
        "binding_unreachable",
        "binding_unbound",
        "binding_tool_missing",
        "Tracker-IO turn",
        "docs/codex-live-session-smoke.md",
        # Retired names stay mentioned for one release (#3744).
        "all-codex-lane/<client>/<lane>",
        "tracker-mcp/<client>/<lane>/codex",
    ],
)
def test_config_reference_documents_the_capability_rows(needle: str) -> None:
    assert _norm(needle) in _text(_CONFIG)


@pytest.mark.parametrize(
    "needle",
    [
        "All-codex lane (RFC 0018 D1, #3064)",
        "confirm_backend_unavailable",
        "`claude` binary on `PATH`",
        "Tracker-IO turn (#3057, RFC 0018 A2)",
        "review-convergence/<client>/<lane>",
        "harness-capabilities/<client>/<lane>/codex",
        "Capability dogfood checklist (RFC 0019 H2b, #3744)",
        "No `tracker-mcp/` or `all-codex-lane/` rows remain",
        "docs/codex-live-session-smoke.md",
        "all-codex-lane/<client>/<lane>",
        "tracker-mcp/<client>/<lane>/codex",
    ],
)
def test_dispatch_runbook_documents_the_capability_rows(needle: str) -> None:
    assert _norm(needle) in _text(_RUNBOOK)


def test_install_doc_lists_the_capability_row_and_the_retired_names() -> None:
    text = _text(_INSTALL)
    assert _norm("`[OK/FAIL] harness-capabilities/<client>/<lane>/<backend>`") in text
    assert _norm("`all-codex-lane/<client>/<lane>` — retired") in text
    assert _norm("`tracker-mcp/<client>/<lane>/<backend>`") in text


@pytest.mark.parametrize(
    "needle",
    [
        '`"binding_unreachable"`, `"binding_unbound"`, `"binding_tool_missing"`',
        '`need` (`"tickets"`',
        "; missing_tools=<a,b>",
        "binding_blocked",
        '`"tracker_mcp_gate"` — the legacy name',
    ],
)
def test_events_doc_documents_the_binding_parks(needle: str) -> None:
    assert _norm(needle) in _text(_EVENTS)


def test_headless_contract_lists_the_binding_aliases() -> None:
    text = _text(_HEADLESS)
    assert _norm("| `binding_unreachable` |") in text
    assert _norm("| `binding_blocked` |") in text


def test_headless_contract_states_no_codex_fails_closed() -> None:
    assert _norm("No codex cell is `fails-closed`") in _text(_HEADLESS)


def test_probe_doc_pins_the_exact_description_and_invocation() -> None:
    raw = _PROBE_DOC.read_text(encoding="utf-8")
    assert _PROBE_DESCRIPTION in raw
    assert _PROBE_INVOCATION in raw


@pytest.mark.parametrize("key", _RESULT_KEYS)
def test_probe_doc_names_every_result_key(key: str) -> None:
    assert f"`{key}`" in _PROBE_DOC.read_text(encoding="utf-8")


def test_probe_doc_discloses_codex_home_and_the_persistent_thread() -> None:
    text = _text(_PROBE_DOC)
    assert _norm("leaves one normal persistent thread in `CODEX_HOME`") in text
    assert (
        _norm("only its printed id is reported") in text
        or _norm("The script prints only the thread id") in text
    )


def test_probe_doc_states_it_never_runs_in_default_ci() -> None:
    assert _norm("It never runs in default CI.") in _text(_PROBE_DOC)
