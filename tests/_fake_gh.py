"""Shared ``gh`` test doubles (#3041).

Two doubles, one per abstraction level:

- :class:`FakeGh` is a ``GhSurface``/``DebtTracker``-level double, injected
  into ``cw.sprint.apply_plan`` and the ``cw.review_debt_filing`` library. It
  moved here verbatim from ``tests/test_sprint.py`` and gained the debt calls.
- :class:`FakeGhCli` is a subprocess-level double. ``FakeGhCli.install``
  patches the ``_sp`` binding in the ``cw.gh`` submodules the debt path spawns
  through, so ``cw.gh`` and CLI end-to-end tests never reach a real ``gh``.

Both record ``cw._lock_guard.held_locks()`` on every call in
``lock_snapshots``: the autouse lock harness only sees a real ``Popen`` under
``sessions_lock``, so the filing tests assert every snapshot is ``()``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from cw._lock_guard import HeldLock, held_locks
from cw.gh import (
    GhFailure,
    IssueCreateResult,
    LabelledIssue,
    LabelledIssuesLookup,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

#: Module-level ``_sp`` bindings the debt path spawns through.
_SP_MODULES = ("cw.gh.filing", "cw.gh.issues", "cw.gh.pr_reads", "cw.gh.probes")

#: Base URL :class:`FakeGh` mints issue URLs under.
FAKE_ISSUE_URL_BASE = "https://github.com/owner/repo/issues/"

#: First issue number :class:`FakeGh` hands out.
_FIRST_FAKE_NUMBER = 100


class FakeGh:
    """Test double for GhSurface. Records call order (title-qualified for
    create_issue, number-qualified for the milestone-scoped lookups) and
    every issue body created/updated, so tests can assert on both call
    sequencing and content without a real `gh` binary.

    ``existing`` seeds titles already filed under the milestone (idempotent
    re-entry); ``milestone_exists`` is deliberately decoupled from it so a
    milestone that already exists but has zero issues filed yet (R6) can be
    modeled distinctly from "milestone + all issues already exist".

    The debt calls (#3041) read and write ``open_issues`` (number to body, the
    open ``review-debt`` issues ``list_open_issues_by_label`` returns),
    ``bodies`` and ``comments``. Each ``fail_*`` switch yields a typed failure.
    ``on_call`` runs before every call, so a test can advance a fake clock.
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


@dataclass
class GhCall:
    """One scripted ``gh`` invocation as :class:`FakeGhCli` saw it."""

    argv: list[str]
    cwd: Path | None
    timeout: object
    stdin: bytes | None
    body_file: str | None


@dataclass
class _Script:
    prefix: tuple[str, ...]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    raises: BaseException | None = None


@dataclass
class FakeGhCli:
    """Scripted ``subprocess`` double for the ``cw.gh`` submodules.

    ``on(*prefix, ...)`` scripts the response for any argv that starts with
    *prefix*; the most recently added matching script wins, so a test can
    override a default. An unscripted call raises ``AssertionError`` instead of
    reaching a real ``gh``. Output is ``str`` when the caller passed
    ``text=True`` and ``bytes`` otherwise, matching what each call site reads.
    """

    calls: list[GhCall] = field(default_factory=list)
    lock_snapshots: list[tuple[HeldLock, ...]] = field(default_factory=list)
    scripts: list[_Script] = field(default_factory=list)

    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess

    def on(
        self,
        *prefix: str,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        """Script the response for argv starting with *prefix*."""
        self.scripts.append(_Script(prefix, returncode, stdout, stderr, raises))

    def argvs(self) -> list[list[str]]:
        """Every recorded argv, in call order."""
        return [call.argv for call in self.calls]

    def run(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[object]:
        """The patched ``subprocess.run``."""
        self.lock_snapshots.append(held_locks())
        cwd = kwargs.get("cwd")
        stdin = kwargs.get("input")
        body_file = None
        if "--body-file" in cmd:
            target = cmd[cmd.index("--body-file") + 1]
            if target != "-":
                body_file = Path(target).read_text(encoding="utf-8")
        self.calls.append(
            GhCall(
                list(cmd),
                Path(str(cwd)) if cwd is not None else None,
                kwargs.get("timeout"),
                stdin if isinstance(stdin, bytes) else None,
                body_file,
            )
        )
        for script in reversed(self.scripts):
            if tuple(cmd[: len(script.prefix)]) == script.prefix:
                if script.raises is not None:
                    raise script.raises
                if kwargs.get("text"):
                    return subprocess.CompletedProcess(
                        cmd, script.returncode, script.stdout, script.stderr
                    )
                return subprocess.CompletedProcess(
                    cmd,
                    script.returncode,
                    script.stdout.encode("utf-8"),
                    script.stderr.encode("utf-8"),
                )
        msg = f"unscripted gh call: {cmd!r}"
        raise AssertionError(msg)

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
        """Patch ``_sp`` in every ``cw.gh`` submodule the debt path uses."""
        fake = cls()
        seam = SimpleNamespace(
            run=fake.run,
            TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
        )
        for module in _SP_MODULES:
            monkeypatch.setattr(f"{module}._sp", seam)
        return fake
