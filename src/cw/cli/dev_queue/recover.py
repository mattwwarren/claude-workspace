"""``cw dev-queue recover-red-pr`` -- recover a shipped PR whose CI went red (#2945).

The recovery path for the ``shipped_pr_checks_failed`` page
(``cw.shipped_pr_signal``): re-read the live PR, build head-pinned CI-failure
feedback with the shared synthesizer, disarm auto-merge (verified by read-back)
and regress the COMPLETED row to impl through the same ``_stage_regress`` the
operator ``--regress`` uses, so the fix passes REVIEW again before finalize
can re-arm auto-merge. No queue state is hand-edited.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import click

from cw.cli._base import handle_errors
from cw.config import get_client, load_orchestrator_config
from cw.dev_queue import (
    classify_requeue_live_session_error,
    load_dev_queue,
    resolve_client,
)
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.red_pr_recovery import recover_red_pr_ticket
from cw.exceptions import CwError, RequeueLiveSessionError
from cw.native_daemon import get_native_daemon_client
from cw.shipped_pr_recovery import prepare_red_pr_recovery

from ._group import dev_queue
from ._requeue_echo import echo_requeue_outcome

if TYPE_CHECKING:
    from cw.dev_queue.red_pr_recovery import RedPrRecovery

_log = logging.getLogger(__name__)

_RECOVER_REASON = "cli_recover_red_pr"


def _warn_row_write_refused(recovery: RedPrRecovery) -> None:
    """Audit the disarm that happened before the locked row write refused."""
    _log.warning(
        "recover-red-pr: row write refused pr_url=%s was_armed=%s disarmed=True"
        " outcome=row_write_refused",
        recovery.pr_url,
        recovery.was_armed,
    )


def _locked_step(
    ticket_id: str, client: str, recovery: RedPrRecovery
) -> dict[str, str | bool | int]:
    """The locked row write; a refusal after the disarm is audited and re-raised."""
    try:
        return recover_red_pr_ticket(
            ticket_id,
            client,
            recovery,
            native_daemon=get_native_daemon_client(),
            bounded=True,
        )
    except RequeueLiveSessionError as exc:
        _warn_row_write_refused(recovery)
        _tag, message = classify_requeue_live_session_error(exc)
        note = (
            f"auto-merge on {recovery.pr_url} is already disarmed but the row was"
            " not changed; finalize re-arms it after review, so a retry is safe"
            " once the session is gone"
        )
        full = f"{message}\n{note}"
        raise click.ClickException(full) from exc
    except CwError:
        _warn_row_write_refused(recovery)
        raise


@dev_queue.command(name="recover-red-pr")
@click.argument("ticket_id", metavar="TICKET")
@click.option("--client", "-c", default=None, help="Client name.")
@handle_errors
def dev_queue_recover_red_pr(ticket_id: str, client: str | None) -> None:
    """Recover a shipped PR whose required check failed.

    Disarms auto-merge on the PR (verified by read-back from GitHub), then
    regresses the COMPLETED row to impl with head-pinned CI-failure feedback.
    The PR stays disarmed until finalize re-arms it after the recovered commit
    passes review; do not re-arm it by hand (gh pr merge --auto) before then.
    """
    config = load_orchestrator_config()
    resolved = resolve_client(ticket_id, config, client)
    task = _find_ticket(load_dev_queue(), ticket_id, resolved)
    recovery = prepare_red_pr_recovery(task, get_client(resolved))
    result = _locked_step(ticket_id, resolved, recovery)
    echo_requeue_outcome(
        ticket_id,
        resolved,
        result,
        reason=_RECOVER_REASON,
        extra_payload={
            "pr_url": recovery.pr_url,
            "head_sha": recovery.head_sha,
            "failing_checks": list(recovery.checks),
            "was_armed": recovery.was_armed,
            "disarmed": True,
        },
    )
    click.echo(
        f"PR {recovery.pr_url} is now disarmed; finalize re-arms auto-merge"
        " after the recovered commit passes review."
    )
