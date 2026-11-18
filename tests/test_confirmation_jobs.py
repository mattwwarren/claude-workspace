"""Independent confirmation keeps its process and result across client calls."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cli import main
from cw.cli.review import confirm, converge_delta
from cw.codex_review import make_codex_blocked
from cw.confirmation_jobs import (
    ACTIVE_FILE,
    JOB_DIR,
    MANIFEST_FILE,
    PROCESS_GROUP_FILE,
    RESULT_FILE,
    RUNNING_EXIT,
    ConfirmationJob,
    JobResult,
    _worker,
    job_result,
    pin_confirmation,
    review_process_remains,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.exceptions import CwError
from cw.review_convergence import LEDGER_RELATIVE_PATH, load_ledger
from tests._review_confirm_harness import Harness, make_harness
from tests._review_convergence_helpers import (
    TICKET_ID,
    branch_with_resolution_and_unrelated_hunks,
    consolidated_verdict,
    ledger_with_rounds,
    resolution_ledger,
    write_ledger,
)
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from click.testing import Result

    from cw.confirmation_jobs import ConfirmKind

pytestmark = pytest.mark.platform_sensitive

# Test teardown guard for a fresh interpreter and repository on loaded CI.
_CHILD_WAIT_SECONDS = 60

_CHILD_HOOK = """\
import os
import sys
from pathlib import Path
from time import sleep

import click

sys.path.insert(0, os.environ["CW_TEST_PROJECT_ROOT"])

from cw.cli.review import confirm, converge_delta
from cw.codex_review import make_codex_blocked
from cw.review_convergence import rev_parse_or_none
from tests._review_convergence_helpers import consolidated_verdict

def review(*, worktree, task, **kwargs):
    with (worktree / '.cw/role-starts').open('a') as starts:
        starts.write('role started\\n')
    while not (worktree / '.cw/release-role').exists():
        sleep(0.01)
    code = int(os.environ.get('CW_TEST_CONFIRM_EXIT', '0'))
    if code:
        click.echo('backend result', err=True)
        raise click.exceptions.Exit(code)
    result = make_codex_blocked(ticket_id=task.ticket_id, worktree=worktree,
        reason='codex_must_fix_findings', retry_eligible=False, details='test')
    return confirm.RoundOutcome(result, consolidated_verdict(
        sha=os.environ.get('CW_TEST_REVIEW_SHA') or rev_parse_or_none(worktree, 'HEAD')
    ), None)

confirm.require_backend_binary = lambda backend: None
converge_delta.require_backend_binary = confirm.require_backend_binary
confirm.run_backend_round = converge_delta.run_backend_round = review
"""


@pytest.fixture
def harness(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    instance = make_harness(
        tmp_path,
        make_git_repo_with_origin,
        monkeypatch,
        ticket=TICKET_ID,
        client="sample",
        session="review-session",
        argv=("review", "converge", "confirm-job", "full"),
        client_extra={"review": {"convergence": "clean"}},
    )
    exclude = git_in(instance.repo, "rev-parse", "--git-path", "info/exclude")
    with (instance.repo / exclude).open("a") as ignored:
        ignored.write("\n.cw/\n")
    diff_base = git_in(instance.repo, "merge-base", "origin/main", "HEAD")
    write_ledger(
        instance.repo,
        ledger_with_rounds(
            (), shas=[instance.head()], confirm="codex", diff_base=diff_base
        ),
    )
    return instance


@pytest.fixture
def detached_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[subprocess.Popen[bytes]]]:
    hook_dir = tmp_path / "worker-hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(_CHILD_HOOK)
    monkeypatch.setenv("CW_TEST_PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("PYTHONPATH", str(hook_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local/share"))
    monkeypatch.setattr("cw.cli.review.confirm_job.POLL_SECONDS", 0.01)

    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def track_process(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = popen(*args, **kwargs)
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv[:3] == [sys.executable, "-P", "-c"]:
            processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", track_process)
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


def _invoke(harness: Harness, job_id: str | None = None) -> Result:
    options = ("--job-id", job_id) if job_id is not None else ()
    return harness.cli.invoke(main, harness.args(*options))


def _start(harness: Harness) -> str:
    start = _invoke(harness)
    assert start.exit_code == RUNNING_EXIT, start.output
    assert start.stdout == ""
    prefix = "CONFIRM_JOB_ID="
    assert start.stderr.startswith(prefix)
    return start.stderr.removeprefix(prefix).strip()


def _release(harness: Harness, process: subprocess.Popen[bytes]) -> None:
    (harness.repo / ".cw/release-role").touch()
    assert process.wait(timeout=_CHILD_WAIT_SECONDS) == 0


def _resume_worker() -> None:
    store = load_dev_queue()
    store.tasks[0].session_id = "resumed-review-session"
    save_dev_queue(store)


def test_another_worker_attaches_then_replays_the_same_completed_verdict(
    harness: Harness, detached_children: list[subprocess.Popen[bytes]]
) -> None:
    job_id = _start(harness)
    _resume_worker()
    assert _start(harness) == job_id
    waiting = _invoke(harness, job_id)
    assert waiting.exit_code == RUNNING_EXIT
    assert waiting.stdout == ""
    assert _invoke(harness, "different-job").exit_code == 1
    assert len(detached_children) == 1

    _release(harness, detached_children[0])
    collected = _invoke(harness, job_id)
    assert collected.exit_code == 0, collected.output
    assert json.loads(collected.stdout)["reviewed_sha"] == harness.head()
    assert _invoke(harness).stdout == collected.stdout
    assert (harness.repo / ".cw/role-starts").read_text() == "role started\n"


def test_a_reviewer_process_interrupted_before_completion_has_no_verdict(
    harness: Harness, detached_children: list[subprocess.Popen[bytes]]
) -> None:
    job_id = _start(harness)
    detached_children[0].send_signal(signal.SIGKILL)
    assert detached_children[0].wait(timeout=_CHILD_WAIT_SECONDS) == -signal.SIGKILL

    failure = _invoke(harness, job_id)
    assert failure.exit_code == 1
    assert failure.stdout == ""
    assert "confirmation was interrupted" in failure.stderr
    assert _invoke(harness).exit_code == 1
    assert len(detached_children) == 1


def test_worker_import_uses_installed_cw_despite_a_worktree_module(
    harness: Harness,
    detached_children: list[subprocess.Popen[bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = shutil.which("git")
    assert git is not None
    bin_dir = tmp_path / "worker-bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("PYTHONPATH")
    (harness.repo / "cw.py").write_text('raise RuntimeError("worktree module")\n')

    job_id = _start(harness)
    assert detached_children[0].wait(timeout=_CHILD_WAIT_SECONDS) == 0

    failure = _invoke(harness, job_id)
    assert failure.exit_code == 3, failure.output
    assert json.loads(failure.stdout)["cause"] == "binary_missing"


def test_stale_head_cannot_replay_a_completed_verdict(
    harness: Harness, detached_children: list[subprocess.Popen[bytes]]
) -> None:
    job_id = _start(harness)
    _release(harness, detached_children[0])
    commit_tracked_file(harness.repo, "changed.py")

    failure = _invoke(harness, job_id)
    assert failure.exit_code == 1
    assert failure.stdout == ""
    assert "HEAD changed" in failure.stderr


def test_stale_diff_base_cannot_replay_a_completed_verdict(
    harness: Harness, detached_children: list[subprocess.Popen[bytes]]
) -> None:
    job_id = _start(harness)
    _release(harness, detached_children[0])
    git_in(harness.repo, "update-ref", "refs/remotes/origin/main", harness.head())

    failure = _invoke(harness, job_id)
    assert failure.exit_code == 1
    assert failure.stdout == ""
    assert "diff base" in failure.stderr


@pytest.mark.parametrize("exit_code", [3, 4])
def test_poll_replays_the_original_failure_code_and_stream(
    harness: Harness,
    detached_children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    monkeypatch.setenv("CW_TEST_CONFIRM_EXIT", str(exit_code))
    job_id = _start(harness)
    _release(harness, detached_children[0])

    failure = _invoke(harness, job_id)
    assert failure.exit_code == exit_code
    assert failure.stdout == ""
    assert "backend result" in failure.stderr
    _resume_worker()
    assert _start(harness) != job_id
    assert _invoke(harness, job_id).exit_code == exit_code
    _release(harness, detached_children[1])


def test_wrong_verdict_head_is_refused(
    harness: Harness,
    detached_children: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CW_TEST_REVIEW_SHA", "a" * 40)
    job_id = _start(harness)
    _release(harness, detached_children[0])

    failure = _invoke(harness, job_id)
    assert failure.exit_code == 1
    assert failure.stdout == ""
    assert "returned a verdict for a different HEAD" in failure.stderr


def test_delta_result_replays_against_its_own_ledger_update(
    harness: Harness, detached_children: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    shape = branch_with_resolution_and_unrelated_hunks(harness.repo, tmp_path / "main")
    write_ledger(harness.repo, resolution_ledger(clean=shape.clean))
    harness.argv = ("review", "converge", "confirm-job", "delta")
    job_id = _start(harness)
    _release(harness, detached_children[0])

    collected = _invoke(harness, job_id)
    assert collected.exit_code == 0, collected.output
    assert json.loads(collected.stdout)["decision"] == "clean"
    assert _invoke(harness).stdout == collected.stdout
    ledger = load_ledger(harness.repo / LEDGER_RELATIVE_PATH)
    assert ledger is not None
    assert ledger.last_clean_sha == harness.head()
    assert ledger.rounds[-1].kind == "delta"

    write_ledger(harness.repo, ledger.model_copy(update={"max_cycles": 9}))
    stale = _invoke(harness, job_id)
    assert stale.exit_code == 1
    assert stale.stdout == ""
    assert "ledger changed" in stale.stderr


def test_invalid_job_state_fails_closed(harness: Harness) -> None:
    root = harness.repo / JOB_DIR
    root.mkdir()
    with (root / ACTIVE_FILE).open("w") as active:
        active.write("not a job")
    failure = _invoke(harness)
    assert failure.exit_code == 1
    assert "cannot access independent confirmation job" in failure.stderr


def test_missing_delta_commit_receipt_cannot_replay_success(harness: Harness) -> None:
    harness.argv = ("review", "converge", "confirm-job", "delta")
    path = _seed_worker_job(harness, "delta")
    (path / RESULT_FILE).write_text(JobResult(exit_code=0).model_dump_json())

    failure = _invoke(harness, path.name)
    assert failure.exit_code == 1
    assert failure.stdout == ""
    assert "did not publish its recorded delta ledger" in failure.stderr


def test_interrupted_group_detection_requires_evidence(tmp_path: Path) -> None:
    group_file = tmp_path / PROCESS_GROUP_FILE
    assert not review_process_remains(tmp_path)
    group_file.write_text(str(os.getpgrp()))
    assert review_process_remains(tmp_path)
    group_file.write_text("0")
    with pytest.raises(CwError, match="invalid process group"):
        review_process_remains(tmp_path)


def _seed_worker_job(harness: Harness, kind: ConfirmKind) -> Path:
    pin = pin_confirmation(
        harness.repo,
        kind=kind,
        ticket_id=TICKET_ID,
        client=harness.client,
        branch="main",
    )
    job = ConfirmationJob(id="a" * 32, pin=pin, session_id=None)
    root = harness.repo / JOB_DIR
    path = root / job.id
    path.mkdir(parents=True)
    manifest = job.model_dump_json()
    (root / ACTIVE_FILE).write_text(manifest)
    (path / MANIFEST_FILE).write_text(manifest)
    return path


def test_worker_refuses_to_start_without_its_persisted_job(harness: Harness) -> None:
    path = _seed_worker_job(harness, "full")
    (path / MANIFEST_FILE).unlink()
    with (
        (path.parent / "lock").open("a+") as lock,
        pytest.raises(CwError, match="lost its launch record"),
    ):
        _worker([str(path), str(os.dup(lock.fileno()))])


@pytest.mark.parametrize("kind", ["full", "delta"])
def test_worker_persists_a_terminal_result_from_the_real_command(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: ConfirmKind
) -> None:
    if kind == "delta":
        shape = branch_with_resolution_and_unrelated_hunks(
            harness.repo, tmp_path / "main"
        )
        write_ledger(harness.repo, resolution_ledger(clean=shape.clean))
    path = _seed_worker_job(harness, kind)
    command = converge_delta if kind == "delta" else confirm
    blocker = make_codex_blocked(
        ticket_id=harness.ticket,
        worktree=harness.repo,
        reason="codex_must_fix_findings",
        retry_eligible=False,
        details="test",
    )
    outcome = confirm.RoundOutcome(
        blocker, consolidated_verdict(sha=harness.head()), None
    )
    monkeypatch.setattr(command, "require_backend_binary", lambda _backend: None)
    monkeypatch.setattr(command, "run_backend_round", lambda **_kwargs: outcome)
    monkeypatch.chdir(harness.repo)

    with (path.parent / "lock").open("a+") as lock:
        _worker([str(path), str(os.dup(lock.fileno()))])

    result = job_result(path)
    assert result is not None
    assert result.exit_code == 0
    assert (result.ledger is not None) == (kind == "delta")
