"""The codex PLAN prompt, its command-doc source, and the ticket prefetch (#3060).

The contract is A3's renderer output
(``render_stage_prompt("plan", CODEX_BACKEND, ...)``), whose codex addendum is
appended by the renderer itself. cw then appends the sections it injects (the
prefetched ticket on GitHub and the resumed draft), each between fixed BEGIN /
END delimiter lines. The denylist check applies to the rendered contract only:
ticket text is data the operator wrote, not cw machinery.

The command docs come from cw's own copy (:func:`command_docs_root`): the
``cw/data/commands`` package data an installed wheel ships (a hatch
``force-include`` of ``.claude/commands/auto-dev-plan.md``), else the cw
source checkout's ``.claude/commands``. Never the client worktree (whose copy,
if any, is not the one the manifest ranges were checked against) and never
``~/.claude/commands``.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import cw
from cw.codex_plan._result import PlanTurnDocument
from cw.contract_manifest import render_stage_prompt
from cw.contract_manifest._render import _STAGE_DOCS
from cw.gh import FETCH_COMMENTS_TIMEOUT, fetch_issue_body, fetch_issue_comments
from cw.models import CODEX_BACKEND
from cw.openai_strict_schema import to_openai_strict_schema

if TYPE_CHECKING:
    from cw.codex_plan._result import ResumeDraft

#: The docs the plan render reads; each must be present in a docs root.
PLAN_DOCS: Final[tuple[str, ...]] = _STAGE_DOCS["plan"]

#: The cap on the injected ticket text (body plus comments), in characters.
#: Oldest comments are dropped first; a body alone over the cap is truncated.
TICKET_TEXT_MAX_CHARS: Final = 60_000

#: The strict ``-o`` schema the PLAN turn answers with.
PLAN_TURN_SCHEMA: Final[dict[str, Any]] = to_openai_strict_schema(
    PlanTurnDocument.model_json_schema()
)

TICKET_SECTION: Final = "Ticket"
RESUME_SECTION: Final = "Resumed plan draft"
APPROVAL_MATCHES: Final = "approval: matches"
APPROVAL_NONE: Final = "approval: none"

_PACKAGED_SUBDIR: Final = ("data", "commands")


@dataclass(frozen=True)
class TicketText:
    """The prefetched ticket, already rendered and capped."""

    text: str
    comments_total: int
    comments_dropped: int


def _has_plan_docs(root: Path) -> bool:
    return all((root / name).is_file() for name in PLAN_DOCS)


def _packaged_commands_dir() -> Path | None:
    """The wheel's ``cw/data/commands`` when it holds the plan docs."""
    root = Path(str(importlib.resources.files("cw").joinpath(*_PACKAGED_SUBDIR)))
    return root if _has_plan_docs(root) else None


def _checkout_commands_dir() -> Path | None:
    """The cw source checkout's ``.claude/commands`` when it holds the plan docs."""
    package_dir = Path(cw.__file__).resolve().parent
    root = package_dir.parent.parent
    if package_dir != root / "src" / "cw" or not (root / "pyproject.toml").is_file():
        return None
    commands = root / ".claude" / "commands"
    return commands if _has_plan_docs(commands) else None


def command_docs_root() -> Path | None:
    """cw's own command-doc tree: packaged first, then the source checkout."""
    return _packaged_commands_dir() or _checkout_commands_dir()


def _section(name: str, body: str) -> str:
    return (
        f"=== BEGIN CW-INJECTED: {name} ===\n"
        f"{body.rstrip()}\n"
        f"=== END CW-INJECTED: {name} ===\n"
    )


def _comment_text(comment: dict[str, Any]) -> str:
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else author
    created = comment.get("createdAt") or comment.get("created_at") or "unknown"
    body = comment.get("body")
    text = body if isinstance(body, str) else ""
    return f"### Comment by {login or 'unknown'} at {created}\n\n{text.rstrip()}\n"


def render_ticket_text(
    body: str, comments: list[dict[str, Any]], *, cap: int = TICKET_TEXT_MAX_CHARS
) -> TicketText:
    """Render *body* and *comments* (oldest first), capped at *cap* characters.

    Comments are kept verbatim, agent-authored ones included (their
    ``cw-agent-authored`` marker is what the provenance rule reads). Oldest
    comments are dropped first until the text fits; a body that alone exceeds
    the cap is truncated with a note.
    """
    head = f"## Ticket body\n\n{body.rstrip()}\n"
    rendered = [_comment_text(c) for c in comments]
    dropped = 0
    while rendered and len(head) + sum(len(r) + 1 for r in rendered) > cap:
        rendered.pop(0)
        dropped += 1
    if len(head) > cap:
        head = head[:cap] + "\n[ticket body truncated by cw]\n"
    note = f"[{dropped} oldest comment(s) omitted by cw]\n" if dropped else ""
    comments_part = "## Comments (oldest first)\n\n" + note + "\n".join(rendered)
    return TicketText(
        text=f"{head}\n{comments_part}",
        comments_total=len(comments),
        comments_dropped=dropped,
    )


def prefetch_ticket(ticket_id: str, *, cwd: Path) -> TicketText | None:
    """Fetch the GitHub ticket body and comments via ``gh``; ``None`` on failure."""
    body = fetch_issue_body(ticket_id, FETCH_COMMENTS_TIMEOUT, cwd=cwd)
    if body is None:
        return None
    comments = fetch_issue_comments(ticket_id, FETCH_COMMENTS_TIMEOUT, cwd=cwd)
    if comments is None:
        return None
    return render_ticket_text(body, comments)


def render_plan_prompt(
    *,
    ticket_id: str,
    docs_root: Path,
    ticket: TicketText | None,
    resume: ResumeDraft | None,
    approval_matches: bool,
) -> str:
    """The PLAN turn's stdin prompt.

    Raises :class:`cw.contract_manifest.RenderError` when the contract cannot
    be rendered from *docs_root*. ``session_id`` is ``None``: cw, not the
    model, records the result.
    """
    contract = render_stage_prompt(
        "plan",
        CODEX_BACKEND,
        docs_root=docs_root,
        ticket_id=ticket_id,
        session_id=None,
    )
    parts = [contract]
    if ticket is not None:
        parts.append("\n" + _section(TICKET_SECTION, ticket.text))
    if resume is not None:
        approval = APPROVAL_MATCHES if approval_matches else APPROVAL_NONE
        parts.append("\n" + _section(RESUME_SECTION, f"{approval}\n\n{resume.body}"))
    return "".join(parts)
