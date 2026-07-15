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
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from cw import gh
from cw.exceptions import RfcContractError, SprintApplyError
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
        return cls(
            milestone_title_pattern=str(
                _require_key(milestone, "milestone", "title_pattern")
            ),
            epic_title_pattern=str(_require_key(epic, "epic", "title_pattern")),
            epic_labels=_str_list(epic.get("labels")),
            children_marker=str(_require_key(epic, "epic", "children_marker")),
            ticket_title_pattern=str(_require_key(ticket, "ticket", "title_pattern")),
            ticket_labels=_str_list(ticket.get("labels")),
            ticket_footer_pattern=str(_require_key(ticket, "ticket", "footer_pattern")),
            ticket_footer_epic_clause=str(
                _require_key(ticket, "ticket", "footer_epic_clause")
            ),
            notion=_require_notion(block.get("notion")),
        )


def _require_mapping(block: dict[str, object], key: str) -> dict[str, object]:
    value = block.get(key)
    if not isinstance(value, dict):
        msg = (
            f"sprint_buildout: missing or malformed section: {key} — "
            "see config/CONFIG_REFERENCE.md"
        )
        raise RfcContractError(msg)
    return value


def _require_key(mapping: dict[str, object], section: str, key: str) -> object:
    """Return ``mapping[key]``, or raise RfcContractError — never a bare KeyError.

    A missing key inside a present ``milestone:``/``epic:``/``ticket:`` section
    (e.g. ``epic:`` without ``children_marker``) must fail the same guided way
    as a missing section: config/CONFIG_REFERENCE.md promises a hard refusal
    pointing here, not a raw traceback.
    """
    if key not in mapping:
        msg = (
            f"sprint_buildout.{section}: missing required key: {key} — "
            "see config/CONFIG_REFERENCE.md"
        )
        raise RfcContractError(msg)
    return mapping[key]


def _require_notion(notion_raw: object) -> NotionConfig | None:
    """Parse the optional ``notion:`` sub-block, or refuse if present-but-malformed.

    Absent (or explicitly ``null``) means "skip the Notion phase" — that is the
    documented enablement signal. A *present* value that isn't a mapping (e.g. a
    stray ``notion: true``) is a config typo, not an opt-out, so it must refuse
    loudly like every other malformed section rather than silently degrading to
    "absent."
    """
    if notion_raw is None:
        return None
    if not isinstance(notion_raw, dict):
        msg = (
            "sprint_buildout.notion: malformed — must be a mapping, or omit the "
            "key entirely to skip the Notion phase — see config/CONFIG_REFERENCE.md"
        )
        raise RfcContractError(msg)
    return NotionConfig.model_validate(notion_raw)


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


@runtime_checkable
class GhSurface(Protocol):
    """Testability seam for the `gh`-issue-creation calls `apply_plan` makes.

    A structural subset of ``cw.gh``'s real functions — each method omits the
    `timeout` kwarg (a legitimate narrower match; ``apply_plan`` never needs
    to override it). ``@runtime_checkable`` matches every other Protocol in
    this codebase (``StageExecutor``, ``NativeDaemonClient``, ``CodexRunner``,
    ``AiderRunner``/``PlanFetcher``).
    """

    def find_milestone(self, title: str) -> tuple[int | None, bool]:
        """Return (number, ok) for an existing milestone titled *title*."""
        ...

    def create_milestone(self, title: str) -> int | None:
        """Create a milestone; return its number, or None on failure."""
        ...

    def milestone_issue_titles(
        self, milestone: int
    ) -> tuple[dict[str, int] | None, bool]:
        """Return ({issue title: number}, ok) for every issue on *milestone*."""
        ...

    def create_issue(
        self, title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None:
        """Create an issue and attach it to *milestone*; return its number."""
        ...

    def update_issue_body(self, number: int, body: str) -> bool:
        """Replace an issue's body via ``gh issue edit``. True on success."""
        ...


class GhClient:
    """Production GhSurface: thin delegations to :mod:`cw.gh`."""

    def find_milestone(self, title: str) -> tuple[int | None, bool]:
        return gh.find_milestone(title)

    def create_milestone(self, title: str) -> int | None:
        return gh.create_milestone(title)

    def milestone_issue_titles(
        self, milestone: int
    ) -> tuple[dict[str, int] | None, bool]:
        return gh.milestone_issue_titles(milestone)

    def create_issue(
        self, title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None:
        return gh.create_issue(title, body, labels=labels, milestone=milestone)

    def update_issue_body(self, number: int, body: str) -> bool:
        return gh.update_issue_body(number, body)


class AppliedBuildout(BaseModel):
    """What `apply_plan` has created, reused, or is about to.

    Carried by ``SprintApplyError.applied`` on a mid-apply failure so the
    operator can see exactly how far the buildout got before it stopped, and
    re-run ``cw sprint apply`` to resume (creation is idempotent by title)
    rather than starting over.
    """

    milestone_number: int
    epic_numbers: dict[str, int] = Field(default_factory=dict)
    ticket_numbers: dict[str, int] = Field(default_factory=dict)
    created: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


def _resolve_milestone(plan: BuildoutPlan, client: GhSurface) -> int:
    """Resolve or create *plan*'s milestone; return its number.

    ``ok=False`` from ``find_milestone`` is a transient lookup failure, never
    a "does not exist" signal (see ``cw.gh.find_milestone``'s own docstring)
    — reading it as "absent" would create a duplicate milestone on a re-run,
    so it raises instead. A ``None`` number with ``ok=True`` is a genuine
    miss, so the milestone is created; a ``None`` return from that create is
    likewise a hard failure.
    """
    number, ok = client.find_milestone(plan.milestone_title)
    if not ok:
        msg = f"failed to look up milestone: {plan.milestone_title}"
        raise SprintApplyError(msg)
    if number is not None:
        return number
    created = client.create_milestone(plan.milestone_title)
    if created is None:
        msg = f"failed to create milestone: {plan.milestone_title}"
        raise SprintApplyError(msg)
    return created


def _create_or_skip(
    draft: IssueDraft,
    milestone: int,
    existing: dict[str, int],
    client: GhSurface,
    applied: AppliedBuildout,
) -> int:
    """Create *draft* under *milestone*, or reuse an issue with the same
    title that already exists (idempotent re-entry). Raises
    ``SprintApplyError``, carrying *applied*'s partial state, if creation
    fails."""
    if draft.title in existing:
        applied.skipped.append(draft.title)
        return existing[draft.title]
    created = client.create_issue(
        draft.title, draft.body, labels=draft.labels, milestone=milestone
    )
    if created is None:
        msg = f"failed to create issue: {draft.title}"
        raise SprintApplyError(msg, applied=applied)
    existing[draft.title] = created
    applied.created.append(draft.title)
    return created


def _resolve_epic_refs(ticket: IssueDraft, epic_numbers: dict[str, int]) -> IssueDraft:
    """Return *ticket* with its ``Epic #<key>`` footer clause (if any)
    rewritten to the real GitHub issue number.

    Boundary-safe by construction (R1): a plain ``.replace("Epic #I", ...)``
    would also match the "I" prefix of "Epic #II", corrupting Epic II's
    ticket footers whenever Epic I happens to be resolved first. The
    ``(?!\\w)`` negative lookahead requires the key not be followed by
    another word character, so "Epic #I" only ever matches a standalone "I",
    never the "II" it is a substring of.
    """
    body = ticket.body
    for key, number in epic_numbers.items():
        body = re.sub(rf"Epic #{re.escape(key)}(?!\w)", f"Epic #{number}", body)
    return ticket.model_copy(update={"body": body})


def _backfill_children(
    plan: BuildoutPlan, applied: AppliedBuildout, client: GhSurface
) -> None:
    """Rewrite every epic's children checklist from *plan*'s pristine data.

    # Why: this pass is idempotent by recomputation, not skip-checking — it
    # always rebuilds the checklist from `plan.epic_children` and
    # unconditionally overwrites the epic's body, rather than reading the
    # issue's current body back from GitHub and checking whether the
    # checklist already looks right. Recomputing from the RFC's pristine
    # ticket list is what makes re-running this pass safe: there is no
    # previously-written Markdown checklist to parse back out and diff
    # against, so "run it again" always produces the same, correct result.
    """
    for epic in plan.epics:
        number = applied.epic_numbers[epic.code]
        children = plan.epic_children.get(epic.code, [])
        checklist = (
            "\n".join(f"- [ ] {code}" for code in children)
            if children
            else "- (no children)"
        )
        body = epic.body.replace(
            plan.children_marker, f"{plan.children_marker}\n{checklist}"
        )
        client.update_issue_body(number, body)


def apply_plan(
    plan: BuildoutPlan, *, client: GhSurface | None = None
) -> AppliedBuildout:
    """Idempotently apply *plan* to GitHub in 4 passes: milestone -> epics ->
    tickets -> children-checklist backfill.

    Safe to re-run: an issue whose title already exists under the milestone
    is skipped, not recreated. Raises ``SprintApplyError``, carrying whatever
    ``AppliedBuildout`` state was accumulated before the failure, the moment
    any `gh` call reports it could not complete (``ok=False`` or a ``None``
    return) — never guesses at what a failed lookup means.
    """
    gh_client: GhSurface = client if client is not None else GhClient()

    milestone_number = _resolve_milestone(plan, gh_client)

    existing, ok = gh_client.milestone_issue_titles(milestone_number)
    if not ok or existing is None:
        msg = f"failed to list issues under milestone {milestone_number}"
        raise SprintApplyError(msg)

    applied = AppliedBuildout(milestone_number=milestone_number)

    for epic in plan.epics:
        number = _create_or_skip(epic, milestone_number, existing, gh_client, applied)
        applied.epic_numbers[epic.code] = number

    for ticket in plan.tickets:
        resolved = _resolve_epic_refs(ticket, applied.epic_numbers)
        number = _create_or_skip(
            resolved, milestone_number, existing, gh_client, applied
        )
        applied.ticket_numbers[ticket.code] = number

    _backfill_children(plan, applied, gh_client)

    return applied
