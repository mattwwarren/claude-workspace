"""The cw session review-debt filing runs in, and its default artifact (#3113).

A cw session is the git toplevel (the cwd when git yields none) carrying a
``.claude/cw-context.json`` whose ``ticket_id`` is a non-empty string. Only
inside one does ``.cw/deferred-findings.md`` have a default location, under
that root. Outside, ``cw review adjudicate`` and ``cw review debt file``/
``emit`` read and write no artifact unless handed one explicitly: a test run
from an operator's checkout once wrote fixture deferrals there, and the next
finalize filed them as real tickets.

A thin composition of :func:`cw.redact_post_text.git_toplevel` and
:func:`cw._hook_context._read_cw_context`; it owns no git or JSON logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cw._hook_context import _read_cw_context
from cw.atomic import atomic_write_text
from cw.redact_post_text import git_toplevel
from cw.review_adjudication import (
    merge_deferred_adjudications,
    render_deferred_findings_md,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.review_debt_filing import DEFERRED_FINDINGS_RELATIVE_PATH, read_artifact

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cw.review_adjudication import Adjudication

#: The artifact's path relative to the session root.
DEFAULT_ARTIFACT = DEFERRED_FINDINGS_RELATIVE_PATH
#: The cw-context key naming the session's queue ticket.
TICKET_ID_KEY = "ticket_id"
#: The stderr line for a run skipped because no session gives an artifact.
NO_SESSION_LINE = (
    "debt filing skipped: not inside a cw session, so there is no default "
    ".cw/deferred-findings.md (pass the artifact path explicitly)"
)


def _root(cwd: Path | None = None) -> Path:
    toplevel = git_toplevel(cwd)
    if toplevel is not None:
        return Path(toplevel)
    return cwd or Path.cwd()


def session_context(cwd: Path | None = None) -> dict[str, object] | None:
    """The cw-context at the git toplevel (cwd fallback), or None.

    *cwd* anchors the lookup for a caller that runs outside the session
    worktree (the detached codex review process); ``None`` keeps the process cwd.
    """
    return _read_cw_context(str(_root(cwd)))


def session_root() -> Path | None:
    """The session root when its context names a ticket, else None."""
    root = _root()
    context = _read_cw_context(str(root))
    ticket = context.get(TICKET_ID_KEY) if context is not None else None
    return root if isinstance(ticket, str) and ticket else None


def default_artifact() -> Path | None:
    """``<session root>/.cw/deferred-findings.md``; None outside a session."""
    root = session_root()
    return root / DEFAULT_ARTIFACT if root is not None else None


def resolve_artifact(explicit: Path | None) -> Path | None:
    """*explicit* when given, else the session default (None outside one)."""
    return explicit if explicit is not None else default_artifact()


def ensure_default_artifact(path: Path, applied: Sequence[Adjudication]) -> None:
    """Persist newly applied deferrals when adjudicate has no output path.

    The default artifact is the rediscovery point for a later finalize retry.
    Without it, a failed or emitted entry exists only in the in-memory
    adjudication payload and cannot be found by ``cw review debt file``.
    """
    deferred = [entry for entry in applied if entry.outcome == DEFERRED_OUTCOME]
    if not deferred and path.exists():
        return
    prior = read_artifact(path) if path.exists() else []
    rendered = render_deferred_findings_md(
        merge_deferred_adjudications(prior, deferred)
    )
    if rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, rendered)
