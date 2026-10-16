"""Pin every ``find_by_name_or_id`` call site to a classified allowlist (#2237).

``CwState.find_by_name_or_id`` raises ``AmbiguousSessionIdentifierError`` when
a *name* matches more than one session. ``CwError`` is a plain ``Exception``,
not a ``click.ClickException``: a CLI command renders it cleanly only when it
is ``@handle_errors``-decorated (``cw.cli._base``) or catches it explicitly.
An undecorated command that reaches the lookup with a user-typed name would
surface a raw traceback instead.

So every call site under ``src/cw/`` is classified here, keyed by
``(module_relpath, qualified_function_name)`` -- the qualified name follows
CPython's ``__qualname__`` convention (``outer.<locals>.inner``,
``Class.method``) so two same-named closures in different scopes never
collide:

- ``id-only`` -- the argument is always an already-resolved session id
  (``session.id``, ``task.session_id``, ...), never user-typed text, so the
  ambiguity error cannot fire there.
- ``user-name-reachable:handle_errors`` -- a raw CLI argument can reach the
  lookup; every owning Click command must be ``@handle_errors``-decorated.
- ``user-name-reachable:explicit-except-arm`` -- a raw CLI argument can reach
  the lookup; the owning command is NOT ``@handle_errors``-decorated and
  instead carries an ``except AmbiguousSessionIdentifierError`` arm.

A new or removed call site anywhere in ``src/cw/`` changes the scanned set and
fails :func:`test_find_by_name_or_id_call_sites_match_classified_allowlist`
until a human classifies it in ``_FIND_BY_NAME_OR_ID_ALLOWLIST``.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Literal

from tests.conftest import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from pathlib import Path

_LOOKUP_ATTR = "find_by_name_or_id"
_HANDLE_ERRORS = "handle_errors"
_AMBIGUOUS_ERROR = "AmbiguousSessionIdentifierError"

_Tag = Literal[
    "id-only",
    "user-name-reachable:handle_errors",
    "user-name-reachable:explicit-except-arm",
]
# (module_relpath, top-level function name) of a Click command callback.
_Owner = tuple[str, str]
_Classification = tuple[_Tag, tuple[_Owner, ...]]

_SESSIONS_CLI = "cw/cli/sessions.py"
_SPAWN_CLI = "cw/cli/spawn.py"
_ORCHESTRATE_CLI = "cw/cli/orchestrate.py"
_RESUME_TRIGGER = "cw/session_resume_trigger.py"
_CODEX_CLI = "cw/cli/codex.py"

# (module_relpath, qualified_function_name) -> (tag, owning Click commands).
# Owners are listed only for user-name-reachable entries; id-only entries have
# no user-facing entry point to check.
_FIND_BY_NAME_OR_ID_ALLOWLIST: dict[tuple[str, str], _Classification] = {
    # --- user-name-reachable (14) ---
    # `cw result emit --session-id <TEXT>`; the Stop-hook harvest path passes
    # session.id. Rendered by result_emit's explicit except arm (A9).
    ("cw/result.py", "emit_result_locked"): (
        "user-name-reachable:explicit-except-arm",
        (("cw/result.py", "result_emit"),),
    ),
    # `cw result emit --session-id <TEXT>` again: the #2382 read-only lookup
    # that runs before the draft is bound, so the same text reaches this
    # lookup first. Rendered by the same explicit except arm.
    ("cw/result.py", "_load_session_for_emit"): (
        "user-name-reachable:explicit-except-arm",
        (("cw/result.py", "result_emit"),),
    ),
    # `cw peek <SESSION_NAME>`.
    (_SESSIONS_CLI, "_peek_session"): (
        "user-name-reachable:handle_errors",
        ((_SESSIONS_CLI, "peek"),),
    ),
    # `cw start --parent <TEXT>`.
    ("cw/session.py", "start_session"): (
        "user-name-reachable:handle_errors",
        ((_SESSIONS_CLI, "start"),),
    ),
    # `cw bg [SESSION_NAME]` and `cw done [SESSION_NAME]`.
    ("cw/session.py", "_resolve_session"): (
        "user-name-reachable:handle_errors",
        ((_SESSIONS_CLI, "bg"), (_SESSIONS_CLI, "done")),
    ),
    # `cw resume <SESSION_NAME>`, plus its two closures capturing the same name.
    ("cw/session.py", "resume_session"): (
        "user-name-reachable:handle_errors",
        ((_SESSIONS_CLI, "resume"),),
    ),
    ("cw/session.py", "resume_session.<locals>._update_live"): (
        "user-name-reachable:handle_errors",
        ((_SESSIONS_CLI, "resume"),),
    ),
    ("cw/session.py", "resume_session.<locals>._update_dead"): (
        "user-name-reachable:handle_errors",
        ((_SESSIONS_CLI, "resume"),),
    ),
    # `cw orchestrate workers <ORCHESTRATOR_ID>` (the worker_id lookup in the
    # same function iterates orch.worker_session_ids, id-only).
    ("cw/orchestrate.py", "orchestrator_workers"): (
        "user-name-reachable:handle_errors",
        ((_ORCHESTRATE_CLI, "orchestrate_workers"),),
    ),
    # `cw orchestrate parent <WORKER_ID>` (the parent lookup in the same
    # function uses worker.parent_session_id, id-only).
    ("cw/orchestrate.py", "orchestrator_parent"): (
        "user-name-reachable:handle_errors",
        ((_ORCHESTRATE_CLI, "orchestrate_parent"),),
    ),
    # `cw spawn close <SESSION_ID>` primary lookup.
    (_SPAWN_CLI, "_spawn_close_impl"): (
        "user-name-reachable:handle_errors",
        ((_SPAWN_CLI, "spawn_close"),),
    ),
    # `cw spawn close --requeue`'s requeue-context lookup.
    (_SPAWN_CLI, "spawn_close"): (
        "user-name-reachable:handle_errors",
        ((_SPAWN_CLI, "spawn_close"),),
    ),
    # `cw spawn complete <SESSION_ID>`.
    (_SPAWN_CLI, "_spawn_complete_impl"): (
        "user-name-reachable:handle_errors",
        ((_SPAWN_CLI, "spawn_complete"),),
    ),
    # `cw codex run --session-id <TEXT>`.
    ("cw/codex_driver.py", "_load_session"): (
        "user-name-reachable:handle_errors",
        ((_CODEX_CLI, "codex_run"),),
    ),
    # --- id-only (12) ---
    # prior_session_id is read back from cw-context.json, which cw writes.
    ("cw/spawn.py", "_write_hook_context"): ("id-only", ()),
    # parent_session.id, already resolved by start_session's own lookup.
    ("cw/session.py", "start_session.<locals>._append"): ("id-only", ()),
    # task.session_id values cleared by cancel_ticket.
    ("cw/cli/dev_queue/crud.py", "dev_queue_cancel"): ("id-only", ()),
    ("cw/dev_queue/requeue.py", "unblock_ticket"): ("id-only", ()),
    # Called with session.id.
    (_RESUME_TRIGGER, "NativeDaemonResumeTriggerAdapter._recheck_gate"): (
        "id-only",
        (),
    ),
    (
        _RESUME_TRIGGER,
        "NativeDaemonResumeTriggerAdapter._commit_resume.<locals>._update",
    ): ("id-only", ()),
    ("cw/dev_queue/approval.py", "_approve_ticket_locked"): ("id-only", ()),
    # task.fix_dispatch_session_id.
    ("cw/reconcile/fix_dispatch.py", "_act_on_fix_dispatch_completions"): (
        "id-only",
        (),
    ),
    # task.session_id.
    ("cw/reconcile/gate_recipes.py", "_detect_auto_approve_review"): ("id-only", ()),
    ("cw/reconcile/gate_recipes.py", "_detect_auto_adopt_plan"): ("id-only", ()),
    ("cw/reconcile/gate_recipes.py", "_act_auto_approve_review"): ("id-only", ()),
    ("cw/reconcile/gate_recipes.py", "_act_auto_adopt_plan"): ("id-only", ()),
}


class _LookupCallCollector(ast.NodeVisitor):
    """Collect the qualified name of every function calling the lookup."""

    def __init__(self) -> None:
        # (kind, name) per enclosing scope; kind is "function" or "class".
        self._stack: list[tuple[str, str]] = []
        self.qualnames: set[str] = set()

    def _qualname(self) -> str:
        parts: list[str] = []
        for index, (kind, name) in enumerate(self._stack):
            parts.append(name)
            if kind == "function" and index < len(self._stack) - 1:
                parts.append("<locals>")
        return ".".join(parts)

    def _visit_scope(self, kind: str, node: ast.AST, name: str) -> None:
        self._stack.append((kind, name))
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope("function", node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope("function", node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope("class", node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == _LOOKUP_ATTR:
            self.qualnames.add(self._qualname())
        self.generic_visit(node)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _scan_call_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _iter_src_files():
        collector = _LookupCallCollector()
        collector.visit(_parse(path))
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        found.update((relpath, qualname) for qualname in collector.qualnames)
    return found


def _top_level_function(owner: _Owner) -> ast.FunctionDef:
    relpath, name = owner
    for node in _parse(_SRC_ROOT / relpath).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    msg = f"{relpath}::{name} not found as a top-level function"
    raise AssertionError(msg)


def _decorator_names(func: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in func.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _catches_ambiguous_error(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        if any(isinstance(c, ast.Name) and c.id == _AMBIGUOUS_ERROR for c in caught):
            return True
    return False


def test_find_by_name_or_id_call_sites_match_classified_allowlist() -> None:
    scanned = _scan_call_sites()
    allowlisted = set(_FIND_BY_NAME_OR_ID_ALLOWLIST)

    unclassified = sorted(scanned - allowlisted)
    stale = sorted(allowlisted - scanned)
    assert not unclassified, (
        "New find_by_name_or_id call site(s) -- classify each in "
        f"_FIND_BY_NAME_OR_ID_ALLOWLIST as id-only or user-name-reachable: "
        f"{unclassified}"
    )
    assert not stale, f"Allowlist entries with no call site any more: {stale}"


def test_user_name_reachable_sites_handler_decorators_still_match() -> None:
    for site, (tag, owners) in _FIND_BY_NAME_OR_ID_ALLOWLIST.items():
        if tag == "id-only":
            assert owners == (), f"{site}: id-only entries name no owning command"
            continue
        assert owners, f"{site}: user-name-reachable entry must name its commands"
        for owner in owners:
            func = _top_level_function(owner)
            decorators = _decorator_names(func)
            assert "command" in decorators, f"{owner} is not a Click command"
            if tag == "user-name-reachable:handle_errors":
                assert _HANDLE_ERRORS in decorators, (
                    f"{owner} (reaching {site}) lost @handle_errors -- an "
                    "ambiguous name would surface as a raw traceback"
                )
            else:
                assert _HANDLE_ERRORS not in decorators, (
                    f"{owner} is now @handle_errors-decorated; retag {site} "
                    "as user-name-reachable:handle_errors"
                )
                assert _catches_ambiguous_error(func), (
                    f"{owner} (reaching {site}) has no except {_AMBIGUOUS_ERROR} arm"
                )
