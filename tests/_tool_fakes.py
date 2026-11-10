"""Shared fakes for every external tool the unit tier must not launch (#3078).

The tool launch guard (``tests/_host_probe_guard.py``) refuses an unmocked
launch of ``gh``, ``claude``, ``codex``, ``opencode``, ``uv`` or
``pre-commit`` and names this module in its error. Each tool is faked at the
seam its callers already patch, so this is a family of seam-specific fakes,
not one class:

``gh``
    :class:`FakeGh` (the ``cw.sprint`` ``GhSurface`` double),
    :class:`FakePostCommentGh` (``cw.tracker_post`` posting),
    :class:`ReadyGh` (``prep_pr_finalize.run`` for ``gh pr ready``),
    :class:`FakeGhComment` (``cw.gh._sp.run`` for ``gh issue comment``),
    :class:`FakeGhCli` (a generic scripted ``subprocess.run``), the
    ``stub_gh*`` shell shims for workflow and script tests, and
    ``FakeGhHead`` (defined in ``tests/_changelog_repair_helpers.py``,
    re-exported lazily below).
``claude``
    ``FakeNativeDaemonClient`` (the ``claude --bg`` daemon, re-exported from
    ``cw.native_daemon``) and :func:`fake_claude_cli` for one-shot calls.
``codex``
    ``FakeFireAndForgetRunner`` (re-exported from ``cw.executor.core``), the
    :func:`fake_codex_job_runner` fixture and the autouse
    :func:`_mock_codex_capability_probe`. The tracker-IO turn (#3057) runs
    through ``cw.codex_runner.FakeCodexRunner``; :func:`marked_parts` and
    :func:`tracker_io_turn_result` build its emitted parts and result JSON.
``opencode``, ``uv``, ``pre-commit``
    :func:`fake_opencode_cli`, :func:`fake_uv_cli` and
    :func:`fake_pre_commit_cli`, recording shims built on
    :class:`RecordingShim`.

A shim lives in ``tmp_path/bin``, inside the test root, so the launch guard
lets it run by bare name once its directory leads ``PATH``. Put it there with
:func:`path_with_stub` (write the stub and lead ``PATH`` with its directory) or
:func:`prepend_to_path` (a directory already in hand); do not open-code
``monkeypatch.setenv("PATH", ...)`` -- ``tests/test_tool_fakes.py`` fails on it.

This module never imports ``tests.conftest``: the conftest imports it.
``FakeGhHead`` is re-exported through a module ``__getattr__`` because its
home module imports ``tests.conftest`` at its top.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw._lock_guard import HeldLock, held_locks
from cw.codex_runner import CodexRunResult
from cw.executor.core import FakeFireAndForgetRunner
from cw.gh import (
    GhFailure,
    IssueCreateResult,
    LabelledIssue,
    LabelledIssuesLookup,
    agent_marked_body,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.tracker_io import READBACK_WINDOW
from tests._host_probe_guard import blocked_binary

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Either flavour of finished process a fake ``subprocess.run`` hands back.
_Completed = subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]

__all__ = [
    "ARM_VIEW_ARMED",
    "ARM_VIEW_OPEN",
    "GH_ARG_SEPARATOR",
    "GH_CALLS_FILE",
    "GH_UNAUTHENTICATED",
    "AbsentToolCli",
    "ArmStep",
    "FakeFireAndForgetRunner",
    "FakeGh",
    "FakeGhCli",
    "FakeGhComment",
    "FakeNativeDaemonClient",
    "FakePostCommentGh",
    "GhReply",
    "ReadyGh",
    "RecordingShim",
    "ScriptedCodexRunner",
    "any_call",
    "assert_gh_calls",
    "fake_claude_cli",
    "fake_codex_job_runner",
    "fake_opencode_cli",
    "fake_pre_commit_cli",
    "fake_uv_cli",
    "gh_calls",
    "marked_parts",
    "path_with_stub",
    "prepend_to_path",
    "read_calls",
    "restore_real_run",
    "shim_env",
    "stdin_contains",
    "stub_gh",
    "stub_gh_arm",
    "stub_gh_issue_create",
    "stub_gh_issue_list",
    "stub_gh_recording",
    "tracker_io_turn_result",
    "write_bin_stub",
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PREP_PR_FINALIZE = _REPO_ROOT / ".claude" / "scripts" / "prep_pr_finalize.py"

# ``subprocess.run`` as it was at import, before any test patched it.
_REAL_RUN: Callable[..., _Completed] = subprocess.run

# Names re-exported lazily (attribute -> module) to dodge an import cycle.
# Kept out of ``__all__``: a star-import would resolve them eagerly.
_LAZY_EXPORTS = {"FakeGhHead": "tests._changelog_repair_helpers"}


def __getattr__(name: str) -> object:
    """Resolve the lazily re-exported names (PEP 562)."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module = importlib.import_module(module_name)
    return getattr(module, name)


# --- Shell shims ---------------------------------------------------------------

# Side file (next to a recording stub) appended to once per invocation; each
# arg is terminated by the ASCII unit separator so args with spaces round-trip.
GH_CALLS_FILE = "gh.calls"
GH_ARG_SEPARATOR = "\x1f"

# One scripted ``gh pr merge`` outcome for ``stub_gh_arm``: (exit code, stderr
# text, ``gh pr view`` JSON the PR reads back as once this merge has run --
# ``None`` leaves the read-back unchanged).
ArmStep = tuple[int, str, str | None]

ARM_VIEW_OPEN = '{"state":"OPEN","autoMergeRequest":null}'
ARM_VIEW_ARMED = '{"state":"OPEN","autoMergeRequest":{"mergeMethod":"SQUASH"}}'

# The ``.author.login`` the workflow-issue stubs keep (the Actions bot).
GITHUB_ACTIONS_AUTHOR = "app/github-actions"

_RECORD_ARGV_LINES = (
    'for a in "$@"; do printf \'%s\\037\' "$a"; done >> "$DIR/{calls}"',
    "printf '\\n' >> \"$DIR/{calls}\"",
)


def _recording_prelude() -> str:
    """Script lines that log the stub's argv to ``gh.calls`` beside it."""
    lines = [
        'DIR=$(dirname "$0")',
        *(line.format(calls=GH_CALLS_FILE) for line in _RECORD_ARGV_LINES),
    ]
    return "\n".join(lines) + "\n"


# Characters a ``RecordingShim`` response key may not carry: it is spliced
# into a double-quoted ``case`` pattern.
_UNSAFE_KEY_CHARS = frozenset('"$`\\*?[')


def write_bin_stub(tmp_path: Path, name: str, body: str) -> Path:
    """Write ``body`` as an executable ``name`` in ``tmp_path/bin``; return the dir.

    The shared mechanics behind every shell shim here, so a new one differs
    only in its script body.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    stub = fake_bin / name
    stub.write_text(body)
    stub.chmod(0o755)
    return fake_bin


def prepend_to_path(
    monkeypatch: pytest.MonkeyPatch, bin_dir: Path, *, keep_existing: bool = True
) -> Path:
    """Put *bin_dir* first on ``PATH``, ahead of the current ``PATH``.

    The value is ``bin_dir`` + ``os.pathsep`` + current ``PATH``;
    ``keep_existing=False`` replaces ``PATH`` with ``str(bin_dir)``. Returns
    *bin_dir*. The change is undone with the rest of *monkeypatch*.
    """
    if keep_existing:
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    else:
        monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def path_with_stub(
    monkeypatch: pytest.MonkeyPatch,
    base: Path,
    name: str,
    body: str,
    *,
    keep_existing: bool = True,
) -> Path:
    """:func:`write_bin_stub` ``(base, name, body)``, then lead ``PATH`` with its dir.

    Pass a ``tempfile`` directory as *base* to put the stub outside the pytest
    basetemp (emulating a host install under the launch guard). Returns the bin
    dir (``base / "bin"``).
    """
    bin_dir = write_bin_stub(base, name, body)
    return prepend_to_path(monkeypatch, bin_dir, keep_existing=keep_existing)


def read_calls(fake_bin: Path, calls_file: str) -> list[list[str]]:
    """Every argv a recording shim logged to *calls_file*; ``[]`` if none."""
    log = fake_bin / calls_file
    if not log.exists():
        return []
    # One record per line; each arg ends with the separator, so the trailing
    # empty field is dropped.
    records = log.read_text(encoding="utf-8").split("\n")[:-1]
    return [record.split(GH_ARG_SEPARATOR)[:-1] for record in records]


def stub_gh(tmp_path: Path, *, exit_code: int, stdout: str = "") -> Path:
    """Write a ``gh`` stub with a fixed exit and stdout; return its bin dir."""
    # Quoted heredoc ('GH_STDOUT_EOF') -- no shell interpolation of `stdout`'s
    # contents, matching how a real `gh` payload is opaque data.
    return write_bin_stub(
        tmp_path,
        "gh",
        f"#!/bin/sh\n{_recording_prelude()}"
        f"cat <<'GH_STDOUT_EOF'\n{stdout}GH_STDOUT_EOF\nexit {exit_code}\n",
    )


def stub_gh_recording(
    tmp_path: Path, *, view_exit: int = 0, create_exit: int = 0, other_exit: int = 0
) -> Path:
    """Write a ``gh`` stub that records argv and returns scripted exit codes (#1626).

    Every invocation appends its argv to ``gh.calls`` beside the stub (read it
    back with :func:`gh_calls`). The exit code is chosen per subcommand:
    ``release view`` -> ``view_exit``, ``release create`` -> ``create_exit``,
    anything else -> ``other_exit``.
    """
    lines = [
        "#!/bin/sh",
        'DIR=$(dirname "$0")',
        *(line.format(calls=GH_CALLS_FILE) for line in _RECORD_ARGV_LINES),
        'case "$1 $2" in',
        f'  "release view") exit {view_exit};;',
        f'  "release create") exit {create_exit};;',
        f"  *) exit {other_exit};;",
        "esac",
    ]
    return write_bin_stub(tmp_path, "gh", "\n".join(lines) + "\n")


def gh_calls(fake_bin: Path) -> list[list[str]]:
    """Every argv a recording ``gh`` stub logged, in call order; ``[]`` if none."""
    return read_calls(fake_bin, GH_CALLS_FILE)


def shim_env(fake_bin: Path, **extra: str) -> dict[str, str]:
    """``extra_env`` putting *fake_bin* ahead of the system tools on ``PATH``."""
    return {"PATH": f"{fake_bin}:/usr/bin:/bin", **extra}


def assert_gh_calls(fake_bin: Path, expected: list[list[str]]) -> None:
    """Assert the recording stub saw exactly *expected*, and saw something."""
    calls = gh_calls(fake_bin)
    assert calls, "the gh shim recorded no calls -- is it first on PATH?"
    assert calls == expected


def stub_gh_arm(
    tmp_path: Path, steps: Sequence[ArmStep], *, initial_view: str = ARM_VIEW_OPEN
) -> Path:
    """Write a stateful ``gh`` stub for ``arm-automerge`` tests (#2576).

    Records argv like :func:`stub_gh_recording`. Each ``pr merge`` bumps a
    counter file and replays step N of *steps* (the last step repeats once
    they run out): it prints the step's stderr, copies the step's view JSON
    over ``view.json`` when it has one, and exits with the step's code.
    ``pr view`` cats ``view.json`` (*initial_view* until a merge step replaces
    it); any other subcommand exits 0.
    """
    assert steps, "stub_gh_arm needs at least one scripted pr merge step"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    for index, (exit_code, stderr, view) in enumerate(steps, start=1):
        (fake_bin / f"step{index}.exit").write_text(str(exit_code))
        (fake_bin / f"step{index}.stderr").write_text(stderr)
        if view is not None:
            (fake_bin / f"step{index}.view").write_text(view)
    (fake_bin / "view.json").write_text(initial_view)
    lines = [
        "#!/bin/sh",
        'DIR=$(dirname "$0")',
        *(line.format(calls=GH_CALLS_FILE) for line in _RECORD_ARGV_LINES),
        'case "$1 $2" in',
        '  "pr view") cat "$DIR/view.json"; exit 0;;',
        '  "pr merge")',
        '    n=$(cat "$DIR/merge.count" 2>/dev/null || echo 0)',
        "    n=$((n + 1))",
        '    printf \'%s\' "$n" > "$DIR/merge.count"',
        f'    if [ "$n" -gt {len(steps)} ]; then n={len(steps)}; fi',
        '    cat "$DIR/step$n.stderr" >&2',
        '    if [ -f "$DIR/step$n.view" ]; then'
        ' cp "$DIR/step$n.view" "$DIR/view.json"; fi',
        '    exit "$(cat "$DIR/step$n.exit")";;',
        "  *) exit 0;;",
        "esac",
    ]
    return write_bin_stub(tmp_path, "gh", "\n".join(lines) + "\n")


def stub_gh_issue_create(tmp_path: Path) -> Path:
    """``gh`` stub accepting only ``issue create``; writes ``--body`` to a file.

    The body lands in ``tmp_path/created-body.txt``. Any other subcommand, or
    a create without ``--body``, exits 2.
    """
    return write_bin_stub(
        tmp_path,
        "gh",
        "#!/bin/sh\n"
        f"{_recording_prelude()}"
        'if [ "$1 $2" != "issue create" ]; then\n'
        '  echo "unexpected gh invocation: $@" >&2\n'
        "  exit 2\n"
        "fi\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--body" ]; then\n'
        "    shift\n"
        '    printf "%s" "$1" > "$(dirname "$0")/../created-body.txt"\n'
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        'echo "missing --body" >&2\n'
        "exit 2\n",
    )


def stub_gh_issue_list(
    tmp_path: Path,
    *,
    list_json: str,
    author: str = GITHUB_ACTIONS_AUTHOR,
    other_calls: str = "reject",
) -> Path:
    """``gh`` stub that runs the workflow's real ``--jq`` filter over *list_json*.

    ``issue list`` must carry ``--app github-actions`` and a ``--jq`` filter;
    the stub keeps only issues authored by *author* (what ``--app`` does on
    GitHub) and pipes them through the filter, so a malformed or inverted
    workflow filter fails the test. Any other subcommand is rejected (exit 2)
    when *other_calls* is ``"reject"``, or appended to ``tmp_path/gh-calls.log``
    and accepted when it is ``"log"``.
    """
    if other_calls == "reject":
        other = '  echo "unexpected gh invocation: $@" >&2\n  exit 2\n'
    elif other_calls == "log":
        other = '  echo "$@" >> "$(dirname "$0")/../gh-calls.log"\n  exit 0\n'
    else:
        msg = f"other_calls must be 'reject' or 'log', not {other_calls!r}"
        raise ValueError(msg)
    return write_bin_stub(
        tmp_path,
        "gh",
        "#!/bin/sh\n"
        f"{_recording_prelude()}"
        'if [ "$1 $2" != "issue list" ]; then\n'
        f"{other}"
        "fi\n"
        "jq_filter=\n"
        "app_filter=\n"
        "previous=\n"
        'for arg in "$@"; do\n'
        '  if [ "$previous" = "--app" ]; then\n'
        '    app_filter="$arg"\n'
        "  fi\n"
        '  if [ "$previous" = "--jq" ]; then\n'
        '    jq_filter="$arg"\n'
        "    break\n"
        "  fi\n"
        '  previous="$arg"\n'
        "done\n"
        'if [ "$app_filter" != "github-actions" ]; then\n'
        '  echo "missing --app github-actions filter" >&2\n'
        "  exit 2\n"
        "fi\n"
        'if [ -z "$jq_filter" ]; then\n'
        '  echo "missing --jq filter" >&2\n'
        "  exit 2\n"
        "fi\n"
        "/usr/bin/jq "
        f"'map(select(.author.login == \"{author}\"))' "
        "<<'GH_LIST_EOF' | /usr/bin/jq -r \"$jq_filter\"\n"
        f"{list_json}\nGH_LIST_EOF\n",
    )


@dataclass(frozen=True, slots=True)
class GhReply:
    """One scripted outcome: exit code plus stdout and stderr text."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class RecordingShim:
    """An executable *name* in ``tmp_path/bin`` that records argv and replies.

    *responses* maps an argv prefix (the space-joined leading args, e.g.
    ``"agents --json"``) to its :class:`GhReply`; the longest matching prefix
    wins and anything unmatched gets *default*. Calls are read back with
    :attr:`calls`. Run it by bare name with :meth:`env` as the child env.
    """

    def __init__(
        self,
        tmp_path: Path,
        name: str,
        *,
        responses: Mapping[str, GhReply] | None = None,
        default: GhReply | None = None,
    ) -> None:
        self.name = name
        self.calls_file = f"{name}.calls"
        replies = dict(responses or {})
        for key in replies:
            if _UNSAFE_KEY_CHARS & set(key):
                msg = f"response key {key!r} carries a shell metacharacter"
                raise ValueError(msg)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        # Longest prefix first, so a specific key beats its own prefix.
        ordered = sorted(replies.items(), key=lambda item: -len(item[0]))
        arms: list[str] = []
        for index, (key, reply) in enumerate(ordered):
            arms.append(self._arm(bin_dir, f'"{key}"*', f"r{index}", reply))
        arms.append(self._arm(bin_dir, "*", "default", default or GhReply()))
        lines = [
            "#!/bin/sh",
            'DIR=$(dirname "$0")',
            *(line.format(calls=self.calls_file) for line in _RECORD_ARGV_LINES),
            'case "$*" in',
            *arms,
            "esac",
        ]
        self.bin_dir = write_bin_stub(tmp_path, name, "\n".join(lines) + "\n")
        self.path = self.bin_dir / name

    def _arm(self, bin_dir: Path, pattern: str, tag: str, reply: GhReply) -> str:
        stem = f"{self.name}.{tag}"
        (bin_dir / f"{stem}.out").write_text(reply.stdout, encoding="utf-8")
        (bin_dir / f"{stem}.err").write_text(reply.stderr, encoding="utf-8")
        return (
            f'  {pattern}) cat "$DIR/{stem}.out"; cat "$DIR/{stem}.err" >&2;'
            f" exit {reply.returncode};;"
        )

    @property
    def calls(self) -> list[list[str]]:
        """Every argv the shim was launched with, in call order."""
        return read_calls(self.bin_dir, self.calls_file)

    def env(
        self, base: Mapping[str, str] | None = None, **extra: str
    ) -> dict[str, str]:
        """*base* (default: empty) with the shim's dir leading ``PATH``."""
        merged = dict(base or {})
        merged.update(shim_env(self.bin_dir, **extra))
        return merged


def fake_claude_cli(
    tmp_path: Path,
    *,
    agents_json: str = "[]",
    version: str = "2.1.0 (Claude Code)",
    default: GhReply | None = None,
) -> RecordingShim:
    """A ``claude`` shim for the one-shot calls (``stop``, ``attach``, ...).

    ``agents --json`` prints *agents_json*; ``--version`` prints *version*;
    ``stop``, ``attach`` and ``respawn`` exit 0. The long-running ``claude --bg``
    daemon is faked in-process by ``FakeNativeDaemonClient`` instead.
    """
    return RecordingShim(
        tmp_path,
        "claude",
        responses={
            "agents --json": GhReply(stdout=agents_json),
            "--version": GhReply(stdout=f"{version}\n"),
            "stop": GhReply(),
            "attach": GhReply(),
            "respawn": GhReply(),
        },
        default=default,
    )


def fake_opencode_cli(
    tmp_path: Path,
    *,
    help_text: str = "opencode run [message..]\n",
    events: Sequence[str] = (),
    run_exit: int = 0,
) -> RecordingShim:
    """An ``opencode`` shim: ``run --help`` prints *help_text*, ``run`` the events.

    *events* are the JSON lines ``opencode run --format json`` streams.
    """
    stream = "".join(f"{event}\n" for event in events)
    return RecordingShim(
        tmp_path,
        "opencode",
        responses={
            "run --help": GhReply(stdout=help_text),
            "run": GhReply(returncode=run_exit, stdout=stream),
        },
    )


def fake_uv_cli(tmp_path: Path, *, default: GhReply | None = None) -> RecordingShim:
    """A ``uv`` shim that records argv and exits per *default* (0)."""
    return RecordingShim(tmp_path, "uv", default=default)


def fake_pre_commit_cli(
    tmp_path: Path, *, default: GhReply | None = None
) -> RecordingShim:
    """A ``pre-commit`` shim that records argv and exits per *default* (0)."""
    return RecordingShim(tmp_path, "pre-commit", default=default)


# --- In-process gh fakes -------------------------------------------------------

#: Base URL :class:`FakeGh` mints issue URLs under.
FAKE_ISSUE_URL_BASE = "https://github.com/owner/repo/issues/"

#: First issue number :class:`FakeGh` hands out.
_FIRST_FAKE_NUMBER = 100


class FakeGh:
    """Test double for ``cw.sprint``'s ``GhSurface`` and the ``DebtTracker`` seam.

    Records call order (title-qualified for ``create_issue``, number-qualified
    for the milestone-scoped lookups) and every issue body created or updated,
    so tests can assert on both call sequencing and content without a real
    ``gh`` binary.

    ``existing`` seeds titles already filed under the milestone (idempotent
    re-entry); ``milestone_exists`` is deliberately decoupled from it so a
    milestone that already exists but has zero issues filed yet (R6) can be
    modeled distinctly from "milestone + all issues already exist".

    The debt calls (#3041) read and write ``open_issues`` (number to body, the
    open ``review-debt`` issues ``list_open_issues_by_label`` returns),
    ``bodies`` and ``comments``. Each ``fail_*`` switch yields a typed failure.
    ``on_call`` runs before every call, so a test can advance a fake clock.

    The close-sweep calls (#3044): ``pr_states`` maps a PR URL to its raw
    state (a missing URL reads as None, unknown); ``issue_states`` maps an
    issue number to its state (missing reads as ``OPEN``); ``fail_issue_state``
    holds ticket ids whose state read fails. ``fail_close`` is a queue of
    failures popped one per ``close_issue`` call; ``closed`` records every
    successful (number, comment, reason). A successful close drops the issue
    from ``open_issues`` and marks it ``CLOSED``.
    """

    def __init__(
        self,
        *,
        existing: dict[str, int] | None = None,
        milestone_exists: bool = False,
    ) -> None:
        self.existing: dict[str, int] = dict(existing or {})
        self.milestone_exists = milestone_exists
        self.milestone_number = 1
        self.calls: list[str] = []
        self.bodies: dict[int, str] = {}
        self.issue_labels: dict[str, list[str]] = {}
        self.fail_find_milestone = False
        self.fail_create_milestone = False
        self.fail_milestone_issue_titles = False
        self.fail_create_issue: set[str] = set()
        self.fail_update_issue_body_at: set[int] = set()
        self._update_issue_body_calls = 0
        self._next_number = _FIRST_FAKE_NUMBER
        # Debt-filing state (#3041).
        self.open_issues: dict[int, str] = {}
        self.comments: dict[str, list[str]] = {}
        self.labels_ensured: list[tuple[str, str, str]] = []
        self.timeouts: list[tuple[str, int | None]] = []
        self.lock_snapshots: list[tuple[HeldLock, ...]] = []
        self.fail_create_kind: GhFailure | None = None
        self.fail_ensure_label: GhFailure | None = None
        self.fail_list: GhFailure | None = None
        self.list_truncated = False
        self.fail_fetch_body: set[str] = set()
        self.fail_post_comment: set[str] = set()
        self.on_call: Callable[[str], None] | None = None
        # Close-sweep state (#3044).
        self.pr_states: dict[str, str | None] = {}
        self.issue_states: dict[int, str] = {}
        self.fail_issue_state: set[str] = set()
        self.fail_close: list[GhFailure] = []
        self.closed: list[tuple[int, str, str]] = []

    def _record(self, name: str, timeout: int | None = None) -> None:
        self.lock_snapshots.append(held_locks())
        self.timeouts.append((name, timeout))
        if self.on_call is not None:
            self.on_call(name)

    def find_milestone(self, title: str) -> tuple[int | None, bool]:
        self.calls.append(f"find_milestone:{title}")
        if self.fail_find_milestone:
            return None, False
        if self.milestone_exists:
            return self.milestone_number, True
        return None, True

    def create_milestone(self, title: str) -> int | None:
        self.calls.append(f"create_milestone:{title}")
        if self.fail_create_milestone:
            return None
        return self.milestone_number

    def milestone_issue_titles(
        self, milestone: int
    ) -> tuple[dict[str, int] | None, bool]:
        self.calls.append(f"milestone_issue_titles:{milestone}")
        if self.fail_milestone_issue_titles:
            return None, False
        return dict(self.existing), True

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: list[str],
        milestone: int | None = None,
        timeout: int | None = None,
    ) -> IssueCreateResult:
        del milestone
        self.calls.append(f"create_issue:{title}")
        self._record("create_issue", timeout)
        if title in self.fail_create_issue:
            return IssueCreateResult(None, None, GhFailure.OTHER, "create failed")
        if self.fail_create_kind is not None:
            detail = f"{self.fail_create_kind.value} failure"
            return IssueCreateResult(None, None, self.fail_create_kind, detail)
        number = self._next_number
        self._next_number += 1
        self.existing[title] = number
        self.bodies[number] = body
        self.open_issues[number] = body
        self.issue_labels[title] = labels
        return IssueCreateResult(number, f"{FAKE_ISSUE_URL_BASE}{number}", None, "")

    def update_issue_body(
        self, number: int, body: str, *, timeout: int | None = None
    ) -> bool:
        self.calls.append(f"update_issue_body:{number}")
        self._record("update_issue_body", timeout)
        call_index = self._update_issue_body_calls
        self._update_issue_body_calls += 1
        if call_index in self.fail_update_issue_body_at:
            return False
        self.bodies[number] = body
        if number in self.open_issues:
            self.open_issues[number] = body
        return True

    def ensure_label(
        self,
        name: str,
        color: str,
        description: str,
        *,
        timeout: int | None = None,
    ) -> GhFailure | None:
        self.calls.append(f"ensure_label:{name}")
        self._record("ensure_label", timeout)
        if self.fail_ensure_label is not None:
            return self.fail_ensure_label
        self.labels_ensured.append((name, color, description))
        return None

    def list_open_issues_by_label(
        self, label: str, *, timeout: int | None = None
    ) -> LabelledIssuesLookup:
        self.calls.append(f"list_open_issues_by_label:{label}")
        self._record("list_open_issues_by_label", timeout)
        if self.fail_list is not None:
            return LabelledIssuesLookup((), self.fail_list, "list failed", False)
        issues = tuple(
            LabelledIssue(number, body)
            for number, body in sorted(self.open_issues.items())
        )
        return LabelledIssuesLookup(issues, None, "", self.list_truncated)

    def fetch_issue_body(
        self, ticket_id: str, *, timeout: int | None = None
    ) -> str | None:
        self.calls.append(f"fetch_issue_body:{ticket_id}")
        self._record("fetch_issue_body", timeout)
        if ticket_id in self.fail_fetch_body:
            return None
        return self.bodies.get(int(ticket_id))

    def post_issue_comment(
        self, ticket_id: str, body: str, *, timeout: int | None = None
    ) -> bool:
        self.calls.append(f"post_issue_comment:{ticket_id}")
        self._record("post_issue_comment", timeout)
        if ticket_id in self.fail_post_comment:
            return False
        self.comments.setdefault(ticket_id, []).append(body)
        return True

    def fetch_issue_state(
        self, ticket_id: str, *, timeout: int | None = None
    ) -> str | None:
        self.calls.append(f"fetch_issue_state:{ticket_id}")
        self._record("fetch_issue_state", timeout)
        if ticket_id in self.fail_issue_state:
            return None
        return self.issue_states.get(int(ticket_id), "OPEN")

    def fetch_pr_state(self, pr_url: str, *, timeout: int | None = None) -> str | None:
        self.calls.append(f"fetch_pr_state:{pr_url}")
        self._record("fetch_pr_state", timeout)
        return self.pr_states.get(pr_url)

    def close_issue(
        self, number: int, comment: str, *, reason: str, timeout: int | None = None
    ) -> GhFailure | None:
        self.calls.append(f"close_issue:{number}")
        self._record("close_issue", timeout)
        if self.fail_close:
            return self.fail_close.pop(0)
        self.closed.append((number, comment, reason))
        self.open_issues.pop(number, None)
        self.issue_states[number] = "CLOSED"
        return None


class FakePostCommentGh:
    """``cw.tracker_post`` posting fake: posts land in a thread the read-back fetches.

    URLs are ``f"{url_base}{n}"`` for the n-th post. ``results`` queues raw
    ``post`` outcomes that replace the default success; ``tamper`` makes the
    stored body differ from what was posted.
    """

    def __init__(self, url_base: str) -> None:
        self.url_base = url_base
        self.posts: list[tuple[str, str, Path | None]] = []
        self.thread: list[dict[str, object]] = []
        self.visibility: str | None = "PUBLIC"
        self.visibility_calls = 0
        self.visibility_cwds: list[Path | None] = []
        self.fetch_cwds: list[Path | None] = []
        self.results: list[subprocess.CompletedProcess[bytes] | None] = []
        self.tamper = False

    def post(
        self, ticket_id: str, body: str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes] | None:
        self.posts.append((ticket_id, body, cwd))
        if self.results:
            return self.results.pop(0)
        url = f"{self.url_base}{len(self.posts)}"
        stored = agent_marked_body(body + (" edited" if self.tamper else ""))
        self.thread.append({"url": url, "body": stored})
        return subprocess.CompletedProcess(["gh"], 0, url.encode() + b"\n", b"")

    def fetch(
        self, _ticket_id: str, _timeout: int, *, cwd: Path | None = None
    ) -> list[dict[str, object]]:
        self.fetch_cwds.append(cwd)
        return list(self.thread)

    def fetch_visibility(self, *, timeout: int, cwd: Path | None) -> str | None:
        del timeout
        self.visibility_cwds.append(cwd)
        self.visibility_calls += 1
        return self.visibility


@functools.cache
def _prep_pr_finalize_run() -> Callable[..., subprocess.CompletedProcess[str]]:
    """The real ``run`` of a private, never-patched copy of the finalize script."""
    spec = importlib.util.spec_from_file_location(
        "_tool_fakes_prep_pr_finalize", _PREP_PR_FINALIZE
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclass`` resolves the defining module through ``sys.modules``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    real_run: Callable[..., subprocess.CompletedProcess[str]] = module.run
    return real_run


class ReadyGh:
    """In-process ``prep_pr_finalize.run`` stub: scripted ``isDraft`` reads.

    ``gh pr ready`` returns *ready_rc* / *ready_stderr*; every other ``gh``
    call pops the next entry of *drafts* (``None`` is an HTTP 502). A non-gh
    command is handed to *real_run*, by default the script's own real ``run``
    (so ``git`` keeps the script's ``GIT_*`` strip).
    """

    def __init__(
        self,
        drafts: list[bool | None],
        *,
        ready_rc: int = 0,
        ready_stderr: str = "",
        view_stdout: str | None = None,
        real_run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.drafts = drafts
        self.ready_rc = ready_rc
        self.ready_stderr = ready_stderr
        self.view_stdout = view_stdout
        self.real_run = real_run
        self.calls: list[list[str]] = []
        self.cwds: list[Path | None] = []

    def __call__(
        self,
        cmd: list[str],
        check: bool = False,
        capture: bool = True,
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:1] != ["gh"]:
            real_run = self.real_run or _prep_pr_finalize_run()
            return real_run(cmd, check, capture, cwd=cwd)
        self.calls.append(list(cmd))
        self.cwds.append(cwd)
        if cmd[:3] == ["gh", "pr", "ready"]:
            return subprocess.CompletedProcess(
                cmd, self.ready_rc, "", self.ready_stderr
            )
        draft = self.drafts.pop(0)
        if draft is None:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
        stdout = self.view_stdout or json.dumps({"isDraft": draft})
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


@dataclass
class FakeGhComment:
    """``cw.gh._sp.run`` recorder for ``gh issue comment`` (no subprocess).

    ``calls`` holds each argv; ``bodies`` the stdin body of each call at the
    same index (``post_issue_comment`` sends ``--body-file -`` bytes on stdin).
    """

    bodies: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        stdin = kwargs.get("input")
        self.bodies.append(stdin.decode("utf-8") if isinstance(stdin, bytes) else "")
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGhComment:
        """Patch ``cw.gh._sp.run`` with this recorder; return it."""
        monkeypatch.setattr("cw.gh._sp.run", self)
        return self


# What an installed but unauthenticated ``gh`` prints and exits with -- the CI
# runner's condition: gh is preinstalled there and no token reaches the tests.
GH_UNAUTHENTICATED = GhReply(
    4,
    "",
    "To get started with GitHub CLI, please run:  gh auth login\n"
    "Alternatively, populate the GH_TOKEN environment variable with a GitHub API "
    "authentication token.\n",
)


def _is_gh(argv: object) -> bool:
    return isinstance(argv, (list, tuple)) and bool(argv) and argv[0] == "gh"


@dataclass
class GhCall:
    """One ``gh`` launch as :class:`FakeGhCli` saw it (``records``)."""

    argv: list[str]
    cwd: Path | None
    timeout: object
    stdin: bytes | None
    body_file: str | None


@dataclass
class _ScriptedReply:
    prefix: tuple[str, ...]
    reply: GhReply
    raises: BaseException | None


class FakeGhCli:
    """A scripted ``subprocess.run`` for ``gh`` that passes everything else through.

    Script outcomes with :meth:`reply` (argv prefix after ``gh``). A ``gh``
    launch is recorded in :attr:`calls` (argv only) and :attr:`records` (argv
    plus cwd, timeout, stdin and ``--body-file`` text), and each is answered by
    the longest matching prefix; on a tie the most recently scripted wins, so a
    test can override a default. :attr:`lock_snapshots` holds the
    ``cw._lock_guard.held_locks()`` seen at every launch. Any other launch goes
    to the real ``subprocess.run``. Output is ``str`` when the caller asked for
    text, ``bytes`` otherwise.

    With *stub_root*, a ``gh`` whose binary resolves inside that directory (a
    test's own ``stub_gh`` shim on the child's ``PATH``) runs for real instead.
    """

    def __init__(
        self, *, default: GhReply | None = None, stub_root: Path | None = None
    ) -> None:
        self.default = default or GhReply(1, "", "FakeGhCli: unscripted gh call")
        self.stub_root = stub_root
        self.calls: list[list[str]] = []
        self.records: list[GhCall] = []
        self.lock_snapshots: list[tuple[HeldLock, ...]] = []
        self._replies: list[_ScriptedReply] = []

    def _runs_a_stub(self, argv: Sequence[str], kwargs: Mapping[str, object]) -> bool:
        return _resolves_inside(argv, self.stub_root, kwargs, "gh")

    def reply(
        self,
        *prefix: str,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> FakeGhCli:
        """Answer ``gh <prefix...>`` with the given outcome; chainable.

        *raises* makes the matching launch raise instead (a timeout or ``OSError``).
        A leading ``"gh"`` in *prefix* is accepted and dropped, so a prefix may
        name the whole command (``reply("gh", "issue", "create")``).
        """
        if prefix[:1] == ("gh",):
            prefix = prefix[1:]
        self._replies.append(
            _ScriptedReply(prefix, GhReply(returncode, stdout, stderr), raises)
        )
        return self

    def argvs(self) -> list[list[str]]:
        """Every recorded ``gh`` argv, in call order."""
        return [list(record.argv) for record in self.records]

    def _match(self, args: Sequence[str]) -> _ScriptedReply | None:
        best: _ScriptedReply | None = None
        for scripted in self._replies:
            if tuple(args[: len(scripted.prefix)]) == scripted.prefix and (
                best is None or len(scripted.prefix) >= len(best.prefix)
            ):
                best = scripted
        return best

    def _record(self, argv: Sequence[str], kwargs: Mapping[str, object]) -> None:
        self.lock_snapshots.append(held_locks())
        self.calls.append(list(argv))
        cwd = kwargs.get("cwd")
        stdin = kwargs.get("input")
        body_file = None
        if "--body-file" in argv:
            target = argv[list(argv).index("--body-file") + 1]
            if target != "-":
                body_file = Path(target).read_text(encoding="utf-8")
        self.records.append(
            GhCall(
                list(argv),
                Path(str(cwd)) if cwd is not None else None,
                kwargs.get("timeout"),
                stdin if isinstance(stdin, bytes) else None,
                body_file,
            )
        )

    def __call__(
        self, argv: Sequence[str], *args: object, **kwargs: object
    ) -> _Completed:
        if not _is_gh(argv) or self._runs_a_stub(argv, kwargs):
            return _REAL_RUN(argv, *args, **kwargs)
        self._record(argv, kwargs)
        scripted = self._match(list(argv[1:]))
        if scripted is not None and scripted.raises is not None:
            raise scripted.raises
        reply = self.default if scripted is None else scripted.reply
        if (
            kwargs.get("text")
            or kwargs.get("universal_newlines")
            or kwargs.get("encoding")
        ):
            return subprocess.CompletedProcess(
                list(argv), reply.returncode, reply.stdout, reply.stderr
            )
        return subprocess.CompletedProcess(
            list(argv),
            reply.returncode,
            reply.stdout.encode(),
            reply.stderr.encode(),
        )

    def install(
        self, monkeypatch: pytest.MonkeyPatch, target: str = "subprocess.run"
    ) -> FakeGhCli:
        """Patch *target* (dotted path) with this fake; return it."""
        monkeypatch.setattr(target, self)
        return self


class AbsentToolCli:
    """A ``subprocess.run`` on which the named tools are not installed.

    A launch of one of *names* raises ``FileNotFoundError(ENOENT)`` -- what CI
    raises for ``claude``, ``codex`` and ``opencode``, none of which its runner
    has -- and is recorded in :attr:`calls`; unless the binary resolves inside
    *stub_root* (a test's own shim), which runs. Everything else is handed to
    the ``subprocess.run`` that was installed before this one.
    """

    def __init__(
        self, names: Collection[str], *, stub_root: Path | None = None
    ) -> None:
        self.names = frozenset(names)
        self.stub_root = stub_root
        self.calls: list[list[str]] = []
        self._next: Callable[..., _Completed] = subprocess.run

    def __call__(self, argv: object, *args: object, **kwargs: object) -> _Completed:
        name = _absent_name(argv, self.names)
        if name is None or _resolves_inside(argv, self.stub_root, kwargs, name):
            return self._next(argv, *args, **kwargs)
        self.calls.append(
            [str(arg) for arg in argv]
            if isinstance(argv, (list, tuple))
            else [str(argv)]
        )
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> AbsentToolCli:
        """Chain onto the current ``subprocess.run``; return this fake."""
        self._next = subprocess.run
        monkeypatch.setattr(subprocess, "run", self)
        return self


def _absent_name(argv: object, names: frozenset[str]) -> str | None:
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    first = argv[0]
    return first if isinstance(first, str) and first in names else None


def _resolves_inside(
    argv: object, root: Path | None, kwargs: Mapping[str, object], name: str
) -> bool:
    if root is None:
        return False
    env = kwargs.get("env")
    return (
        blocked_binary(
            argv,
            {name},
            root=root,
            env=env if isinstance(env, Mapping) else None,
            cwd=kwargs.get("cwd"),
        )
        is None
    )


def restore_real_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite's ``subprocess.run`` tool defaults for one test.

    For tests of the launch guard itself, which need a launch to reach
    ``Popen`` (and so the guard) rather than be answered by a default fake.
    """
    monkeypatch.setattr(subprocess, "run", _REAL_RUN)


# --- codex ---------------------------------------------------------------------


@pytest.fixture
def fake_codex_job_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FakeFireAndForgetRunner]:
    """Swap CodexExecutor's detached ``cw codex run`` launcher for a fake.

    Keeps a dispatch-driven codex spawn from launching a real ``cw`` child
    (which would resolve the real, not the test's, state dirs).
    """
    runner = FakeFireAndForgetRunner()
    monkeypatch.setattr("cw.executor.codex.RealCodexJobRunner", lambda: runner)
    yield runner
    for proc in runner.procs:
        with contextlib.suppress(OSError):
            proc.kill()
            proc.wait()


@pytest.fixture(autouse=True)
def _mock_codex_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the codex filesystem-capability fingerprint probe (#1709).

    Without this, every test that reaches ``_prepare_review_pass`` for the
    first time in a process would shell out to the *host machine's* real
    ``codex --version``, making the runtime fingerprint (and therefore
    cache-hit/miss behavior) depend on whatever happens to be installed.
    Patching both seams autouse makes the fingerprint deterministic; the probe
    itself still runs for real against the mocked boundary.

    The patch targets are ``_capability``'s own module-level seam functions,
    NOT ``cw.codex_review._capability.subprocess.run`` / ``.shutil.which``:
    those attribute paths resolve to the *global* ``subprocess``/``shutil``
    module objects, so patching them autouse would replace ``subprocess.run``
    process-wide and break every git helper in this suite.

    ``tests/test_codex_capability.py`` re-patches the same two names directly
    for its binary-absent/timeout/non-zero-exit/unparseable cases; pytest's
    patch stack lets the test-level patch win. Registered suite-wide by its
    binding in ``tests/conftest.py``.
    """
    monkeypatch.setattr(
        "cw.codex_review._capability._which_codex", lambda: "/usr/bin/codex"
    )
    monkeypatch.setattr(
        "cw.codex_review._capability._run_codex_version",
        lambda _timeout_seconds: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="codex-cli 0.144.5\n", stderr=""
        ),
    )


# --- scripted codex runner -----------------------------------------------------

#: What a :class:`ScriptedCodexRunner` rule matches on: ``(argv, stdin)``.
CodexMatcher = Callable[[list[str], str | None], bool]
#: A rule's reply: a fixed result, or one computed from ``(argv, stdin)``.
CodexReply = Callable[[list[str], str | None], CodexRunResult] | CodexRunResult


def stdin_contains(*fragments: str) -> CodexMatcher:
    """A matcher true when every fragment occurs in the call's stdin."""

    def _match(_argv: list[str], stdin: str | None) -> bool:
        return stdin is not None and all(fragment in stdin for fragment in fragments)

    return _match


def any_call(_argv: list[str], _stdin: str | None) -> bool:
    """A matcher true for every call."""
    return True


@dataclass
class _CodexRule:
    matcher: CodexMatcher
    reply: CodexReply
    remaining: int | None


class ScriptedCodexRunner:
    """A ``CodexRunner`` that answers each call from an ordered list of rules.

    A rule is ``(matcher over (argv, stdin)) -> reply``; the first rule whose
    matcher accepts a call answers it, and a rule added with ``times=N`` is
    spent after N answers (``None``: unlimited). A call no rule answers raises
    ``AssertionError`` naming it, so a test never passes on an unplanned call.

    Records every call in ``calls`` (the ``FakeCodexRunner`` shape, plus
    ``probe``) and writes nothing itself: a result's ``output_file_content`` is
    handed back as-is, which is what the real runner reads from the ``-o`` file.
    A reply may be a callable, which is how a test makes a call touch the
    worktree.

    The filesystem-capability probe call ``_prepare_review_pass`` makes with no
    cached verdict is answered here (capable) without a rule, and flagged
    ``probe=True``; ``review_calls`` lists every other call.
    """

    def __init__(self) -> None:
        self._rules: list[_CodexRule] = []
        self.calls: list[dict[str, object]] = []

    def add(
        self, matcher: CodexMatcher, reply: CodexReply, *, times: int | None = None
    ) -> ScriptedCodexRunner:
        """Append a rule; returns ``self`` so rules chain."""
        self._rules.append(_CodexRule(matcher, reply, times))
        return self

    @property
    def review_calls(self) -> list[dict[str, object]]:
        """Every recorded call except the capability probe."""
        return [call for call in self.calls if not call["probe"]]

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        from cw.codex_review._capability import _PROBE_SENTINEL

        probe = "--skip-git-repo-check" in argv and "--output-schema" not in argv
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "timeout": timeout_seconds,
                "stdin": stdin,
                "probe": probe,
            }
        )
        if probe:
            return CodexRunResult(
                returncode=0, stdout=f"{_PROBE_SENTINEL}\n", stderr=""
            )
        for rule in self._rules:
            if rule.remaining == 0 or not rule.matcher(argv, stdin):
                continue
            if rule.remaining is not None:
                rule.remaining -= 1
            if isinstance(rule.reply, CodexRunResult):
                return rule.reply
            return rule.reply(argv, stdin)
        msg = f"ScriptedCodexRunner: no rule answers argv={argv!r}"
        raise AssertionError(msg)


def marked_parts(*bodies: str) -> tuple[str, ...]:
    """The tracker-IO turn's emitted parts: each body plus the agent marker."""
    return tuple(agent_marked_body(body) for body in bodies)


def tracker_io_turn_result(
    ticket_id: str,
    parts: Sequence[str],
    *,
    status: str = "ok",
    detail: str = "",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """A strict-schema tracker-IO turn result that verifies against *parts*.

    Part ``i`` gets comment id ``cmt-<i>``, a Linear-shaped URL naming
    *ticket_id* and that id, and read-back windows taken verbatim from the
    marked part (the first and last ``READBACK_WINDOW`` characters). *mutate*
    edits the document before it is serialised, to build a failing case.
    """
    window = READBACK_WINDOW
    doc: dict[str, Any] = {
        "status": status,
        "detail": detail,
        "ticket_id": ticket_id,
        "parts": [
            {
                "index": index,
                "comment_id": f"cmt-{index}",
                "url": (
                    f"https://linear.app/acme/issue/{ticket_id}/x#comment-cmt-{index}"
                ),
                "readback_head": part[:window],
                "readback_tail": part[-window:],
            }
            for index, part in enumerate(parts, start=1)
        ],
    }
    if mutate is not None:
        mutate(doc)
    return json.dumps(doc)
