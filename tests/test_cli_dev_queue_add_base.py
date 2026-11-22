"""``cw dev-queue add --base`` (#3783, RFC 0020 B1): the CLI enqueue gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import cw.cli.dev_queue._epic_base as cli_epic_base
from cw.dev_queue import load_dev_queue
from cw.models import QueueItemStatus, Stage, TicketTask
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._shared.base_branch_builders import epic_repo_with_origin
from tests._ticket_view_helpers import (
    ACME,
    invoke_dev_queue,
    make_task,
    queue_row,
    seed_queue,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig

BASE = "epic/x"
FILES = (("big.py", 50), ("uv.lock", 40))
FULL = 1 + sum(n for _, n in FILES)


@pytest.fixture
def epic_client(
    make_git_repo: Callable[..., Path],
) -> Callable[..., Path]:
    """Write an ``acme`` client over a fresh epic repo; returns the repo path."""

    def _setup(
        *, extra: dict[str, object] | None = None, default_branch: str = "main"
    ) -> Path:
        repo, _ = epic_repo_with_origin(make_git_repo, BASE, files=FILES)
        write_clients_yaml(
            ClientSpec(
                ACME,
                repo_path=repo,
                branch="main",
                default_branch=default_branch,
                lanes=[{"name": "default"}],
                extra=extra or {},
            )
        )
        seed_queue([])
        return repo

    return _setup


def test_add_with_base_stamps_the_row(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue("add", "ACM-5", "--base", BASE, "-c", ACME)
    assert result.exit_code == 0, result.output
    row = queue_row("ACM-5")
    assert row.base_branch == BASE
    assert row.ever_spawned is False


def test_default_add_has_no_base(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue("add", "ACM-6", "-c", ACME)
    assert result.exit_code == 0, result.output
    assert queue_row("ACM-6").base_branch is None


@pytest.mark.parametrize("bad", ["bad branch", "../x"])
def test_invalid_charset_is_refused_at_parse_time(
    epic_client: Callable[..., Path], bad: str
) -> None:
    epic_client()
    result = invoke_dev_queue("add", "ACM-7", "--base", bad, "-c", ACME)
    assert result.exit_code == 2
    assert "Invalid base_branch" in result.output
    assert "Invalid ticket" not in result.output
    assert not [t for t in _rows() if t.ticket_id == "ACM-7"]


def test_redundant_default_base_is_refused(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue("add", "ACM-8", "--base", "main", "-c", ACME)
    assert result.exit_code == 1
    assert "it is the default branch of client 'acme'" in result.output
    assert not [t for t in _rows() if t.ticket_id == "ACM-8"]


def test_redundant_base_allowed_when_default_differs(
    epic_client: Callable[..., Path],
) -> None:
    epic_client(default_branch="trunk")
    result = invoke_dev_queue("add", "ACM-9", "--base", "main", "-c", ACME)
    assert result.exit_code == 0, result.output


def test_absent_on_origin_is_refused(
    epic_client: Callable[..., Path],
) -> None:
    epic_client()
    result = invoke_dev_queue("add", "ACM-10", "--base", "epic/missing", "-c", ACME)
    assert result.exit_code == 1
    assert "push `epic/missing` to origin first" in result.output
    assert not [t for t in _rows() if t.ticket_id == "ACM-10"]


def test_ls_remote_timeout_is_refused_and_writes_no_row(
    epic_client: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.exceptions import GitTimeoutError

    epic_client()

    def _timeout(*_a: object, **_kw: object) -> None:
        msg = "timed out"
        raise GitTimeoutError(msg)

    monkeypatch.setattr("cw.dev_queue.epic_base._run_git", _timeout)
    result = invoke_dev_queue("add", "ACM-11", "--base", BASE, "-c", ACME)
    assert result.exit_code == 1
    assert "could not verify" in result.output
    assert not [t for t in _rows() if t.ticket_id == "ACM-11"]


def test_unknown_client_is_an_error(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue("add", "ACM-12", "--base", BASE, "-c", "nope")
    assert result.exit_code == 1


def test_origin_check_runs_once_per_client(
    epic_client: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    epic_client()
    calls: list[str] = []
    real = cli_epic_base.verify_base_on_origin

    def _count(client: ClientConfig, base: str) -> None:
        calls.append(base)
        real(client, base)

    monkeypatch.setattr(cli_epic_base, "verify_base_on_origin", _count)
    result = invoke_dev_queue(
        "add", "ACM-20", "ACM-21", "ACM-22", "--base", BASE, "-c", ACME
    )
    assert result.exit_code == 0, result.output
    assert calls == [BASE]


def test_ceiling_over_is_refused_with_no_row(
    epic_client: Callable[..., Path],
) -> None:
    epic_client(extra={"epic": {"max_diff_lines": 10}})
    result = invoke_dev_queue("add", "ACM-30", "--base", BASE, "-c", ACME)
    assert result.exit_code == 1
    assert f"{FULL} changed lines" in result.output
    assert not [t for t in _rows() if t.ticket_id == "ACM-30"]


def test_ceiling_unset_makes_no_measurement_call(
    epic_client: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    epic_client()

    def _forbidden(*_a: object, **_kw: object) -> None:
        msg = "no measurement when the ceiling is unset"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.dev_queue.epic_base.fetch_default_branch", _forbidden)
    result = invoke_dev_queue("add", "ACM-31", "--base", BASE, "-c", ACME)
    assert result.exit_code == 0, result.output


def test_existing_live_row_gets_the_not_applied_suffix(
    epic_client: Callable[..., Path],
) -> None:
    epic_client()
    seed_queue([make_task(ticket_id="ACM-1", client=ACME)])
    result = invoke_dev_queue("add", "ACM-1", "--base", BASE, "-c", ACME)
    assert result.exit_code == 0, result.output
    assert "Skipped ACM-1 -> acme: already queued" in result.stderr
    assert (
        " --base not applied: the existing row keeps its own base"
        " (there is no retarget verb)." in result.stderr
    )
    assert queue_row("ACM-1").base_branch is None


def test_skipped_without_base_has_no_suffix(epic_client: Callable[..., Path]) -> None:
    epic_client()
    seed_queue([make_task(ticket_id="ACM-1", client=ACME)])
    result = invoke_dev_queue("add", "ACM-1", "-c", ACME)
    assert "Skipped ACM-1 -> acme" in result.stderr
    assert "--base not applied" not in result.stderr


def test_sibling_note_on_a_different_base(epic_client: Callable[..., Path]) -> None:
    epic_client()
    seed_queue(
        [
            make_task(
                ticket_id="ACM-40",
                status=QueueItemStatus.COMPLETED,
                stage=Stage.PLAN,
            )
        ]
    )
    result = invoke_dev_queue(
        "add", "ACM-40", "--stage", "impl", "--base", BASE, "-c", ACME
    )
    assert result.exit_code == 0, result.output
    assert (
        "Note: ACM-40 already has a plan row (completed) on the client default;"
        " this new row is on base 'epic/x'. Rows do not inherit a base"
        " (RFC 0020, ADR-0024)."
    ) in result.stderr
    stages = {t.stage for t in _rows() if t.ticket_id == "ACM-40"}
    assert stages == {Stage.PLAN, Stage.IMPL}


def test_no_sibling_note_when_bases_match(epic_client: Callable[..., Path]) -> None:
    epic_client()
    seed_queue([make_task(ticket_id="ACM-41", base_branch=BASE)])
    result = invoke_dev_queue(
        "add", "ACM-41", "--stage", "impl", "--base", BASE, "-c", ACME
    )
    assert "Note:" not in result.stderr


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("corrupt")])
def test_sibling_note_is_fail_soft(
    epic_client: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    epic_client()

    def _broken() -> None:
        raise error

    monkeypatch.setattr(cli_epic_base, "load_dev_queue", _broken)
    result = invoke_dev_queue("add", "ACM-9X", "-c", ACME)
    assert result.exit_code == 0, result.output
    assert "Enqueued ACM-9X -> acme (priority=0)" in result.stdout
    assert "Traceback" not in result.output
    assert "Note:" not in result.stderr
    assert [t for t in _rows() if t.ticket_id == "ACM-9X"]


def test_adopt_path_passes_base_to_run_adopt(
    epic_client: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    epic_client()
    captured: dict[str, object] = {}

    def _capture(*_a: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("cw.cli.dev_queue.crud.run_adopt", _capture)
    result = invoke_dev_queue(
        "add", "ACM-50", "--adopt-pr", "7", "--base", BASE, "-c", ACME
    )
    assert result.exit_code == 0, result.output
    assert captured["base"] == BASE


def test_adopt_path_without_base_passes_none(
    epic_client: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    epic_client()
    captured: dict[str, object] = {}

    def _capture(*_a: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("cw.cli.dev_queue.crud.run_adopt", _capture)
    result = invoke_dev_queue("add", "ACM-51", "--adopt-pr", "7", "-c", ACME)
    assert result.exit_code == 0, result.output
    assert captured["base"] is None


def test_adopt_path_passes_a_default_equal_base_through(
    epic_client: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6: the adopt path adds no refusal; B2 owns the PR-base check."""
    epic_client()
    captured: dict[str, object] = {}

    def _capture(*_a: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("cw.cli.dev_queue.crud.run_adopt", _capture)
    result = invoke_dev_queue(
        "add", "ACM-52", "--adopt-pr", "7", "--base", "main", "-c", ACME
    )
    assert result.exit_code == 0, result.output
    assert captured["base"] == "main"


def test_help_names_the_base_option(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue("add", "--help")
    text = " ".join(result.output.split())
    assert "--base TEXT" in text
    assert "Merge target for this row (default: the client's default branch)." in text
    assert "Rows do not inherit a base from sibling rows." in text


def _rows() -> list[TicketTask]:
    return list(load_dev_queue().tasks)
