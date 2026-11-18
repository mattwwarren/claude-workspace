"""Launch or collect a detached independent confirmation from a worktree."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import click

from cw._flock import try_flock_until
from cw.cli._base import handle_errors
from cw.cli.review.confirm import resolve_round_inputs
from cw.cli.review.converge import converge, worktree_option
from cw.confirmation_jobs import (
    JOB_DIR,
    POLL_INTERVAL_SECONDS,
    POLL_SECONDS,
    RUNNING_EXIT,
    STDERR_FILE,
    STDOUT_FILE,
    active_job,
    find_job,
    job_result,
    launch_confirmation,
    pin_confirmation,
    review_process_remains,
)
from cw.exceptions import CwError

if TYPE_CHECKING:
    from pathlib import Path
    from typing import IO

    from cw.confirmation_jobs import ConfirmationJob, ConfirmKind, JobResult


def _pending(job: ConfirmationJob | None) -> NoReturn:
    job_id = job.id if job is not None else ""
    click.echo(f"CONFIRM_JOB_ID={job_id}", err=True)
    raise click.exceptions.Exit(RUNNING_EXIT)


def _matches(
    job: ConfirmationJob, *, client: str, ticket_id: str, kind: ConfirmKind, branch: str
) -> bool:
    return (
        job.pin.client,
        job.pin.ticket_id,
        job.pin.kind,
        job.pin.default_branch,
    ) == (
        client,
        ticket_id,
        kind,
        branch,
    )


def _replay(worktree: Path, job: ConfirmationJob, result: JobResult | None) -> NoReturn:
    path = worktree / JOB_DIR / job.id
    if result is None:
        msg = f"independent confirmation was interrupted; see {path / STDERR_FILE}"
        raise CwError(msg)
    if job.pin.kind == "delta" and result.exit_code == 0 and result.ledger is None:
        msg = "independent confirmation did not publish its recorded delta ledger"
        raise CwError(msg)
    job.pin.check(worktree, ledger=result.ledger)
    stdout = (path / STDOUT_FILE).read_text()
    stderr = (path / STDERR_FILE).read_text()
    click.echo(stdout, nl=False)
    click.echo(stderr, err=True, nl=False)
    raise click.exceptions.Exit(result.exit_code)


def _poll_job(
    worktree: Path,
    *,
    job_id: str,
    kind: ConfirmKind,
    client: str,
    ticket_id: str,
    branch: str,
    lock: IO[str],
) -> NoReturn:
    root = worktree / JOB_DIR
    job = find_job(root, job_id)
    if job is None or not _matches(
        job, client=client, ticket_id=ticket_id, kind=kind, branch=branch
    ):
        msg = "no matching independent confirmation job at this worktree"
        raise CwError(msg)
    path = root / job.id
    result = job_result(path)
    if result is None and active_job(root) == job:
        owned = try_flock_until(
            lock, timeout_s=POLL_SECONDS, poll_interval_s=POLL_INTERVAL_SECONDS
        )
        result = job_result(path)
        if not owned and result is None and active_job(root) == job:
            _pending(job)
    _replay(worktree, job, result)


def _start_or_replay(
    worktree: Path,
    *,
    job: ConfirmationJob | None,
    kind: ConfirmKind,
    client: str,
    ticket_id: str,
    branch: str,
    session_id: str | None,
    lock_fd: int,
) -> NoReturn:
    root = worktree / JOB_DIR
    result = job_result(root / job.id) if job is not None else None
    pin = pin_confirmation(
        worktree, kind=kind, client=client, ticket_id=ticket_id, branch=branch
    )
    if job is not None:
        succeeded = result is not None and result.exit_code == 0
        expected = job.pin
        if result is not None and result.ledger is not None:
            expected = expected.model_copy(update={"ledger": result.ledger})
        if expected == pin and (succeeded or job.session_id == session_id):
            _replay(worktree, job, result)
        if result is None and review_process_remains(root / job.id):
            msg = "interrupted independent confirmation still has a live reviewer"
            raise CwError(msg)
    _pending(launch_confirmation(worktree, pin, session_id, lock_fd))


@converge.command(name="confirm-job")
@click.argument("kind", type=click.Choice(("full", "delta")))
@click.option(
    "--ticket-id", required=True, help="Ticket whose dev-queue row to review."
)
@click.option("--client", "-c", required=True, help="Client the ticket belongs to.")
@click.option("--job-id", help="Poll an existing job without starting another review.")
@worktree_option
@handle_errors
def confirm_job(
    kind: ConfirmKind, ticket_id: str, client: str, job_id: str | None, worktree: Path
) -> None:
    """Start or resume independent confirmation without a review deadline.

    Start with no --job-id. Exit 75 means pending and prints CONFIRM_JOB_ID on
    stderr. Call again with that ID to wait briefly and collect the original
    stdout, stderr and exit code. A stale or interrupted job exits 1. No
    deadline is applied to the detached review and polling never signals it.
    """
    task, branch = resolve_round_inputs(
        ticket_id=ticket_id, client=client, worktree=worktree, default_branch=None
    )
    worktree = worktree.resolve()
    root = worktree / JOB_DIR
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (root / "lock").open("a+") as lock:
            if job_id is not None:
                _poll_job(
                    worktree,
                    job_id=job_id,
                    kind=kind,
                    client=client,
                    ticket_id=ticket_id,
                    branch=branch,
                    lock=lock,
                )
            owned = try_flock_until(lock, timeout_s=0, poll_interval_s=0)
            job = active_job(root)
            if not owned:
                if job is not None and not _matches(
                    job, client=client, ticket_id=ticket_id, kind=kind, branch=branch
                ):
                    msg = "a different independent confirmation job owns this worktree"
                    raise CwError(msg)
                _pending(job)
            _start_or_replay(
                worktree,
                job=job,
                kind=kind,
                client=client,
                ticket_id=ticket_id,
                branch=branch,
                session_id=task.session_id,
                lock_fd=lock.fileno(),
            )
    except (OSError, ValueError) as exc:
        msg = f"cannot access independent confirmation job: {exc}"
        raise CwError(msg) from exc
