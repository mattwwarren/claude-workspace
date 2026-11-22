"""Merge-target reads take the row's base; trust-root reads stay on the client default.

RFC 0020 S3a. Each flipped site is shown twice: a row with a recorded base
resolves to it, and a null-base row is unchanged (the client default).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

import cw.cli.review.confirm as confirm_cli
from cw.claude_review import core as claude_core
from cw.cli import main
from cw.cli.review import attestation as attestation_cli
from cw.cli.review import converge_delta, converge_tip
from cw.cli.review.confirm_job import _matches
from cw.cli.review.converge import _default_branch
from cw.codex_review import _engine_round
from cw.codex_review import core as codex_core
from cw.codex_review._engine_round import EnginePassInputs, EngineRoundSpec
from cw.codex_review._engine_start import engine_round_spec
from cw.confirmation_jobs import ConfirmationJob, ConfirmationPin
from cw.dev_queue import add_ticket
from cw.dispatch import review_gates
from cw.dispatch import review_tip_sweep as sweep_mod
from cw.dispatch.review_approval_gate import _evaluate
from cw.dispatch.review_tip_verdicts import TipCheck, TipKey
from cw.models import ClientConfig, TicketTask
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.reconcile.review_recipes.repair_fence import (
    build_fence_snapshot,
    evaluate_repair_fence,
)
from cw.review_approval import AttestationVerdict
from cw.review_convergence import (
    MergeTolerance,
    resolve_loop_diff_base,
    review_move_tolerance,
    tolerance_for_client,
    tolerance_from_context,
    tolerates_review_move,
)
from cw.review_engine.context import EngineContext, read_engine_context
from cw.review_engine.materialize import materialize_skill
from cw.review_engine.preflight import run_preflight
from cw.stage_verification.fence import check_fix_fence, scope_violations
from cw.ticket_snapshot import DEFAULT_SNAPSHOT_CONTEXT
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_approval_helpers import (
    ApprovalRepo,
    approval_task,
    cw_context,
    make_approval_repo,
    write_context,
)
from tests._review_convergence_helpers import (
    confirmed_ledger,
    make_review_client,
    write_ledger,
)
from tests._review_engine_helpers import (
    EngineRepo,
    engine_context_payload,
    make_engine_repo,
    write_engine_context,
)
from tests._shared.base_branch_builders import (
    CLIENT_DEFAULT,
    ROW_BASE,
    epic_merged_worktree,
)
from tests._shared.base_branch_builders import (
    ENGINE_SKILL as _SKILL,
)
from tests._shared.base_branch_builders import (
    ENGINE_SKILL_DIR as _SKILL_DIR,
)
from tests._shared.base_branch_builders import (
    ENGINE_SKILL_MD as _SKILL_MD,
)
from tests._shared.base_branch_builders import (
    ENGINE_STUB as _STUB,
)
from tests._shared.base_branch_builders import (
    base_row as _row,
)
from tests._shared.base_branch_builders import (
    push_epic_branch as _push_epic_branch,
)
from tests._shared.git_repos import (
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable


# --- merge tolerance (review-move chain, S3a) -----------------------------


def test_tolerance_for_client_takes_the_row_base(tmp_path: Path) -> None:
    client = make_review_client(tmp_path, default_branch=CLIENT_DEFAULT)
    assert tolerance_for_client(client, _row(ROW_BASE)) == MergeTolerance(ROW_BASE)


def test_tolerance_for_client_null_row_is_the_client_default(tmp_path: Path) -> None:
    client = make_review_client(tmp_path, default_branch=CLIENT_DEFAULT)
    assert tolerance_for_client(client, _row(None)) == MergeTolerance(CLIENT_DEFAULT)
    assert tolerance_for_client(client) == MergeTolerance(CLIENT_DEFAULT)


def test_tolerance_from_context_reads_the_top_level_base() -> None:
    context: dict[str, object] = {"base_branch": ROW_BASE}
    assert tolerance_from_context(context, {}) == MergeTolerance(ROW_BASE)


# --- converge diff-base reader (S3a) ---------------------------------------


def _write_context(path: Path, body: dict[str, Any]) -> Path:
    path.write_text(json.dumps(body))
    return path


def test_diff_base_reads_the_row_base(tmp_path: Path) -> None:
    ctx = _write_context(
        tmp_path / "ctx.json",
        {
            "base_branch": ROW_BASE,
            "world_state_snapshot": {"origin_main_branch": "main"},
        },
    )
    assert _default_branch(ctx, None) == ROW_BASE


def test_diff_base_v22_context_falls_back_to_origin_main(tmp_path: Path) -> None:
    ctx = _write_context(
        tmp_path / "ctx.json", {"world_state_snapshot": {"origin_main_branch": "main"}}
    )
    assert _default_branch(ctx, None) == "main"


def test_diff_base_flag_wins_over_the_context(tmp_path: Path) -> None:
    ctx = _write_context(tmp_path / "ctx.json", {"base_branch": ROW_BASE})
    assert _default_branch(ctx, "develop") == "develop"


def test_diff_base_missing_context_is_main(tmp_path: Path) -> None:
    assert _default_branch(tmp_path / "absent.json", None) == "main"


# --- confirm round: the trust-root split (S3a, R2) -------------------------


def test_run_backend_round_diffs_the_row_base_and_reads_config_from_the_trust_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_review_client(tmp_path, default_branch=CLIENT_DEFAULT)
    seen: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "round"

    monkeypatch.setattr(confirm_cli, "_run_round", _spy)
    monkeypatch.setattr(confirm_cli, "get_client", lambda _name: client)

    result = confirm_cli.run_backend_round(
        backend="codex",
        task=_row(ROW_BASE),
        worktree=tmp_path,
        base_branch=ROW_BASE,
        base_ref="base-sha",
        model=None,
        reasoning_effort="low",
        snapshots=DEFAULT_SNAPSHOT_CONTEXT,
    )

    assert result == "round"
    assert seen["base_branch"] == ROW_BASE
    assert seen["config_branch"] == CLIENT_DEFAULT
    assert seen["base_ref"] == "base-sha"


# --- ConfirmationPin read-compat (S3a, R6) ---------------------------------


def _pin_fields(**extra: object) -> dict[str, object]:
    return {
        "kind": "full",
        "client": "test-client",
        "ticket_id": "T-1",
        "backend": "codex",
        "head": "a" * 40,
        "diff_base": "b" * 40,
        "ledger": "c" * 64,
        **extra,
    }


def test_pin_legacy_default_branch_key_loads_as_the_base() -> None:
    pin = ConfirmationPin.model_validate(_pin_fields(default_branch=ROW_BASE))
    assert pin.base_branch == ROW_BASE


def test_pin_new_base_key_loads() -> None:
    pin = ConfirmationPin.model_validate(_pin_fields(base_branch=ROW_BASE))
    assert pin.base_branch == ROW_BASE


def test_pin_with_both_keys_keeps_the_base() -> None:
    pin = ConfirmationPin.model_validate(
        _pin_fields(default_branch="main", base_branch=ROW_BASE)
    )
    assert pin.base_branch == ROW_BASE


def test_pin_still_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ConfirmationPin.model_validate(_pin_fields(base_branch=ROW_BASE, bogus=1))


def _job(base: str) -> ConfirmationJob:
    pin = ConfirmationPin.model_validate(_pin_fields(base_branch=base))
    return ConfirmationJob(id="a" * 32, pin=pin, session_id=None)


def _match(job: ConfirmationJob, **overrides: Any) -> bool:
    asked: dict[str, Any] = {
        "client": "test-client",
        "ticket_id": "T-1",
        "kind": "full",
        "branch": ROW_BASE,
    }
    return _matches(job, **{**asked, **overrides})


def test_a_job_matches_the_base_its_pin_carries() -> None:
    job = _job(ROW_BASE)

    assert _match(job, branch=ROW_BASE) is True
    assert _match(job, branch=CLIENT_DEFAULT) is False


def test_a_job_pinned_to_the_client_default_does_not_match_the_row_base() -> None:
    job = _job(CLIENT_DEFAULT)

    assert _match(job, branch=ROW_BASE) is False
    assert _match(job, branch=CLIENT_DEFAULT) is True


@pytest.mark.parametrize(
    "override",
    [{"client": "other"}, {"ticket_id": "T-2"}, {"kind": "delta"}],
)
def test_a_job_with_a_matching_base_still_needs_the_other_fields(
    override: dict[str, Any],
) -> None:
    assert _match(_job(ROW_BASE), **override) is False


# --- confirm.resolve_round_inputs (S3a, R6) --------------------------------


def _queued_row(tmp_path: Path, base: str | None) -> Path:
    """A client whose default is ``trunk`` and a dev-queue row carrying *base*."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_clients_yaml(
        ClientSpec("acme", workspace_path=workspace, default_branch=CLIENT_DEFAULT)
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    add_ticket(
        _make_ticket_task(
            ticket_id="T-1", client="acme", worktree_path=worktree, base_branch=base
        )
    )
    return worktree


def _round_inputs(worktree: Path, flag: str | None) -> tuple[TicketTask, str]:
    return confirm_cli.resolve_round_inputs(
        ticket_id="T-1", client="acme", worktree=worktree, default_branch=flag
    )


def test_resolve_round_inputs_returns_the_row_base(tmp_path: Path) -> None:
    worktree = _queued_row(tmp_path, ROW_BASE)

    task, base = _round_inputs(worktree, None)

    assert task.base_branch == ROW_BASE
    assert base == ROW_BASE


def test_resolve_round_inputs_null_row_is_the_client_default(tmp_path: Path) -> None:
    worktree = _queued_row(tmp_path, None)

    _task, base = _round_inputs(worktree, None)

    assert base == CLIENT_DEFAULT


def test_resolve_round_inputs_flag_overrides_a_base_row(tmp_path: Path) -> None:
    worktree = _queued_row(tmp_path, ROW_BASE)

    _task, base = _round_inputs(worktree, "develop")

    assert base == "develop"


# --- the review-move chain on a real repo (S3a) ----------------------------


@pytest.fixture
def epic_merge(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]], tmp_path: Path
) -> tuple[Path, str, str]:
    """``(worktree, clean, head)``: feature merged ``origin/epic/x``, not main.

    ``origin/epic/x`` starts at ``main`` and advances out of band; the feature
    branch (one commit past main) merges it, so ``head`` is a clean merge whose
    second parent is on the epic branch only.
    """
    wt, clean, head = epic_merged_worktree(
        make_git_repo_with_origin, tmp_path / "scratch"
    )
    write_ledger(wt, confirmed_ledger(sha=clean))
    return wt, clean, head


def test_tolerates_review_move_accepts_a_merge_of_the_row_base(
    epic_merge: tuple[Path, str, str],
) -> None:
    wt, clean, head = epic_merge
    client = make_review_client(wt)

    tolerated = tolerates_review_move(
        client, wt, reviewed_sha=clean, head_sha=head, task=_row(ROW_BASE)
    )

    assert tolerated is True


def test_tolerates_review_move_refuses_that_merge_for_a_null_row(
    epic_merge: tuple[Path, str, str],
) -> None:
    wt, clean, head = epic_merge
    client = make_review_client(wt)

    assert not tolerates_review_move(
        client, wt, reviewed_sha=clean, head_sha=head, task=_row(None)
    )
    assert not tolerates_review_move(client, wt, reviewed_sha=clean, head_sha=head)


def test_review_move_tolerance_names_the_row_base_not_the_client_default(
    epic_merge: tuple[Path, str, str],
) -> None:
    wt, clean, _head = epic_merge
    client = make_review_client(wt, default_branch=CLIENT_DEFAULT)

    with_base = review_move_tolerance(
        client, wt, reviewed_sha=clean, task=_row(ROW_BASE)
    )
    null_row = review_move_tolerance(client, wt, reviewed_sha=clean, task=_row(None))

    assert with_base == MergeTolerance(ROW_BASE)
    assert null_row == MergeTolerance(CLIENT_DEFAULT)


# --- dispatch threads the row to the tip check (S3a) -----------------------


def _tip_check(task: TicketTask, client: ClientConfig, worktree: Path) -> TipCheck:
    return TipCheck(
        key=TipKey(task.client, task.ticket_id, "r" * 40, "h" * 40),
        client=client,
        worktree=worktree,
        task=task,
    )


def test_plan_review_staleness_carries_the_row_into_the_tip_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_review_client(tmp_path)
    row = _row(ROW_BASE)
    seen: list[TicketTask | None] = []

    def _tolerance(
        _client: object, _wt: Path, *, reviewed_sha: str, task: TicketTask | None = None
    ) -> MergeTolerance:
        seen.append(task)
        return MergeTolerance(ROW_BASE)

    monkeypatch.setattr(review_gates, "_reports_review_content", lambda _r: True)
    monkeypatch.setattr(
        review_gates,
        "_measure_review",
        lambda *_a: ("r" * 40, "h" * 40, client, tmp_path),
    )
    monkeypatch.setattr(review_gates, "review_move_tolerance", _tolerance)

    plan = review_gates.plan_review_staleness(row, {}, {})

    assert isinstance(plan, TipCheck)
    assert plan.task is row
    assert seen == [row]


def test_the_tip_sweep_checks_each_tip_against_its_rows_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_review_client(tmp_path)
    row = _row(ROW_BASE)
    seen: list[TicketTask | None] = []

    def _tolerates(
        _client: object,
        _wt: Path,
        *,
        reviewed_sha: str,
        head_sha: str,
        task: TicketTask | None = None,
    ) -> bool:
        seen.append(task)
        return True

    plan = _tip_check(row, client, tmp_path)
    monkeypatch.setattr(sweep_mod, "_plan_for_event", lambda *_a: plan)
    monkeypatch.setattr(sweep_mod, "tolerates_review_move", _tolerates)

    verdicts = sweep_mod.resolve_review_tip_sweep(
        [cast("Any", object())], cast("Any", None), cast("Any", None), {}
    ).verdicts

    assert verdicts == {plan.key: True}
    assert seen == [row]


def test_the_in_lock_gate_checks_an_unswept_tip_against_the_rows_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_review_client(tmp_path)
    row = _row(ROW_BASE)
    seen: list[TicketTask | None] = []

    def _tolerates(
        _client: object,
        _wt: Path,
        *,
        reviewed_sha: str,
        head_sha: str,
        task: TicketTask | None = None,
    ) -> bool:
        seen.append(task)
        return True

    plan = _tip_check(row, client, tmp_path)
    monkeypatch.setattr(review_gates, "plan_review_staleness", lambda *_a: plan)
    monkeypatch.setattr(review_gates, "tolerates_review_move", _tolerates)

    gated = review_gates._should_gate_for_review_staleness(row, {}, {})

    assert gated is False
    assert seen == [row]


# --- the engine stays on the trust root (S3a, R5) --------------------------


@pytest.fixture
def engine_repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo, engine_files=_STUB)


def _engine_context(repo: EngineRepo) -> EngineContext:
    return EngineContext(
        skill=_SKILL,
        guarded_paths=(),
        ticket_id=repo.ticket,
        client=repo.client,
        default_branch="main",
    )


def test_engine_context_default_branch_ignores_the_context_base(
    tmp_path: Path,
) -> None:
    payload = engine_context_payload(skill=_SKILL, default_branch="main")
    payload["base_branch"] = ROW_BASE
    path = write_engine_context(tmp_path, payload)

    context = read_engine_context(path)

    assert context.default_branch == "main"


def test_materialize_skill_copies_the_default_branch_skill_not_the_bases(
    engine_repo: EngineRepo,
) -> None:
    _push_epic_branch(engine_repo, edit_engine=True)
    wt = engine_repo.worktree
    main_text = git_in(wt, "show", f"origin/main:{_SKILL_MD}")
    assert git_in(wt, "show", f"origin/{ROW_BASE}:{_SKILL_MD}") != main_text

    materialized = materialize_skill(
        wt,
        round_id="r1",
        expect_head=engine_repo.head,
        context=_engine_context(engine_repo),
    )

    assert Path(materialized.skill_md).read_text().strip() == main_text.strip()


def test_engine_round_spec_reads_the_default_branch_manifest_for_a_base_row(
    engine_repo: EngineRepo,
) -> None:
    # The epic branch deleted the manifest: a read of the row's base would refuse.
    _push_epic_branch(engine_repo, edit_engine=True)
    client = ClientConfig(
        name=engine_repo.client,
        workspace_path=engine_repo.worktree,
        default_branch="main",
        review=ReviewConfig(
            convergence="clean",
            engine=ReviewEngineConfig(skill=_SKILL, guarded_paths=[]),
        ),
    )
    task = _make_ticket_task(
        ticket_id=engine_repo.ticket, client=engine_repo.client, base_branch=ROW_BASE
    )

    spec = engine_round_spec(client, task, engine_repo.worktree)

    assert isinstance(spec, EngineRoundSpec)
    assert spec.context.default_branch == "main"


def _preflight_against(repo: EngineRepo, base: str) -> tuple[str, list[str]]:
    diff_base = resolve_loop_diff_base(repo.worktree, base)
    verdict = run_preflight(
        repo.worktree, diff_base=diff_base, context=_engine_context(repo)
    )
    return verdict.decision, verdict.touched


def test_a_base_row_whose_branch_touches_the_engine_parks_like_a_default_row(
    engine_repo: EngineRepo,
) -> None:
    _push_epic_branch(engine_repo, edit_engine=False)
    touched = f"{_SKILL_DIR}/notes.md"
    commit_tracked_file(engine_repo.worktree, touched, "edited\n")

    on_base = _preflight_against(engine_repo, ROW_BASE)
    on_default = _preflight_against(engine_repo, "main")

    assert on_base == ("park", [touched])
    assert on_base == on_default


def test_a_base_that_carries_the_engine_edit_does_not_park_its_branch(
    engine_repo: EngineRepo,
) -> None:
    _push_epic_branch(engine_repo, edit_engine=True)
    wt = engine_repo.worktree
    git_in(wt, "checkout", "-q", "-B", "dev/1", f"origin/{ROW_BASE}")
    commit_tracked_file(wt, "src/b.py", "y = 2\n")

    on_base = _preflight_against(engine_repo, ROW_BASE)
    on_default = _preflight_against(engine_repo, "main")

    assert on_base == ("run", [])
    assert on_default[0] == "park"


# --- run_review / run_claude_review split the base from the trust root -----


class _StoppedError(Exception):
    """Raised by the spy so a review pass stops right after its inputs."""


def _spy_prepare(monkeypatch: pytest.MonkeyPatch, module: object) -> dict[str, object]:
    seen: dict[str, object] = {}

    def _stop(
        _task: object, _worktree: object, base_branch: str, **kwargs: object
    ) -> None:
        seen.update(kwargs, diff_base=base_branch)
        raise _StoppedError

    monkeypatch.setattr(module, "_prepare_review_pass", _stop)
    return seen


def _codex_round(tmp_path: Path, *, base_branch: str = "main", **extra: object) -> None:
    codex_core.run_review(
        runner=cast("Any", None),
        task=_row(None),
        worktree=tmp_path,
        default_branch="main",
        base_branch=base_branch,
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s",
        fix_loop_enabled=False,
        **cast("Any", extra),
    )


def _claude_round(
    tmp_path: Path, *, base_branch: str = "main", **extra: object
) -> None:
    claude_core.run_claude_review(
        runner=cast("Any", None),
        task=_row(None),
        worktree=tmp_path,
        default_branch="main",
        base_branch=base_branch,
        base_ref=None,
        model=None,
        wall_clock_budget_seconds=None,
        session_id="s",
        **cast("Any", extra),
    )


def test_run_review_diffs_the_base_and_reads_config_from_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_prepare(monkeypatch, codex_core)

    with pytest.raises(_StoppedError):
        _codex_round(tmp_path, base_branch=ROW_BASE)

    assert seen["diff_base"] == ROW_BASE
    assert seen["config_branch"] == "main"


def test_run_review_requires_a_base_branch() -> None:
    """S3b (#3840): base_branch is a required keyword with no default."""
    params = inspect.signature(codex_core.run_review).parameters
    param = params["base_branch"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_run_claude_review_diffs_the_base_and_reads_config_from_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_prepare(monkeypatch, claude_core)

    with pytest.raises(_StoppedError):
        _claude_round(tmp_path, base_branch=ROW_BASE)

    assert seen["diff_base"] == ROW_BASE
    assert seen["config_branch"] == "main"


def test_run_claude_review_base_ref_still_wins_over_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_prepare(monkeypatch, claude_core)

    with pytest.raises(_StoppedError):
        claude_core.run_claude_review(
            runner=cast("Any", None),
            task=_row(None),
            worktree=tmp_path,
            default_branch="main",
            base_ref="pinned-sha",
            base_branch=ROW_BASE,
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s",
        )

    assert seen["diff_base"] == "pinned-sha"


def test_run_claude_review_requires_a_base_branch() -> None:
    """S3b (#3840): base_branch is a required keyword with no default."""
    params = inspect.signature(claude_core.run_claude_review).parameters
    param = params["base_branch"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


# --- synthesis gets the base, not the trust root (S3a, R2) -----------------


def _stub_to_synthesis(
    monkeypatch: pytest.MonkeyPatch, module: object, roles_attr: str
) -> dict[str, Any]:
    """Let a review entry point run past its prepare step to the synthesis call.

    The prepared pass, the role runner and synthesis are replaced by minimal
    stand-ins, so the returned dict holds exactly what synthesis was handed.
    """
    seen: dict[str, Any] = {}
    prepared = SimpleNamespace(
        diff="d",
        reviewed_sha="s" * 40,
        capability=None,
        agent_spec_status=None,
        review_config=None,
        voided_findings=[],
        finding_dispositions=[],
        refused_dispositions=[],
        prompts_by_role={},
        roles=[],
    )
    ran = SimpleNamespace(
        documents=[],
        failures=[],
        metrics_by_role={},
        pre_validation_rejected=[],
        coverage=[],
        mutation=None,
    )

    def _synthesize(**kwargs: Any) -> tuple[object, None]:
        seen.update(kwargs)
        return SimpleNamespace(blocker=None), None

    monkeypatch.setattr(module, "_prepare_review_pass", lambda *_a, **_k: prepared)
    monkeypatch.setattr(module, roles_attr, lambda *_a, **_k: ran)
    monkeypatch.setattr(module, "synthesize_codex_review_result", _synthesize)
    return seen


@pytest.mark.parametrize(
    ("base", "expected"), [(ROW_BASE, ROW_BASE), (None, CLIENT_DEFAULT)]
)
def test_run_review_hands_synthesis_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str | None, expected: str
) -> None:
    seen = _stub_to_synthesis(monkeypatch, codex_core, "run_prepared_roles")

    codex_core.run_review(
        runner=cast("Any", None),
        task=_row(base),
        worktree=tmp_path,
        default_branch=CLIENT_DEFAULT,
        # A null row's base is the client default (resolve_base_branch).
        base_branch=base or CLIENT_DEFAULT,
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s",
        fix_loop_enabled=False,
    )

    assert seen["default_branch"] == expected


@pytest.mark.parametrize(
    ("base", "base_ref", "expected"),
    [
        (ROW_BASE, None, ROW_BASE),
        (None, None, CLIENT_DEFAULT),
        (ROW_BASE, "pinned-sha", ROW_BASE),
        (None, "pinned-sha", CLIENT_DEFAULT),
    ],
)
def test_run_claude_review_hands_synthesis_the_base_not_the_diff_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: str | None,
    base_ref: str | None,
    expected: str,
) -> None:
    seen = _stub_to_synthesis(monkeypatch, claude_core, "run_claude_roles")
    monkeypatch.setattr(claude_core, "_fail_if_prompt_too_large", lambda *_a: None)

    claude_core.run_claude_review(
        runner=cast("Any", None),
        task=_row(base),
        worktree=tmp_path,
        default_branch=CLIENT_DEFAULT,
        base_ref=base_ref,
        base_branch=base or CLIENT_DEFAULT,
        model=None,
        wall_clock_budget_seconds=None,
        session_id="s",
    )

    assert seen["default_branch"] == expected


# --- EnginePassInputs: base for the diff, config for the trust root --------


def _pass_inputs(
    repo: EngineRepo, *, base_branch: str, config_branch: str = "main"
) -> EnginePassInputs:
    return EnginePassInputs(
        runner=cast("Any", None),
        task=_make_ticket_task(ticket_id=repo.ticket, client=repo.client),
        worktree=repo.worktree,
        base_branch=base_branch,
        config_branch=config_branch,
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-engine",
        fix_loop_enabled=True,
        prior_open_findings=None,
        claim_tier_enabled=False,
        disposition_drift_check_enabled=True,
        settled_suppression=None,
        max_prompt_chars=None,
    )


def test_engine_preflight_diffs_the_base_not_the_trust_root(
    engine_repo: EngineRepo,
) -> None:
    # The base carries the engine edit; the branch above it does not touch it.
    _push_epic_branch(engine_repo, edit_engine=True)
    wt = engine_repo.worktree
    git_in(wt, "checkout", "-q", "-B", "dev/1", f"origin/{ROW_BASE}")
    commit_tracked_file(wt, "src/b.py", "y = 2\n")
    spec = EngineRoundSpec(context=_engine_context(engine_repo))

    on_base = _engine_round._preflight(
        spec, _pass_inputs(engine_repo, base_branch=ROW_BASE), 0
    )
    on_default = _engine_round._preflight(
        spec, _pass_inputs(engine_repo, base_branch="main"), 0
    )

    assert on_base is None
    assert on_default is not None


def test_engine_prepare_reads_config_from_the_trust_root_and_diffs_the_base(
    engine_repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def _stop(
        _task: object, _worktree: object, base_branch: str, **kwargs: object
    ) -> None:
        seen.update(kwargs, diff_base=base_branch)
        raise _StoppedError

    monkeypatch.setattr(_engine_round, "_prepare_review_pass", _stop)
    monkeypatch.setattr(_engine_round, "_engine_roster", lambda _manifest: None)
    inputs = _pass_inputs(engine_repo, base_branch=ROW_BASE, config_branch="main")

    with pytest.raises(_StoppedError):
        _engine_round._prepare(inputs, cast("Any", SimpleNamespace(manifest=None)))

    assert seen["diff_base"] == ROW_BASE
    assert seen["config_branch"] == "main"


def test_engine_synthesis_measures_scope_against_the_base(
    engine_repo: EngineRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def _synthesize(**kwargs: object) -> tuple[None, None]:
        seen.update(kwargs)
        return None, None

    monkeypatch.setattr(_engine_round, "synthesize_codex_review_result", _synthesize)
    prepared = SimpleNamespace(
        diff="",
        reviewed_sha="r",
        capability=None,
        agent_spec_status=None,
        review_config=None,
    )
    run = SimpleNamespace(metrics_by_role={}, pre_validation_rejected=[], coverage=[])
    inputs = _pass_inputs(engine_repo, base_branch=ROW_BASE, config_branch="main")

    _engine_round._synthesize(
        inputs,
        cast("Any", prepared),
        cast("Any", SimpleNamespace(document=None)),
        cast("Any", run),
    )

    assert seen["default_branch"] == ROW_BASE


# --- approval-chain parity: gate, fence and CLI agree (S3a, R3) ------------


@pytest.fixture
def epic_approval(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]], tmp_path: Path
) -> ApprovalRepo:
    """Approved at the feature tip, then a clean merge of ``origin/epic/x``.

    The merged commit is on the epic branch only, so it counts as landed work
    against ``origin/main`` and as a clean merge against ``origin/epic/x``.
    """
    made = make_approval_repo(make_git_repo_with_origin, "wt-approval-epic")
    wt = made.worktree
    git_in(wt, "push", "-q", "origin", f"main:refs/heads/{ROW_BASE}")
    push_commit_to_origin(made.origin, ROW_BASE, tmp_path / "side", "epic_only.py")
    git_in(wt, "fetch", "-q", "origin")
    git_in(wt, "merge", "--no-ff", "--no-edit", f"origin/{ROW_BASE}")
    return made


def _approved_row(repo: ApprovalRepo, base: str | None) -> TicketTask:
    row = approval_task(repo.approved)
    return row.model_copy(update={"base_branch": base, "scope_hint": "large"})


def _gate_token(repo: ApprovalRepo, row: TicketTask) -> str:
    client = make_review_client(repo.worktree, convergence="blocking")

    verdict, _head = _evaluate(client, row, "feature")

    return verdict.token


def _fence_token(repo: ApprovalRepo, row: TicketTask) -> str:
    client = make_review_client(repo.worktree, convergence="blocking")
    snapshot = build_fence_snapshot(row, client)

    verdict = evaluate_repair_fence(
        snapshot,
        repo=repo.worktree,
        new_sha=git_in(repo.worktree, "rev-parse", "HEAD"),
        origin_tip=None,
    )

    return verdict.approval_token


def _cli_token(repo: ApprovalRepo, row: TicketTask) -> str:
    extra = {} if row.base_branch is None else {"base_branch": row.base_branch}
    write_context(repo.worktree, cw_context(repo.worktree, row, **extra))

    result = CliRunner().invoke(
        main,
        [
            "review",
            "check-approval",
            "--tier",
            "large",
            "--worktree",
            str(repo.worktree),
        ],
    )

    line = result.output.strip()
    assert result.exit_code == 0, line
    if line == "REVIEW_APPROVAL_RECORDED":
        return "recorded"
    return line.removeprefix("REVIEW_APPROVAL_PARK: ").split(" — ")[0]


@pytest.mark.parametrize(
    ("base", "expected"), [(ROW_BASE, "recorded"), (None, "stale")]
)
def test_the_three_approval_producers_agree_on_a_merge_of_the_row_base(
    epic_approval: ApprovalRepo, base: str | None, expected: str
) -> None:
    row = _approved_row(epic_approval, base)

    tokens = (
        _gate_token(epic_approval, row),
        _fence_token(epic_approval, row),
        _cli_token(epic_approval, row),
    )

    assert tokens == (expected, expected, expected)


# --- converge verify-tip: the base names the merge target (S3a) ------------


def test_verify_tip_refreshes_the_base_ref_not_the_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argvs: list[list[str]] = []
    rejections = iter(["non_merge_commit abc1234", None])

    def _run_git(argv: list[str], **_kw: object) -> SimpleNamespace:
        argvs.append(argv)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(converge_tip, "run_git", _run_git)
    monkeypatch.setattr(
        converge_tip, "default_merge_rejection", lambda *_a: next(rejections)
    )

    rejection = converge_tip._rejection_after_refresh(
        tmp_path, "c" * 40, "d" * 40, MergeTolerance(ROW_BASE)
    )

    assert rejection is None
    assert argvs == [
        ["fetch", "origin", f"+refs/heads/{ROW_BASE}:refs/remotes/origin/{ROW_BASE}"]
    ]


def test_verify_tip_rejection_names_the_base(tmp_path: Path) -> None:
    check = converge_tip._rejected(
        tmp_path,
        cast("Any", None),
        "d" * 40,
        cast("Any", None),
        "non_merge_commit abc1234",
        tolerance=MergeTolerance(ROW_BASE),
        assess_resolution=False,
    )

    assert f"not a clean merge of origin/{ROW_BASE}:" in check.detail
    assert CLIENT_DEFAULT not in check.detail


# --- converge delta-confirm takes the row (S3a) ----------------------------


@pytest.mark.parametrize(
    ("base", "expected"), [(ROW_BASE, ROW_BASE), (None, CLIENT_DEFAULT)]
)
def test_delta_plan_judges_the_merge_against_the_rows_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str | None, expected: str
) -> None:
    client = make_review_client(tmp_path, default_branch=CLIENT_DEFAULT)
    seen: list[MergeTolerance | None] = []

    def _assess(*_a: object, tolerance: MergeTolerance | None, **_k: object) -> object:
        seen.append(tolerance)
        return SimpleNamespace(plan=None, refusal="ineligible")

    monkeypatch.setattr(converge_delta, "get_client", lambda _name: client)
    monkeypatch.setattr(converge_delta, "rev_parse_or_none", lambda *_a: "h" * 40)
    monkeypatch.setattr(
        converge_delta, "require_ledger", lambda _p: SimpleNamespace(ticket_id="T-1")
    )
    monkeypatch.setattr(converge_delta, "assess_resolution_round", _assess)

    with pytest.raises(converge_delta.DeltaRefusedError):
        converge_delta._resolve_plan(
            client="test-client",
            ticket_id="T-1",
            worktree=tmp_path,
            ledger_path=tmp_path / "ledger.json",
            task=_row(base),
        )

    assert seen == [MergeTolerance(expected)]


# --- attestation CLI: no row, so the context's base replaces the default ----


def _attestation_tolerance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    context: dict[str, object],
    client_tolerance: MergeTolerance | None,
) -> MergeTolerance | None:
    write_context(tmp_path, {"schema_version": 23, **context})
    captured: dict[str, Any] = {}

    def _evaluate_attestation(_raw: object, **kwargs: Any) -> AttestationVerdict:
        captured.update(kwargs)
        return AttestationVerdict("valid", "")

    client = make_review_client(tmp_path)
    monkeypatch.setattr(attestation_cli, "live_client", lambda _name: client)
    monkeypatch.setattr(
        attestation_cli, "review_move_tolerance", lambda *_a, **_k: client_tolerance
    )
    monkeypatch.setattr(
        attestation_cli, "evaluate_review_attestation", _evaluate_attestation
    )

    attestation_cli.attestation_verdict(tmp_path, None, "T-1", "test-client")

    return captured["merge_tolerance_for"]("c" * 40)


def test_attestation_applies_the_context_base_to_the_client_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tolerance = _attestation_tolerance(
        tmp_path,
        monkeypatch,
        context={"base_branch": ROW_BASE},
        client_tolerance=MergeTolerance(CLIENT_DEFAULT),
    )

    assert tolerance == MergeTolerance(ROW_BASE)


def test_attestation_without_a_context_base_keeps_the_client_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tolerance = _attestation_tolerance(
        tmp_path,
        monkeypatch,
        context={},
        client_tolerance=MergeTolerance(CLIENT_DEFAULT),
    )

    assert tolerance == MergeTolerance(CLIENT_DEFAULT)


def test_attestation_strict_client_stays_strict_despite_a_context_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tolerance = _attestation_tolerance(
        tmp_path,
        monkeypatch,
        context={"base_branch": ROW_BASE},
        client_tolerance=None,
    )

    assert tolerance is None


# --- the fix fence: diff base is the row's, the registry is the trust root --


@pytest.fixture
def epic_feature(
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]], tmp_path: Path
) -> Path:
    """feature = feature.py + a merge of ``origin/epic/x`` (which adds epic_only.py)."""
    wt, origin = make_git_repo_with_origin("wt-fence")
    git_in(wt, "push", "-q", "origin", f"main:refs/heads/{ROW_BASE}")
    push_commit_to_origin(origin, ROW_BASE, tmp_path / "side", "epic_only.py")
    git_in(wt, "fetch", "-q", "origin")
    git_in(wt, "merge", "--no-ff", "--no-edit", f"origin/{ROW_BASE}")
    git_in(wt, "rm", "-q", "feature.py")
    return wt


def _fence_paths(wt: Path, base: str) -> tuple[str, ...] | None:
    breach = check_fix_fence(
        wt,
        base_branch=base,
        allowed_files=None,
        finding_files=frozenset(),
        cycle=2,
        touched={"feature.py"},
    )
    return None if breach is None else breach.paths


def test_the_fix_fence_measures_the_branch_diff_from_the_rows_base(
    epic_feature: Path,
) -> None:
    on_base = _fence_paths(epic_feature, ROW_BASE)
    on_default = _fence_paths(epic_feature, "main")

    assert on_base == ("feature.py",)
    assert on_default != on_base


_REGISTRY = (
    "sensitive_files:\n  - path: pyproject.toml\n    category: dependency\n"
    "    reason: dependency changes need review\n"
)


def test_a_base_that_empties_the_registry_does_not_wave_an_edit_through(
    make_git_repo: Callable[..., Path],
) -> None:
    wt = make_git_repo("wt-registry")
    commit_tracked_file(wt, ".claude/sensitive-files.yml", _REGISTRY)
    git_in(wt, "checkout", "-q", "-b", ROW_BASE)
    commit_tracked_file(wt, ".claude/sensitive-files.yml", "sensitive_files: []\n")
    git_in(wt, "checkout", "-q", "-b", "dev/1")

    on_default = scope_violations(wt, ["pyproject.toml"], frozenset(), "small", "main")
    on_base = scope_violations(wt, ["pyproject.toml"], frozenset(), "small", ROW_BASE)

    assert [hit.path for hit in on_default] == ["pyproject.toml"]
    assert on_base == []  # the base's own registry would wave it through
