"""RFC 0020 S3b (#3840): codex-lane crossings that carry the row's base.

Each crossing is pinned twice where it can differ: a based row (the base
differs from the client default) and a null row (the base is the default, so
the text or value must be byte-identical to before S3b).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from cw.auto_dev_result import AutoDevResult
from cw.codex_fix_loop import _driver, review_pass
from cw.codex_impl._run import _merge_note
from cw.codex_review._engine_round import EnginePassOutcome, EngineRoundSpec
from cw.codex_review._engine_start import engine_round_spec
from cw.local_runner import git_facts
from cw.models import ClientConfig
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.review_convergence import resolve_loop_diff_base
from cw.stage_verification.impl_block import ImplBlock
from cw.stage_verification.impl_feedback import (
    CONFLICT_PREFIX,
    MergeOutcome,
    merge_default_on_conflict,
    probe_default_branch,
)
from cw.stage_verification.impl_gates import check_on_branch
from tests._review_engine_helpers import EngineRepo, make_engine_repo
from tests._shared.base_branch_builders import (
    CLIENT_DEFAULT,
    ENGINE_SKILL,
    ENGINE_STUB,
    ROW_BASE,
    base_row,
    epic_merged_worktree,
    push_epic_branch,
)
from tests._shared.git_repos import commit_tracked_file, git_in, push_commit_to_origin

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.codex_review._context import _ReviewPassInputs
    from cw.models import TicketTask

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NOTE_TAIL = (
    "cw has started the merge and left the conflicts in the worktree. "
    "Resolve them (edit the files; do not commit, stage or abort)."
)


def test_merge_note_names_the_row_base_for_a_based_row() -> None:
    note = _merge_note(("src/a.py",), "epic/x")
    assert note is not None
    assert note == (
        f"origin/epic/x conflicts with this branch in:\n- src/a.py\n{_NOTE_TAIL}"
    )


def test_merge_note_keeps_the_default_branch_text_for_a_null_row() -> None:
    note = _merge_note(("src/a.py",), None)
    assert note is not None
    head = "origin's default branch conflicts with this branch in:"
    assert note == f"{head}\n- src/a.py\n{_NOTE_TAIL}"


def test_merge_note_is_none_without_conflicts() -> None:
    assert _merge_note((), "epic/x") is None


def test_codex_plan_has_no_default_branch_read() -> None:
    """The codex plan lane has no merge-target read; the S3b claim is vacuous there."""
    plan_dir = _REPO_ROOT / "src" / "cw" / "codex_plan"
    sources = sorted(plan_dir.rglob("*.py"))
    assert sources, "the scan must see the codex_plan package"
    reads = [
        f"{path.name}:{node.lineno}"
        for path in sources
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if (isinstance(node, ast.Attribute) and node.attr == "default_branch")
        or (isinstance(node, ast.arg) and node.arg in {"default_branch", "base_branch"})
        or (isinstance(node, ast.keyword) and node.arg == "default_branch")
    ]
    # The one allowed read: the origin-sync health gate (Step P1) on the primary
    # checkout, not a merge target (#3465). Exactly one, in _preflight.py.
    origin_sync = [read for read in reads if read.startswith("_preflight.py:")]
    assert len(origin_sync) == 1, origin_sync
    assert [read for read in reads if read not in origin_sync] == []


# --- check_on_branch: the row's base is refused beside the client default -----


@pytest.fixture
def on_epic(make_git_repo: Callable[..., Path]) -> Path:
    """A repo on ``epic/x``, with ``main`` also present."""
    repo = make_git_repo("on-epic")
    git_in(repo, "checkout", "-b", ROW_BASE)
    return repo


def test_a_worktree_on_the_row_base_is_refused_with_the_base_message(
    on_epic: Path,
) -> None:
    block = check_on_branch(
        on_epic, "dev/1", default_branch=CLIENT_DEFAULT, base_branch=ROW_BASE
    )
    assert block == ImplBlock(
        "impl_failed",
        f"the IMPL turn cannot run on this row's base branch {ROW_BASE!r}",
    )


def test_a_worktree_on_the_client_default_keeps_the_default_message(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("on-default")
    git_in(repo, "checkout", "-b", CLIENT_DEFAULT)
    block = check_on_branch(
        repo, "dev/1", default_branch=CLIENT_DEFAULT, base_branch=ROW_BASE
    )
    assert block == ImplBlock(
        "impl_failed",
        f"the IMPL turn cannot run on the configured default branch {CLIENT_DEFAULT!r}",
    )


def test_a_null_row_refuses_byte_identically_to_the_pre_base_call(
    make_git_repo: Callable[..., Path],
) -> None:
    repo = make_git_repo("null-row")
    git_in(repo, "checkout", "-b", CLIENT_DEFAULT)
    before = check_on_branch(repo, "dev/1", default_branch=CLIENT_DEFAULT)
    null_row = check_on_branch(
        repo, "dev/1", default_branch=CLIENT_DEFAULT, base_branch=CLIENT_DEFAULT
    )
    assert before is not None
    assert null_row == before


# --- the epic topology: probe, merge and git_facts fetch the row's base --------


@pytest.fixture
def epic(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]], tmp_path: Path
) -> Path:
    """A feature branch that merged ``origin/epic/x`` (so main and epic differ)."""
    return epic_merged_worktree(make_git_repo_with_origin, tmp_path / "side").worktree


def _conflict_on(wt: Path, tmp_path: Path, branch: str) -> None:
    """Make *wt*'s ``feature.py`` conflict with an out-of-band commit on *branch*."""
    origin = Path(git_in(wt, "remote", "get-url", "origin"))
    commit_tracked_file(wt, "feature.py", "mine\n")
    push_commit_to_origin(origin, branch, tmp_path / f"conflict-{branch}", "feature.py")


def test_a_fresh_row_probe_blocks_on_the_rows_base_and_names_it(
    epic: Path, tmp_path: Path
) -> None:
    _conflict_on(epic, tmp_path, ROW_BASE)
    probe = probe_default_branch(epic, base_branch=ROW_BASE, repair=False)
    assert probe.block is not None
    assert probe.block.details.startswith(
        f"{CONFLICT_PREFIX}: merging origin/{ROW_BASE} into this branch conflicts in "
    )
    assert "feature.py" in probe.block.details
    # The conflict is on the epic branch only: main is not what the row merges.
    assert probe_default_branch(epic, base_branch="main", repair=False).block is None


def test_a_null_row_probe_names_origin_main(epic: Path, tmp_path: Path) -> None:
    _conflict_on(epic, tmp_path, "main")
    probe = probe_default_branch(epic, base_branch="main", repair=False)
    assert probe.block is not None
    assert "merging origin/main into this branch conflicts in" in probe.block.details
    assert probe_default_branch(epic, base_branch=ROW_BASE, repair=False).block is None


@pytest.mark.parametrize("target", [ROW_BASE, "main"])
def test_a_repair_row_merges_the_target_and_reports_its_files(
    epic: Path, tmp_path: Path, target: str
) -> None:
    _conflict_on(epic, tmp_path, target)
    probe = probe_default_branch(epic, base_branch=target, repair=True)
    assert probe.needs_merge
    assert probe.conflicted_files == ("feature.py",)
    merged = merge_default_on_conflict(epic, base_branch=target)
    assert isinstance(merged, MergeOutcome)
    assert "feature.py" in merged.merge_files
    assert git_in(epic, "rev-parse", "MERGE_HEAD") == git_in(
        epic, "rev-parse", f"origin/{target}"
    )


def test_git_facts_forks_from_the_base_a_null_row_forks_from_main(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]], tmp_path: Path
) -> None:
    merge = epic_merged_worktree(make_git_repo_with_origin, tmp_path / "side")
    wt = merge.worktree
    on_base = git_facts(wt, ROW_BASE)["fork_point"]
    on_default = git_facts(wt, "main")["fork_point"]
    assert on_base == git_in(wt, "rev-parse", f"origin/{ROW_BASE}")
    assert on_default == git_in(wt, "rev-parse", "origin/main")
    assert on_base != on_default


def test_the_loop_diff_measures_from_the_base_not_the_default(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]], tmp_path: Path
) -> None:
    """``_capture_diff`` (cycle 0 and ``_loop_inputs``) sees the epic merge as base."""
    wt = epic_merged_worktree(make_git_repo_with_origin, tmp_path / "side").worktree
    assert resolve_loop_diff_base(wt, ROW_BASE) != resolve_loop_diff_base(wt, "main")
    _, _, on_base = _driver._capture_diff(wt, ROW_BASE)
    _, _, on_default = _driver._capture_diff(wt, "main")
    assert sorted(on_base) == ["feature.py"]
    assert sorted(on_default) == ["epic_only.py", "feature.py"]


# --- the review pass: base for the diff, default for the trust root -------------

_RESULT = cast("AutoDevResult", SimpleNamespace(tag="result"))


def _cycle_zero_kwargs(task: TicketTask, base: str) -> dict[str, Any]:
    return {
        "runner": cast("Any", None),
        "task": task,
        "worktree": Path(),
        "default_branch": CLIENT_DEFAULT,
        "base_branch": base,
        "model": None,
        "reasoning_effort": None,
        "wall_clock_budget_seconds": None,
        "session_id": "s",
        "fix_loop_enabled": True,
        "claim_tier_enabled": False,
        "disposition_drift_check_enabled": True,
        "settled_suppression": None,
        "max_prompt_chars": None,
    }


def _rereview_kwargs(task: TicketTask, base: str) -> dict[str, Any]:
    kwargs = _cycle_zero_kwargs(task, base)
    for key in ("wall_clock_budget_seconds", "fix_loop_enabled"):
        del kwargs[key]
    return kwargs | {
        "remaining": None,
        "previous_reviewed_sha": "p" * 40,
        "prior_open_findings": [],
        "cycle": 1,
    }


@pytest.mark.parametrize("base", [ROW_BASE, CLIENT_DEFAULT])
def test_cycle_zero_plain_path_diffs_the_base_and_configures_from_the_default(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    seen: dict[str, Any] = {}

    def _run_review(**kwargs: Any) -> tuple[AutoDevResult, None]:
        seen.update(kwargs)
        return _RESULT, None

    monkeypatch.setattr(review_pass, "run_review", _run_review)
    review_pass._review_cycle_zero(**_cycle_zero_kwargs(base_row(None), base))
    assert seen["base_branch"] == base
    assert seen["default_branch"] == CLIENT_DEFAULT


@pytest.mark.parametrize("base", [ROW_BASE, CLIENT_DEFAULT])
def test_rereview_plain_path_measures_prepare_and_synthesis_against_the_base(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    prepare: dict[str, Any] = {}
    synth: dict[str, Any] = {}
    prepared = cast(
        "_ReviewPassInputs",
        SimpleNamespace(
            delta_diff=None,
            diff="d",
            reviewed_sha="r",
            capability=None,
            agent_spec_status=None,
            review_config=None,
            voided_findings=[],
            finding_dispositions=[],
            refused_dispositions=[],
        ),
    )

    def _prepare(
        _task: object, _worktree: object, diff_base: str, **kwargs: object
    ) -> object:
        prepare.update(kwargs, diff_base=diff_base)
        return prepared

    def _synthesize(**kwargs: Any) -> tuple[AutoDevResult, None]:
        synth.update(kwargs)
        return _RESULT, None

    roles = SimpleNamespace(
        documents=[],
        failures=[],
        metrics_by_role={},
        pre_validation_rejected=[],
        coverage=None,
    )
    monkeypatch.setattr(review_pass, "_prepare_review_pass", _prepare)
    monkeypatch.setattr(review_pass, "run_prepared_roles", lambda *_a, **_k: roles)
    monkeypatch.setattr(review_pass, "synthesize_codex_review_result", _synthesize)
    review_pass._rereview(**_rereview_kwargs(base_row(None), base))
    assert prepare["diff_base"] == base
    assert prepare["config_branch"] == CLIENT_DEFAULT
    # The legacy kwarg name carries the merge target into the scope tier.
    assert synth["default_branch"] == base


@pytest.mark.parametrize("base", [ROW_BASE, CLIENT_DEFAULT])
def test_both_engine_passes_diff_the_base_and_configure_from_the_default(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    seen: list[Any] = []

    def _engine_pass(_spec: object, inputs: Any, *, cycle: int) -> EnginePassOutcome:
        seen.append(inputs)
        return EnginePassOutcome(_RESULT, None, None)

    monkeypatch.setattr(review_pass, "_run_engine_pass", _engine_pass)
    spec = cast("EngineRoundSpec", object())
    task = base_row(None)
    review_pass._review_cycle_zero(**_cycle_zero_kwargs(task, base), engine=spec)
    review_pass._rereview(**_rereview_kwargs(task, base), engine=spec)
    assert [i.base_branch for i in seen] == [base, base]
    assert [i.config_branch for i in seen] == [CLIENT_DEFAULT, CLIENT_DEFAULT]


# --- the engine stays on the trust root ------------------------------------------


@pytest.fixture
def engine_repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo, engine_files=ENGINE_STUB)


def test_a_base_row_on_the_codex_lane_reads_the_default_branch_manifest(
    engine_repo: EngineRepo,
) -> None:
    # The epic branch deleted the manifest: reading the row's base would refuse.
    push_epic_branch(engine_repo, edit_engine=True)
    client = ClientConfig(
        name=engine_repo.client,
        workspace_path=engine_repo.worktree,
        default_branch="main",
        review=ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill=ENGINE_SKILL, guarded_paths=[]),
        ),
    )
    task = base_row(ROW_BASE, ticket_id=engine_repo.ticket, client=engine_repo.client)
    spec = engine_round_spec(client, task, engine_repo.worktree)
    assert isinstance(spec, EngineRoundSpec)
    assert spec.context.default_branch == "main"
