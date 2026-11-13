"""Tests for the shared session-event payload vocabulary (#3124).

NEW file, 1:1 with ``src/cw/session_event_vocabulary.py``. The module is the one
home for four payload literals that the reconcile/CLI producers write and the
audit-only ``cw.session_outcomes`` reader matches. Fixture inventory: autouse
``tmp_config_dir`` + ``_isolate_home`` only; producer modules are parsed with
``ast`` from the source tree, nothing is written.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cw.models import OrchestratorEvent, OrchestratorEventType
from cw.reconcile._shared._constants import (
    _CAUSE_USAGE_LIMIT,
    _PHANTOM_REAP_MERGED_REASON,
    _USAGE_LIMITED_MID_TURN_REASON,
)
from cw.reconcile.session_close import record_operator_close
from cw.session_event_vocabulary import (
    CAUSE_USAGE_LIMIT_CUTOFF,
    OPERATOR_CLOSE_DETAIL,
    PHANTOM_REAP_MERGED_REASON,
    USAGE_LIMITED_MID_TURN,
)
from cw.session_outcomes import NoResultCause, classify_sessions

_SRC = Path(__file__).resolve().parent.parent / "src" / "cw"
_VOCAB_MODULE = "cw.session_event_vocabulary"
_WIRE_VALUES = frozenset(
    {
        "usage_limited_mid_turn",
        "usage_limit_cutoff",
        "phantom_reap_merged",
        "operator_close",
    }
)

# Producer module -> vocabulary names it must import.
_PRODUCERS: dict[str, frozenset[str]] = {
    "reconcile/_shared/_constants.py": frozenset(
        {
            "USAGE_LIMITED_MID_TURN",
            "CAUSE_USAGE_LIMIT_CUTOFF",
            "PHANTOM_REAP_MERGED_REASON",
        }
    ),
    "cli/spawn.py": frozenset({"OPERATOR_CLOSE_DETAIL"}),
    "reconcile/session_close.py": frozenset({"OPERATOR_CLOSE_DETAIL"}),
}

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _parse(relative: str) -> ast.Module:
    return ast.parse((_SRC / relative).read_text(encoding="utf-8"))


def _vocab_imports(tree: ast.Module) -> list[ast.ImportFrom]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _VOCAB_MODULE
    ]


def _imported_names(tree: ast.Module) -> dict[str, str | None]:
    return {
        alias.name: alias.asname
        for node in _vocab_imports(tree)
        for alias in node.names
    }


def test_wire_values_are_pinned() -> None:
    assert USAGE_LIMITED_MID_TURN == "usage_limited_mid_turn"
    assert CAUSE_USAGE_LIMIT_CUTOFF == "usage_limit_cutoff"
    assert PHANTOM_REAP_MERGED_REASON == "phantom_reap_merged"
    assert OPERATOR_CLOSE_DETAIL == "operator_close"


def test_reconcile_private_names_alias_the_vocabulary() -> None:
    assert _USAGE_LIMITED_MID_TURN_REASON == USAGE_LIMITED_MID_TURN
    assert _PHANTOM_REAP_MERGED_REASON == PHANTOM_REAP_MERGED_REASON
    assert _CAUSE_USAGE_LIMIT == CAUSE_USAGE_LIMIT_CUTOFF


def test_record_operator_close_default_detail_is_the_vocabulary_constant() -> None:
    default = inspect.signature(record_operator_close).parameters["detail"].default
    assert default == OPERATOR_CLOSE_DETAIL


def test_cli_spawn_imports_operator_close_detail_without_alias() -> None:
    names = _imported_names(_parse("cli/spawn.py"))
    assert "OPERATOR_CLOSE_DETAIL" in names
    assert names["OPERATOR_CLOSE_DETAIL"] is None


@pytest.mark.parametrize(("relative", "required"), sorted(_PRODUCERS.items()))
def test_producer_imports_and_does_not_redeclare(
    relative: str, required: frozenset[str]
) -> None:
    """Equal strings intern, so only the AST can see a re-declared literal."""
    tree = _parse(relative)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not literals & _WIRE_VALUES
    assert required <= set(_imported_names(tree))


def _module_level_strings(tree: ast.Module) -> set[str]:
    values: set[str] = set()
    for node in tree.body:
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.add(value.value)
    return values


def test_session_outcomes_keeps_the_audit_only_rule() -> None:
    tree = _parse("session_outcomes.py")
    modules = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert not [m for m in modules if m.startswith(("cw.reconcile", "cw.dispatch"))]
    assert _vocab_imports(tree)
    assert not _module_level_strings(tree) & _WIRE_VALUES


def _ev(etype: OrchestratorEventType, **payload: Any) -> OrchestratorEvent:
    return OrchestratorEvent(
        type=etype,
        payload={"session_id": "s1", **payload},
        created_at=T0 + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    ("expected", "evidence"),
    [
        pytest.param(
            NoResultCause.USAGE_LIMIT,
            _ev(OrchestratorEventType.SESSION_COMPLETED, reason=USAGE_LIMITED_MID_TURN),
            id="completed-reason",
        ),
        pytest.param(
            NoResultCause.USAGE_LIMIT,
            _ev(
                OrchestratorEventType.SESSION_TIMED_OUT, cause=CAUSE_USAGE_LIMIT_CUTOFF
            ),
            id="timed-out-cause",
        ),
        pytest.param(
            NoResultCause.USAGE_LIMIT,
            _ev(
                OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                paused_status=USAGE_LIMITED_MID_TURN,
            ),
            id="needs-attention-paused-status",
        ),
        pytest.param(
            NoResultCause.PHANTOM,
            _ev(
                OrchestratorEventType.SESSION_COMPLETED,
                reason=PHANTOM_REAP_MERGED_REASON,
            ),
            id="phantom-reap-merged",
        ),
    ],
)
def test_classifier_reads_the_producer_constants(
    expected: NoResultCause, evidence: OrchestratorEvent
) -> None:
    spawn = OrchestratorEvent(
        type=OrchestratorEventType.SESSION_SPAWNED,
        payload={"session_id": "s1", "ticket_id": "T-1", "client": "acme"},
        created_at=T0,
    )
    report = classify_sessions(
        [spawn, evidence],
        operator_closed_ids=frozenset(),
        live_session_ids=frozenset(),
        window_start=T0 - timedelta(days=1),
        window_end=T0 + timedelta(days=1),
        samples=0,
    )
    assert {b.cause: b.count for b in report.buckets if b.count} == {expected: 1}
