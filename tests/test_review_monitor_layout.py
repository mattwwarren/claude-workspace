"""Layout and dependency-direction contract for ``review_monitor_lib`` (#2499).

``.claude/scripts/review_monitor.py`` is a thin entry point over the sibling
``review_monitor_lib`` package. These tests pin the shape of that split so a
later edit cannot quietly undo it:

- every module stays under the 800-line ceiling, the entry point stays a thin
  shim, and ``__init__.py`` re-exports nothing;
- ``EXPECTED_DEFS`` is a hardcoded name-to-module table of all 123 top-level
  definitions (deliberately not re-derived from the package, in the style of
  ``tests/test_review_findings_reexports.py``), so a missing, duplicated or
  misplaced name fails;
- ``ALLOWED_EDGES`` is the AST-derived dependency table from the plan: each
  module's set of imported sibling modules must EQUAL its entry (extra edges,
  missing edges and dead imports all fail), every imported name must be
  defined by the module it is imported from, and the graph must be acyclic;
- annotation-only imports (``TYPE_CHECKING_IMPORTS``) sit under
  ``if TYPE_CHECKING:`` and nothing else does;
- neither the package nor the shim carries a ``# noqa`` or ``# type: ignore``
  (the one pre-split ``noqa: S108`` on ``PENDING_INBOX_DIR`` was removed by
  composing the path; ``tests/test_review_monitor_inbox.py`` pins its value).
"""

from __future__ import annotations

import ast
import graphlib
import io
import tokenize
from pathlib import Path

import pytest

from tests import _review_monitor_helpers as helpers

PACKAGE_DIR = helpers.SCRIPTS_DIR / helpers.LIB_PACKAGE
MODULE_LINE_CEILING = 800
SHIM_LINE_CEILING = 60

EXPECTED_DEFS: dict[str, str] = {
    # models
    "ThreadStatus": "models",
    "CommentReviewRef": "models",
    "MonitoredPR": "models",
    "MonitorState": "models",
    # shell
    "_run_gh": "shell",
    "_run_git": "shell",
    "_get_our_username": "shell",
    # state
    "CENTRAL_STATE_DIR": "state",
    "LEGACY_STATE_FILE": "state",
    "state_path_for_repo": "state",
    "_merge_states": "state",
    "_load_json_state": "state",
    "load_state": "state",
    "save_state": "state",
    "cmd_list_repos": "state",
    # threads
    "DEFERRAL_PATTERNS": "threads",
    "is_deferral": "threads",
    "_extract_login": "threads",
    "_discover_author_threads": "threads",
    "_collect_deferred_threads_for_followup": "threads",
    "_update_thread_status": "threads",
    "_apply_status_transitions": "threads",
    "_refresh_threads": "threads",
    # delta
    "CODE_CHANGE_WINDOW": "delta",
    "check_code_changed": "delta",
    "parse_diff_changed_lines": "delta",
    "_apply_code_changes": "delta",
    "_detect_touched_threads": "delta",
    # comment_reviews
    "BOT_LOGIN_SUFFIXES": "comment_reviews",
    "KNOWN_BOT_LOGINS": "comment_reviews",
    "MERGE_BLOCKING_BOT_LOGINS": "comment_reviews",
    "is_bot_login": "comment_reviews",
    "_refresh_comment_reviews": "comment_reviews",
    "_fetch_inline_comment_bodies_by_review": "comment_reviews",
    "_collect_new_comment_reviews": "comment_reviews",
    "_has_engaged_human_reviewer": "comment_reviews",
    "_collect_pending_comment_reviews": "comment_reviews",
    "cmd_mark_comment_review": "comment_reviews",
    # attention
    "IMMEDIATE_ESCALATION_STATES": "attention",
    "USER_ATTENTION_STATES": "attention",
    "ESCALATION_GRACE": "attention",
    "_FAILED_CHECKRUN_CONCLUSIONS": "attention",
    "_PENDING_CHECKRUN_STATUSES": "attention",
    "_summarize_status_checks": "attention",
    "_resolve_change_request_source": "attention",
    "_compute_attention_state": "attention",
    "_compute_needs_escalation": "attention",
    # escalation
    "BUSINESS_TZ": "escalation",
    "BUSINESS_START_HOUR": "escalation",
    "BUSINESS_END_HOUR": "escalation",
    "FIRST_WEEKEND_WEEKDAY": "escalation",
    "STALE_REVIEW_THRESHOLD_MIN": "escalation",
    "CHANNEL_BUMP_COOLDOWN": "escalation",
    "AUTO_FIX_DAILY_CAP": "escalation",
    "DM_ESCALATION_COOLDOWN": "escalation",
    "_today_utc_str": "escalation",
    "_business_minutes_between": "escalation",
    "_ensure_state_entered_at": "escalation",
    "_reset_auto_fix_counter_if_stale": "escalation",
    "_auto_fix_attempts_today": "escalation",
    "_compute_auto_fix_ok": "escalation",
    "_auto_fix_already_addressed_state": "escalation",
    "_business_minutes_in_state": "escalation",
    "_needs_channel_bump": "escalation",
    "_dm_escalation_reason": "escalation",
    # lifecycle
    "CANONICAL_REPO_PATHS": "lifecycle",
    "CANONICAL_REPO_PATHS_ENV": "lifecycle",
    "_canonical_repo_paths_override": "lifecycle",
    "_canonical_repo_path": "lifecycle",
    "_normalize_thread_ids": "lifecycle",
    "cmd_register": "lifecycle",
    "cmd_ack_delta": "lifecycle",
    "cmd_drop": "lifecycle",
    "cmd_complete": "lifecycle",
    "cmd_set_status": "lifecycle",
    "cmd_confirm_thread": "lifecycle",
    "cmd_update_slack_cursor": "lifecycle",
    "cmd_slack_thread_cursor": "lifecycle",
    # notify
    "NUDGE_COOLDOWN": "notify",
    "_nudge_activity_check": "notify",
    "cmd_nudge_ok": "notify",
    "cmd_record_nudge": "notify",
    "cmd_mark_notified": "notify",
    "cmd_mark_escalated": "notify",
    "cmd_record_auto_fix": "notify",
    "cmd_record_channel_bump": "notify",
    "cmd_pending_channel_bumps": "notify",
    "cmd_catchup": "notify",
    # inbox
    "PENDING_INBOX_DIR": "inbox",
    "PENDING_STALE_AFTER": "inbox",
    "DESKTOP_QUEUE_DIR": "inbox",
    "DESKTOP_ACTION_TYPES": "inbox",
    "_PER_PR_ACTIONS": "inbox",
    "cmd_consume_pending": "inbox",
    "_file_is_stale": "inbox",
    "_desktop_queue_filename": "inbox",
    "cmd_enqueue_action": "inbox",
    # discovery
    "cmd_discover": "discovery",
    "_search_reviewed_prs": "discovery",
    "_our_reviews": "discovery",
    "_our_unresolved_threads": "discovery",
    "_recover_one_review": "discovery",
    "cmd_recover_reviews": "discovery",
    # status
    "cmd_status": "status",
    "cmd_status_all": "status",
    # check
    "_BLOCKING_MERGE_STATES": "check",
    "_complete_terminal_pr": "check",
    "_derive_check_signals": "check",
    "cmd_check": "check",
    # cli
    "_add_lifecycle_subparsers": "cli",
    "_add_signal_subparsers": "cli",
    "_add_query_subparsers": "cli",
    "_build_argument_parser": "cli",
    "_dispatch_status_all": "cli",
    "_dispatch_pr_state_mutation": "cli",
    "_dispatch_discovery": "cli",
    "_emit_mutation_result": "cli",
    "_dispatch_mutation": "cli",
    "_MUTATION_COMMANDS": "cli",
    "_QUERY_COMMANDS": "cli",
    "_print_repo_list": "cli",
    "_dispatch_status_command": "cli",
    "main": "cli",
}

ALLOWED_EDGES: dict[str, frozenset[str]] = {
    "models": frozenset(),
    "shell": frozenset(),
    "state": frozenset({"models"}),
    "threads": frozenset({"models", "shell"}),
    "delta": frozenset({"models", "shell"}),
    "attention": frozenset({"models"}),
    "escalation": frozenset({"models"}),
    "comment_reviews": frozenset({"models", "shell", "state"}),
    "lifecycle": frozenset({"models", "state", "threads"}),
    "notify": frozenset({"attention", "escalation", "shell", "state"}),
    "status": frozenset({"models", "state"}),
    "inbox": frozenset({"lifecycle"}),
    "discovery": frozenset({"lifecycle", "shell", "state", "threads"}),
    "check": frozenset(
        {
            "attention",
            "comment_reviews",
            "delta",
            "escalation",
            "models",
            "shell",
            "state",
            "threads",
        }
    ),
    "cli": frozenset(
        {
            "check",
            "comment_reviews",
            "discovery",
            "inbox",
            "lifecycle",
            "notify",
            "shell",
            "state",
            "status",
        }
    ),
}

# Package imports used only in annotations: (module, imported module, name).
TYPE_CHECKING_IMPORTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("threads", "models", "MonitoredPR"),
        ("delta", "models", "MonitoredPR"),
        ("attention", "models", "MonitoredPR"),
        ("escalation", "models", "MonitoredPR"),
        ("comment_reviews", "models", "MonitoredPR"),
        ("check", "models", "MonitoredPR"),
        ("check", "models", "MonitorState"),
    }
)

_PACKAGE_PREFIX = f"{helpers.LIB_PACKAGE}."


def _modules() -> dict[str, Path]:
    return {path.stem: path for path in sorted(PACKAGE_DIR.glob("*.py"))}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        return target.id if isinstance(target, ast.Name) else None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _package_imports(tree: ast.Module) -> list[tuple[str, str, bool]]:
    """Return (imported module, name, under TYPE_CHECKING) for package imports."""
    found: list[tuple[str, str, bool]] = []
    for node in tree.body:
        is_type_checking = (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        )
        statements = node.body if isinstance(node, ast.If) else [node]
        for stmt in statements:
            if isinstance(stmt, ast.ImportFrom) and (stmt.module or "").startswith(
                _PACKAGE_PREFIX
            ):
                module = (stmt.module or "").removeprefix(_PACKAGE_PREFIX)
                found.extend(
                    (module, alias.name, is_type_checking) for alias in stmt.names
                )
    return found


def _suppressions(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [
        f"{path.name}:{tok.start[0]}: {tok.string}"
        for tok in tokens
        if tok.type == tokenize.COMMENT
        and ("noqa" in tok.string or "type: ignore" in tok.string)
    ]


def test_package_has_exactly_the_planned_modules() -> None:
    assert set(_modules()) == {"__init__", *ALLOWED_EDGES}


@pytest.mark.parametrize("name", sorted({"__init__", *ALLOWED_EDGES}))
def test_module_stays_under_line_ceiling(name: str) -> None:
    path = PACKAGE_DIR / f"{name}.py"
    lines = len(path.read_text(encoding="utf-8").splitlines())
    assert lines <= MODULE_LINE_CEILING, f"{path.name} has {lines} lines"


def test_init_is_docstring_only() -> None:
    tree = _tree(PACKAGE_DIR / "__init__.py")
    assert len(tree.body) == 1
    assert isinstance(tree.body[0], ast.Expr)
    assert isinstance(tree.body[0].value, ast.Constant)


def test_shim_is_thin_and_imports_only_cli() -> None:
    source = helpers.ENTRY_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert len(source.splitlines()) <= SHIM_LINE_CEILING
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    package_imports = [
        (node.module, [alias.name for alias in node.names])
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[0] == helpers.LIB_PACKAGE
    ]
    assert package_imports == [(f"{_PACKAGE_PREFIX}cli", ["main"])]


def test_expected_defs_covers_all_123_names() -> None:
    assert len(EXPECTED_DEFS) == 123
    assert set(EXPECTED_DEFS.values()) == set(ALLOWED_EDGES)


def test_every_definition_lives_in_its_planned_module_exactly_once() -> None:
    seen: dict[str, list[str]] = {}
    for module, path in _modules().items():
        for node in _tree(path).body:
            name = _top_level_name(node)
            if name is not None and name != "logger":
                seen.setdefault(name, []).append(module)

    expected = {name: [module] for name, module in EXPECTED_DEFS.items()}
    assert seen == expected


def test_import_edges_equal_the_allowed_table() -> None:
    actual = {
        module: frozenset(imported for imported, _, _ in _package_imports(_tree(path)))
        for module, path in _modules().items()
        if module != "__init__"
    }
    assert actual == ALLOWED_EDGES
    assert sum(len(edges) for edges in ALLOWED_EDGES.values()) == 41


def test_imported_names_are_defined_by_their_source_module() -> None:
    wrong = [
        f"{module}: {name} from {imported} (defined in {EXPECTED_DEFS.get(name)})"
        for module, path in _modules().items()
        for imported, name, _ in _package_imports(_tree(path))
        if EXPECTED_DEFS.get(name) != imported
    ]
    assert wrong == []


def test_dependency_graph_is_acyclic() -> None:
    order = list(graphlib.TopologicalSorter(ALLOWED_EDGES).static_order())
    assert order.index("models") < order.index("cli")
    assert len(order) == len(ALLOWED_EDGES)


def test_annotation_only_imports_sit_under_type_checking() -> None:
    under_type_checking = {
        (module, imported, name)
        for module, path in _modules().items()
        for imported, name, is_type_checking in _package_imports(_tree(path))
        if is_type_checking
    }
    assert under_type_checking == TYPE_CHECKING_IMPORTS


def test_no_suppressions_in_package_or_shim() -> None:
    paths = [helpers.ENTRY_SCRIPT, *_modules().values()]
    found = [hit for path in paths for hit in _suppressions(path)]
    assert found == []
