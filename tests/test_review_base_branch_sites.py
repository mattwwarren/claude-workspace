"""Merge-target reads take the row's base; trust-root reads stay on the client default.

RFC 0020 S3a. Each flipped site is shown twice: a row with a recorded base
resolves to it, and a null-base row is unchanged (the client default).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

import cw.cli.review.confirm as confirm_cli
from cw.cli.review.converge import _default_branch
from cw.confirmation_jobs import ConfirmationPin
from cw.dev_queue import add_ticket
from cw.models import TicketTask
from cw.review_convergence import (
    MergeTolerance,
    tolerance_for_client,
    tolerance_from_context,
)
from cw.ticket_snapshot import DEFAULT_SNAPSHOT_CONTEXT
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_convergence_helpers import make_review_client
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path

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
