"""Worktree-local jobs for independent confirmation (ADR-0014).

The child inherits the launcher's exclusive flock and closes it only on
exit. A stopped polling client cannot kill the review, and an available
lock without a terminal marker is evidence of interruption, never success.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import click
from pydantic import BaseModel, ConfigDict, Field

from cw.atomic import atomic_write_text
from cw.cli import main
from cw.exceptions import CwError
from cw.review_convergence import (
    LEDGER_RELATIVE_PATH,
    load_ledger,
    resolve_diff_base,
    rev_parse_or_none,
    verify_diff_base,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_convergence import ConvergenceLedger

JOB_DIR = Path(".cw/review-confirm-job")
ACTIVE_FILE = "active.json"
MANIFEST_FILE = "job.json"
RESULT_FILE = "result.json"
PROCESS_GROUP_FILE = "process-group"
STDOUT_FILE = "stdout.json"
STDERR_FILE = "stderr.txt"
# Short foreground wait for evidence. The detached job has no deadline.
POLL_SECONDS = 20
POLL_INTERVAL_SECONDS = 0.2
RUNNING_EXIT = 75

type ConfirmKind = Literal["full", "delta"]
JOB_ID_PATTERN = r"^[0-9a-f]{32}$"


class ConfirmationPin(BaseModel):
    """Inputs that must agree at launch, review, and result collection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ConfirmKind
    client: str
    ticket_id: str
    backend: Literal["codex", "claude"]
    default_branch: str
    head: str
    diff_base: str
    ledger: str

    def check(self, worktree: Path, *, ledger: str | None = None) -> None:
        """Reject a result if its git inputs or ledger generation moved."""
        if rev_parse_or_none(worktree, "HEAD") != self.head:
            msg = "independent confirmation is stale: HEAD changed"
            raise CwError(msg)
        verify_diff_base(worktree, self.diff_base, self.default_branch)
        current = require_job_ledger(worktree)
        if ledger_digest(current) != (ledger if ledger is not None else self.ledger):
            msg = "independent confirmation is stale: the convergence ledger changed"
            raise CwError(msg)


class ConfirmationJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=JOB_ID_PATTERN)
    pin: ConfirmationPin
    session_id: str | None


class JobResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: Literal[0, 1, 2, 3, 4]
    ledger: str | None = None


@dataclass
class ConfirmationRun:
    """Click context of the detached child, separate from direct CLI calls."""

    pin: ConfirmationPin
    recorded_ledger: str | None = None


def ledger_digest(ledger: ConvergenceLedger) -> str:
    return sha256(ledger.model_dump_json().encode()).hexdigest()


def require_job_ledger(worktree: Path) -> ConvergenceLedger:
    path = worktree / LEDGER_RELATIVE_PATH
    ledger = load_ledger(path)
    if ledger is None:
        msg = f"no convergence ledger at {path}"
        raise CwError(msg)
    return ledger


def pin_confirmation(
    worktree: Path, *, kind: ConfirmKind, client: str, ticket_id: str, branch: str
) -> ConfirmationPin:
    ledger = require_job_ledger(worktree)
    head = rev_parse_or_none(worktree, "HEAD")
    backend = ledger.confirm_backend
    if ledger.ticket_id != ticket_id or backend == "none" or head is None:
        msg = "no independent confirmation for this ticket and worktree"
        raise CwError(msg)
    if kind == "full":
        if ledger.awaiting_confirm_sha != head or ledger.diff_base is None:
            msg = "confirmation requires a loop round and a diff-base pin at HEAD"
            raise CwError(msg)
        diff_base = verify_diff_base(worktree, ledger.diff_base, branch)
    else:
        # The full-round ledger pin predates the merge. Delta confirmation
        # derives its own resolution from the pinned HEAD and default branch.
        diff_base = resolve_diff_base(worktree, branch)
    return ConfirmationPin(
        kind=kind,
        client=client,
        ticket_id=ticket_id,
        backend=backend,
        default_branch=branch,
        head=head,
        diff_base=diff_base,
        ledger=ledger_digest(ledger),
    )


def validate_managed_round(
    worktree: Path, *, head: str, backend: str, diff_base: str | None = None
) -> ConfirmationRun | None:
    """Bind the real synchronous round to the inputs selected by its launcher."""
    run = click.get_current_context().obj
    if not isinstance(run, ConfirmationRun):
        return None
    run.pin.check(worktree)
    if (
        head != run.pin.head
        or backend != run.pin.backend
        or (diff_base is not None and diff_base != run.pin.diff_base)
    ):
        msg = "independent confirmation is stale: reviewer inputs changed"
        raise CwError(msg)
    return run


def read_job(path: Path) -> ConfirmationJob | None:
    try:
        return ConfirmationJob.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None


def active_job(root: Path) -> ConfirmationJob | None:
    return read_job(root / ACTIVE_FILE)


def find_job(root: Path, job_id: str) -> ConfirmationJob | None:
    if re.fullmatch(JOB_ID_PATTERN, job_id) is None:
        return None
    job = read_job(root / job_id / MANIFEST_FILE)
    return job if job is not None and job.id == job_id else None


def job_result(path: Path) -> JobResult | None:
    try:
        return JobResult.model_validate_json((path / RESULT_FILE).read_bytes())
    except FileNotFoundError:
        return None


def review_process_remains(path: Path) -> bool:
    """After interruption, keep out retries while any reviewer still runs."""
    try:
        group = int((path / PROCESS_GROUP_FILE).read_text())
    except FileNotFoundError:
        # The worker publishes its group before starting any reviewer.
        return False
    if group <= 1:
        msg = "independent confirmation has an invalid process group"
        raise CwError(msg)
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def launch_confirmation(
    worktree: Path, pin: ConfirmationPin, session_id: str | None, lock_fd: int
) -> ConfirmationJob:
    """Persist intent, then hand off the lock without an ownership gap."""
    job = ConfirmationJob(id=uuid4().hex, pin=pin, session_id=session_id)
    root = worktree / JOB_DIR
    path = root / job.id
    path.mkdir(mode=0o700)
    manifest = job.model_dump_json()
    atomic_write_text(path / MANIFEST_FILE, manifest)
    atomic_write_text(root / ACTIVE_FILE, manifest)
    with (
        (path / STDOUT_FILE).open("wb") as stdout,
        (path / STDERR_FILE).open("wb") as stderr,
    ):
        subprocess.Popen(
            [
                sys.executable,
                # The ticket worktree can contain a different `cw` package.
                "-P",
                "-c",
                "import sys; from cw.confirmation_jobs import _worker; "
                "_worker(sys.argv[1:])",
                str(path),
                str(lock_fd),
            ],
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            pass_fds=(lock_fd,),
            start_new_session=True,
        )
    return job


def _review_args(pin: ConfirmationPin) -> list[str]:
    args = ["review", "confirm", "--backend", pin.backend]
    if pin.kind == "delta":
        args = ["review", "converge", "delta-confirm"]
    return [*args, "--ticket-id", pin.ticket_id, "--client", pin.client]


def _run_cli(pin: ConfirmationPin, run: ConfirmationRun) -> JobResult:
    try:
        # Convert Click exits exactly as the foreground executable does.
        main.main(_review_args(pin), obj=run)
    except SystemExit as exc:
        match exc.code:
            case 0 | 1 | 2 | 3 | 4 as code:
                return JobResult(exit_code=code, ledger=run.recorded_ledger)
            case _:
                raise
    return JobResult(exit_code=0, ledger=run.recorded_ledger)


def _worker(argv: Sequence[str]) -> None:
    path = Path(argv[0])
    lock_fd = int(argv[1])
    # Keep the inherited descriptor open until the result and streams are
    # complete. Children of this worker must never inherit the lock.
    os.set_inheritable(lock_fd, False)
    with os.fdopen(lock_fd, encoding="utf-8"):
        job = find_job(path.parent, path.name)
        if job is None:
            msg = "independent confirmation lost its launch record"
            raise CwError(msg)
        atomic_write_text(path / PROCESS_GROUP_FILE, str(os.getpgrp()))
        run = ConfirmationRun(job.pin)
        result = _run_cli(job.pin, run)
        sys.stdout.flush()
        sys.stderr.flush()
        atomic_write_text(path / RESULT_FILE, result.model_dump_json())
