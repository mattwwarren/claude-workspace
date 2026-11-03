"""Real-git tests for the repair_mechanical_conflict recipe (#2917).

Temp repos and real git throughout; only gh (``FakeGhHead``) and the arm
script (``FakeArmScript``) are faked. The repair and verify commands are
small Python stubs written into the client workspace. The history: main and
the branch each added a ``migrations/0002_*.py`` and pointed
``migrations/head.txt`` at it, so ``head.txt`` is the only textual conflict.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest

from cw.models import ClientConfig, OrchestratorEventType
from cw.reconcile.review_recipes import repair_git as git_mod
from cw.reconcile.review_recipes.mechanical_repair import MECHANICAL_SPEC
from cw.reconcile.review_recipes.mechanical_repair_git import run_declared_command
from cw.reconcile.review_recipes.repair_engine import dispatch_repair
from cw.repair_disposition import RepairDisposition as D
from tests._changelog_repair_helpers import (
    BRANCH,
    TICKET,
    FakeArmScript,
    FakeGhHead,
    LogEntry,
    ReleaseMovedRepo,
    assert_no_scratch_left,
    commit_files,
    make_client,
    make_repair_job,
    origin_file,
    origin_tip,
    payloads,
)
from tests.conftest import git_in, init_bare_origin

ChangelogMode = Literal["untouched", "disjoint", "conflicting"]

_CL_BASE = (
    "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- Alpha (#1).\n\n"
    "### Fixed\n\n- Beta (#2).\n\n## [1.0.0] - 2026-01-01\n\n- Initial (#0).\n"
)
_CL_BRANCH_DISJOINT = _CL_BASE.replace(
    "- Alpha (#1).\n", "- Alpha (#1).\n- Gamma (#3).\n"
)
_CL_MAIN_DISJOINT = _CL_BASE.replace(
    "- Initial (#0).\n", "- Initial (#0).\n- Zeta (#0).\n"
)
_CL_BRANCH_CONFLICT = _CL_BASE.replace("- Beta (#2).", "- Beta, branch (#2).")
_CL_MAIN_CONFLICT = _CL_BASE.replace("- Beta (#2).", "- Beta, main (#2).")

_REPAIR_SCRIPT = """\
#!{python}
import pathlib
import sys

root = pathlib.Path.cwd()
mig = root / "migrations"
branch = mig / "0002_branch.py"
if branch.exists():
    branch.rename(mig / "0003_branch.py")
(mig / "head.txt").write_text("0003_branch\\n", encoding="utf-8")
{extra}
sys.exit({code})
"""

_VERIFY_SCRIPT = """\
#!{python}
import pathlib
import sys

mig = pathlib.Path.cwd() / "migrations"
head = (mig / "head.txt").read_text(encoding="utf-8").strip()
ok = head == "0003_branch" and (mig / "0003_branch.py").is_file()
sys.exit(0 if ok and {code} == 0 else 1)
"""


def build_declared_conflict_repo(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    *,
    extra_src_conflict: bool = False,
    changelog: ChangelogMode = "untouched",
    union: bool = False,
    branch_files: dict[str, str] | None = None,
) -> ReleaseMovedRepo:
    """Base, then main and the branch each add a migration and repoint head.txt.

    The clone is left on ``main`` at the base commit, both refs pushed to the
    bare origin beside it. *extra_src_conflict* also conflicts ``src/app.py``.
    *changelog* edits CHANGELOG.md on both sides: disjoint hunks (git merges
    them) or the same line (a git-visible conflict). *union* adds
    ``CHANGELOG.md merge=union``. *branch_files* rides on the branch commit.
    """
    del tmp_path
    clone = make_git_repo("client")
    base_files = {
        "migrations/head.txt": "0001\n",
        "migrations/0001_init.py": "PARENT = None\n",
        "src/app.py": "def app():\n    return 1\n",
        "CHANGELOG.md": _CL_BASE,
    }
    if union:
        base_files[".gitattributes"] = "CHANGELOG.md merge=union\n"
    base_sha = commit_files(clone, base_files, "base")
    origin = init_bare_origin(clone)
    git_in(clone, "push", "origin", "main")
    git_in(clone, "checkout", "-b", BRANCH)
    on_branch = {
        "migrations/0002_branch.py": "PARENT = '0001'\n",
        "migrations/head.txt": "0002_branch\n",
        **(branch_files or {}),
    }
    on_main = {
        "migrations/0002_main.py": "PARENT = '0001'\n",
        "migrations/head.txt": "0002_main\n",
    }
    if extra_src_conflict:
        on_branch["src/app.py"] = "def app():\n    return 2\n"
        on_main["src/app.py"] = "def app():\n    return 3\n"
    if changelog == "disjoint":
        on_branch["CHANGELOG.md"] = _CL_BRANCH_DISJOINT
        on_main["CHANGELOG.md"] = _CL_MAIN_DISJOINT
    elif changelog == "conflicting":
        on_branch["CHANGELOG.md"] = _CL_BRANCH_CONFLICT
        on_main["CHANGELOG.md"] = _CL_MAIN_CONFLICT
    branch_sha = commit_files(clone, on_branch, "feat: branch migration (#3)")
    git_in(clone, "push", "origin", BRANCH)
    git_in(clone, "checkout", "main")
    main_sha = commit_files(clone, on_main, "feat: main migration (#4)")
    git_in(clone, "push", "origin", "main")
    git_in(clone, "reset", "--hard", base_sha)
    git_in(clone, "branch", "-D", BRANCH)
    return ReleaseMovedRepo(clone, origin, base_sha, main_sha, branch_sha)


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _install_commands(
    workspace: Path, *, repair_code: int = 0, verify_code: int = 0, extra: str = ""
) -> dict[str, Any]:
    """Plant the stub scripts in *workspace*; the entry naming them relatively."""
    _write_script(
        workspace / "scripts" / "rechain.py",
        _REPAIR_SCRIPT.format(python=sys.executable, extra=extra, code=repair_code),
    )
    _write_script(
        workspace / "scripts" / "single_head.py",
        _VERIFY_SCRIPT.format(python=sys.executable, code=verify_code),
    )
    return {
        "paths": ["migrations/**"],
        "repair_command": ["scripts/rechain.py"],
        "verify_command": ["scripts/single_head.py"],
        "repair_timeout_seconds": 60,
        "verify_timeout_seconds": 60,
    }


def _client(scenario: ReleaseMovedRepo, **commands: Any) -> ClientConfig:
    entry = _install_commands(scenario.clone, **commands)
    return make_client(scenario.clone, mechanical_conflict_repair=entry)


def _logged_git(monkeypatch: pytest.MonkeyPatch, log: list[LogEntry]) -> None:
    real = git_mod.run_git

    def _logged(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        log.append(("git", *(str(a) for a in argv)))
        return real(argv, **kwargs)

    monkeypatch.setattr(git_mod, "run_git", _logged)


def _ran(log: list[LogEntry], verb: str) -> bool:
    return any(verb in entry for entry in log if entry[0] == "git")


def _setup(
    monkeypatch: pytest.MonkeyPatch, scenario: ReleaseMovedRepo
) -> tuple[list[LogEntry], FakeArmScript]:
    log: list[LogEntry] = []
    _logged_git(monkeypatch, log)
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    return log, FakeArmScript(scenario.clone)


def _only_failure(disposition: D) -> dict[str, Any]:
    assert payloads(OrchestratorEventType.PR_ACTION_TAKEN) == []
    (failure,) = payloads(OrchestratorEventType.PR_ACTION_FAILED)
    assert failure["disposition"] == disposition.value
    return failure


@pytest.fixture
def scenario(tmp_path: Path, make_git_repo: Callable[..., Path]) -> ReleaseMovedRepo:
    return build_declared_conflict_repo(tmp_path, make_git_repo)


def test_repairs_declared_path_conflict_end_to_end(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, arm = _setup(monkeypatch, scenario)

    assert (
        dispatch_repair(make_repair_job(_client(scenario), spec=MECHANICAL_SPEC))
        == TICKET
    )

    new_tip = origin_tip(scenario)
    parents = git_in(scenario.origin, "rev-list", "--parents", "-n", "1", new_tip)
    assert parents.split()[1:] == [scenario.branch_sha, scenario.main_sha]
    assert origin_file(scenario, "migrations/head.txt") == "0003_branch\n"
    assert origin_file(scenario, "migrations/0003_branch.py") == "PARENT = '0001'\n"
    assert origin_file(scenario, "migrations/0002_main.py") == "PARENT = '0001'\n"
    tree = git_in(scenario.origin, "ls-tree", "-r", "--name-only", new_tip)
    assert "migrations/0002_branch.py" not in tree.split()
    (push,) = [entry for entry in log if entry[0] == "git" and "push" in entry]
    assert f"--force-with-lease=refs/heads/{BRANCH}:{scenario.branch_sha}" in push
    (taken,) = payloads(OrchestratorEventType.PR_ACTION_TAKEN)
    assert taken["evidence_snapshot"]["conflicted_paths"] == ["migrations/head.txt"]
    assert taken["evidence_snapshot"]["new_sha"] == new_tip
    (argv,) = arm.calls()
    assert argv[-2:] == ["--head-sha", new_tip]
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_out_of_scope_conflict_leaves_origin_untouched(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = build_declared_conflict_repo(
        tmp_path, make_git_repo, extra_src_conflict=True
    )
    log, arm = _setup(monkeypatch, scenario)

    assert (
        dispatch_repair(make_repair_job(_client(scenario), spec=MECHANICAL_SPEC))
        is None
    )

    failure = _only_failure(D.OUT_OF_SCOPE_CONFLICT)
    assert "src/app.py" in failure["error"]
    assert origin_tip(scenario) == scenario.branch_sha
    assert not _ran(log, "merge")
    assert not _ran(log, "push")
    assert arm.calls() == []
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_changelog_touched_on_both_sides_in_glob_conflict_is_repaired(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = build_declared_conflict_repo(
        tmp_path, make_git_repo, changelog="disjoint"
    )
    _setup(monkeypatch, scenario)

    assert (
        dispatch_repair(make_repair_job(_client(scenario), spec=MECHANICAL_SPEC))
        == TICKET
    )

    changelog = origin_file(scenario, "CHANGELOG.md")
    assert "- Gamma (#3)." in changelog
    assert "- Zeta (#0)." in changelog
    assert origin_tip(scenario) != scenario.branch_sha
    assert len(payloads(OrchestratorEventType.PR_ACTION_TAKEN)) == 1
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_changelog_genuinely_conflicting_parks_out_of_scope(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = build_declared_conflict_repo(
        tmp_path, make_git_repo, changelog="conflicting"
    )
    log, _arm = _setup(monkeypatch, scenario)

    assert (
        dispatch_repair(make_repair_job(_client(scenario), spec=MECHANICAL_SPEC))
        is None
    )

    failure = _only_failure(D.OUT_OF_SCOPE_CONFLICT)
    assert "CHANGELOG.md" in failure["error"]
    assert origin_tip(scenario) == scenario.branch_sha
    assert not _ran(log, "push")
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_nonzero_repair_leaves_origin_untouched(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, arm = _setup(monkeypatch, scenario)
    client = _client(scenario, repair_code=4)

    assert dispatch_repair(make_repair_job(client, spec=MECHANICAL_SPEC)) is None

    failure = _only_failure(D.REPAIR_FAILED)
    assert "exit 4" in failure["error"]
    assert origin_tip(scenario) == scenario.branch_sha
    assert not _ran(log, "push")
    assert arm.calls() == []
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_failing_check_leaves_origin_untouched(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _arm = _setup(monkeypatch, scenario)
    client = _client(scenario, verify_code=1)

    assert dispatch_repair(make_repair_job(client, spec=MECHANICAL_SPEC)) is None

    _only_failure(D.CHECK_FAILED)
    assert origin_tip(scenario) == scenario.branch_sha
    assert not _ran(log, "push")
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_script_edits_outside_globs_parks(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(monkeypatch, scenario)
    extra = '(root / "src" / "app.py").write_text("hacked\\n", encoding="utf-8")'
    client = _client(scenario, extra=extra)

    assert dispatch_repair(make_repair_job(client, spec=MECHANICAL_SPEC)) is None

    failure = _only_failure(D.REPAIR_OUT_OF_SCOPE)
    assert "src/app.py" in failure["error"]
    assert origin_tip(scenario) == scenario.branch_sha
    assert_no_scratch_left(scenario, MECHANICAL_SPEC)


def test_relative_script_resolves_to_workspace_copy_not_branch_copy(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tampered = f"#!{sys.executable}\nimport sys\nsys.exit(7)\n"
    scenario = build_declared_conflict_repo(
        tmp_path, make_git_repo, branch_files={"scripts/rechain.py": tampered}
    )
    _setup(monkeypatch, scenario)
    client = _client(scenario)

    assert dispatch_repair(make_repair_job(client, spec=MECHANICAL_SPEC)) == TICKET

    assert payloads(OrchestratorEventType.PR_ACTION_FAILED) == []
    assert origin_file(scenario, "migrations/head.txt") == "0003_branch\n"


def test_run_declared_command_kills_process_group_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "sleeper = [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        "child = subprocess.Popen(sleeper)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()

    outcome = run_declared_command(
        [sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ), timeout=2
    )

    assert time.monotonic() - started < 30
    assert outcome.timed_out is True
    assert outcome.returncode is None
    assert "started" in outcome.tail
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.05)
    assert not _alive(grandchild)


def _alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/stat")
    try:
        fields = status.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (FileNotFoundError, IndexError):
        return False
    return fields[0] != "Z"


def test_run_declared_command_tail_and_returncode(tmp_path: Path) -> None:
    code = "import sys\nprint('x' * 500)\nsys.stderr.write('err-tail')\nsys.exit(5)\n"

    outcome = run_declared_command(
        [sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ), timeout=30
    )

    assert outcome.returncode == 5
    assert outcome.timed_out is False
    assert len(outcome.tail) == 300
    assert outcome.tail.endswith("err-tail")


def test_run_declared_command_launch_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_declared_command(
            [str(tmp_path / "missing")], cwd=tmp_path, env={}, timeout=5
        )
