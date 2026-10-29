"""Real-git tests for the repair_changelog_conflict recipe (#2771).

Temp repos and real git throughout; only gh (``FakeGhHead``) and the arm
script (``FakeArmScript``) are faked. The history is the release-moved case:
main renamed ``[Unreleased]`` to ``1.1.0`` after the branch forked, and
``.gitattributes`` puts CHANGELOG.md on the ``union`` merge driver.
"""

from __future__ import annotations

import importlib.util
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cw._git import run_git
from cw.changelog_rebuild import ChangelogRepairDisposition as D
from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.reconcile.review_recipes import _shared, changelog_repair
from cw.reconcile.review_recipes import changelog_repair_git as git_mod
from cw.reconcile.review_recipes.changelog_repair import _dispatch_changelog_repair
from cw.reconcile.review_recipes.changelog_repair_git import _conflict_set
from tests._changelog_repair_helpers import (
    BRANCH,
    CL_BASE,
    CL_EXPECTED,
    CL_MAIN,
    DEFAULT_REF,
    NEW_SHA,
    TICKET,
    FakeArmScript,
    FakeGhHead,
    LogEntry,
    ReleaseMovedRepo,
    build_release_moved_repo,
    make_client,
    make_repair_job,
    repair_task,
    scratch_path,
)
from tests.conftest import _clean_git_env, git_in, push_commit_to_origin

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_GATE = _REPO_ROOT / ".claude" / "scripts" / "check_changelog_frozen.py"
_REAL_ARM_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "prep_pr_finalize.py"
_FREEZE_PYPROJECT = (
    '[project]\nname = "x"\nversion = "1.1.0"\n\n'
    '[tool.cw.changelog_freeze]\nsince_tag = "v1.0.0"\n'
)
_REPAIR_IDENTITY = "cw-changelog-repair|cw-changelog-repair@users.noreply.invalid"


@pytest.fixture
def scenario(tmp_path: Path, make_git_repo: Callable[..., Path]) -> ReleaseMovedRepo:
    return build_release_moved_repo(tmp_path, make_git_repo)


def _origin_tip(scenario: ReleaseMovedRepo) -> str:
    return git_in(scenario.origin, "rev-parse", BRANCH)


def _origin_file(scenario: ReleaseMovedRepo, relpath: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(scenario.origin), "show", f"{BRANCH}:{relpath}"],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    return result.stdout


def _origin_bytes(scenario: ReleaseMovedRepo, relpath: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(scenario.origin), "show", f"{BRANCH}:{relpath}"],
        capture_output=True,
        check=True,
        env=_clean_git_env(),
    ).stdout


def _payloads(event_type: OrchestratorEventType) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in read_events(event_types=[event_type])]


def _assert_no_scratch_left(scenario: ReleaseMovedRepo) -> None:
    client = make_client(scenario.clone)
    assert not scratch_path(client).exists()
    listed = git_in(scenario.clone, "worktree", "list", "--porcelain")
    trees = [line for line in listed.splitlines() if line.startswith("worktree ")]
    assert trees == [f"worktree {scenario.clone}"]


def test_baseline_union_rebase_silently_corrupts(
    scenario: ReleaseMovedRepo, tmp_path: Path
) -> None:
    """Documents the premise: a plain rebase reports no conflict at all.

    ``git_in`` raises on a non-zero exit, so reaching the assertions proves the
    rebase finished without stopping on CHANGELOG.md.
    """
    side = tmp_path / "side"
    subprocess.run(
        ["git", "clone", "-q", str(scenario.origin), str(side)],
        check=True,
        env=_clean_git_env(),
    )
    git_in(side, "checkout", "-q", BRANCH)
    git_in(side, "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "origin/main")
    assert git_in(side, "status", "--porcelain", "--untracked-files=no") == ""

    # Only the stable premise: the rebase exits 0 (no conflict stop) and its
    # result is not the text a structural rebuild produces. Where git's union
    # driver files the bullet depends on its diff alignment, which varies by
    # git version, so it is deliberately not asserted.
    assert (side / "CHANGELOG.md").read_text(encoding="utf-8") != CL_EXPECTED


def test_detector_under_merge_union(scenario: ReleaseMovedRepo) -> None:
    """R1: merge-tree alone sees no conflict; the both-sides check adds it."""
    git_in(scenario.clone, "fetch", "origin")
    merged = run_git(
        [
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            DEFAULT_REF,
            scenario.branch_sha,
        ],
        cwd=scenario.clone,
        capture_output=True,
    )

    assert merged.returncode == 0
    assert len(merged.stdout.split()) == 1
    conflict_set, merge_base = _conflict_set(
        scenario.clone, DEFAULT_REF, scenario.branch_sha
    )
    assert conflict_set == frozenset({"CHANGELOG.md"})
    assert merge_base == scenario.base_sha


def _arm_proves_push(
    monkeypatch: pytest.MonkeyPatch, scenario: ReleaseMovedRepo
) -> list[str]:
    """At the moment PR_ACTION_TAKEN is recorded, origin already has the push."""
    tips: list[str] = []
    real = _shared.record_event

    def _hook(event_type: OrchestratorEventType, payload: Any, **kw: Any) -> object:
        if event_type == OrchestratorEventType.PR_ACTION_TAKEN:
            tips.append(_origin_tip(scenario))
        return real(event_type, payload, **kw)

    monkeypatch.setattr(_shared, "record_event", _hook)
    return tips


def test_real_repair_rebuilds_pushes_and_arms_small_tier(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    arm = FakeArmScript(scenario.clone)
    tips = _arm_proves_push(monkeypatch, scenario)
    client = make_client(scenario.clone)

    assert _dispatch_changelog_repair(make_repair_job(client)) == TICKET

    assert _origin_file(scenario, "CHANGELOG.md") == CL_EXPECTED
    assert _origin_file(scenario, "src/app.py") == "def app():\n    return 2\n"
    assert _origin_file(scenario, "src/other.py") == "OTHER = 2\n"
    new_tip = _origin_tip(scenario)
    # The rebased branch commit, then the CHANGELOG rebuild commit, on main.
    assert git_in(scenario.origin, "rev-parse", f"{new_tip}~2") == scenario.main_sha
    (taken,) = _payloads(OrchestratorEventType.PR_ACTION_TAKEN)
    assert taken["evidence_snapshot"]["expected_sha"] == scenario.branch_sha
    assert taken["evidence_snapshot"]["new_sha"] == new_tip
    assert tips == [new_tip]
    (argv,) = arm.calls()
    assert argv[-2:] == ["--head-sha", new_tip]
    assert _payloads(OrchestratorEventType.PR_ACTION_FAILED) == []
    _assert_no_scratch_left(scenario)


def test_real_repair_large_tier_ends_unarmed(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    arm = FakeArmScript(scenario.clone)
    client = make_client(scenario.clone)
    task = repair_task(
        computed_scope_tier="large",
        review_approver_kind="operator",
        review_approved_sha=scenario.branch_sha,
    )

    assert _dispatch_changelog_repair(make_repair_job(client, task=task)) == TICKET

    assert arm.calls() == []
    (attention,) = _payloads(OrchestratorEventType.SESSION_NEEDS_ATTENTION)
    assert attention["paused_status"] == "changelog_repair_unarmed"
    assert attention["review_approval_check"] == "stale"
    assert attention["new_sha"] == _origin_tip(scenario)
    _assert_no_scratch_left(scenario)


@pytest.mark.parametrize("tier", ["small", "large"])
def test_real_repair_of_an_armed_pr_disarms_before_the_push(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    log: list[LogEntry] = []
    real = git_mod.run_git

    def _logged(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        log.append(("git", *argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(git_mod, "run_git", _logged)
    gh_head = FakeGhHead(log, armed=True, oid=scenario.branch_sha).install(monkeypatch)
    arm = FakeArmScript(scenario.clone)
    client = make_client(scenario.clone)
    task = repair_task(computed_scope_tier=tier)

    assert _dispatch_changelog_repair(make_repair_job(client, task=task)) == TICKET

    push_at = next(i for i, e in enumerate(log) if "push" in e)
    assert log.index(("gh", "disarm")) < push_at
    assert gh_head.armed is False
    if tier == "large":
        assert arm.calls() == []
    else:
        (argv,) = arm.calls()
        assert argv[-2:] == ["--head-sha", _origin_tip(scenario)]
    _assert_no_scratch_left(scenario)


def test_real_lease_holds_against_a_concurrent_push(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)
    real = git_mod.run_git
    late: list[str] = []

    def _race(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "push" in argv and not late:
            late.append(
                push_commit_to_origin(
                    scenario.origin, BRANCH, tmp_path / "racer", "late.txt"
                )
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(git_mod, "run_git", _race)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone))) is None
    )

    (failed,) = _payloads(OrchestratorEventType.PR_ACTION_FAILED)
    assert failed["disposition"] == D.PUSH_REJECTED.value
    assert _origin_tip(scenario) == late[0]
    assert git_in(scenario.origin, "rev-parse", f"{late[0]}^") == scenario.branch_sha
    assert _payloads(OrchestratorEventType.PR_ACTION_TAKEN) == []
    _assert_no_scratch_left(scenario)


def test_real_non_changelog_conflict_parks(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = build_release_moved_repo(
        tmp_path,
        make_git_repo,
        main_code_file="src/app.py",
        main_code="def app():\n    return 3\n",
    )
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone))) is None
    )

    (failed,) = _payloads(OrchestratorEventType.PR_ACTION_FAILED)
    assert failed["disposition"] == D.NON_CHANGELOG_CONFLICT.value
    assert "src/app.py" in failed["error"]
    assert _origin_tip(scenario) == scenario.branch_sha
    assert (
        changelog_repair._LATCH.attempts(("acme", TICKET, failed["pr_url"]))
        == changelog_repair._MAX_ATTEMPTS
    )
    _assert_no_scratch_left(scenario)


def _install_real_gate(scenario: ReleaseMovedRepo) -> None:
    """The repo's real freeze-gate script, in the client workspace."""
    target = scenario.clone / ".claude" / "scripts" / _REAL_GATE.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REAL_GATE, target)


def _frozen_scenario(
    tmp_path: Path, make_git_repo: Callable[..., Path], **kwargs: Any
) -> ReleaseMovedRepo:
    scenario = build_release_moved_repo(
        tmp_path,
        make_git_repo,
        tags=True,
        extra_base_files={"pyproject.toml": _FREEZE_PYPROJECT},
        **kwargs,
    )
    _install_real_gate(scenario)
    return scenario


def test_real_freeze_gate_passes_on_the_rebuilt_file(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _frozen_scenario(tmp_path, make_git_repo)
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone)))
        == TICKET
    )

    assert _origin_file(scenario, "CHANGELOG.md") == CL_EXPECTED
    assert _payloads(OrchestratorEventType.PR_ACTION_FAILED) == []


def test_real_freeze_gate_violation_parks_as_gate_failed(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main edited the released 1.0.0 section after its tag: the gate must say so."""
    scenario = _frozen_scenario(
        tmp_path,
        make_git_repo,
        main_changelog=CL_MAIN.replace("- Initial (#0).", "- Initial (#0), edited."),
    )
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone))) is None
    )

    (failed,) = _payloads(OrchestratorEventType.PR_ACTION_FAILED)
    assert failed["disposition"] == D.GATE_FAILED.value
    assert "section_changed" in failed["error"]
    assert _origin_tip(scenario) == scenario.branch_sha
    _assert_no_scratch_left(scenario)


def test_run_arm_argv_is_accepted_by_the_real_arm_parser(tmp_path: Path) -> None:
    repo = tmp_path / "armrepo"
    repo.mkdir()
    fake = FakeArmScript(repo)
    git_mod._run_arm(fake.path, 42, repo, NEW_SHA)
    (argv,) = fake.calls()
    spec = importlib.util.spec_from_file_location(
        "_real_prep_pr_finalize", _REAL_ARM_SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    args = module.build_parser().parse_args(argv)

    assert args.head_sha == NEW_SHA
    assert str(args.repo_path) == str(repo)


def test_rebase_stopping_on_changelog_without_the_union_attribute(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main and the branch both append after Beta, so the rebase really stops.

    The branch's second commit touches only CHANGELOG.md, so resolving the stop
    with main's text leaves it empty: ``rebase --continue`` must cope.
    """
    after_beta = "- Beta fix (#2).\n\n"
    scenario = build_release_moved_repo(
        tmp_path,
        make_git_repo,
        union=False,
        split_branch_commits=True,
        main_changelog=CL_BASE.replace(
            after_beta, f"{after_beta}- Delta fix (#4).\n\n"
        ),
    )
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone)))
        == TICKET
    )

    assert _origin_file(scenario, "CHANGELOG.md") == CL_BASE.replace(
        after_beta, f"{after_beta}- Delta fix (#4).\n\n- Gamma fix (#3).\n\n"
    )
    assert _origin_file(scenario, "src/app.py") == "def app():\n    return 2\n"
    assert _origin_file(scenario, "src/other.py") == "OTHER = 2\n"
    assert _payloads(OrchestratorEventType.PR_ACTION_FAILED) == []
    _assert_no_scratch_left(scenario)


def test_stale_registered_scratch_worktree_is_replaced(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(scenario.clone)
    stale = scratch_path(client)
    stale.parent.mkdir(parents=True, exist_ok=True)
    git_in(scenario.clone, "worktree", "add", "--detach", str(stale), scenario.base_sha)
    (stale / "leftover.txt").write_text("crashed run\n", encoding="utf-8")
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert _dispatch_changelog_repair(make_repair_job(client)) == TICKET

    assert _origin_file(scenario, "CHANGELOG.md") == CL_EXPECTED
    _assert_no_scratch_left(scenario)


def _failing_hooks(repo: Path) -> None:
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "commit-msg", "pre-push", "pre-rebase"):
        hook = hooks / name
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def test_repair_uses_its_own_identity_and_ignores_client_hooks(
    scenario: ReleaseMovedRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_hooks(scenario.clone)
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone)))
        == TICKET
    )

    new_tip = _origin_tip(scenario)
    rebuild, rebased = git_in(
        scenario.origin, "log", "-2", "--format=%an|%ae|%cn|%ce|%s", new_tip
    ).splitlines()
    # The rebuild commit is authored and committed by the repair identity; the
    # rebased branch commit keeps its author but is committed by it.
    assert rebuild.startswith(f"{_REPAIR_IDENTITY}|{_REPAIR_IDENTITY}|")
    assert not rebased.startswith(_REPAIR_IDENTITY)
    assert rebased.split("|")[2:4] == _REPAIR_IDENTITY.split("|")


def test_crlf_changelog_is_preserved_byte_for_byte(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = build_release_moved_repo(tmp_path, make_git_repo, changelog_eol="\r\n")
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone)))
        == TICKET
    )

    assert _origin_bytes(scenario, "CHANGELOG.md") == CL_EXPECTED.replace(
        "\n", "\r\n"
    ).encode("utf-8")


def test_mixed_line_endings_fail_closed_through_git(
    tmp_path: Path, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One CRLF line in main: git must hand the rebuild the CR, not normalise it."""
    first, rest = CL_MAIN.split("\n", 1)
    scenario = build_release_moved_repo(
        tmp_path, make_git_repo, main_changelog=f"{first}\r\n{rest}"
    )
    log: list[LogEntry] = []
    FakeGhHead(log, oid=scenario.branch_sha).install(monkeypatch)
    FakeArmScript(scenario.clone)

    assert (
        _dispatch_changelog_repair(make_repair_job(make_client(scenario.clone))) is None
    )

    (failed,) = _payloads(OrchestratorEventType.PR_ACTION_FAILED)
    assert failed["disposition"] == D.CHANGELOG_UNPARSEABLE.value
    assert "mixed line endings" in failed["error"]
    assert _origin_tip(scenario) == scenario.branch_sha
    _assert_no_scratch_left(scenario)
