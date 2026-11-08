"""``cw review check-approval`` — finalize Step 4.0's verdict command (#2773).

Step 4.0 refuses to ship a large-tier ticket whose review approval is missing
or stale. The decision used to be an ~80-line bash fence; it is now this
command, a thin adapter that turns the worker's ``cw-context.json`` and ``HEAD``
into the inputs of the one shared predicate
(:func:`cw.review_approval.evaluate_review_approval`), which the dispatch claim
gate and the repair recipes also call. The fence keeps only the exit-status
classification of a missing or outdated ``cw``.

The adapter reproduces the old fence's behaviour: the guard root is the git
toplevel of ``--worktree`` (or that directory when it is not in a repository),
overridden by the context's own ``worktree_path``; a context older than schema
14 is ``channel_absent``; the default branch is the context's
``world_state_snapshot.origin_main_branch`` and must be a valid ref name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, run_git
from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.cli._base import handle_errors
from cw.cli.review._group import review
from cw.cli.review.converge import read_context
from cw.review_approval import (
    APPROVAL_CHANNEL_ABSENT,
    APPROVAL_UNREADABLE,
    ApprovalInputs,
    ApprovalVerdict,
    evaluate_review_approval,
    render_verdict_line,
    resolve_commit,
)

# The first cw-context schema that carries ``queue_metadata.review_approval``.
_MIN_APPROVAL_SCHEMA = 14
_CONTEXT_RELATIVE_PATH = Path(".claude") / "cw-context.json"

_TIER_HELP = (
    "The resolved scope.tier, verbatim. Only exactly 'small' or 'large' is "
    "accepted; any other value prints an unresolved_tier park."
)
_WORKTREE_HELP = (
    "Directory to run git in; its repository root is the guard root, or the "
    "directory itself when it is not in a repository. Default: the current "
    "directory."
)
_CONTEXT_HELP = (
    "Path to cw-context.json. Default: <guard root>/.claude/cw-context.json."
)


def _guard_root(worktree: Path) -> Path:
    """The git toplevel of *worktree*, or *worktree* itself when it has none."""
    try:
        result = run_git(
            ["rev-parse", "--show-toplevel"],
            cwd=worktree,
            capture_output=True,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return worktree
    top = result.stdout.strip()
    return Path(top) if result.returncode == 0 and top else worktree


def _string_field(container: object, *keys: str) -> str | None:
    """The string at ``container[k0][k1]...``, None for any other shape."""
    value: object = container
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) else None


def _valid_branch(root: Path, name: str | None) -> str | None:
    """*name* when it is a valid branch ref name, else None."""
    if not name:
        return None
    try:
        result = run_git(
            ["check-ref-format", f"refs/heads/{name}"],
            cwd=root,
            capture_output=True,
            timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return name if result.returncode == 0 else None


def _schema_ok(context: dict[str, object]) -> bool:
    schema = context.get("schema_version")
    return (
        isinstance(schema, int)
        and not isinstance(schema, bool)
        and schema >= _MIN_APPROVAL_SCHEMA
    )


def _large_verdict(worktree: Path, context_path: Path | None) -> ApprovalVerdict:
    """The verdict for a ``large`` tier, from the context at the guard root."""
    root = _guard_root(worktree)
    ctx_path = context_path or root / _CONTEXT_RELATIVE_PATH
    context = read_context(ctx_path)
    override = _string_field(context, "worktree_path")
    if override:
        root = Path(override)
        if context_path is None:
            ctx_path = root / _CONTEXT_RELATIVE_PATH
            context = read_context(ctx_path)
    if context is None:
        return ApprovalVerdict(
            APPROVAL_UNREADABLE,
            f"{ctx_path} is missing, unreadable or not valid JSON",
        )
    if not _schema_ok(context):
        return ApprovalVerdict(
            APPROVAL_CHANNEL_ABSENT,
            "cw-context schema_version is below 14 or malformed",
        )
    approval = ("queue_metadata", "review_approval")
    inputs = ApprovalInputs(
        tier=SCOPE_TIER_LARGE,
        approver_kind=_string_field(context, *approval, "approver_kind"),
        approved_sha=_string_field(context, *approval, "reviewed_sha"),
        default_branch=_valid_branch(
            root, _string_field(context, "world_state_snapshot", "origin_main_branch")
        ),
    )
    return evaluate_review_approval(
        inputs, repo=root, head_sha=resolve_commit(root, "HEAD").sha
    )


@review.command(name="check-approval")
@click.option("--tier", required=True, help=_TIER_HELP)
@click.option(
    "--worktree",
    type=click.Path(path_type=Path),
    default=".",
    show_default=False,
    help=_WORKTREE_HELP,
)
@click.option(
    "--context", "context_path", type=click.Path(path_type=Path), help=_CONTEXT_HELP
)
@handle_errors
def check_approval(tier: str, worktree: Path, context_path: Path | None) -> None:
    """Print the Step 4.0 large-tier review approval verdict (#2773).

    Evaluates the review approval recorded in the worker's cw-context against
    HEAD with the same predicate the dispatch claim gate applies, and prints
    exactly one line: REVIEW_APPROVAL_NOT_REQUIRED, REVIEW_APPROVAL_RECORDED,
    or REVIEW_APPROVAL_PARK: <token> — <detail> (token: missing, stale,
    channel_absent, unreadable or unresolved_tier). Exits 0 for every verdict,
    parks included; a nonzero exit means cw itself failed, which finalize Step
    4.0 treats as a park. Never writes.
    """
    if tier == SCOPE_TIER_LARGE:
        verdict = _large_verdict(worktree.resolve(), context_path)
    else:
        # Not large: no context is read, so only the tier decides the verdict.
        verdict = evaluate_review_approval(
            ApprovalInputs(tier, None, None, None), repo=worktree, head_sha=None
        )
    click.echo(render_verdict_line(verdict))
