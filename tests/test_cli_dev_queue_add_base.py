"""``cw dev-queue add --base`` (#3783, RFC 0020 B1): the CLI enqueue gate."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import cw.cli.dev_queue._epic_base as cli_epic_base
import cw.config
from cw._lock_guard import held_locks
from cw.dev_queue import epic_base, load_dev_queue
from cw.dev_queue.crud import build_queued_task
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


class GitCalls:
    """Every git call ``cw.dev_queue.epic_base`` makes, with the locks held then."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def named(self, name: str) -> list[tuple[str, tuple[object, ...]]]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> GitCalls:
    """Spy on ``_run_git`` and both ``fetch_*`` seams, delegating to the real ones.

    Records ``run_git:<subcommand>``, ``fetch_default`` and ``fetch_base`` with
    ``cw._lock_guard.held_locks()`` at call time.
    """
    spy = GitCalls()
    real_run = epic_base._run_git
    real_default = epic_base.fetch_default_branch
    real_base = epic_base.fetch_base_branch

    def _run(
        *args: str, cwd: Path, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        spy.calls.append((f"run_git:{args[0]}", held_locks()))
        return real_run(*args, cwd=cwd, check=check, timeout=timeout)

    def _default(client: ClientConfig) -> object:
        spy.calls.append(("fetch_default", held_locks()))
        return real_default(client)

    def _base(client: ClientConfig, base: str) -> object:
        spy.calls.append(("fetch_base", held_locks()))
        return real_base(client, base)

    monkeypatch.setattr(epic_base, "_run_git", _run)
    monkeypatch.setattr(epic_base, "fetch_default_branch", _default)
    monkeypatch.setattr(epic_base, "fetch_base_branch", _base)
    return spy


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
    epic_client: Callable[..., Path], git_calls: GitCalls, bad: str
) -> None:
    epic_client(extra={"epic": {"max_diff_lines": 10}})
    result = invoke_dev_queue("add", "ACM-7", "--base", bad, "-c", ACME)
    assert result.exit_code == 2
    assert "Invalid base_branch" in result.output
    assert "Invalid ticket" not in result.output
    assert git_calls.calls == []
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


def test_redundancy_is_checked_before_the_network(
    epic_client: Callable[..., Path], git_calls: GitCalls
) -> None:
    """R3 order: ``trunk`` is absent on origin, yet the redundancy refusal wins."""
    epic_client(default_branch="trunk")
    result = invoke_dev_queue("add", "ACM-13", "--base", "trunk", "-c", ACME)
    assert result.exit_code == 1
    assert "it is the default branch of client 'acme'" in result.output
    assert "no such branch on origin" not in result.output
    assert git_calls.named("run_git:ls-remote") == []
    assert git_calls.calls == []
    assert not [t for t in _rows() if t.ticket_id == "ACM-13"]


def test_origin_is_checked_before_the_ceiling(
    epic_client: Callable[..., Path], git_calls: GitCalls
) -> None:
    """R3 order: an absent base is refused at the origin check, never measured."""
    epic_client(extra={"epic": {"max_diff_lines": 10}})
    result = invoke_dev_queue("add", "ACM-14", "--base", "epic/missing", "-c", ACME)
    assert result.exit_code == 1
    assert "push `epic/missing` to origin first" in result.output
    assert [name for name, _ in git_calls.calls] == ["run_git:ls-remote"]
    assert not [t for t in _rows() if t.ticket_id == "ACM-14"]


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
    by_stage = {t.stage: t for t in _rows() if t.ticket_id == "ACM-40"}
    assert by_stage[Stage.IMPL].base_branch == BASE
    assert by_stage[Stage.PLAN].base_branch is None


def test_sibling_note_when_the_flag_is_absent(
    epic_client: Callable[..., Path],
) -> None:
    epic_client()
    seed_queue(
        [
            make_task(
                ticket_id="ACM-42",
                base_branch=BASE,
                status=QueueItemStatus.COMPLETED,
                stage=Stage.PLAN,
            )
        ]
    )
    result = invoke_dev_queue("add", "ACM-42", "--stage", "impl", "-c", ACME)
    assert result.exit_code == 0, result.output
    assert ("on base 'epic/x'; this new row is on the client default") in result.stderr
    by_stage = {t.stage: t for t in _rows() if t.ticket_id == "ACM-42"}
    assert by_stage[Stage.IMPL].base_branch is None
    assert by_stage[Stage.PLAN].base_branch == BASE


def test_no_sibling_note_when_bases_match(epic_client: Callable[..., Path]) -> None:
    epic_client()
    seed_queue(
        [
            make_task(
                ticket_id="ACM-41",
                base_branch=BASE,
                status=QueueItemStatus.COMPLETED,
                stage=Stage.PLAN,
            )
        ]
    )
    result = invoke_dev_queue(
        "add", "ACM-41", "--stage", "impl", "--base", BASE, "-c", ACME
    )
    assert "Enqueued ACM-41" in result.stdout
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


def test_review_stage_row_is_stamped(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue(
        "add", "ACM-60", "--stage", "review", "--base", BASE, "-c", ACME
    )
    assert result.exit_code == 0, result.output
    row = queue_row("ACM-60")
    assert row.stage is Stage.REVIEW
    assert row.base_branch == BASE


def test_named_lane_row_is_stamped(epic_client: Callable[..., Path]) -> None:
    epic_client(extra={"lanes": [{"name": "default"}, {"name": "fast"}]})
    result = invoke_dev_queue(
        "add", "ACM-61", "--lane", "fast", "--base", BASE, "-c", ACME
    )
    assert result.exit_code == 0, result.output
    row = queue_row("ACM-61")
    assert row.lane == "fast"
    assert row.base_branch == BASE


def test_requeue_keeps_the_rows_base(epic_client: Callable[..., Path]) -> None:
    epic_client()
    seed_queue(
        [
            make_task(
                ticket_id="ACM-62",
                base_branch=BASE,
                status=QueueItemStatus.FAILED,
            )
        ]
    )
    result = invoke_dev_queue("requeue", "ACM-62", "-c", ACME, "--from-failed")
    assert result.exit_code == 0, result.output
    row = queue_row("ACM-62")
    assert row.status is QueueItemStatus.PENDING
    assert row.base_branch == BASE


def test_build_queued_task_rejects_a_bad_base() -> None:
    with pytest.raises(ValidationError):
        build_queued_task("ACM-63", ACME, lane="default", base_branch="bad branch")


def test_build_queued_task_defaults_to_no_base() -> None:
    task = build_queued_task("ACM-64", ACME, lane="default")
    assert task.base_branch is None


def test_two_clients_each_get_one_origin_check(
    epic_client: Callable[..., Path], git_calls: GitCalls
) -> None:
    repo = epic_client()
    write_clients_yaml(
        ClientSpec(
            ACME,
            repo_path=repo,
            branch="main",
            default_branch="main",
            lanes=[{"name": "default"}],
        ),
        ClientSpec(
            "beta",
            repo_path=repo,
            branch="main",
            default_branch="main",
            lanes=[{"name": "default"}],
        ),
    )
    config_file = cw.config.orchestrator_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("linear_prefix_map:\n  ACM: acme\n  BET: beta\n")
    result = invoke_dev_queue(
        "add", "ACM-70", "ACM-71", "BET-70", "BET-71", "--base", BASE
    )
    assert result.exit_code == 0, result.output
    assert len(git_calls.named("run_git:ls-remote")) == 2
    clients = {queue_row(t).client for t in ("ACM-70", "BET-70")}
    assert clients == {ACME, "beta"}


def test_adopt_path_skips_origin_and_ceiling_checks(
    epic_client: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    git_calls: GitCalls,
) -> None:
    """R6: a base absent on origin, with a ceiling set, still reaches ``run_adopt``."""
    epic_client(extra={"epic": {"max_diff_lines": 10}})
    captured: dict[str, object] = {}

    def _capture(*_a: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("cw.cli.dev_queue.crud.run_adopt", _capture)
    result = invoke_dev_queue(
        "add", "ACM-53", "--adopt-pr", "7", "--base", "epic/missing", "-c", ACME
    )
    assert result.exit_code == 0, result.output
    assert captured["base"] == "epic/missing"
    assert git_calls.calls == []


def test_verification_runs_with_no_lock_held(
    epic_client: Callable[..., Path], git_calls: GitCalls
) -> None:
    """ADR-0019 at the call site: verify runs before ``add_ticket`` takes its lock."""
    epic_client(extra={"epic": {"max_diff_lines": FULL}})
    result = invoke_dev_queue("add", "ACM-80", "--base", BASE, "-c", ACME)
    assert result.exit_code == 0, result.output
    names = {name for name, _ in git_calls.calls}
    assert names >= {
        "run_git:ls-remote",
        "run_git:diff",
        "fetch_default",
        "fetch_base",
    }
    assert all(locks == () for _, locks in git_calls.calls)
    assert queue_row("ACM-80").base_branch == BASE


def test_help_names_the_base_option(epic_client: Callable[..., Path]) -> None:
    epic_client()
    result = invoke_dev_queue("add", "--help")
    text = " ".join(result.output.split())
    assert "--base TEXT" in text
    assert "Merge target for this row (default: the client's default branch)." in text
    assert "Rows do not inherit a base from sibling rows." in text


def _rows() -> list[TicketTask]:
    return list(load_dev_queue().tasks)
