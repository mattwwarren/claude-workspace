"""Merge-target reads take the row's base; trust-root reads stay on the client default.

RFC 0020 S3a. Each flipped site is shown twice: a row with a recorded base
resolves to it, and a null-base row is unchanged (the client default).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError

import cw.cli.review.confirm as confirm_cli
from cw.cli.review.converge import _default_branch
from cw.codex_review._engine_round import EngineRoundSpec
from cw.codex_review._engine_start import engine_round_spec
from cw.confirmation_jobs import ConfirmationPin
from cw.dev_queue import add_ticket
from cw.dispatch import review_gates
from cw.dispatch import review_tip_sweep as sweep_mod
from cw.dispatch.review_tip_verdicts import TipCheck, TipKey
from cw.models import ClientConfig, TicketTask
from cw.models.client import ReviewConfig, ReviewEngineConfig
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
from cw.ticket_snapshot import DEFAULT_SNAPSHOT_CONTEXT
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_convergence_helpers import (
    confirmed_ledger,
    make_review_client,
    merge_origin_commit,
    write_ledger,
)
from tests._review_engine_helpers import (
    EngineRepo,
    engine_context_payload,
    make_engine_repo,
    write_engine_context,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable


ROW_BASE = "epic/x"
CLIENT_DEFAULT = "trunk"


def _row(base: str | None) -> TicketTask:
    return TicketTask(ticket_id="T-1", client="test-client", base_branch=base)


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
    wt = make_git_repo_with_origin("wt")[0]
    git_in(wt, "push", "origin", "main:refs/heads/epic/x")
    clean = git_in(wt, "rev-parse", "HEAD")
    merge_origin_commit(wt, tmp_path / "scratch", ROW_BASE, "epic_only.py")
    write_ledger(wt, confirmed_ledger(sha=clean))
    return wt, clean, git_in(wt, "rev-parse", "HEAD")


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

_STUB = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SKILL = "repo-review"
_SKILL_DIR = f".claude/skills/{_SKILL}"
_SKILL_MD = f"{_SKILL_DIR}/SKILL.md"
_EPIC_SKILL_TEXT = "# repo-review\n\nEdited on the epic branch.\n"


@pytest.fixture
def engine_repo(make_git_repo: Callable[..., Path]) -> EngineRepo:
    return make_engine_repo(make_git_repo, engine_files=_STUB)


def _push_epic_branch(repo: EngineRepo, *, edit_engine: bool) -> None:
    """``origin/epic/x``: main, plus an edited skill and no manifest if asked."""
    wt = repo.worktree
    git_in(wt, "checkout", "-b", ROW_BASE, "main")
    if edit_engine:
        commit_tracked_file(wt, _SKILL_MD, _EPIC_SKILL_TEXT)
        git_in(wt, "rm", "-q", f"{_SKILL_DIR}/cw-engine.json")
        git_in(wt, "commit", "-q", "-m", "drop the manifest on the epic branch")
    git_in(wt, "push", "-q", "origin", ROW_BASE)
    git_in(wt, "checkout", "dev/1")


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
