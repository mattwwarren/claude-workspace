"""The ``cw post-comment`` command (#2810).

The one deterministic redact-and-post seam for agent-authored tracker
comments. A stage session writes its raw findings text to a file with the
Write tool and hands it over; this command reads it, refuses when it cannot
post safely, redacts it, wraps and splits it, posts every part and reads each
back (:mod:`cw.tracker_post`). It prints exactly one JSON object on stdout:

- ``status`` (``posted | emitted | refused | partial``) and ``reason`` (a
  :class:`~cw.tracker_post.PostReason` value, ``null`` for posted/emitted);
- ``ticket``, ``repo_visibility``, ``redacted``, ``parts_total``, ``posted``
  (``[{"index", "url"}]``) and ``out_files`` (``--emit-only`` paths, else
  ``null``).

Exit 0 posted or emitted, 1 refused (nothing public), 3 partial (a comment may
be public). A usage error (including an unknown or missing client, or a
non-numeric TICKET on a GitHub tracker) exits 2 with no JSON; any other
failure exits non-zero with no JSON and posts nothing. The command records no
event: its only outputs are the JSON and stderr reason lines. The call contract
is single-sourced in ``.claude/commands/auto-dev.md`` ("Tracker post seam
(#2810)").
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import click

from cw._git import run_git
from cw._hook_context import _read_cw_context
from cw.cli._base import handle_errors, main
from cw.config import get_client
from cw.dev_queue.lifecycle import _tracker_allows_github_fetch
from cw.exceptions import CwError
from cw.gh import FETCH_COMMENTS_TIMEOUT, agent_marked_body, fetch_repo_visibility
from cw.redact_post_text import resolve_home
from cw.tracker_post import PartPlan, PostReason, PostResult, PostStatus
from cw.tracker_post import plan_parts as _plan_parts
from cw.tracker_post import post_parts as _post_parts
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig

__all__ = ["EMITTED_STATUS", "exit_code_for", "post_comment"]

#: The CLI-only status for ``--emit-only``; the library statuses are PostStatus.
EMITTED_STATUS = "emitted"
_EXIT_OK = 0
_EXIT_REFUSED = 1
_EXIT_PARTIAL = 3
_EXIT_CODES = {
    PostStatus.POSTED.value: _EXIT_OK,
    EMITTED_STATUS: _EXIT_OK,
    PostStatus.REFUSED.value: _EXIT_REFUSED,
    PostStatus.PARTIAL.value: _EXIT_PARTIAL,
}
_PARTS_SPLIT = "split"
_PARTS_SINGLE = "single"
_ISSUE_NUMBER = re.compile(r"#?\d+")
_GIT_TIMEOUT_SECONDS = 10
_HELP = (
    "Redact paths and secret shapes from an agent-authored body, wrap and split "
    "it, post it to a GitHub issue and read every part back. Prints one JSON "
    "result on stdout: exit 0 posted or emitted, 1 refused (nothing public), 3 "
    "partial (a comment may be public; review and delete it)."
)


@dataclass(frozen=True)
class _Options:
    """The wrapping and output options, passed through as one value."""

    header: str | None
    subheading: str | None
    fence: bool
    single: bool
    out_dir: Path | None
    worktrees: tuple[str, ...]
    context: dict[str, object] | None


@dataclass(frozen=True)
class _Outcome:
    """Everything the result JSON reports except the ticket."""

    status: str
    reason: PostReason | None = None
    repo_visibility: str | None = None
    redacted: bool = False
    parts_total: int = 0
    posted: list[dict[str, object]] = field(default_factory=list)
    out_files: list[str] | None = None


def exit_code_for(status: str) -> int:
    """The process exit code for a result ``status`` (0, 1 or 3)."""
    return _EXIT_CODES[status]


def _resolve_client(
    client_name: str | None, context: dict[str, object] | None
) -> ClientConfig:
    """``-c``, else ``client`` from cw-context.json; a usage error otherwise."""
    name = client_name
    if name is None and context is not None:
        ctx_client = context.get("client")
        name = ctx_client if isinstance(ctx_client, str) and ctx_client else None
    if name is None:
        msg = "no client: pass -c/--client or run from a cw worktree"
        raise click.UsageError(msg)
    try:
        return get_client(name)
    except CwError as exc:
        raise click.UsageError(str(exc)) from exc


def _require_issue_number(ticket: str, cfg: ClientConfig, *, emit_only: bool) -> None:
    """On the GitHub path TICKET must be an issue number, else a usage error.

    ``gh issue comment`` accepts an issue URL (any repo) and parses a leading
    ``-`` as a flag, either of which would bypass the visibility check that
    ran against the client's own repo.
    """
    if emit_only or not _tracker_allows_github_fetch(cfg):
        return
    if _ISSUE_NUMBER.fullmatch(ticket) is None:
        msg = f"TICKET must be a GitHub issue number (got {ticket!r})"
        raise click.UsageError(msg)


def _git_toplevel() -> str | None:
    """The cwd's git toplevel, or None outside a repo or on any git failure."""
    try:
        result = run_git(
            ["rev-parse", "--show-toplevel"],
            cwd=Path.cwd(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    toplevel = result.stdout.strip()
    return toplevel if result.returncode == 0 and toplevel else None


def _anchors(
    cfg: ClientConfig,
    toplevel: str | None,
    context: dict[str, object] | None,
    extra: tuple[str, ...],
) -> list[str]:
    """Every path redacted to ``<worktree>``: toplevel, context, client, flags."""
    ctx_worktree = context.get("worktree_path") if context is not None else None
    candidates: list[object] = [
        toplevel,
        ctx_worktree,
        cfg.workspace_path,
        cfg.repo_path,
        *extra,
    ]
    return [str(c) for c in candidates if isinstance(c, (str, Path)) and str(c)]


def _read_body(path: Path) -> str | None:
    """The body file as UTF-8 text (CRLF kept), or None when it cannot be read."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _consume(path: Path) -> None:
    """Delete the body file; a failure is reported and does not stop the post."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        click.echo(f"cw post-comment: could not delete {path}: {exc}", err=True)


def _write_parts(out_dir: Path, plan: PartPlan) -> list[str]:
    """Write each marked part to ``out_dir/part-<i>.md``; return absolute paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index, body in enumerate(plan.bodies, start=1):
        path = (out_dir / f"part-{index}.md").resolve()
        path.write_text(agent_marked_body(body), encoding="utf-8", newline="")
        paths.append(str(path))
    return paths


def _posted_outcome(result: PostResult, visibility: str, redacted: bool) -> _Outcome:
    return _Outcome(
        result.status.value,
        result.reason,
        repo_visibility=visibility,
        redacted=redacted,
        parts_total=result.parts_total,
        posted=[{"index": p.index, "url": p.url} for p in result.posted],
    )


def _plan(raw: str, cfg: ClientConfig, opts: _Options) -> PartPlan:
    """Redact *raw* against every anchor, then wrap and split it."""
    return _plan_parts(
        raw,
        home=resolve_home(os.environ),
        worktrees=_anchors(cfg, _git_toplevel(), opts.context, opts.worktrees),
        header=opts.header,
        subheading=opts.subheading,
        fence=opts.fence,
        single=opts.single,
    )


def _too_long(plan: PartPlan, visibility: str | None) -> _Outcome:
    return _Outcome(
        PostStatus.REFUSED.value,
        PostReason.SINGLE_PART_TOO_LONG,
        repo_visibility=visibility,
        redacted=plan.redacted,
        parts_total=len(plan.bodies),
    )


def _emit(raw: str, cfg: ClientConfig, opts: _Options, out_dir: Path) -> _Outcome:
    """``--emit-only``: no tracker or visibility call; write the part files."""
    plan = _plan(raw, cfg, opts)
    if not plan.fits:
        return _too_long(plan, None)
    return _Outcome(
        EMITTED_STATUS,
        redacted=plan.redacted,
        parts_total=len(plan.bodies),
        out_files=_write_parts(out_dir, plan),
    )


def _post(ticket: str, raw: str, cfg: ClientConfig, opts: _Options) -> _Outcome:
    """The GitHub path: tracker check, visibility, redact and plan, post."""
    if not _tracker_allows_github_fetch(cfg):
        return _Outcome(PostStatus.REFUSED.value, PostReason.TRACKER_NOT_GITHUB)
    visibility = fetch_repo_visibility(
        timeout=FETCH_COMMENTS_TIMEOUT, cwd=_git_dir(cfg)
    )
    if visibility is None:
        return _Outcome(PostStatus.REFUSED.value, PostReason.VISIBILITY_UNDETERMINED)
    plan = _plan(raw, cfg, opts)
    if not plan.fits:
        return _too_long(plan, visibility)
    result = _post_parts(ticket, plan.bodies, cwd=_git_dir(cfg))
    return _posted_outcome(result, visibility, plan.redacted)


def _run(
    ticket: str, body_file: Path, cfg: ClientConfig, opts: _Options, *, consume: bool
) -> _Outcome:
    """Read the body, refuse an empty one, then emit or post it.

    Check order: body read, empty body, then (posting only) tracker and
    visibility, then redaction and planning, then the post.
    """
    raw = _read_body(body_file)
    if raw is None:
        return _Outcome(PostStatus.REFUSED.value, PostReason.BODY_UNREADABLE)
    if consume:
        _consume(body_file)
    if not raw.strip():
        return _Outcome(PostStatus.REFUSED.value, PostReason.EMPTY_BODY)
    if opts.out_dir is not None:
        return _emit(raw, cfg, opts, opts.out_dir)
    return _post(ticket, raw, cfg, opts)


def _emit_result(ticket: str, outcome: _Outcome) -> None:
    """Print the eight-key result object; reasons also go to stderr."""
    reason = outcome.reason.value if outcome.reason is not None else None
    click.echo(
        json.dumps(
            {
                "status": outcome.status,
                "reason": reason,
                "ticket": ticket,
                "repo_visibility": outcome.repo_visibility,
                "redacted": outcome.redacted,
                "parts_total": outcome.parts_total,
                "posted": outcome.posted,
                "out_files": outcome.out_files,
            }
        )
    )
    if reason is not None:
        click.echo(f"cw post-comment: {outcome.status}: {reason}", err=True)


@main.command("post-comment", help=_HELP)
@click.argument("ticket")
@click.option(
    "--body-file",
    "body_file",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="The raw, Write-tool-authored body (no header, fence or marker).",
)
@click.option(
    "--client",
    "-c",
    "client_name",
    default=None,
    help="Client whose repo and tracker TICKET lives in (default: cw-context.json).",
)
@click.option(
    "--header", default=None, help="Header line, e.g. '## Blocking Review Findings'."
)
@click.option("--subheading", default=None, help="Sub-heading line after the header.")
@click.option(
    "--fence/--no-fence", default=False, help="Fence the body text in backticks."
)
@click.option(
    "--parts",
    type=click.Choice([_PARTS_SPLIT, _PARTS_SINGLE]),
    default=_PARTS_SPLIT,
    show_default=True,
    help=(
        "split: labelled parts of at most 60,000 characters; single: refuse if longer."
    ),
)
@click.option(
    "--worktree",
    "worktrees",
    multiple=True,
    help="Extra path to redact to <worktree> (repeatable).",
)
@click.option(
    "--consume",
    is_flag=True,
    help="Delete the body file once it has been read, on every path.",
)
@click.option(
    "--emit-only",
    is_flag=True,
    help=(
        "Write the redacted, wrapped, marker-bearing parts to --out-dir and post "
        "nothing (for Linear)."
    ),
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Directory for --emit-only part files.",
)
@handle_errors
def post_comment(
    ticket: str,
    body_file: Path,
    client_name: str | None,
    header: str | None,
    subheading: str | None,
    fence: bool,
    parts: str,
    worktrees: tuple[str, ...],
    consume: bool,
    emit_only: bool,
    out_dir: Path | None,
) -> None:
    """Post (or, with --emit-only, write) a redacted agent-authored comment."""
    if emit_only != (out_dir is not None):
        msg = "--emit-only and --out-dir must be given together"
        raise click.UsageError(msg)
    context = _read_cw_context(str(Path.cwd()))
    cfg = _resolve_client(client_name, context)
    _require_issue_number(ticket, cfg, emit_only=emit_only)
    opts = _Options(
        header=header,
        subheading=subheading,
        fence=fence,
        single=parts == _PARTS_SINGLE,
        out_dir=out_dir,
        worktrees=worktrees,
        context=context,
    )
    outcome = _run(ticket, body_file, cfg, opts, consume=consume)
    _emit_result(ticket, outcome)
    code = exit_code_for(outcome.status)
    if code != _EXIT_OK:
        raise SystemExit(code)
