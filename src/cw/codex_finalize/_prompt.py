"""The codex FINALIZE prompt and its injected sections (#3062, RFC 0018 C3).

The contract is A3's renderer output
(``render_stage_prompt("finalize", CODEX_BACKEND, ...)``), whose codex
addendum (``_FINALIZE_CODEX_ADDENDUM``) is appended by the renderer itself and
states which steps cw owns. cw then appends the sections it injects, each
between fixed BEGIN / END delimiter lines (the ``_section`` helper PLAN
shares, with IMPL's fence escape): the prefetched ticket, the approved plan,
cw's branch facts, the operator override JSON (only when overridden) and the
deferred review findings.

An absent, unreadable or whitespace-only artifact omits its whole section (no
heading, no placeholder): ``.cw/deferred-findings.md`` exists on a codex lane
only when the review recorded SHOULD_FIX deferrals, and the finalize doc says
to omit the section when it is absent or empty.

The command docs come from cw's own copy via
:func:`cw.codex_plan._prompt.command_docs_root` called with
:data:`FINALIZE_DOCS`. ``ship-it.md`` is never packaged; the renderer
appends its ``_SHIP_IT_FALLBACK`` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from cw.codex_finalize._result import FinalizeTurnDocument
from cw.codex_impl._prompt import _escape_injected_fences
from cw.codex_plan._prompt import TICKET_SECTION, _section
from cw.contract_manifest import render_stage_prompt
from cw.contract_manifest._render import _STAGE_DOCS
from cw.models import CODEX_BACKEND
from cw.openai_strict_schema import to_openai_strict_schema

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_plan._prompt import TicketText

#: The docs the finalize render reads; each must be present in a docs root.
FINALIZE_DOCS: Final[tuple[str, ...]] = _STAGE_DOCS["finalize"]

#: The strict ``-o`` schema the FINALIZE turn answers with.
FINALIZE_TURN_SCHEMA: Final[dict[str, Any]] = to_openai_strict_schema(
    FinalizeTurnDocument.model_json_schema()
)

PLAN_SECTION: Final = "Approved plan"
BRANCH_SECTION: Final = "Branch facts"
OVERRIDE_SECTION: Final = "Operator override (JSON)"
DEFERRED_SECTION: Final = "Deferred review findings"

PLAN_RELATIVE: Final = ".cw/plan.md"
DEFERRED_RELATIVE: Final = ".cw/deferred-findings.md"


def read_artifact(worktree: Path, relative: str) -> str | None:
    """The artifact's text, or ``None`` when absent, unreadable or blank."""
    try:
        text = (worktree / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def render_finalize_prompt(
    *,
    ticket_id: str,
    docs_root: Path,
    worktree: Path,
    ticket: TicketText | None,
    branch_facts: str,
    override_json: str | None,
) -> str:
    """The FINALIZE turn's stdin prompt.

    Raises :class:`cw.contract_manifest.RenderError` when the contract cannot
    be rendered from *docs_root*. ``session_id`` is ``None``: cw, not the
    model, records the result. *override_json* is set only when the must-fix
    override check reported ``overridden``.
    """
    contract = render_stage_prompt(
        "finalize",
        CODEX_BACKEND,
        docs_root=docs_root,
        ticket_id=ticket_id,
        session_id=None,
    )
    sections = [
        (TICKET_SECTION, ticket.text if ticket is not None else None),
        (PLAN_SECTION, read_artifact(worktree, PLAN_RELATIVE)),
        (BRANCH_SECTION, branch_facts if branch_facts.strip() else None),
        (OVERRIDE_SECTION, override_json),
        (DEFERRED_SECTION, read_artifact(worktree, DEFERRED_RELATIVE)),
    ]
    parts = [contract]
    parts.extend(
        "\n" + _section(name, _escape_injected_fences(body))
        for name, body in sections
        if body is not None
    )
    return "".join(parts)
