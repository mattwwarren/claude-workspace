"""Shared test helpers for the ``.claude/scripts/review_monitor.py`` test suite.

The review monitor is a script, not a package under ``src/``, so tests load it
by path (:func:`load_entry`). Its implementation is being split into the
sibling ``review_monitor_lib`` package (#2499): once a name moves, every module
that imports it holds its own binding, and a single ``monkeypatch.setattr`` on
one module silently misses the rest. :func:`patch` and :func:`get` therefore
act on the entry module *and* every already-imported ``review_monitor_lib.*``
submodule, so the same test reads and patches correctly before, during and
after the split.

This module has no ``test_`` prefix, so pytest does not collect it (same
convention as ``tests/_cli_review_helpers.py``), and it has no import-time
side effects: the script is loaded lazily on first use and cached.

Hosted here so no test module keeps a private copy:

- loader / binding helpers: :func:`load_entry`, :func:`modules`, :func:`get`,
  :func:`patch`, :func:`isolate_state` (the body of the conftest
  ``review_monitor_state_dir`` fixture);
- builders: :func:`make_pr`, :func:`seed_prs`, :func:`stored_pr`, :func:`ago`,
  :func:`register_argv`, :data:`REPO`, :data:`KEY`;
- CLI driver: :func:`run_cli`;
- the fake ``gh``/``git`` router :class:`FakeCommands`;
- ``statusCheckRollup`` builders :func:`checkrun` / :func:`status_context` and
  the shared ``ROLLUP_*`` literals;
- the golden CLI-contract extractor :func:`extract_cli_contract` and its
  one-line-per-argument writer :func:`format_cli_contract`.

The ``gh`` payloads tests build with these helpers are hand-authored literals
restricted to documented fields, not captured responses (see
``tests/test_review_monitor.py``'s module docstring).
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import types

    import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
ENTRY_SCRIPT = SCRIPTS_DIR / "review_monitor.py"
ENTRY_MODULE_NAME = "review_monitor"
LIB_PACKAGE = "review_monitor_lib"
CLI_CONTRACT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "review_monitor_cli_contract.json"
)

REPO = "acme/widgets"
PR_NUMBER = 42
KEY = f"{REPO}#{PR_NUMBER}"

# Paths the script binds at import time and writes under. Every test that runs
# a command must point them at a tmp dir; run_cli / FakeCommands.install refuse
# to run while any of them still holds its import-time (real) value.
GUARDED_PATHS = (
    "CENTRAL_STATE_DIR",
    "LEGACY_STATE_FILE",
    "PENDING_INBOX_DIR",
    "DESKTOP_QUEUE_DIR",
)


@functools.cache
def _load() -> tuple[types.ModuleType, dict[str, object]]:
    """Load the entry script once; return it and its import-time guarded paths."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(ENTRY_MODULE_NAME, ENTRY_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[ENTRY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    import_time = {
        name: _get_from(_bound_modules(module), name) for name in GUARDED_PATHS
    }
    return module, import_time


def load_entry() -> types.ModuleType:
    """Return the review-monitor entry module, loading it by path on first use."""
    return _load()[0]


def import_time_value(name: str) -> object:
    """Return the value guarded path *name* had when the script was loaded."""
    return _load()[1][name]


def _bound_modules(entry: types.ModuleType) -> list[types.ModuleType]:
    prefix = f"{LIB_PACKAGE}."
    lib = sorted(
        (name, mod)
        for name, mod in sys.modules.items()
        if name.startswith(prefix) and mod is not None
    )
    return [entry, *(mod for _, mod in lib)]


def modules() -> list[types.ModuleType]:
    """The entry module plus every already-imported ``review_monitor_lib`` module."""
    return _bound_modules(load_entry())


def _get_from(candidates: list[types.ModuleType], name: str) -> Any:
    bound = [mod for mod in candidates if hasattr(mod, name)]
    if not bound:
        msg = f"no review_monitor module binds {name!r}"
        raise AttributeError(msg)
    value = getattr(bound[0], name)
    for mod in bound[1:]:
        assert getattr(mod, name) is value, (
            f"{name!r} differs between {bound[0].__name__} and {mod.__name__}"
        )
    return value


def get(name: str) -> Any:
    """Return *name*, asserting every module binding it holds the same object."""
    return _get_from(modules(), name)


def patch(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """Patch *name* on every review-monitor module that binds it.

    Mirrors ``tests/_worktree_helpers.patch_worktree``: split modules import
    shared helpers by name (``from review_monitor_lib.shell import _run_gh``),
    so each holds its own binding. Patching every binding keeps the meaning of
    a single pre-split ``review_monitor.<name>`` patch.
    """
    bound = [mod for mod in modules() if hasattr(mod, name)]
    if not bound:
        msg = f"no review_monitor module binds {name!r}"
        raise AttributeError(msg)
    for mod in bound:
        monkeypatch.setattr(mod, name, value)


def isolate_state(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point every path the script writes under *root*; return the state dir.

    Covers the central state dir, the legacy state file, the ship-it pending
    inbox (``/tmp/review-monitor/pending`` for real) and the Desktop action
    queue (bound from ``desktop_queue_dir()`` at import). Also empties the
    canonical repo-path overrides and clears the cached GitHub username.
    """
    central = root / "monitor-state"
    patch(monkeypatch, "CENTRAL_STATE_DIR", central)
    patch(monkeypatch, "LEGACY_STATE_FILE", root / "legacy-state.json")
    patch(monkeypatch, "PENDING_INBOX_DIR", root / "pending-inbox")
    patch(monkeypatch, "DESKTOP_QUEUE_DIR", root / "desktop-queue")
    patch(monkeypatch, "CANONICAL_REPO_PATHS", {})
    monkeypatch.delenv(get("CANONICAL_REPO_PATHS_ENV"), raising=False)
    get("_get_our_username").cache_clear()
    return central


def assert_isolated() -> None:
    """Fail unless every guarded path has been patched away from its real value."""
    for name in GUARDED_PATHS:
        assert get(name) != import_time_value(name), (
            f"{name} still points at its real location {get(name)!s}; "
            "use the review_monitor_state_dir fixture"
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_pr(**overrides: object) -> Any:
    """Build a ``MonitoredPR`` for ``acme/widgets#42`` at SHA ``deadbeef``."""
    fields: dict[str, object] = {
        "role": "author",
        "repo": REPO,
        "repo_path": "/tmp/widgets",
        "pr_number": PR_NUMBER,
        "last_seen_sha": "deadbeef",
    }
    fields.update(overrides)
    return get("MonitoredPR")(**fields)


def seed_prs(*prs: Any, completed: dict[str, dict[str, Any]] | None = None) -> None:
    """Persist *prs* (all in one repo) as that repo's monitor state."""
    repo = prs[0].repo if prs else REPO
    state = get("MonitorState")(
        monitored={f"{pr.repo}#{pr.pr_number}": pr for pr in prs},
        completed=dict(completed or {}),
    )
    get("save_state")(state, repo)


def stored_pr(key: str = KEY) -> Any:
    """Reload state from disk and return the monitored PR under *key*."""
    repo = key.split("#", 1)[0]
    return get("load_state")(repo).monitored[key]


def ago(**delta: float) -> str:
    """ISO-8601 timestamp for ``now - timedelta(**delta)`` (UTC)."""
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def register_argv(sha: str = "abc123") -> list[str]:
    """The standard ``register`` argv for ``acme/widgets#42``."""
    return [
        "register",
        str(PR_NUMBER),
        "--role",
        "author",
        "--repo",
        REPO,
        "--repo-path",
        "/canon/widgets",
        "--sha",
        sha,
    ]


def run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, str]:
    """Run the real ``main()`` with *argv*; return (exit code, stdout, stderr)."""
    assert_isolated()
    monkeypatch.setattr(sys, "argv", ["review_monitor.py", *argv])
    code = 0
    try:
        get("main")()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Fake gh / git router
# ---------------------------------------------------------------------------


class FakeCommands:
    """Route faked ``_run_gh`` / ``_run_git`` calls by argv prefix.

    Routes are matched in insertion order against the full argv including the
    tool name (``("gh", "pr", "view")``, ``("git", "diff")``). A routed result
    is returned as stdout, or raised when it is an exception. An unrouted call
    raises ``AssertionError`` so a test never silently shells out.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[tuple[str, ...], str | BaseException]] = []
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def add(self, prefix: tuple[str, ...], result: str | BaseException) -> FakeCommands:
        """Answer calls whose argv starts with *prefix* with *result*."""
        self.routes.append((prefix, result))
        return self

    def argvs(self) -> list[tuple[str, ...]]:
        """Return the argv of every call made so far, in order."""
        return [argv for argv, _ in self.calls]

    def _dispatch(self, argv: tuple[str, ...], where: str | None) -> str:
        self.calls.append((argv, where))
        for prefix, result in self.routes:
            if argv[: len(prefix)] == prefix:
                if isinstance(result, BaseException):
                    raise result
                return result
        msg = f"unrouted command {argv!r}"
        raise AssertionError(msg)

    def run_gh(self, args: list[str], repo: str | None = None) -> str:
        """Stand-in for ``_run_gh(args, repo=None)``."""
        return self._dispatch(("gh", *args), repo)

    def run_git(self, args: list[str], cwd: str | None = None) -> str:
        """Stand-in for ``_run_git(args, cwd=None)``."""
        return self._dispatch(("git", *args), cwd)

    def install(
        self, monkeypatch: pytest.MonkeyPatch, *, gh: bool = True, git: bool = True
    ) -> FakeCommands:
        """Patch every ``_run_gh`` / ``_run_git`` binding to this router.

        Requires the ``review_monitor_state_dir`` fixture (asserted), whose
        teardown clears the ``_get_our_username`` cache this call clears now.
        """
        assert_isolated()
        get("_get_our_username").cache_clear()
        if gh:
            patch(monkeypatch, "_run_gh", self.run_gh)
        if git:
            patch(monkeypatch, "_run_git", self.run_git)
        return self


# ---------------------------------------------------------------------------
# statusCheckRollup entries
# ---------------------------------------------------------------------------


def checkrun(status: str, conclusion: str = "", name: str = "check") -> dict[str, Any]:
    """A ``CheckRun`` rollup entry (GitHub Actions shape)."""
    return {
        "__typename": "CheckRun",
        "status": status,
        "conclusion": conclusion,
        "name": name,
        "workflowName": "wf",
        "detailsUrl": "https://ci/1",
    }


def status_context(state: str, context: str = "ci") -> dict[str, Any]:
    """A ``StatusContext`` rollup entry (legacy commit-status shape)."""
    return {
        "__typename": "StatusContext",
        "state": state,
        "context": context,
        "targetUrl": "https://ci/2",
    }


ROLLUP_GREEN = (
    checkrun("COMPLETED", "SUCCESS", name="test"),
    status_context("SUCCESS"),
)
ROLLUP_FAILING = (
    checkrun("COMPLETED", "FAILURE", name="lint"),
    checkrun("COMPLETED", "SUCCESS", name="test"),
)
ROLLUP_PENDING = (checkrun("IN_PROGRESS", name="build"), status_context("PENDING"))
ROLLUP_CANCELLED_ONLY = (checkrun("COMPLETED", "CANCELLED"),)
ROLLUP_STATUS_CONTEXT_FAIL = (status_context("ERROR", context="sonar"),)


# ---------------------------------------------------------------------------
# Golden CLI contract
# ---------------------------------------------------------------------------


def _argument_contract(action: argparse.Action) -> dict[str, Any]:
    return {
        "action": type(action).__name__,
        "choices": None if action.choices is None else list(action.choices),
        "default": action.default,
        "dest": action.dest,
        "help": action.help,
        "nargs": action.nargs,
        "option_strings": list(action.option_strings),
        "required": action.required,
        "type": getattr(action.type, "__name__", None),
    }


def extract_cli_contract(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Snapshot *parser*'s subcommand tree as plain JSON-able data.

    argparse has no public enumeration API, so this reads its internals:
    ``parser._actions`` to find the ``_SubParsersAction``, its
    ``_choices_actions`` for each subcommand's help, and each sub-parser's
    ``_actions`` (skipping ``_HelpAction``). ``prog`` and ``usage`` are left
    out: the root parser sets no ``prog``, so it defaults to
    ``basename(sys.argv[0])`` and would make the snapshot depend on the runner.
    """
    sub_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    helps = {pseudo.dest: pseudo.help for pseudo in sub_action._choices_actions}
    commands: dict[str, Any] = {}
    for name, sub_parser in sub_action.choices.items():
        arguments = [
            _argument_contract(action)
            for action in sub_parser._actions
            if not isinstance(action, argparse._HelpAction)
        ]
        commands[name] = {
            "arguments": sorted(arguments, key=lambda arg: arg["dest"]),
            "help": helps.get(name),
        }
    return {
        "commands": commands,
        "description": parser.description,
        "subcommands_dest": sub_action.dest,
        "subcommands_required": sub_action.required,
    }


def format_cli_contract(contract: dict[str, Any]) -> str:
    """Render *contract* as JSON with one argument object per line."""
    command_blocks: list[str] = []
    for name in sorted(contract["commands"]):
        command = contract["commands"][name]
        argument_lines = [
            f"    {json.dumps(arg, sort_keys=True)}" for arg in command["arguments"]
        ]
        arguments = (
            "[\n" + ",\n".join(argument_lines) + "\n   ]" if argument_lines else "[]"
        )
        command_blocks.append(
            f"  {json.dumps(name)}: {{\n"
            f'   "arguments": {arguments},\n'
            f'   "help": {json.dumps(command["help"])}\n'
            "  }"
        )
    top_level = [
        f" {json.dumps(key)}: {json.dumps(contract[key])}"
        for key in sorted(contract)
        if key != "commands"
    ]
    commands_block = ' "commands": {\n' + ",\n".join(command_blocks) + "\n }"
    return "{\n" + ",\n".join([commands_block, *top_level]) + "\n}\n"
