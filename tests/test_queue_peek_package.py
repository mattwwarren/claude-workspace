"""Seam assertions for the ``cw.queue_peek`` package split (#2793).

The flat ``cw/queue_peek.py`` passed CLAUDE.md's ~1000-line module ceiling.
#2793 moves its code verbatim into five submodules (``locate``, ``parse``,
``ladder``, ``rows``, ``render``) behind a re-exporting ``__init__``.

The behavioral suite cannot see four things the move must keep true:

1. The surface survives: every name reachable as ``cw.queue_peek.<name>``
   before the split still resolves there and is listed in ``__all__``.
2. Each name is *defined in* its owning submodule, and the package attribute
   is that object, so a patch on the owner reaches the real call site.
3. The patch seams live where the tests patch them: ``CW_STATE`` /
   ``CLAUDE_PROJECTS`` are read by ``locate``; ``rows`` binds the names
   ``build_peek_rows`` / ``format_row`` / ``gh_pr_state`` look up. A patch on
   the package re-export silently stops intercepting.
4. The package stays split: every submodule is under the size flag and
   ``__init__`` is a pure re-export.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import inspect
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import cw.queue_peek
from cw.models import QueueItemStatus
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from types import ModuleType

_LOCATE = "cw.queue_peek.locate"
_PARSE = "cw.queue_peek.parse"
_LADDER = "cw.queue_peek.ladder"
_ROWS = "cw.queue_peek.rows"
_RENDER = "cw.queue_peek.render"

_PACKAGE_DIR = Path(cw.queue_peek.__file__).parent

# Owning submodule for every name on the package surface.
_OWNER: dict[str, str] = {
    "CLAUDE_PROJECTS": _LOCATE,
    "CW_STATE": _LOCATE,
    "_EPOCH": _LOCATE,
    "_load_session_refs": _LOCATE,
    "_is_known_idless_session": _LOCATE,
    "load_claude_session_id": _LOCATE,
    "_parse_started_at": _LOCATE,
    "_find_transcript_in_project_dir": _LOCATE,
    "_matching_project_dirs": _LOCATE,
    "_find_transcript_heuristic": _LOCATE,
    "_compute_jsonl_idle_min": _LOCATE,
    "find_transcript_for_ticket": _LOCATE,
    "_newest_subagent_ts": _PARSE,
    "_scan_assistant_content": _PARSE,
    "_tool_result_pr_number": _PARSE,
    "parse_transcript": _PARSE,
    "parse_opencode_transcript": _PARSE,
    "WAIT_AGE_MIN": _LADDER,
    "PEEK_AGE_MIN": _LADDER,
    "STOP_AGE_MIN": _LADDER,
    "STOP_CEILING_MIN": _LADDER,
    "IDLE_PEEK_MIN": _LADDER,
    "IDLE_STALL_MIN": _LADDER,
    "IDLE_POST_PR_MIN": _LADDER,
    "STOP_UNPRODUCTIVE_ATTEMPTS_MIN": _LADDER,
    "IDLE_LIVE_MAX_MIN": _LADDER,
    "_STAGE_ORDER": _LADDER,
    "_reached_deep_stage": _LADDER,
    "_stage5_rec": _LADDER,
    "_stall_check": _LADDER,
    "_score_session": _LADDER,
    "_gate_stop_on_liveness": _LADDER,
    "recommend": _LADDER,
    "RECOMMEND_BLIND": _ROWS,
    "RECOMMEND_AWAITING_OPERATOR": _ROWS,
    "_AWAITING_OPERATOR_FALLBACK_REASON": _ROWS,
    "_BLIND_REASON_NEVER_STARTED": _ROWS,
    "_BLIND_REASON_LOCAL_HANDLE": _ROWS,
    "_SIGNAL_SOURCE_BLIND": _ROWS,
    "_SIGNAL_SOURCE_TRANSCRIPT": _ROWS,
    "_SIGNAL_SOURCE_PARKED": _ROWS,
    "_PEEK_STATUSES": _ROWS,
    "load_attention_tasks": _ROWS,
    "gh_pr_state": _ROWS,
    "minutes_since": _ROWS,
    "_awaiting_operator_reason": _ROWS,
    "_awaiting_operator_row": _ROWS,
    "format_row": _ROWS,
    "build_peek_rows": _ROWS,
    "print_table": _RENDER,
}

# The package surface: every name the flat module defined at top level.
_SURFACE = frozenset(_OWNER)

# Module-level constants carry no ``__module__``; ownership is checked by
# membership in the owner's namespace instead.
_CONSTANTS = frozenset(
    {
        "CLAUDE_PROJECTS",
        "CW_STATE",
        "_EPOCH",
        "WAIT_AGE_MIN",
        "PEEK_AGE_MIN",
        "STOP_AGE_MIN",
        "STOP_CEILING_MIN",
        "IDLE_PEEK_MIN",
        "IDLE_STALL_MIN",
        "IDLE_POST_PR_MIN",
        "STOP_UNPRODUCTIVE_ATTEMPTS_MIN",
        "IDLE_LIVE_MAX_MIN",
        "_STAGE_ORDER",
        "RECOMMEND_BLIND",
        "RECOMMEND_AWAITING_OPERATOR",
        "_AWAITING_OPERATOR_FALLBACK_REASON",
        "_BLIND_REASON_NEVER_STARTED",
        "_BLIND_REASON_LOCAL_HANDLE",
        "_SIGNAL_SOURCE_BLIND",
        "_SIGNAL_SOURCE_TRANSCRIPT",
        "_SIGNAL_SOURCE_PARKED",
        "_PEEK_STATUSES",
    }
)

# (patched name, consumer module that must bind it, reader functions in that
# consumer whose globals the patch has to reach). Tests patch the name on the
# consumer, never on the package.
_SEAMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("CW_STATE", _LOCATE, ("_load_session_refs",)),
    ("CLAUDE_PROJECTS", _LOCATE, ("_matching_project_dirs",)),
    ("gh_pr_state", _ROWS, ("format_row",)),
    ("_fetch_pr_state", _ROWS, ("gh_pr_state",)),
    ("list_tickets", _ROWS, ("load_attention_tasks",)),
    ("load_attention_tasks", _ROWS, ("build_peek_rows",)),
    ("find_transcript_for_ticket", _ROWS, ("build_peek_rows",)),
    ("_compute_jsonl_idle_min", _ROWS, ("build_peek_rows",)),
    ("_load_session_refs", _ROWS, ("build_peek_rows",)),
    ("parse_transcript", _ROWS, ("build_peek_rows",)),
    ("convergence_peek_override", _ROWS, ("format_row",)),
)

#: CLAUDE.md's "Module Size" yellow flag; the flat module was 1012 lines.
_MODULE_LINE_LIMIT = 800


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def _package_files() -> list[Path]:
    return (
        sorted(_PACKAGE_DIR.glob("*.py")) if _PACKAGE_DIR.name == "queue_peek" else []
    )


# ---------------------------------------------------------------------------
# Surface: green on the flat module, stays green after the split
# ---------------------------------------------------------------------------


def test_owner_table_covers_constants() -> None:
    """The owner and constant tables describe the same names."""
    assert len(_OWNER) == 50
    assert _CONSTANTS.issubset(_OWNER)


@pytest.mark.parametrize("name", sorted(_SURFACE))
def test_surface_name_resolves_on_the_package(name: str) -> None:
    """Every pre-split name is still reachable as ``cw.queue_peek.<name>``."""
    assert hasattr(cw.queue_peek, name)


# ---------------------------------------------------------------------------
# Ownership: red until the split lands
# ---------------------------------------------------------------------------


def test_package_all_is_the_pinned_surface() -> None:
    """``__all__`` lists exactly the surface names, no more and no fewer."""
    assert sorted(getattr(cw.queue_peek, "__all__", ())) == sorted(_SURFACE)


@pytest.mark.parametrize("name", sorted(_SURFACE))
def test_package_attribute_is_the_owner_object(name: str) -> None:
    """The package attribute is the owner's object, not a copy."""
    assert getattr(cw.queue_peek, name) is vars(_owner(name))[name]


@pytest.mark.parametrize("name", sorted(_SURFACE - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions report their owning submodule as ``__module__``."""
    obj = vars(_owner(name))[name]
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_bound_in_its_owner(name: str) -> None:
    """Constants live in their owner's namespace."""
    assert name in vars(_owner(name))


@pytest.mark.parametrize(
    ("seam", "consumer", "readers"), _SEAMS, ids=[seam for seam, _, _ in _SEAMS]
)
def test_patch_seam_is_bound_where_it_is_read(
    seam: str, consumer: str, readers: tuple[str, ...]
) -> None:
    """The consumer binds the seam and its readers look it up there."""
    module = importlib.import_module(consumer)
    assert seam in vars(module)
    for reader in readers:
        fn = vars(module)[reader]
        assert inspect.isfunction(fn)
        assert fn.__globals__ is vars(module)


def test_format_row_calls_the_rows_convergence_override() -> None:
    """A patch on ``rows.convergence_peek_override`` reaches ``format_row``."""
    task = _make_ticket_task(ticket_id="T-1", status=QueueItemStatus.RUNNING)
    now = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
    with patch(
        "cw.queue_peek.rows.convergence_peek_override",
        return_value=("WAIT", "pinned"),
    ) as override:
        row = cw.queue_peek.format_row(task, {}, now)
    override.assert_called_once()
    assert row["reason"] == "pinned"


# ---------------------------------------------------------------------------
# Final-state guards: module size and a pure ``__init__``
# ---------------------------------------------------------------------------


def test_package_files_are_under_the_module_size_flag() -> None:
    """``cw.queue_peek`` is a package and no submodule nears the ceiling."""
    files = _package_files()
    assert files, "cw.queue_peek is not a package"
    for path in files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count < _MODULE_LINE_LIMIT, path.name


def test_package_init_is_a_pure_reexport() -> None:
    """``__init__`` binds only re-exports, its submodules and ``annotations``."""
    files = _package_files()
    assert files, "cw.queue_peek is not a package"
    tree = ast.parse((_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        )
    ]
    bound = {name for name in vars(cw.queue_peek) if not name.startswith("__")}
    submodules = {path.stem for path in files} - {"__init__"}
    # ``annotations`` is the ``from __future__ import annotations`` binding.
    assert bound == set(_SURFACE) | submodules | {"annotations"}
