"""Argument parser, subcommand dispatch and the ``main`` entry point."""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

from review_monitor_lib.check import cmd_check
from review_monitor_lib.comment_reviews import cmd_mark_comment_review
from review_monitor_lib.discovery import cmd_discover, cmd_recover_reviews
from review_monitor_lib.inbox import (
    DESKTOP_ACTION_TYPES,
    cmd_consume_pending,
    cmd_enqueue_action,
)
from review_monitor_lib.lifecycle import (
    cmd_ack_delta,
    cmd_complete,
    cmd_confirm_thread,
    cmd_drop,
    cmd_register,
    cmd_set_status,
    cmd_slack_thread_cursor,
    cmd_update_slack_cursor,
)
from review_monitor_lib.notify import (
    cmd_catchup,
    cmd_mark_escalated,
    cmd_mark_notified,
    cmd_nudge_ok,
    cmd_pending_channel_bumps,
    cmd_record_auto_fix,
    cmd_record_channel_bump,
    cmd_record_nudge,
)
from review_monitor_lib.shell import _run_gh
from review_monitor_lib.state import cmd_list_repos
from review_monitor_lib.status import cmd_status, cmd_status_all

if TYPE_CHECKING:
    from collections.abc import Callable


def _add_lifecycle_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register subcommands that create or change a PR's monitored lifecycle."""
    p_reg = subparsers.add_parser("register", help="Start monitoring a PR")
    p_reg.add_argument("pr_number", type=int)
    p_reg.add_argument("--role", required=True, choices=["reviewer", "author"])
    p_reg.add_argument("--repo", required=True)
    p_reg.add_argument("--repo-path", required=True)
    p_reg.add_argument("--sha", required=True)
    p_reg.add_argument("--review-id")
    p_reg.add_argument("--threads", nargs="*", default=[])
    p_reg.add_argument("--thread-details", help="JSON list of {id,file,line} objects")
    p_reg.add_argument(
        "--slack-channel", help="Slack channel ID for PR announcement thread"
    )
    p_reg.add_argument("--slack-ts", help="Parent ts for PR announcement thread")

    p_drop = subparsers.add_parser("drop", help="Stop monitoring a PR")
    p_drop.add_argument("pr_number", type=int)
    p_drop.add_argument("--repo", required=True)

    p_complete = subparsers.add_parser("complete", help="Mark a PR as done")
    p_complete.add_argument("pr_number", type=int)
    p_complete.add_argument("--repo", required=True)
    p_complete.add_argument("--reason", default="merged")

    p_set_status = subparsers.add_parser(
        "set-status", help="Set lifecycle status for a PR"
    )
    p_set_status.add_argument("pr_number", type=int)
    p_set_status.add_argument("--repo", required=True)
    p_set_status.add_argument(
        "--status",
        required=True,
        choices=["watching", "ready_to_approve", "approved"],
    )

    p_confirm_thread = subparsers.add_parser(
        "confirm-thread",
        help="Mark a thread addressed-by-code-change (delta-review confirmation pass)",
    )
    p_confirm_thread.add_argument("pr_number", type=int)
    p_confirm_thread.add_argument("--repo", required=True)
    p_confirm_thread.add_argument("--thread", required=True, dest="thread_id")

    p_ack_delta = subparsers.add_parser(
        "ack-delta",
        help=(
            "Acknowledge the reviewer delta through --sha has been processed"
            " (Step 3 close)"
        ),
    )
    p_ack_delta.add_argument("pr_number", type=int)
    p_ack_delta.add_argument("--repo", required=True)
    p_ack_delta.add_argument("--sha", required=True)


def _add_signal_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register subcommands that record nudges, notifications, and cursors."""
    p_nudge_ok = subparsers.add_parser("nudge-ok", help="Check if a nudge is allowed")
    p_nudge_ok.add_argument("pr_number", type=int)
    p_nudge_ok.add_argument("--repo", required=True)

    p_record_nudge = subparsers.add_parser(
        "record-nudge", help="Record a nudge was sent"
    )
    p_record_nudge.add_argument("pr_number", type=int)
    p_record_nudge.add_argument("--repo", required=True)

    p_mark_cr = subparsers.add_parser(
        "mark-comment-review",
        help="Record the classifier's verdict on a tracked COMMENTED review",
    )
    p_mark_cr.add_argument("pr_number", type=int)
    p_mark_cr.add_argument("--repo", required=True)
    p_mark_cr.add_argument("--review-id", required=True, dest="review_id")
    p_mark_cr.add_argument(
        "--classification",
        required=True,
        choices=["requests_changes", "neutral"],
    )

    p_mark_notified = subparsers.add_parser(
        "mark-notified", help="Record that a local ping fired for a state"
    )
    p_mark_notified.add_argument("pr_number", type=int)
    p_mark_notified.add_argument("--repo", required=True)
    p_mark_notified.add_argument("--state", required=True, dest="state_value")

    p_mark_escalated = subparsers.add_parser(
        "mark-escalated", help="Record that a Slack-bot escalation fired"
    )
    p_mark_escalated.add_argument("pr_number", type=int)
    p_mark_escalated.add_argument("--repo", required=True)

    p_slack_cursor = subparsers.add_parser(
        "slack-thread-cursor", help="Print Slack channel+ts+last_seen for a PR"
    )
    p_slack_cursor.add_argument("pr_number", type=int)
    p_slack_cursor.add_argument("--repo", required=True)

    p_update_cursor = subparsers.add_parser(
        "update-slack-cursor", help="Advance the slack_last_seen_ts cursor"
    )
    p_update_cursor.add_argument("pr_number", type=int)
    p_update_cursor.add_argument("--repo", required=True)
    p_update_cursor.add_argument("--last-seen-ts", required=True)

    p_record_fix = subparsers.add_parser(
        "record-auto-fix",
        help="Increment the per-day auto-fix attempt counter for a PR",
    )
    p_record_fix.add_argument("pr_number", type=int)
    p_record_fix.add_argument("--repo", required=True)

    p_record_bump = subparsers.add_parser(
        "record-channel-bump",
        help="Record that a stale-review channel bump was posted for a PR",
    )
    p_record_bump.add_argument("pr_number", type=int)
    p_record_bump.add_argument("--repo", required=True)


def _add_query_subparsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register read-only query, discovery, and queue subcommands."""
    p_status = subparsers.add_parser("status", help="Show current monitor state")
    p_status.add_argument("--repo")
    p_status.add_argument("--all", dest="all_repos", action="store_true")
    p_status.add_argument("--json", dest="as_json", action="store_true")

    p_list = subparsers.add_parser("list-repos", help="List repos with state files")
    p_list.add_argument("--json", dest="as_json", action="store_true")

    p_check = subparsers.add_parser("check", help="Run one monitoring cycle for a PR")
    p_check.add_argument("pr_number", type=int)
    p_check.add_argument("--repo", required=True)

    subparsers.add_parser(
        "consume-pending",
        help="Scan /tmp/review-monitor/pending/ and register any PRs found",
    )
    subparsers.add_parser(
        "catchup",
        help=(
            "Mark every existing author-role attention PR as already notified"
            " (no pings fired)"
        ),
    )
    subparsers.add_parser(
        "pending-channel-bumps",
        help="Across all repos, list author PRs needing a stale-review channel bump",
    )

    p_discover = subparsers.add_parser(
        "discover",
        help="Auto-register open author PRs from the past N days for a repo",
    )
    p_discover.add_argument("--repo", required=True)
    p_discover.add_argument("--repo-path", required=True)
    p_discover.add_argument("--days", type=int, default=7)

    p_recover = subparsers.add_parser(
        "recover-reviews",
        help=(
            "Auto-register open PRs reviewed by you in the past N days"
            " that were never monitored"
        ),
    )
    p_recover.add_argument("--repo", required=True)
    p_recover.add_argument("--repo-path", required=True)
    p_recover.add_argument("--days", type=int, default=7)

    p_enqueue = subparsers.add_parser(
        "enqueue-action",
        help="Write one outbound action to the Desktop action queue",
    )
    p_enqueue.add_argument(
        "--type",
        required=True,
        dest="action_type",
        choices=sorted(DESKTOP_ACTION_TYPES),
    )
    p_enqueue.add_argument("--repo")
    p_enqueue.add_argument("--pr", type=int, dest="pr_number")
    p_enqueue.add_argument(
        "--payload", required=True, help="JSON object with the action's message data"
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        description="Review monitor: track PR review threads",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_lifecycle_subparsers(subparsers)
    _add_signal_subparsers(subparsers)
    _add_query_subparsers(subparsers)
    return parser


def _dispatch_status_all(as_json: bool) -> None:
    """Print merged status across all repos."""
    combined = cmd_status_all()
    if as_json:
        print(json.dumps(combined.to_dict(), indent=2))
        return
    if not combined.monitored:
        print("No PRs currently monitored across any repo.")
    else:
        print(f"{'PR':<40} {'ROLE':<10} {'STATUS':<12} {'THREADS ADDRESSED'}")
        print("-" * 80)
        for key, pr in sorted(combined.monitored.items()):
            total = len(pr.thread_status)
            addressed = sum(1 for ts in pr.thread_status.values() if ts.is_addressed)
            threads_col = f"{addressed}/{total}" if total else "n/a"
            print(f"{key:<40} {pr.role:<10} {pr.status:<12} {threads_col}")
    print(f"\n{len(combined.completed)} completed PR(s) in history.")


def _dispatch_pr_state_mutation(args: argparse.Namespace) -> None:
    """Dispatch the single-PR state mutations
    (set-status/confirm-thread/mark-*/cursor)."""
    if args.command == "set-status":
        cmd_set_status(pr_number=args.pr_number, repo=args.repo, status=args.status)
    elif args.command == "confirm-thread":
        cmd_confirm_thread(
            pr_number=args.pr_number, repo=args.repo, thread_id=args.thread_id
        )
    elif args.command == "mark-notified":
        cmd_mark_notified(
            pr_number=args.pr_number, repo=args.repo, state_value=args.state_value
        )
    elif args.command == "mark-escalated":
        cmd_mark_escalated(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "update-slack-cursor":
        cmd_update_slack_cursor(
            pr_number=args.pr_number, repo=args.repo, last_seen_ts=args.last_seen_ts
        )


def _dispatch_discovery(args: argparse.Namespace) -> None:
    """Dispatch the repo-wide PR discovery commands (discover / recover-reviews)."""
    if args.command == "discover":
        result = cmd_discover(repo=args.repo, days=args.days, repo_path=args.repo_path)
    else:
        result = cmd_recover_reviews(
            repo=args.repo, days=args.days, repo_path=args.repo_path
        )
    print(json.dumps(result, indent=2))


def _emit_mutation_result(label: str, run: Callable[[], dict[str, Any]]) -> None:
    """Run a state-mutating command and print its one-line JSON result.

    A state read/write failure (``OSError``) or malformed JSON in an argument
    (``json.JSONDecodeError``) becomes an ``Error:`` line on stderr and exit 1 —
    never a silent success or a traceback — so the caller learns from the
    command itself whether the mutation landed.
    """
    try:
        result = run()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: {label} failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result))


def _dispatch_mutation(args: argparse.Namespace) -> None:
    """Dispatch register/drop/complete/nudge-ok/record-nudge/set-status/
    confirm-thread commands."""
    label = f"{args.command} {args.repo}#{args.pr_number}"
    if args.command == "register":
        _emit_mutation_result(
            label,
            lambda: cmd_register(
                pr_number=args.pr_number,
                role=args.role,
                repo=args.repo,
                repo_path=args.repo_path,
                sha=args.sha,
                review_id=args.review_id,
                threads=args.threads,
                thread_details=(
                    json.loads(args.thread_details) if args.thread_details else None
                ),
                slack_channel=args.slack_channel,
                slack_ts=args.slack_ts,
            ),
        )
    elif args.command == "drop":
        _emit_mutation_result(
            label, lambda: cmd_drop(pr_number=args.pr_number, repo=args.repo)
        )
    elif args.command == "complete":
        _emit_mutation_result(
            label,
            lambda: cmd_complete(
                pr_number=args.pr_number, repo=args.repo, reason=args.reason
            ),
        )
    elif args.command == "nudge-ok":
        print(
            json.dumps(cmd_nudge_ok(pr_number=args.pr_number, repo=args.repo), indent=2)
        )
    elif args.command == "record-nudge":
        cmd_record_nudge(pr_number=args.pr_number, repo=args.repo)
    elif args.command == "enqueue-action":
        result = cmd_enqueue_action(
            action=args.action_type,
            payload=json.loads(args.payload),
            repo=args.repo,
            pr_number=args.pr_number,
        )
        print(json.dumps(result, indent=2))
    else:
        _dispatch_pr_state_mutation(args)


_MUTATION_COMMANDS: frozenset[str] = frozenset(
    {
        "register",
        "drop",
        "complete",
        "nudge-ok",
        "record-nudge",
        "enqueue-action",
        "set-status",
        "confirm-thread",
        "mark-notified",
        "mark-escalated",
        "update-slack-cursor",
    }
)

# Query commands: each maps to a callable returning a JSON-serializable result
# that main() prints with indent=2. list-repos and status branch on flags and
# are handled separately.
_QUERY_COMMANDS: dict[str, Callable[[argparse.Namespace], Any]] = {
    "check": lambda a: cmd_check(pr_number=a.pr_number, repo=a.repo),
    "slack-thread-cursor": lambda a: cmd_slack_thread_cursor(
        pr_number=a.pr_number, repo=a.repo
    ),
    "consume-pending": lambda _: cmd_consume_pending(),
    "record-auto-fix": lambda a: cmd_record_auto_fix(
        pr_number=a.pr_number, repo=a.repo
    ),
    "record-channel-bump": lambda a: cmd_record_channel_bump(
        pr_number=a.pr_number, repo=a.repo
    ),
    "pending-channel-bumps": lambda _: cmd_pending_channel_bumps(),
    "mark-comment-review": lambda a: cmd_mark_comment_review(
        pr_number=a.pr_number,
        repo=a.repo,
        review_id=a.review_id,
        classification=a.classification,
    ),
    "catchup": lambda _: cmd_catchup(),
    "ack-delta": lambda a: cmd_ack_delta(pr_number=a.pr_number, repo=a.repo, sha=a.sha),
}


def _print_repo_list(*, as_json: bool) -> None:
    """Print the monitored-repo list as JSON or newline-separated names."""
    repos = cmd_list_repos()
    if as_json:
        print(json.dumps(repos))
    else:
        for r in repos:
            print(r)


def _dispatch_status_command(args: argparse.Namespace) -> None:
    """Run the ``status`` command for one repo or all repos.

    When neither ``--repo`` nor ``--all`` is supplied, auto-derive ``--repo``
    from ``gh repo view`` against the current working directory so callers
    inside a checkout don't have to pass it explicitly.
    """
    if args.all_repos:
        _dispatch_status_all(as_json=args.as_json)
        return
    repo = args.repo or _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if not repo:
        print(
            "Error: --repo is required when --all is not set "
            "(auto-derive via `gh repo view` failed — run inside a checkout "
            "or pass --repo owner/name)",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd_status(repo=repo, as_json=args.as_json)


def main() -> None:
    """CLI entry point."""
    parser = _build_argument_parser()
    args = parser.parse_args()
    command: str | None = args.command

    if command in _MUTATION_COMMANDS:
        _dispatch_mutation(args)
    elif command in ("discover", "recover-reviews"):
        _dispatch_discovery(args)
    elif command == "list-repos":
        _print_repo_list(as_json=args.as_json)
    elif command == "status":
        _dispatch_status_command(args)
    else:
        handler = _QUERY_COMMANDS.get(command or "")
        if handler is not None:
            print(json.dumps(handler(args), indent=2))
