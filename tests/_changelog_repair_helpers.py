"""Shared fakes and builders for the repair_changelog_conflict tests (#2771).

- :func:`build_release_moved_repo` builds the real-git release-moved history
  (bare origin, a client clone, ``.gitattributes: CHANGELOG.md merge=union``).
- :class:`ScriptedGit` stands in for ``cw._git.run_git`` in the repair git and
  fence modules, keyed on argv prefixes (``-c`` options stripped), recording
  every call with its cwd and timeout.
- :class:`FakeGhHead` is a stateful stub for ``cw.gh.fetch_pr_head`` and
  ``cw.gh.disarm_pr_auto_merge``.
- :class:`FakeArmScript` plants an executable ``prep_pr_finalize.py`` that
  records its argv and exits with a chosen code. It runs as a separate
  subprocess, so it can never flip :class:`FakeGhHead`'s armed flag.
- :func:`make_repair_job` builds a ``_RepairJob`` the way the act phase does.

Every fake appends to one shared ``log`` list, so a test can assert the order
of git calls, gh calls and recorded events against each other
(:func:`track_events` adds the events).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from cw import gh
from cw.events import record_event
from cw.gh import PrHead
from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.reconcile.review_recipes import _shared, changelog_repair
from cw.reconcile.review_recipes import changelog_repair_fence as fence_mod
from cw.reconcile.review_recipes import changelog_repair_git as git_mod
from cw.reconcile.review_recipes.changelog_repair import (
    _repair_payload_base,
    _RepairJob,
)
from cw.reconcile.review_recipes.changelog_repair_fence import build_fence_snapshot
from tests.conftest import git_in, init_bare_origin
from tests.test_pr_hydrate import _pr_state
from tests.test_reconcile_gate_recipes import _make_task

if TYPE_CHECKING:
    import pytest

    from cw.reconcile.review_recipes.changelog_repair_fence import FenceSnapshot

TICKET = "GEN-1"
CLIENT = "acme"
BRANCH = f"dev/{TICKET}"
PR_URL = "https://github.com/acme/widgets/pull/42"
PR_NUMBER = "42"
EXPECTED_SHA = "a" * 40
NEW_SHA = "b" * 40
MB_SHA = "c" * 40
DEFAULT_REF = "refs/remotes/origin/main"

LogEntry = tuple[str, ...]

PREAMBLE = "# Changelog\n\n"
RELEASED_1_0_0 = "## [1.0.0] - 2026-01-01\n\n### Added\n\n- Initial (#0).\n"
CL_BASE = (
    PREAMBLE
    + "## [Unreleased]\n\n### Added\n\n- Alpha feature (#1).\n\n"
    + "### Fixed\n\n- Beta fix (#2).\n\n"
    + RELEASED_1_0_0
)
CL_BRANCH = (
    PREAMBLE
    + "## [Unreleased]\n\n### Added\n\n- Alpha feature (#1).\n\n"
    + "### Fixed\n\n- Beta fix (#2).\n\n- Gamma fix (#3).\n\n"
    + RELEASED_1_0_0
)
RELEASED_1_1_0 = (
    "## [1.1.0] - 2026-02-01\n\n### Added\n\n- Alpha feature (#1).\n\n"
    "### Fixed\n\n- Beta fix (#2).\n\n"
)
CL_MAIN = PREAMBLE + "## [Unreleased]\n\n" + RELEASED_1_1_0 + RELEASED_1_0_0
CL_EXPECTED = (
    PREAMBLE
    + "## [Unreleased]\n\n### Fixed\n\n- Gamma fix (#3).\n\n"
    + RELEASED_1_1_0
    + RELEASED_1_0_0
)


# --- real git ---------------------------------------------------------------


class ReleaseMovedRepo(NamedTuple):
    clone: Path
    origin: Path
    base_sha: str
    main_sha: str
    branch_sha: str


def _commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    for relpath, content in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git_in(repo, "add", relpath)
    git_in(repo, "commit", "-m", message)
    return git_in(repo, "rev-parse", "HEAD")


def build_release_moved_repo(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    branch_code: str = "def app():\n    return 2\n",
    main_code_file: str = "src/other.py",
    main_code: str = "OTHER = 2\n",
    union: bool = True,
    split_branch_commits: bool = False,
    changelog_eol: str = "\n",
    main_eol: str | None = None,
    main_changelog: str = CL_MAIN,
    extra_base_files: dict[str, str] | None = None,
    tags: bool = False,
) -> ReleaseMovedRepo:
    """Base commit; main renames Unreleased to 1.1.0 (plus a code edit); the
    branch adds Gamma under the old Unreleased (plus a code change).

    The clone is left on ``main`` at the base commit, with both refs pushed to
    the bare origin; ``origin`` sits beside the clone.

    *union* writes ``.gitattributes: CHANGELOG.md merge=union``; without it a
    rebase stops on CHANGELOG.md. *split_branch_commits* makes the branch two
    commits, the second touching only CHANGELOG.md. *changelog_eol* /
    *main_eol* set the line endings of the base+branch / main CHANGELOG.md.
    *tags* tags the base ``v1.0.0`` and main ``v1.1.0`` (pushed to origin).
    """
    eol = changelog_eol
    main_nl = eol if main_eol is None else main_eol
    clone = make_git_repo("client")
    base_files = {
        "CHANGELOG.md": CL_BASE.replace("\n", eol),
        "src/app.py": "def app():\n    return 1\n",
        "src/other.py": "OTHER = 1\n",
        **(extra_base_files or {}),
    }
    if union:
        base_files[".gitattributes"] = "CHANGELOG.md merge=union\n"
    base_sha = _commit_files(clone, base_files, "base")
    if tags:
        git_in(clone, "tag", "v1.0.0")
    origin = init_bare_origin(clone)
    git_in(clone, "push", "origin", "main")
    git_in(clone, "checkout", "-b", BRANCH)
    branch_changelog = CL_BRANCH.replace("\n", eol)
    if split_branch_commits:
        _commit_files(clone, {"src/app.py": branch_code}, "feat: gamma code (#3)")
        branch_sha = _commit_files(
            clone, {"CHANGELOG.md": branch_changelog}, "docs: gamma entry (#3)"
        )
    else:
        branch_sha = _commit_files(
            clone,
            {"CHANGELOG.md": branch_changelog, "src/app.py": branch_code},
            "feat: gamma (#3)",
        )
    git_in(clone, "push", "origin", BRANCH)
    git_in(clone, "checkout", "main")
    main_sha = _commit_files(
        clone,
        {
            "CHANGELOG.md": main_changelog.replace("\n", main_nl),
            main_code_file: main_code,
        },
        "chore(release): 1.1.0",
    )
    if tags:
        git_in(clone, "tag", "v1.1.0")
    git_in(clone, "push", "origin", "main")
    if tags:
        git_in(clone, "push", "origin", "--tags")
    git_in(clone, "reset", "--hard", base_sha)
    git_in(clone, "branch", "-D", BRANCH)
    return ReleaseMovedRepo(clone, origin, base_sha, main_sha, branch_sha)


# --- ScriptedGit ------------------------------------------------------------


class GitCall(NamedTuple):
    argv: tuple[str, ...]
    cwd: Path | None
    timeout: float | None


class GitResult(NamedTuple):
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


TIMEOUT = "timeout"
OSERROR = "oserror"
Response = GitResult | str | Callable[[tuple[str, ...]], GitResult]


def strip_flags(argv: tuple[str, ...]) -> tuple[str, ...]:
    """*argv* without its leading ``-c <key=value>`` pairs."""
    while argv and argv[0] == "-c":
        argv = argv[2:]
    return argv


def _mkdir_scratch(argv: tuple[str, ...]) -> GitResult:
    Path(argv[3]).mkdir(parents=True, exist_ok=True)
    return GitResult()


def _rm_scratch(argv: tuple[str, ...]) -> GitResult:
    shutil.rmtree(argv[3], ignore_errors=True)
    return GitResult()


def happy_rules(
    *, base: str = CL_BASE, branch: str = CL_BRANCH, main: str = CL_MAIN
) -> list[tuple[tuple[str, ...], Response]]:
    """Responses for a repair that succeeds up to and including the push."""
    return [
        (("fetch", "origin"), GitResult()),
        (("rev-parse", "--verify", "--quiet"), GitResult(stdout=f"{EXPECTED_SHA}\n")),
        (("merge-base", DEFAULT_REF), GitResult(stdout=f"{MB_SHA}\n")),
        (("merge-tree",), GitResult(stdout="tree0\n")),
        (("diff", "--name-only", "--diff-filter=U"), GitResult()),
        (("diff", "--name-only"), GitResult(stdout="CHANGELOG.md\n")),
        (("show", f"{MB_SHA}:CHANGELOG.md"), GitResult(stdout=base)),
        (("show", f"{EXPECTED_SHA}:CHANGELOG.md"), GitResult(stdout=branch)),
        (("show", f"{DEFAULT_REF}:CHANGELOG.md"), GitResult(stdout=main)),
        (("rev-list", "--count"), GitResult(stdout="1\n")),
        (("worktree", "add"), _mkdir_scratch),
        (("worktree", "remove"), _rm_scratch),
        (("rev-parse", "HEAD"), GitResult(stdout=f"{NEW_SHA}\n")),
        (("diff", "-U0"), GitResult(stdout="+x = 1\n")),
        (("ls-remote",), GitResult(stdout=f"{NEW_SHA}\trefs/heads/{BRANCH}\n")),
    ]


class ScriptedGit:
    """A ``run_git`` fake: first matching prefix wins, else exit 0 / no output."""

    def __init__(
        self,
        log: list[LogEntry],
        rules: list[tuple[tuple[str, ...], Response]] | None = None,
    ) -> None:
        self.log = log
        self.rules = list(rules if rules is not None else happy_rules())
        self.calls: list[GitCall] = []

    def set(self, prefix: tuple[str, ...], response: Response) -> None:
        """Override *prefix* ahead of every existing rule."""
        self.rules.insert(0, (prefix, response))

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        check: bool = False,
        capture_output: bool = False,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        full = tuple(str(a) for a in argv)
        self.calls.append(GitCall(full, None if cwd is None else Path(cwd), timeout))
        stripped = strip_flags(full)
        self.log.append(("git", *stripped))
        for prefix, response in self.rules:
            if stripped[: len(prefix)] == prefix:
                return self._respond(full, stripped, response, text=text)
        return subprocess.CompletedProcess(list(full), 0, "", "")

    @staticmethod
    def _respond(
        full: tuple[str, ...],
        stripped: tuple[str, ...],
        response: Response,
        *,
        text: bool,
    ) -> subprocess.CompletedProcess[Any]:
        if response == TIMEOUT:
            raise subprocess.TimeoutExpired(list(full), 1)
        if response == OSERROR:
            msg = "git vanished"
            raise OSError(msg)
        result = response(stripped) if callable(response) else response
        assert isinstance(result, GitResult)
        if not text:
            return subprocess.CompletedProcess(
                list(full),
                result.returncode,
                result.stdout.encode("utf-8"),
                result.stderr.encode("utf-8"),
            )
        return subprocess.CompletedProcess(
            list(full), result.returncode, result.stdout, result.stderr
        )

    def stripped(self) -> list[tuple[str, ...]]:
        return [strip_flags(call.argv) for call in self.calls]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> ScriptedGit:
        monkeypatch.setattr(git_mod, "run_git", self)
        monkeypatch.setattr(fence_mod, "run_git", self)
        return self


# --- FakeGhHead -------------------------------------------------------------

DISARM_OK = "ok"
DISARM_NONZERO_STILL_ARMED = "nonzero_still_armed"
DISARM_NONZERO_UNARMED = "nonzero_unarmed"
DISARM_ZERO_STILL_ARMED = "zero_still_armed"
DISARM_NONE = "none"


def make_head(
    ref_name: str = BRANCH,
    oid: str = EXPECTED_SHA,
    armed: bool = False,
    *,
    base_ref_name: str = "main",
    state: str = "OPEN",
) -> PrHead:
    """A ``PrHead`` that passes ``_read_head`` unless a field is overridden."""
    return PrHead(ref_name, oid, base_ref_name, state, armed)


class FakeGhHead:
    """Stateful ``fetch_pr_head`` / ``disarm_pr_auto_merge`` stub."""

    def __init__(
        self,
        log: list[LogEntry],
        *,
        armed: bool = False,
        ref_name: str = BRANCH,
        oid: str = EXPECTED_SHA,
        base_ref_name: str = "main",
        state: str = "OPEN",
        disarm: str = DISARM_OK,
        arm_after_push: bool = False,
        unreadable_after_disarm: bool = False,
        reads: dict[int, PrHead | None] | None = None,
    ) -> None:
        self.log = log
        self.armed = armed
        self.ref_name = ref_name
        self.oid = oid
        self.base_ref_name = base_ref_name
        self.state = state
        self.disarm_mode = disarm
        self.arm_after_push = arm_after_push
        self.unreadable_after_disarm = unreadable_after_disarm
        self.reads = reads or {}
        self.read_count = 0
        self.disarm_calls = 0
        self.timeouts: list[int] = []

    def _pushed(self) -> bool:
        return any(entry[:2] == ("git", "push") for entry in self.log)

    def fetch_pr_head(
        self, pr_ref: str, *, timeout: int = 15, cwd: Path | None = None
    ) -> PrHead | None:
        self.log.append(("gh", "fetch_pr_head"))
        self.timeouts.append(timeout)
        index = self.read_count
        self.read_count += 1
        if index in self.reads:
            return self.reads[index]
        if self.arm_after_push and self._pushed():
            self.arm_after_push = False
            self.armed = True
        if self.unreadable_after_disarm and self.disarm_calls:
            return None
        return make_head(
            self.ref_name,
            self.oid,
            self.armed,
            base_ref_name=self.base_ref_name,
            state=self.state,
        )

    def disarm_pr_auto_merge(
        self, pr_ref: str, *, timeout: int = 30, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes] | None:
        self.log.append(("gh", "disarm"))
        self.disarm_calls += 1
        mode = self.disarm_mode
        if mode == DISARM_NONE:
            return None
        if mode in {DISARM_OK, DISARM_NONZERO_UNARMED}:
            self.armed = False
        rc = 0 if mode in {DISARM_OK, DISARM_ZERO_STILL_ARMED} else 1
        return subprocess.CompletedProcess(["gh"], rc, b"", b"")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGhHead:
        monkeypatch.setattr(gh, "fetch_pr_head", self.fetch_pr_head)
        monkeypatch.setattr(gh, "disarm_pr_auto_merge", self.disarm_pr_auto_merge)
        return self


# --- FakeArmScript ----------------------------------------------------------

_ARM_SCRIPT_BODY = """\
import json
import sys
import time
from pathlib import Path

with Path({record!r}).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
time.sleep({sleep})
print({stdout!r})
sys.stderr.write("arm script stderr\\n")
sys.exit({code})
"""


class FakeArmScript:
    """An executable ``.claude/scripts/prep_pr_finalize.py`` under *repo*."""

    def __init__(
        self,
        repo: Path,
        *,
        exit_code: int = 0,
        status: str = "armed",
        stdout: str | None = None,
        sleep: float = 0,
    ) -> None:
        self.record = repo.parent / f"{repo.name}-arm-argv.jsonl"
        self.path = repo / ".claude" / "scripts" / "prep_pr_finalize.py"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            _ARM_SCRIPT_BODY.format(
                record=str(self.record),
                stdout=json.dumps({"status": status}) if stdout is None else stdout,
                code=exit_code,
                sleep=sleep,
            ),
            encoding="utf-8",
        )

    def calls(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
        ]


# --- events -----------------------------------------------------------------


def track_events(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> None:
    """Record every event the recipe emits into *log*, then really record it."""

    def _tracked(
        event_type: OrchestratorEventType, payload: dict[str, Any], **kwargs: Any
    ) -> object:
        log.append(("event", str(event_type)))
        return record_event(event_type, payload, **kwargs)

    monkeypatch.setattr(_shared, "record_event", _tracked)
    monkeypatch.setattr(changelog_repair, "record_event", _tracked)


# --- builders ---------------------------------------------------------------


def make_client(repo: Path, **fields: Any) -> ClientConfig:
    """The client whose git dir is *repo*; scratch trees go beside it."""
    return ClientConfig(
        name=CLIENT,
        workspace_path=repo,
        default_branch="main",
        worktree_base=repo.parent / "wt",
        **fields,
    )


def scratch_path(client_cfg: ClientConfig) -> Path:
    assert client_cfg.worktree_base is not None
    return client_cfg.worktree_base / f".changelog-repair-{TICKET}"


def repair_task(**fields: Any) -> TicketTask:
    """A COMPLETED row whose PR is CONFLICTING/DIRTY and merge_blocked."""
    values: dict[str, Any] = {
        "status": QueueItemStatus.COMPLETED,
        "pr_url": PR_URL,
        "pr_state": _pr_state(
            mergeable="CONFLICTING",
            merge_state_status="DIRTY",
            attention_state="merge_blocked",
        ),
        "computed_scope_tier": "small",
    }
    values.update(fields)
    return _make_task(**values)


def make_repair_job(
    client_cfg: ClientConfig,
    *,
    task: TicketTask | None = None,
    fence: FenceSnapshot | None = None,
    config: OrchestratorConfig | None = None,
) -> _RepairJob:
    """A ``_RepairJob`` built as ``_act_repair_changelog_conflict`` builds one."""
    row = task if task is not None else repair_task()
    assert row.pr_url is not None
    return _RepairJob(
        client=row.client,
        ticket_id=row.ticket_id,
        lane=row.lane,
        pr_url=row.pr_url,
        session_id=row.session_id,
        payload_base=_repair_payload_base(row, row.session_id),
        client_cfg=client_cfg,
        fence=fence if fence is not None else build_fence_snapshot(row, client_cfg),
        config=config,
        repeat_fire_counts=None if config is None else {},
        latch_key=(row.client, row.ticket_id, row.pr_url),
    )
