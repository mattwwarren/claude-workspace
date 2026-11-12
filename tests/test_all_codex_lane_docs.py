"""Doc conformance for the all-codex lane (RFC 0018 D1, #3064).

Pins the prose that carries the contract: the config reference section, the
dispatch runbook section, the install doctor line, the headless-contract
sentence, and the operator probe doc (its exact one-line description, invocation,
result keys and disclosure). Needles are matched after :func:`_norm`, so line
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
        "all-codex-lane/<client>/<lane>",
        "review.confirm_backend: none",
        "review.confirm_backend: codex",
        "`claude` binary on `PATH`",
        "confirm_backend_unavailable",
        "tracker_mcp_gate.servers.codex",
        "tracker-mcp/<client>/<lane>/codex",
        "Tracker-IO turn",
        "docs/codex-live-session-smoke.md",
    ],
)
def test_config_reference_documents_the_all_codex_lane(needle: str) -> None:
    assert _norm(needle) in _text(_CONFIG)


@pytest.mark.parametrize(
    "needle",
    [
        "All-codex lane (RFC 0018 D1, #3064)",
        "all-codex-lane/<client>/<lane>",
        "confirm_backend_unavailable",
        "`claude` binary on `PATH`",
        "Tracker-IO turn (#3057, RFC 0018 A2)",
        "tracker-mcp/<client>/<lane>/codex",
        "docs/codex-live-session-smoke.md",
    ],
)
def test_dispatch_runbook_documents_the_all_codex_lane(needle: str) -> None:
    assert _norm(needle) in _text(_RUNBOOK)


def test_install_doc_lists_the_all_codex_lane_check() -> None:
    text = _text(_INSTALL)
    assert _norm("`[OK/WARN] all-codex-lane/<client>/<lane>`") in text
    assert _norm("`tracker-mcp/<client>/<lane>/codex`") in text


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
