"""RFC → GitHub sprint block: parse, plan, apply.

The RFC is a strict input contract (``docs/rfcs/TEMPLATE.md``). Its ``## Tickets``
section is the sole parse source — buildout transcribes, it never infers. The
``## Phasing`` table is a human-facing summary and is deliberately NOT parsed: its
cells pack code, name, and role together with epic membership implied by column
position, which cannot be read without guessing.
"""

from __future__ import annotations

import re
import subprocess as _sp
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from cw.exceptions import RfcContractError
from cw.tracker import load_project_config_dict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_RFC_TITLE_RE = re.compile(
    r"^#\s+RFC\s+(?P<number>\d{4})\s+—\s+(?P<title>.+?)\s*$", re.MULTILINE
)
_EPIC_RE = re.compile(
    r"^###\s+Epic\s+(?P<key>[IVX]+)\s+—\s+(?P<name>.+?)\s*$", re.MULTILINE
)
_TICKET_RE = re.compile(
    r"^###\s+(?P<code>[A-Z]+\d+)\s+—\s+(?P<name>.+?)\s*$", re.MULTILINE
)
_DECISION_RE = re.compile(r"^-\s+\*\*(?P<id>D-[A-Za-z0-9]+)\s+—", re.MULTILINE)
_REFERENCE_RE = re.compile(r"^-\s+`(?P<ref>[^`]+)`", re.MULTILINE)
_FIELD_RE = re.compile(
    r"^-\s+\*\*(?P<key>[A-Za-z ]+):\*\*\s*(?P<value>.*)$", re.MULTILINE
)
_BULLET_RE = re.compile(r"^\s+-\s+(?P<item>.+?)\s*$", re.MULTILINE)

_SEC_DESIGN = "## Design"
_SEC_TICKETS = "## Tickets"
_SEC_RESOLVED_DECISIONS = "## Resolved decisions"
_SEC_REFERENCES = "## References"
_REQUIRED_SECTIONS = (
    _SEC_DESIGN,
    _SEC_TICKETS,
    _SEC_RESOLVED_DECISIONS,
    _SEC_REFERENCES,
)
_REQUIRED_TICKET_FIELDS = (
    "Epic",
    "Wave",
    "Sprint",
    "Depends on",
    "Context",
    "Scope",
    "Acceptance",
)
_NONE_VALUES = frozenset({"none", "-", "—", ""})
_LOAD_RFC_TIMEOUT = 15


class EpicSpec(BaseModel):
    """One epic declared under ``## Design`` as ``### Epic <key> — <name>``."""

    key: str
    name: str
    intent: str


class TicketSpec(BaseModel):
    """One ticket transcribed verbatim from a ``## Tickets`` subsection."""

    code: str
    name: str
    epic: str | None
    wave: int
    sprint: int
    depends_on: list[str] = Field(default_factory=list)
    context: str
    scope: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)


class RfcDoc(BaseModel):
    """A parsed, contract-valid RFC."""

    number: str
    title: str
    epics: list[EpicSpec] = Field(default_factory=list)
    tickets: list[TicketSpec] = Field(default_factory=list)
    decisions: dict[str, str] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)


def load_rfc_text(rfc_path: str, root: Path) -> str:
    """Return the RFC's text, preferring origin/main over the working tree.

    Why origin/main first: an RFC commonly merges to main *after* the worktree
    doing the buildout was created, so the file is simply absent from disk here.
    That exact trap cost the RFC 0011 session its first several minutes. Falls
    back to reading the working-tree file whenever ``git show`` cannot produce
    the origin/main copy — a non-zero exit (the path isn't on origin/main yet,
    e.g. a brand-new, unmerged RFC) or a raised OSError/TimeoutExpired (git
    binary missing, or hung past the timeout).
    """
    try:
        result = _sp.run(
            ["git", "show", f"origin/main:{rfc_path}"],
            capture_output=True,
            cwd=root,
            timeout=_LOAD_RFC_TIMEOUT,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        return result.stdout.decode("utf-8")
    return (root / rfc_path).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of a ``## Heading`` section, or raise RfcContractError.

    Matches the heading by *prefix*, tolerating trailing annotation text on the
    same line (e.g. ``## Resolved decisions (hardening pass, operator,
    2026-07-12)`` — this repo's RFC house style annotates level-2 headings this
    way; RFC 0011 line 326 is a real example). This tolerance is cosmetic only:
    it forgives what a heading is called, not what a ticket contains.
    """
    pattern = re.compile(
        rf"^{re.escape(heading)}\b[^\n]*$(?P<body>.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        msg = f"missing section: {heading}"
        raise RfcContractError(msg)
    return match.group("body")


def _iter_matches_with_end(
    pattern: re.Pattern[str], text: str
) -> Iterator[tuple[re.Match[str], int]]:
    """Yield each match of *pattern* in *text* paired with where its slice ends.

    A match's body runs until the next match's start, or the end of *text* for
    the last match — the boundary every ``###``/decision block parser in this
    module needs to isolate one block from the next.
    """
    matches = list(pattern.finditer(text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield match, end


def _split_ticket_blocks(tickets_section: str) -> list[tuple[str, str, str]]:
    """Return [(code, name, block_body)] in document order."""
    return [
        (match.group("code"), match.group("name"), tickets_section[match.end() : end])
        for match, end in _iter_matches_with_end(_TICKET_RE, tickets_section)
    ]


def _ticket_fields(code: str, block: str) -> dict[str, str]:
    """Return the ticket's field map, raising on any missing required field."""
    fields = {
        m.group("key").strip(): m.group("value").strip()
        for m in _FIELD_RE.finditer(block)
    }
    for required in _REQUIRED_TICKET_FIELDS:
        if required not in fields:
            msg = f"ticket {code}: missing field: {required}"
            raise RfcContractError(msg)
    return fields


def _csv_list(value: str) -> list[str]:
    """Split a comma-separated field value, treating 'none'/'-'/'—' as empty."""
    if value.strip().lower() in _NONE_VALUES:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _acceptance_bullets(code: str, block: str) -> list[str]:
    """Return the indented bullets under ``- **Acceptance:**``."""
    _, _, tail = block.partition("- **Acceptance:**")
    bullets = [m.group("item") for m in _BULLET_RE.finditer(tail)]
    if not bullets:
        msg = f"ticket {code}: missing field: Acceptance"
        raise RfcContractError(msg)
    return bullets


def _parse_int_field(code: str, key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        msg = f"ticket {code}: {key} must be an integer, got: {value!r}"
        raise RfcContractError(msg) from exc


def _check_ticket_block_lines(code: str, block: str) -> None:
    """Raise if *block* has a non-blank line that is neither a field nor a bullet.

    ``_FIELD_RE`` captures only the first physical line of a field's value — a
    hard-wrapped ``Context:`` continuation onto a second line would otherwise be
    silently dropped, which is worse than a refusal. Every non-blank line in a
    ticket block must therefore be recognizable as a field line or a bullet line.
    """
    for line in block.splitlines():
        if not line.strip():
            continue
        if _FIELD_RE.match(line) or _BULLET_RE.match(line):
            continue
        msg = f"ticket {code}: unparseable line (fields must be single-line): {line}"
        raise RfcContractError(msg)


def _parse_ticket(code: str, name: str, block: str) -> TicketSpec:
    _check_ticket_block_lines(code, block)
    fields = _ticket_fields(code, block)
    epic_raw = fields["Epic"].strip()
    return TicketSpec(
        code=code,
        name=name,
        epic=None if epic_raw.lower() in _NONE_VALUES else epic_raw,
        wave=_parse_int_field(code, "Wave", fields["Wave"]),
        sprint=_parse_int_field(code, "Sprint", fields["Sprint"]),
        depends_on=_csv_list(fields["Depends on"]),
        context=fields["Context"],
        scope=_csv_list(fields["Scope"]),
        acceptance=_acceptance_bullets(code, block),
    )


def _validate_cross_references(doc: RfcDoc) -> None:
    """Every Scope/Depends-on/Epic citation must resolve within this RFC."""
    codes = {t.code for t in doc.tickets}
    epic_keys = {e.key for e in doc.epics}
    for ticket in doc.tickets:
        for decision in ticket.scope:
            if decision not in doc.decisions:
                msg = (
                    f"ticket {ticket.code}: Scope cites undefined decision: {decision}"
                )
                raise RfcContractError(msg)
        for dependency in ticket.depends_on:
            if dependency not in codes:
                msg = f"ticket {ticket.code}: Depends on unknown ticket: {dependency}"
                raise RfcContractError(msg)
        if ticket.epic is not None and ticket.epic not in epic_keys:
            msg = f"ticket {ticket.code}: unknown epic: {ticket.epic}"
            raise RfcContractError(msg)


def _parse_epics(text: str) -> list[EpicSpec]:
    """Parse ``### Epic`` subsections. *text* must already be scoped to the
    ``## Design`` section body (via ``_section(text, "## Design")``) — every
    sibling parser in this module is scoped the same way, so a stray
    ``### Epic``-shaped line elsewhere in the document is never picked up."""
    return [
        EpicSpec(
            key=match.group("key"),
            name=match.group("name"),
            intent=text[match.end() : end].strip(),
        )
        for match, end in _iter_matches_with_end(_EPIC_RE, text)
    ]


def _parse_decisions(section: str) -> dict[str, str]:
    return {
        match.group("id"): section[match.start() : end].strip().lstrip("- ")
        for match, end in _iter_matches_with_end(_DECISION_RE, section)
    }


def parse_rfc(text: str) -> RfcDoc:
    """Parse an RFC into a contract-valid RfcDoc, or raise RfcContractError.

    Every refusal names the exact defect ("missing section: ## Tickets", "ticket
    A1: Scope cites undefined decision: D-NOPE") so the operator fixes the RFC
    instead of guessing at what buildout wanted.
    """
    title_match = _RFC_TITLE_RE.search(text)
    if title_match is None:
        msg = "missing section: # RFC NNNN — <title>"
        raise RfcContractError(msg)

    sections = {heading: _section(text, heading) for heading in _REQUIRED_SECTIONS}

    doc = RfcDoc(
        number=title_match.group("number"),
        title=title_match.group("title"),
        epics=_parse_epics(sections[_SEC_DESIGN]),
        tickets=[
            _parse_ticket(code, name, block)
            for code, name, block in _split_ticket_blocks(sections[_SEC_TICKETS])
        ],
        decisions=_parse_decisions(sections[_SEC_RESOLVED_DECISIONS]),
        references=[
            m.group("ref") for m in _REFERENCE_RE.finditer(sections[_SEC_REFERENCES])
        ],
    )
    _validate_cross_references(doc)
    return doc


_EPIC_FOOTER_NOTE = (
    "Children are listed below. This checklist is maintained by "
    "`cw sprint apply`; the marker comment is its insertion point."
)


class NotionConfig(BaseModel):
    """Optional Notion mirror settings. Absent ⇒ the skill skips the Notion phase."""

    data_source: str
    project_page: str
    sprint_page_properties: dict[str, str] = Field(default_factory=dict)


class BuildoutConfig(BaseModel):
    """The ``sprint_buildout:`` block of .claude/project-config.yaml."""

    milestone_title_pattern: str
    epic_title_pattern: str
    epic_labels: list[str] = Field(default_factory=list)
    children_marker: str
    ticket_title_pattern: str
    ticket_labels: list[str] = Field(default_factory=list)
    ticket_footer_pattern: str
    ticket_footer_epic_clause: str
    notion: NotionConfig | None = None

    @classmethod
    def from_block(cls, block: dict[str, object]) -> BuildoutConfig:
        """Build from the nested YAML shape (milestone:/epic:/ticket:/notion:).

        The model is deliberately flat while the YAML is nested — the YAML groups
        keys for the human reader; the code wants them addressable. Not named
        ``model_validate_*``: that namespace is Pydantic's, and shadowing it invites
        a caller to reach for ``model_validate`` (which would reject this shape).
        """
        milestone = _require_mapping(block, "milestone")
        epic = _require_mapping(block, "epic")
        ticket = _require_mapping(block, "ticket")
        notion_raw = block.get("notion")
        return cls(
            milestone_title_pattern=str(milestone["title_pattern"]),
            epic_title_pattern=str(epic["title_pattern"]),
            epic_labels=_str_list(epic.get("labels")),
            children_marker=str(epic["children_marker"]),
            ticket_title_pattern=str(ticket["title_pattern"]),
            ticket_labels=_str_list(ticket.get("labels")),
            ticket_footer_pattern=str(ticket["footer_pattern"]),
            ticket_footer_epic_clause=str(ticket["footer_epic_clause"]),
            notion=NotionConfig.model_validate(notion_raw)
            if isinstance(notion_raw, dict)
            else None,
        )


def _require_mapping(block: dict[str, object], key: str) -> dict[str, object]:
    value = block.get(key)
    if not isinstance(value, dict):
        msg = f"sprint_buildout: missing or malformed section: {key}"
        raise RfcContractError(msg)
    return value


def _str_list(value: object) -> list[str]:
    """Coerce an optional/absent YAML list field to ``list[str]``.

    ``labels:`` is declared as a YAML list but read back through
    ``dict[str, object]`` (no schema-typed intermediate), so mypy sees
    ``object`` at this seam — narrow explicitly rather than trusting the
    YAML shape. Absent or falsy (``None``, empty list) both degrade to ``[]``.
    """
    if not isinstance(value, list):
        return []
    return [str(x) for x in value]


def load_buildout_config(root: Path) -> BuildoutConfig:
    """Read the sprint_buildout block, or raise with a pointer to the reference doc."""
    raw = load_project_config_dict(root)
    block = raw.get("sprint_buildout") if raw is not None else None
    if not isinstance(block, dict):
        msg = (
            "missing sprint_buildout block in .claude/project-config.yaml — "
            "see config/CONFIG_REFERENCE.md"
        )
        raise RfcContractError(msg)
    return BuildoutConfig.from_block(block)


class IssueDraft(BaseModel):
    """One issue to create. ``code`` is the RFC ticket code, or the epic key."""

    kind: str  # "epic" | "ticket"
    code: str
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)


class BuildoutPlan(BaseModel):
    """Everything `cw sprint apply` needs. Written to disk; reviewed by a human.

    Self-contained on purpose — including ``children_marker``, which is a config
    value. The plan is written, reviewed by the operator, then applied, possibly by
    a later invocation; apply must not depend on config having stayed unchanged in
    between. The artifact carries everything it needs.
    """

    rfc_number: str
    rfc_title: str
    milestone_title: str
    children_marker: str
    epics: list[IssueDraft] = Field(default_factory=list)
    tickets: list[IssueDraft] = Field(default_factory=list)
    sprint_map: dict[int, list[str]] = Field(default_factory=dict)
    epic_children: dict[str, list[str]] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)


def _epic_body(epic: EpicSpec, marker: str) -> str:
    return (
        f"## Intent\n\n{epic.intent}\n\n"
        f"## Workstreams\n\n{_EPIC_FOOTER_NOTE}\n\n{marker}\n"
    )


def _ticket_body(ticket: TicketSpec, doc: RfcDoc, cfg: BuildoutConfig) -> str:
    scope = "\n".join(f"- {doc.decisions[d]}" for d in ticket.scope) or "- (none cited)"
    deps = "\n".join(f"- {d}" for d in ticket.depends_on) or "- none"
    acceptance = "\n".join(f"- [ ] {a}" for a in ticket.acceptance)
    footer = cfg.ticket_footer_pattern.format(
        rfc_num=doc.number,
        wave=ticket.wave,
        sprint=ticket.sprint,
    )
    # Epic-less tickets (RFC 0011's S1/S2) omit the clause entirely — no
    # `Epic #—` placeholder ships. A ticket's footer either names its real
    # epic or says nothing about epics at all.
    if ticket.epic is not None:
        footer += cfg.ticket_footer_epic_clause.format(epic=ticket.epic)
    return (
        f"## Context\n\n{ticket.context}\n\n"
        f"## Scope\n\n{scope}\n\n"
        f"## Dependencies\n\n{deps}\n\n"
        f"## Acceptance\n\n{acceptance}\n\n"
        f"---\n\n{footer}\n"
    )


def build_plan(doc: RfcDoc, cfg: BuildoutConfig, version: str) -> BuildoutPlan:
    """Render every issue body from the RFC. Pure — no network, no side effects."""
    epics = [
        IssueDraft(
            kind="epic",
            code=epic.key,
            title=cfg.epic_title_pattern.format(name=epic.name),
            body=_epic_body(epic, cfg.children_marker),
            labels=list(cfg.epic_labels),
        )
        for epic in doc.epics
    ]
    tickets = [
        IssueDraft(
            kind="ticket",
            code=ticket.code,
            title=cfg.ticket_title_pattern.format(
                rfc_num=doc.number, code=ticket.code, name=ticket.name
            ),
            body=_ticket_body(ticket, doc, cfg),
            labels=list(cfg.ticket_labels),
        )
        for ticket in doc.tickets
    ]

    sprint_map: dict[int, list[str]] = {}
    epic_children: dict[str, list[str]] = {}
    for ticket in doc.tickets:
        sprint_map.setdefault(ticket.sprint, []).append(ticket.code)
        if ticket.epic is not None:
            epic_children.setdefault(ticket.epic, []).append(ticket.code)

    return BuildoutPlan(
        rfc_number=doc.number,
        rfc_title=doc.title,
        milestone_title=cfg.milestone_title_pattern.format(
            version=version, rfc_title=doc.title
        ),
        children_marker=cfg.children_marker,
        epics=epics,
        tickets=tickets,
        sprint_map=sprint_map,
        epic_children=epic_children,
        references=list(doc.references),
    )
