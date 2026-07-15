# sprint-buildout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an RFC into a fully-ticketed sprint block (milestone + epics + children + Notion pages + RFC footer PR) via `cw sprint plan|apply` behind a single operator confirmation, replacing a ~1-hour hand-driven pipeline that re-derives its conventions from prior issues every time.

**Architecture:** Three layers split on one rule — *the code owns anything with a shell-quoting hazard or an ordering constraint; the skill owns anything requiring a judgment call.* `docs/rfcs/TEMPLATE.md` is a strict input contract (a `## Tickets` section is the sole parse source, so buildout is transcription with zero inference). `src/cw/sprint.py` parses + builds a plan (pure, unit-testable) and applies it (the four-pass `gh` dance GitHub's number assignment forces). `.claude/skills/sprint-buildout/SKILL.md` drives the pipeline, runs the adjacent-bug scan, and owns the one operator gate.

**Tech Stack:** Python 3.12+, Click, Pydantic v2, `gh` CLI (via the existing `cw.gh` subprocess wrapper), PyYAML (via the existing `cw.tracker` loader), pytest + `CliRunner`.

**Spec:** `docs/superpowers/specs/2026-07-13-sprint-buildout-design.md`

## Global Constraints

- **Quality gates (all must pass before every commit; mirrors `.github/workflows/ci.yml`):**
  `uv run ruff check src/ tests/` · `uv run ruff format --check src/ tests/` · `uv run mypy --strict src/` · `uv run pre-commit run --all-files` · `uv run --extra mcp pytest tests/ -m 'not integration' --cov=cw --cov-report=xml --cov-fail-under=88` · `uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90`
- **Zero suppressions.** No `# noqa`, no `# type: ignore` without explicit operator approval.
- **Complexity ceilings (ruff `PLR`):** ≤12 branches, ≤50 statements, ≤6 returns per function. When tripped, **extract a helper** — never suppress.
- **Module ceiling ~1000 lines.** `src/cw/sprint.py` is expected to land ~400–600; if it exceeds ~800, split it into a `sprint/` package (one submodule per concern) as `cw.cli` and `cw.reconcile` do.
- **Every function fully annotated,** including `-> None`. `from __future__ import annotations` at the top of every new module.
- **Error messages are extracted to variables before raising** (ruff `EM101`).
- **New code reuses existing infrastructure, never parallel machinery:** `cw.tracker.load_project_config_dict` for `.claude/project-config.yaml`; `cw.gh` for every `gh` subprocess call; `cw.exceptions.CwError` as the base for new exceptions; `cw.cli._base.handle_errors` at the CLI boundary.
- **`gh` helpers are policy-free** — they return `CompletedProcess | None` (or a parsed value | `None`) and never log or raise. Callers decide.
- **All issue/milestone bodies are passed via `--body-file`, never `--body` and never a heredoc.** RFC titles contain em-dashes and ampersands; this sidesteps shell quoting entirely.

---

## File Structure

- **Create** `docs/rfcs/TEMPLATE.md` — the RFC input contract.
- **Create** `src/cw/sprint.py` — RFC parse + validation, plan build, plan apply. One responsibility: *RFC → GitHub sprint block*.
- **Create** `src/cw/cli/sprint.py` — the `cw sprint` Click group (`plan`, `apply`). Thin; delegates to `cw.sprint`.
- **Modify** `src/cw/gh.py` — add issue/milestone **creation** helpers (it has `post_issue_comment` but no create surface).
- **Modify** `src/cw/cli/__init__.py` — import `sprint` for its registration side effect.
- **Modify** `.claude/project-config.yaml` — add the `sprint_buildout:` block.
- **Modify** `config/CONFIG_REFERENCE.md` — document that block.
- **Create** `tests/test_sprint.py` — parse/validate/build/apply. The main test surface.
- **Create** `tests/fixtures/rfc-0011-tickets.md` — RFC 0011 with a `## Tickets` section back-filled to describe what that session actually filed. The end-to-end acceptance fixture.
- **Modify** `tests/test_gh.py` — cover the new creation helpers.
- **Modify** `tests/test_cli.py` — `CliRunner` coverage of `cw sprint plan|apply`.
- **Create** `.claude/skills/sprint-buildout/SKILL.md` — the judgment wrapper. Ships to `~/.claude/skills/` automatically via `scripts/install-skills.sh` (manifest-scoped sync of every dir under `.claude/skills/`); no install plumbing needed.

---

### Task 1: RFC contract — template, models, parser

**Files:**
- Create: `docs/rfcs/TEMPLATE.md`
- Create: `src/cw/sprint.py`
- Create: `tests/test_sprint.py`
- Modify: `src/cw/exceptions.py` (append one exception class)

**Interfaces:**
- Consumes: `cw.exceptions.CwError` (existing base).
- Produces:
  - `cw.exceptions.RfcContractError(CwError)`
  - `cw.sprint.TicketSpec`, `EpicSpec`, `RfcDoc` (Pydantic models)
  - `cw.sprint.parse_rfc(text: str) -> RfcDoc` — raises `RfcContractError`
  - `cw.sprint.load_rfc_text(rfc_path: str, root: Path) -> str`

- [ ] **Step 1: Write `docs/rfcs/TEMPLATE.md`**

The `## Tickets` section is the sole parse source. `## Phasing` is a human-facing
summary and is **not** parsed — that is deliberate: RFC 0011's phasing table packs
code, name, and role into one cell (`A1 (park class, keystone) · A2 (detector)`)
with epic membership implied by column position, which cannot be read without
inference. `## Design` is also required and parsed — epics are transcribed from
its `### Epic` subsections, scoped to that section so a stray `### Epic`-shaped
line elsewhere in the document can't be picked up by accident.

Required headings (`## Tickets`, `## Design`, `## Resolved decisions`,
`## References`) are matched by **prefix**, tolerating trailing annotation text
on the same line — this repo's RFC house style annotates level-2 headings (e.g.
`## Resolved decisions (hardening pass, operator, 2026-07-12)`, RFC 0011 line
326), and a byte-exact match would reject RFC 0011 itself. This tolerance is
cosmetic only: it forgives what a heading is *called*, not what a ticket
*contains* — it does not extend to inferring ticket content.

````markdown
# RFC NNNN — <Title>

## Summary

<Two or three paragraphs. What changes and why.>

## Motivation

<The problem. Evidence, not assertion.>

## Design

<REQUIRED. Epics are parsed from the `### Epic` subsections below.>

### Epic I — <Epic name>

<Intent prose. This becomes the epic issue's Intent section.>

### Epic II — <Epic name>

<Intent prose.>

## Phasing

<Human-facing summary table. NOT parsed — `## Tickets` is the parse source.
Keep it readable for humans; it may pack multiple tickets per cell.>

| Wave | Track A | Track B |
|------|---------|---------|
| 0 (seams, blocking) | S1 — <name> | S2 — <name> |
| 1 | A1 · A2 | B1 · B2 |

## Resolved decisions

<Firm leans, decided to unblock sprint breakout. Each is reversible at
ticket-hardening if the code contradicts it. Each `D-*` id here is citable from a
ticket's `Scope:` field.>

- **D-S1 — <short name>.** <Decision text.>
- **D-A1 — <short name>.** <Decision text.>

## Tickets

<REQUIRED and PARSED. One `###` subsection per ticket. Every field is required
except `Depends on:`, which may be `none`. `Epic:` may be `none` for shared seams
that belong to no epic. Buildout transcribes these verbatim — it infers nothing.
Every field value (`Context:`, `Scope:`, etc.) must fit on a single line — do
not hard-wrap it across two physical lines. A wrapped continuation line is not
silently dropped: it is a contract violation and buildout refuses the RFC.>

### S1 — <ticket name>

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** <One or more sentences. Becomes the ticket's Context section.>
- **Scope:** D-S1, D-S2b
- **Acceptance:**
  - <Checkable statement.>
  - <Checkable statement.>

### A1 — <ticket name>

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** <...>
- **Scope:** D-A1
- **Acceptance:**
  - <...>

## References

- `path/to/file.py:123` — <what lives here and why it matters>

## Issues

Issues: _(filled by `/sprint-buildout`)_
````

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_sprint.py
"""Tests for cw.sprint — RFC parse/validate, plan build, plan apply."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cw import sprint
from cw.exceptions import RfcContractError
from cw.sprint import load_rfc_text, parse_rfc

MINIMAL_RFC = """\
# RFC 0011 — Availability- & Counterparty-Aware Holding

## Summary

Body.

## Design

### Epic I — Availability-aware holding (inward)

Hold work when the environment cannot carry it.

## Phasing

| Wave | Track A |
|------|---------|
| 0 | S1 |

## Resolved decisions

- **D-S1 — Counterparty derivation.** Derive at pr_hydrate, no stored field.
- **D-A1 — Park-class shape.** New blocker.reason value.

## Tickets

### S1 — counterparty axis + self-identity

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** The shared seam both epics build on.
- **Scope:** D-S1
- **Acceptance:**
  - Counterparty resolves to self when no PR exists.

### A1 — park class (keystone)

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** The keystone park class.
- **Scope:** D-A1
- **Acceptance:**
  - A held task routes to BLOCKED_ON_USER.
  - No new QueueItemStatus is introduced.

## References

- `src/cw/pr_hydrate.py:257` — where counterparty derivation lands

## Issues

Issues: _(filled by `/sprint-buildout`)_
"""


def test_parse_rfc_extracts_number_and_title() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert doc.number == "0011"
    assert doc.title == "Availability- & Counterparty-Aware Holding"


def test_parse_rfc_extracts_epics() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert [e.key for e in doc.epics] == ["I"]
    assert doc.epics[0].name == "Availability-aware holding (inward)"
    assert "Hold work when the environment" in doc.epics[0].intent


def test_parse_rfc_extracts_tickets_in_document_order() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert [t.code for t in doc.tickets] == ["S1", "A1"]

    s1, a1 = doc.tickets
    assert s1.epic is None
    assert s1.wave == 0
    assert s1.sprint == 0
    assert s1.depends_on == []
    assert s1.scope == ["D-S1"]
    assert s1.acceptance == ["Counterparty resolves to self when no PR exists."]

    assert a1.name == "park class (keystone)"
    assert a1.epic == "I"
    assert a1.depends_on == ["S1"]
    assert len(a1.acceptance) == 2


def test_parse_rfc_expands_scope_citations_from_resolved_decisions() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert doc.decisions["D-S1"].startswith("**D-S1 — Counterparty derivation.**")


def test_parse_rfc_extracts_references() -> None:
    doc = parse_rfc(MINIMAL_RFC)
    assert doc.references == ["src/cw/pr_hydrate.py:257"]


@pytest.mark.parametrize(
    "heading",
    ["## Tickets", "## Design", "## Resolved decisions", "## References"],
)
def test_parse_rfc_refuses_rfc_missing_a_required_section(heading: str) -> None:
    mangled = MINIMAL_RFC.replace(heading, "## Something Else")
    with pytest.raises(RfcContractError, match=f"missing section: {heading}"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_ticket_missing_a_required_field() -> None:
    mangled = MINIMAL_RFC.replace("- **Acceptance:**\n  - Counterparty resolves to self when no PR exists.\n", "")
    with pytest.raises(RfcContractError, match="ticket S1: missing field: Acceptance"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_scope_citing_an_undefined_decision() -> None:
    mangled = MINIMAL_RFC.replace("- **Scope:** D-A1", "- **Scope:** D-NOPE")
    with pytest.raises(RfcContractError, match="ticket A1: Scope cites undefined decision: D-NOPE"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_depends_on_an_unknown_ticket() -> None:
    mangled = MINIMAL_RFC.replace("- **Depends on:** S1", "- **Depends on:** ZZ9")
    with pytest.raises(RfcContractError, match="ticket A1: Depends on unknown ticket: ZZ9"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_ticket_naming_an_undefined_epic() -> None:
    mangled = MINIMAL_RFC.replace("- **Epic:** I\n", "- **Epic:** IX\n")
    with pytest.raises(RfcContractError, match="ticket A1: unknown epic: IX"):
        parse_rfc(mangled)


def test_parse_rfc_refuses_a_hard_wrapped_field_continuation_line() -> None:
    """A field value split across two physical lines must be a loud refusal,
    not a silent drop — ``_FIELD_RE`` only ever captures the first line."""
    mangled = MINIMAL_RFC.replace(
        "- **Context:** The shared seam both epics build on.",
        "- **Context:** The shared seam both epics\nbuild on.",
    )
    with pytest.raises(
        RfcContractError,
        match=r"ticket S1: unparseable line \(fields must be single-line\): build on\.",
    ):
        parse_rfc(mangled)


def test_load_rfc_text_prefers_origin_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, b"origin/main content", b"")

    monkeypatch.setattr(sprint._sp, "run", fake_run)
    assert load_rfc_text("docs/rfcs/0011-x.md", tmp_path) == "origin/main content"


def test_load_rfc_text_falls_back_to_the_working_tree_on_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent from origin/main (e.g. a brand-new, unmerged RFC) -> read from disk."""
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text("working tree content", encoding="utf-8")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 1, b"", b"fatal: path not in origin/main")

    monkeypatch.setattr(sprint._sp, "run", fake_run)
    assert load_rfc_text("docs/rfcs/0011-x.md", tmp_path) == "working tree content"


def test_load_rfc_text_falls_back_to_the_working_tree_on_a_git_show_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git binary missing/hung (OSError/TimeoutExpired) -> same fallback as a miss."""
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text("working tree content", encoding="utf-8")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("git not found")

    monkeypatch.setattr(sprint._sp, "run", fake_run)
    assert load_rfc_text("docs/rfcs/0011-x.md", tmp_path) == "working tree content"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_sprint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cw.sprint'`

- [ ] **Step 4: Add the exception**

Append to `src/cw/exceptions.py`, following the existing class style:

```python
class RfcContractError(CwError):
    """An RFC does not satisfy the buildout input contract.

    Raised by :func:`cw.sprint.parse_rfc` when a required section or ticket
    field is absent, or when a ticket cites a decision/ticket/epic that the RFC
    does not define. The message always names the exact defect (e.g. "missing
    section: ## Tickets") so the operator can fix the RFC rather than guess.
    """

    __slots__ = ()
```

- [ ] **Step 5: Implement models + parser**

Create `src/cw/sprint.py`. Keep `parse_rfc` itself small — the per-ticket
field parse and each validation pass are separate helpers (ruff `PLR0912`
caps branches at 12; a single monolithic parser will trip it).

```python
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
from pathlib import Path

from pydantic import BaseModel, Field

from cw.exceptions import RfcContractError

_RFC_TITLE_RE = re.compile(r"^#\s+RFC\s+(?P<number>\d{4})\s+—\s+(?P<title>.+?)\s*$", re.M)
_EPIC_RE = re.compile(r"^###\s+Epic\s+(?P<key>[IVX]+)\s+—\s+(?P<name>.+?)\s*$", re.M)
_TICKET_RE = re.compile(r"^###\s+(?P<code>[A-Z]+\d+)\s+—\s+(?P<name>.+?)\s*$", re.M)
_DECISION_RE = re.compile(r"^-\s+\*\*(?P<id>D-[A-Za-z0-9]+)\s+—", re.M)
_REFERENCE_RE = re.compile(r"^-\s+`(?P<ref>[^`]+)`", re.M)
_FIELD_RE = re.compile(r"^-\s+\*\*(?P<key>[A-Za-z ]+):\*\*\s*(?P<value>.*)$", re.M)
_BULLET_RE = re.compile(r"^\s+-\s+(?P<item>.+?)\s*$", re.M)

_REQUIRED_SECTIONS = ("## Design", "## Tickets", "## Resolved decisions", "## References")
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
        rf"^{re.escape(heading)}\b[^\n]*$(?P<body>.*?)(?=^## |\Z)", re.M | re.S
    )
    match = pattern.search(text)
    if match is None:
        msg = f"missing section: {heading}"
        raise RfcContractError(msg)
    return match.group("body")


def _split_ticket_blocks(tickets_section: str) -> list[tuple[str, str, str]]:
    """Return [(code, name, block_body)] in document order."""
    matches = list(_TICKET_RE.finditer(tickets_section))
    blocks: list[tuple[str, str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tickets_section)
        body = tickets_section[match.end() : end]
        blocks.append((match.group("code"), match.group("name"), body))
    return blocks


def _ticket_fields(code: str, block: str) -> dict[str, str]:
    """Return the ticket's field map, raising on any missing required field."""
    fields = {m.group("key").strip(): m.group("value").strip() for m in _FIELD_RE.finditer(block)}
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
                msg = f"ticket {ticket.code}: Scope cites undefined decision: {decision}"
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
    matches = list(_EPIC_RE.finditer(text))
    epics: list[EpicSpec] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        intent = text[match.end() : end]
        # Belt-and-suspenders: the caller already scopes `text` to ## Design, so
        # this never matches — kept in case _parse_epics is ever called unscoped.
        intent = re.split(r"^##\s", intent, maxsplit=1, flags=re.M)[0]
        epics.append(
            EpicSpec(
                key=match.group("key"),
                name=match.group("name"),
                intent=intent.strip(),
            )
        )
    return epics


def _parse_decisions(section: str) -> dict[str, str]:
    matches = list(_DECISION_RE.finditer(section))
    decisions: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
        decisions[match.group("id")] = section[match.start() : end].strip().lstrip("- ")
    return decisions


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

    for heading in _REQUIRED_SECTIONS:
        _section(text, heading)  # raises on absence

    tickets_section = _section(text, "## Tickets")
    doc = RfcDoc(
        number=title_match.group("number"),
        title=title_match.group("title"),
        epics=_parse_epics(_section(text, "## Design")),
        tickets=[
            _parse_ticket(code, name, block)
            for code, name, block in _split_ticket_blocks(tickets_section)
        ],
        decisions=_parse_decisions(_section(text, "## Resolved decisions")),
        references=[
            m.group("ref") for m in _REFERENCE_RE.finditer(_section(text, "## References"))
        ],
    )
    _validate_cross_references(doc)
    return doc
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_sprint.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 7: Run the gates**

Run:
```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ && uv run mypy --strict src/
```
Expected: zero violations, zero type errors. If `parse_rfc` trips `PLR0912`, extract another helper — do not suppress.

- [ ] **Step 8: Commit**

```bash
git add docs/rfcs/TEMPLATE.md src/cw/sprint.py src/cw/exceptions.py tests/test_sprint.py
git commit -m "feat(sprint): RFC buildout input contract — template, parser, validation"
```

---

### Task 2: Config block + plan builder

**Files:**
- Modify: `src/cw/sprint.py`
- Modify: `.claude/project-config.yaml`
- Modify: `config/CONFIG_REFERENCE.md`
- Modify: `tests/test_sprint.py`

**Interfaces:**
- Consumes: `cw.sprint.RfcDoc` (Task 1); `cw.tracker.load_project_config_dict(root: Path) -> dict[str, object] | None` (existing).
- Produces:
  - `cw.sprint.BuildoutConfig`, `IssueDraft`, `BuildoutPlan` (Pydantic models)
  - `cw.sprint.load_buildout_config(root: Path) -> BuildoutConfig` — raises `RfcContractError` when the block is absent
  - `cw.sprint.build_plan(doc: RfcDoc, cfg: BuildoutConfig, version: str) -> BuildoutPlan`

- [ ] **Step 1: Add the config block to `.claude/project-config.yaml`**

Append:

```yaml
# Consumed by `cw sprint plan` (/sprint-buildout). These are the conventions the
# RFC 0011 buildout recovered by reading prior issues — recorded here so buildout
# is transcription, not archaeology. Documented in config/CONFIG_REFERENCE.md.
sprint_buildout:
  milestone:
    title_pattern: "v{version} — {rfc_title}"
  epic:
    title_pattern: "epic: {name}"
    labels: []                          # epics are deliberately unlabeled
    children_marker: "<!-- children -->"
  ticket:
    title_pattern: "RFC {rfc_num} {code} — {name}"
    labels: [feature]
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})"
    footer_epic_clause: ", Epic #{epic}"  # appended only when the ticket has an epic
  notion:                               # omit this block ⇒ the Notion phase skips
    data_source: "collection://673ac7cd-797a-4c76-b9eb-fb5bc7ee050a"
    project_page: "38b59b27-0a42-81da-b234-ea951daa0216"
    sprint_page_properties:
      Type: Sprint
      Status: Planning
      Repo: claude-workspace
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_sprint.py`:

```python
from cw.sprint import BuildoutConfig, build_plan, load_buildout_config

CONFIG_YAML = """\
sprint_buildout:
  milestone:
    title_pattern: "v{version} — {rfc_title}"
  epic:
    title_pattern: "epic: {name}"
    labels: []
    children_marker: "<!-- children -->"
  ticket:
    title_pattern: "RFC {rfc_num} {code} — {name}"
    labels: [feature]
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})"
    footer_epic_clause: ", Epic #{epic}"
"""


def _write_config(root: Path, body: str) -> None:
    config_dir = root / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project-config.yaml").write_text(body, encoding="utf-8")


def test_load_buildout_config_reads_the_sprint_buildout_block(tmp_path: Path) -> None:
    _write_config(tmp_path, CONFIG_YAML)
    cfg = load_buildout_config(tmp_path)
    assert cfg.ticket_labels == ["feature"]
    assert cfg.epic_labels == []
    assert cfg.children_marker == "<!-- children -->"
    assert cfg.notion is None


def test_load_buildout_config_refuses_when_the_block_is_absent(tmp_path: Path) -> None:
    _write_config(tmp_path, "tracking:\n  primary:\n    system: github-issues\n")
    with pytest.raises(RfcContractError, match="missing sprint_buildout block"):
        load_buildout_config(tmp_path)


def _config() -> BuildoutConfig:
    """Build from the nested YAML shape — the model itself is flat, so this must
    go through from_block(), not model_validate()."""
    return BuildoutConfig.from_block(
        {
            "milestone": {"title_pattern": "v{version} — {rfc_title}"},
            "epic": {
                "title_pattern": "epic: {name}",
                "labels": [],
                "children_marker": "<!-- children -->",
            },
            "ticket": {
                "title_pattern": "RFC {rfc_num} {code} — {name}",
                "labels": ["feature"],
                "footer_pattern": "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint})",
                "footer_epic_clause": ", Epic #{epic}",
            },
        }
    )


def test_build_plan_renders_the_milestone_title() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert plan.milestone_title == "v1.20.0 — Availability- & Counterparty-Aware Holding"


def test_build_plan_renders_epic_titles_and_embeds_the_children_marker() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert [e.title for e in plan.epics] == ["epic: Availability-aware holding (inward)"]
    # The marker is templated in from the start so the checklist backfill is a
    # marker replacement, not a fragile string-surgery pass.
    assert "<!-- children -->" in plan.epics[0].body


def test_build_plan_renders_ticket_titles_and_bodies() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    titles = [t.title for t in plan.tickets]
    assert titles == [
        "RFC 0011 S1 — counterparty axis + self-identity",
        "RFC 0011 A1 — park class (keystone)",
    ]

    a1 = plan.tickets[1]
    assert a1.labels == ["feature"]
    assert "## Context" in a1.body
    assert "The keystone park class." in a1.body
    # Scope cites D-A1, so the decision's full text is transcribed into the body.
    assert "Park-class shape" in a1.body
    assert "## Acceptance" in a1.body
    assert "- [ ] A held task routes to BLOCKED_ON_USER." in a1.body
    assert "## Dependencies" in a1.body
    assert "S1" in a1.body


def test_build_plan_omits_the_epic_clause_for_an_epic_less_ticket() -> None:
    """S1 has no epic. Its footer must not carry an `Epic #—` placeholder —
    the clause is omitted entirely, not rendered with an em-dash default."""
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    s1, a1 = plan.tickets

    assert "Epic #" not in s1.body
    assert "Part of RFC 0011 Wave 0 (Sprint 0)" in s1.body
    assert "Epic #I" in a1.body


def test_build_plan_maps_sprints_to_ticket_codes() -> None:
    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    assert plan.sprint_map == {0: ["S1"], 1: ["A1"]}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_sprint.py -v -k "config or build_plan"`
Expected: FAIL — `ImportError: cannot import name 'BuildoutConfig' from 'cw.sprint'`

- [ ] **Step 4: Implement config + builder**

Append to `src/cw/sprint.py`:

```python
from cw.tracker import load_project_config_dict

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
            epic_labels=[str(x) for x in epic.get("labels", []) or []],
            children_marker=str(epic["children_marker"]),
            ticket_title_pattern=str(ticket["title_pattern"]),
            ticket_labels=[str(x) for x in ticket.get("labels", []) or []],
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
```

Note: for a ticket with an epic, the footer's `Epic #{epic}` clause renders the
epic *key* (`I`) at plan time, not an issue number — the number does not exist
until `apply` creates the epic. `apply` rewrites it via `_resolve_epic_refs` in
Task 4. For an epic-less ticket (`ticket.epic is None`), the clause is omitted
entirely at plan time — there is nothing for `apply` to rewrite.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_sprint.py -v`
Expected: PASS

- [ ] **Step 6: Document the config block**

Add a "Sprint Buildout Config" section to `config/CONFIG_REFERENCE.md`, matching
the style of the existing "Review Strategy Config" section: show the full block,
state that every key is required except `notion:`, and state the consequence of
omitting `notion:` (the skill's Notion phase silently skips). Note specifically
that `ticket.footer_epic_clause` is appended to `ticket.footer_pattern` only
when a ticket has an epic — an epic-less ticket's footer omits it entirely, it
is not a template substitution with an empty/placeholder value. Say plainly that
these values exist so buildout does not have to rediscover them by reading prior
issues.

- [ ] **Step 7: Run the gates and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ && uv run mypy --strict src/
git add src/cw/sprint.py tests/test_sprint.py .claude/project-config.yaml config/CONFIG_REFERENCE.md
git commit -m "feat(sprint): buildout config block and plan builder"
```

---

### Task 3: `gh` creation helpers

**Files:**
- Modify: `src/cw/gh.py`
- Modify: `tests/test_gh.py`

**Interfaces:**
- Produces (all policy-free: return the value or `None`, never log, never raise):
  - `cw.gh.create_milestone(title: str, *, timeout: int = ...) -> int | None`
  - `cw.gh.find_milestone(title: str, *, timeout: int = ...) -> tuple[int | None, bool]`
  - `cw.gh.create_issue(title: str, body: str, *, labels: list[str], milestone: int, timeout: int = ...) -> int | None`
  - `cw.gh.update_issue_body(number: int, body: str, *, timeout: int = ...) -> bool`
  - `cw.gh.milestone_issue_titles(milestone: int, *, timeout: int = ...) -> tuple[dict[str, int] | None, bool]`

  `find_milestone` and `milestone_issue_titles` return `(value, ok)`: `ok=False`
  means the gh call itself failed (non-zero exit, exception, unparseable JSON) —
  the caller must not read that as "doesn't exist yet" — while `ok=True` with a
  `None`/empty value is a genuine miss. `create_milestone`, `create_issue`, and
  `update_issue_body` are mutations: a `None`/`False` there is unambiguously a
  failure, so they keep their plain return type.

## Touch-point Contract

- Touch-point: `src/cw/gh.py` import-block insertion point
  File:line: `src/cw/gh.py:1-8`
  Read: `"""GitHub CLI helpers for cw."""` / `from __future__ import annotations` / `import json` / `import subprocess as _sp` / `from typing import Any` / `from urllib.parse import quote as _urlquote` (the last line of the existing top-of-file import block; the file is currently 477 lines total)
  Plan asserts: new imports (`tempfile`, `re`, `contextmanager`, `Path`, `TYPE_CHECKING`) must be added to this existing header, not appended near the new functions at the end of the ~478-line file, to avoid tripping ruff's E402.
  Match: CONFIRMED

- Touch-point: `TYPE_CHECKING`-guarded `Iterator` import precedent
  File:line: `src/cw/_util.py:13,15-16`; `src/cw/history.py:11,19-20`
  Read: `_util.py` — `from typing import TYPE_CHECKING` (13) ... `if TYPE_CHECKING:` / `    from collections.abc import Iterator` (15-16). `history.py` — `from typing import TYPE_CHECKING` (11) ... `if TYPE_CHECKING:` / `    from collections.abc import Iterator` (19-20).
  Plan asserts: `gh.py`'s new `def _body_file(body: str) -> Iterator[Path]:` annotation should guard `Iterator` under `TYPE_CHECKING` — "the same shape already used in `src/cw/_util.py` and `src/cw/history.py`."
  Match: CONFIRMED

- Touch-point: `tests/test_gh.py`'s 7 existing `class Test*:` groups
  File:line: `tests/test_gh.py:44,358,415,533,812,852,921`
  Read: `class TestPrIsMergedForTicket:` (44), `class TestPrExistsForBranch:` (358), `class TestBranchExistsOnOrigin:` (415), `class TestFetchApprovedPlanComment:` (533), `class TestCurrentGhLogin:` (812), `class TestFetchPrView:` (852), `class TestAddPrReviewer:` (921) — seven `class Test<FunctionName>:` groups; no bare top-level test functions exist in the file.
  Plan asserts: "following that file's existing monkeypatch-of-`_sp.run` pattern AND its existing `class Test<FunctionName>:` grouping (all 7 existing test groups in the file use this shape — do not add bare top-level test functions)."
  Match: CONFIRMED

## Pre-flight Resolution Conformance

- R1: LOOKUP helpers (`find_milestone`, `milestone_issue_titles`) return `tuple[T | None, bool]`, not bare `T | None`, so a transient `gh` failure is distinguishable from a genuine miss — the plan's Interfaces block and implementation both use this exact shape, with docstrings explaining the `(None, False)` vs `(None, True)` split. [SATISFIED]
- R2: MUTATION helpers (`create_milestone`, `create_issue`, `update_issue_body`) keep simple, non-tupled returns — plan interfaces show `int | None`, `int | None`, `bool` respectively, with no tuple-ification. [SATISFIED]
- R3: Policy-free convention (return value/failure sentinel, never log, never raise, caller decides meaning) — stated verbatim in the Interfaces block preamble and followed in every implemented function (`except (OSError, _sp.TimeoutExpired): return None`/`False`, no logging calls anywhere). [SATISFIED]
- R4: Bodies always go through `--body-file`, never `--body`/argv/heredoc — `_body_file` context manager is implemented and used by both `create_issue` and `update_issue_body`; `test_writes_the_body_to_a_temp_file_not_the_argv` explicitly asserts `"--body" not in calls[0]`. [SATISFIED]
- R5: Test seam is `monkeypatch.setattr(gh._sp, "run", fake_run)`, with a test distinguishing call-failure `(None, False)` from genuine-miss `(None, True)` — used throughout; `TestFindMilestone.test_returns_none_true_on_a_genuine_miss` and `test_returns_none_false_when_the_gh_call_fails` cover exactly this pair (mirrored in `TestMilestoneIssueTitles`). [SATISFIED]
- R6: Avoid non-ASCII bytes literals (`b'... — ...'` is a `SyntaxError`); build byte fixtures via `'...'.encode()` — `test_maps_title_to_number` does exactly this for the em-dash title, with an explanatory comment. [SATISFIED]
- R7: Document (don't solve) the known limitation that duplicate issue titles collapse to one entry in `milestone_issue_titles` — the function's docstring states the assumption explicitly: "if two issues under *milestone* share the exact same title, this dict comprehension keeps only the last one seen. The idempotent re-entry check in `apply_plan` assumes ticket/epic titles are unique within a milestone." [SATISFIED]

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gh.py`, following that file's existing monkeypatch-of-`_sp.run`
pattern AND its existing `class Test<FunctionName>:` grouping (all 7 existing test
groups in the file use this shape — do not add bare top-level test functions).
Every test method annotates `monkeypatch: pytest.MonkeyPatch`, matching the file's
existing `if TYPE_CHECKING: import pytest` header import.

These new tests deliberately build ad hoc `fake_run` closures rather than reusing
the file's existing `_make_run_result`/`_make_issue_result`/`_make_pr_result`
helpers (lines 24-41): those helpers hand back `str` stdout via `MagicMock` with
no `text=True` on the `_sp.run` call, whereas the new call sites intentionally
omit `text=True` and return raw `bytes` (per the "Why: no text=True" comment
already in this plan's test code), so a raw `CompletedProcess(cmd, code, bytes,
bytes)` closure is the correct fake here, not the string-based helpers.

```python
# Add to tests/test_gh.py's header. The file already imports `subprocess`, but
# it imports gh's functions by name (`from cw.gh import ...`) and never the
# module — so `gh._sp` / `gh.create_issue` below need the module import, and the
# temp-file assertion needs Path. Without these two lines the block NameErrors.
from pathlib import Path

from cw import gh


class TestCreateIssue:
    """Tests for create_issue (and _attach_milestone, exercised through it)."""

    def test_writes_the_body_to_a_temp_file_not_the_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bodies never ride argv: RFC titles carry em-dashes and ampersands."""
        calls: list[list[str]] = []
        seen: dict[str, object] = {}

        # The PATCH echoes the updated issue back; _attach_milestone reads the
        # milestone off it to confirm the change actually stuck (see below).
        attached = b'{"number": 42, "milestone": {"number": 11}}'

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(cmd)
            if "--body-file" in cmd:
                body_file = Path(cmd[cmd.index("--body-file") + 1])
                seen["body"] = body_file.read_text(encoding="utf-8")
                return subprocess.CompletedProcess(
                    cmd, 0, b"https://github.com/o/r/issues/42\n", b""
                )
            return subprocess.CompletedProcess(cmd, 0, attached, b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        number = gh.create_issue(
            "RFC 0011 A1 — park class",
            "## Context\n\nBody & more.",
            labels=["feature"],
            milestone=11,
        )

        assert number == 42
        assert "--body" not in calls[0]  # only --body-file
        assert seen["body"] == "## Context\n\nBody & more."

    def test_attaches_the_milestone_by_id_not_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`gh issue create -m` resolves a milestone BY NAME, so the id cannot ride it.

        Passing str(11) there would hunt for a milestone *titled* "11". The id goes
        through the REST endpoint instead, as a typed (-F) field so it lands as a
        JSON number rather than the string "11".
        """
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(cmd)
            if "--body-file" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, b"https://github.com/o/r/issues/42\n", b""
                )
            return subprocess.CompletedProcess(
                cmd, 0, b'{"number": 42, "milestone": {"number": 11}}', b""
            )

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=11) == 42

        create, attach = calls
        assert "--milestone" not in create  # the id must NOT ride `gh issue create`
        assert attach[:2] == ["gh", "api"]
        assert "repos/{owner}/{repo}/issues/42" in attach
        assert "-X" in attach and attach[attach.index("-X") + 1] == "PATCH"
        assert "-F" in attach and attach[attach.index("-F") + 1] == "milestone=11"

    def test_returns_none_when_the_milestone_attach_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A created issue with no milestone is a half-applied buildout — report it."""

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "--body-file" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, b"https://github.com/o/r/issues/42\n", b""
                )
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: HTTP 422")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=11) is None

    def test_catches_a_silently_dropped_milestone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 200 whose echoed issue has NO milestone is a silent drop, not a success.

        GitHub: "without push access to the repository, milestone changes are
        silently dropped." Exit-code-only would report success and leave the issue
        off the milestone — a half-applied buildout that apply_plan could not see.
        """

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "--body-file" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, b"https://github.com/o/r/issues/42\n", b""
                )
            # 200 OK, but the milestone never applied.
            return subprocess.CompletedProcess(cmd, 0, b'{"number": 42, "milestone": null}', b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=11) is None

    def test_returns_none_when_gh_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: not authenticated")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=1) is None

    # Why: no text=True — gh emits UTF-8; decoding via the platform locale would
    # mangle em-dashes in RFC titles, so these fakes hand back raw bytes instead.
    def test_returns_none_when_the_create_call_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=1) is None

    def test_returns_none_when_the_create_call_stdout_has_no_issue_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b"no url here\n", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=1) is None

    def test_returns_none_when_the_attach_call_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "--body-file" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, b"https://github.com/o/r/issues/42\n", b""
                )
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=11) is None

    def test_returns_none_when_the_attach_response_is_malformed_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "--body-file" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, b"https://github.com/o/r/issues/42\n", b""
                )
            return subprocess.CompletedProcess(cmd, 0, b"not json", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_issue("t", "b", labels=[], milestone=11) is None


class TestUpdateIssueBody:
    """Tests for update_issue_body."""

    def test_writes_the_body_to_a_temp_file_and_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            body_file = Path(cmd[cmd.index("--body-file") + 1])
            seen["body"] = body_file.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.update_issue_body(42, "new body & stuff") is True
        assert seen["body"] == "new body & stuff"

    def test_returns_false_when_gh_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: HTTP 404")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.update_issue_body(42, "body") is False

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.update_issue_body(42, "body") is False


class TestCreateMilestone:
    """Tests for create_milestone."""

    def test_returns_the_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b'{"number": 11}', b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_milestone("v1.20.0 — Availability & Counterparty") == 11

    def test_returns_none_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: HTTP 422")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_milestone("t") is None

    def test_returns_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_milestone("t") is None

    def test_returns_none_on_malformed_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b"not json", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.create_milestone("t") is None


class TestFindMilestone:
    """Tests for find_milestone."""

    def test_returns_the_number_and_ok_true_when_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = b'{"number": 11, "title": "v1.20.0 milestone"}\n'

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, payload, b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.find_milestone("v1.20.0 milestone") == (11, True)

    def test_asks_for_closed_milestones_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ?state=all, GitHub lists only OPEN milestones (documented default).

        A finished sprint's milestone is CLOSED. If find_milestone can't see it, a
        re-run of apply_plan reads "no such milestone", creates a duplicate under
        the same title, and orphans every issue filed under the first one.
        """
        seen: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        gh.find_milestone("v1.20.0 milestone")

        assert "repos/{owner}/{repo}/milestones?state=all&per_page=100" in seen[0]

    def test_returns_none_true_on_a_genuine_miss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The call succeeded; no milestone with this title exists yet."""

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            payload = b'{"number": 3, "title": "unrelated milestone"}\n'
            return subprocess.CompletedProcess(cmd, 0, payload, b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.find_milestone("v1.20.0 milestone") == (None, True)

    def test_returns_none_false_when_the_gh_call_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero gh exit must be distinguishable from a genuine miss — reading
        it as "doesn't exist" would make apply_plan re-file a duplicate milestone
        on a re-run after a transient failure."""

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: rate limited")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.find_milestone("v1.20.0 milestone") == (None, False)

    def test_returns_none_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.find_milestone("v1.20.0 milestone") == (None, False)

    def test_skips_an_unparseable_jq_line_and_still_finds_a_later_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One malformed line in the --jq stream must not abort the scan — a
        later, well-formed line with the matching title still resolves."""
        payload = b"not json\n" + b'{"number": 11, "title": "v1.20.0 milestone"}\n'

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, payload, b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.find_milestone("v1.20.0 milestone") == (11, True)


class TestMilestoneIssueTitles:
    """Tests for milestone_issue_titles."""

    def test_maps_title_to_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Why: the em-dash is non-ASCII, so this cannot be a bytes literal — `gh`
        # emits UTF-8 and the helper must decode it, so encode the fixture the same way.
        payload = '[{"number": 1155, "title": "RFC 0011 A1 — park class"}]'.encode()

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, payload, b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.milestone_issue_titles(11) == ({"RFC 0011 A1 — park class": 1155}, True)

    def test_returns_none_false_when_the_gh_call_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: not authenticated")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.milestone_issue_titles(11) == (None, False)

    def test_returns_none_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.milestone_issue_titles(11) == (None, False)

    def test_returns_none_false_on_malformed_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b"not json", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.milestone_issue_titles(11) == (None, False)

    def test_returns_none_false_when_the_payload_is_not_a_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b'{"not": "a list"}', b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.milestone_issue_titles(11) == (None, False)

    def test_a_milestone_with_no_issues_returns_an_empty_dict_not_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A milestone that exists but has nothing filed under it yet is
        ``({}, True)`` — NOT ``(None, True)``. Conflating the two would make
        apply_plan treat a genuinely-empty milestone as a failed lookup."""

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b"[]", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.milestone_issue_titles(11) == ({}, True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gh.py -v -k "create or milestone"`
Expected: FAIL — `AttributeError: module 'cw.gh' has no attribute 'create_issue'`

- [ ] **Step 3: Implement the helpers**

First, edit `src/cw/gh.py`'s EXISTING top-of-file import block — the one that
currently ends at `from urllib.parse import quote as _urlquote` (line 8) — to
add `tempfile`, `re`, `contextmanager`, and `Path`. Do **not** add these at the
insertion point below with the appended functions: landing new imports after
~478 lines of existing code trips ruff's E402 (no per-file ignore exists for
`src/cw/gh.py`). `Iterator` is used only inside the annotation
`def _body_file(body: str) -> Iterator[Path]:` — with `from __future__ import
annotations` already at the top of the file, annotations are strings at
runtime, so `Iterator` only needs to be visible to type checkers. Ruff's TCH003
therefore requires it under `TYPE_CHECKING` rather than a real import; this is
the same shape already used in `src/cw/_util.py` and `src/cw/history.py`.
`Path`, `tempfile`, and `contextmanager` ARE used at runtime (`Path(handle.name)`,
`path.unlink(...)`, the `@contextmanager` decorator), so they stay real imports.

The whole header becomes:

```python
"""GitHub CLI helpers for cw."""

from __future__ import annotations

import json
import re
import subprocess as _sp
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as _urlquote

if TYPE_CHECKING:
    from collections.abc import Iterator
```

Then append the following to the end of `src/cw/gh.py`, below its existing code.
Match the file's established shape: module-level timeout constants,
`_sp.run(..., capture_output=True, check=False)`, `except (OSError,
_sp.TimeoutExpired)` → `None`.

```python
_CREATE_TIMEOUT = 30
_ISSUE_URL_NUMBER_RE = re.compile(r"/(\d+)\s*$")


@contextmanager
def _body_file(body: str) -> Iterator[Path]:
    """Write *body* to a temp file for --body-file.

    Why: issue/milestone bodies and titles carry em-dashes, ampersands, and
    backticks. Passing them on argv invites a quoting bug on every call; a
    --body-file never can.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", encoding="utf-8", delete=False
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def create_issue(
    title: str,
    body: str,
    *,
    labels: list[str],
    milestone: int,
    timeout: int = _CREATE_TIMEOUT,
) -> int | None:
    """Create an issue and attach it to *milestone*; return its number, or None.

    Two calls, deliberately. ``gh issue create --milestone`` resolves a milestone
    BY NAME (``-m, --milestone name``), not by id — handing it ``str(11)`` would
    look for a milestone *titled* "11" and fail. ``gh issue edit -m`` is name-only
    too. So the milestone is attached afterwards through the REST endpoint, which
    does take the numeric id. This keeps ``milestone: int`` in the signature (the
    id is what ``apply_plan`` holds, and it is unambiguous where a title is not).

    ``-F`` (not ``-f``) sends a *typed* field, so ``milestone`` arrives as a JSON
    number rather than the string "11", which is what the API expects.
    """
    cmd = ["gh", "issue", "create", "--title", title]
    for label in labels:
        cmd += ["--label", label]
    try:
        with _body_file(body) as path:
            cmd += ["--body-file", str(path)]
            result = _sp.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _ISSUE_URL_NUMBER_RE.search(result.stdout.decode("utf-8", "replace"))
    if not match:
        return None
    number = int(match.group(1))
    return number if _attach_milestone(number, milestone, timeout) else None


def _attach_milestone(number: int, milestone: int, timeout: int) -> bool:
    """Attach *number* to *milestone* by numeric id; confirm it actually stuck.

    Why read the response back instead of trusting the exit code: GitHub's
    "Update an issue" reference states that "without push access to the
    repository, milestone changes are silently dropped" — a 200 with the
    milestone simply not applied. Exit-code-only would report success and leave
    the issue off the milestone, which is precisely the half-applied buildout
    apply_plan exists to prevent. So confirm the echoed milestone number.
    """
    try:
        result = _sp.run(
            [
                "gh", "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}",
                "-X", "PATCH",
                "-F", f"milestone={milestone}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return False
    attached = payload.get("milestone") if isinstance(payload, dict) else None
    if not isinstance(attached, dict):
        return False
    return attached.get("number") == milestone


def update_issue_body(number: int, body: str, *, timeout: int = _CREATE_TIMEOUT) -> bool:
    """Replace an issue's body via ``gh issue edit --body-file``. True on success."""
    try:
        with _body_file(body) as path:
            result = _sp.run(
                ["gh", "issue", "edit", str(number), "--body-file", str(path)],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except (OSError, _sp.TimeoutExpired):
        return False
    return result.returncode == 0


def create_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> int | None:
    """Create a milestone via the REST API; return its number, or None on failure."""
    try:
        result = _sp.run(
            [
                "gh", "api", "repos/{owner}/{repo}/milestones",
                "-f", f"title={title}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    number = payload.get("number") if isinstance(payload, dict) else None
    return number if isinstance(number, int) else None


def find_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> tuple[int | None, bool]:
    """Return (number, ok) for an existing milestone titled *title*, open OR closed.

    ``?state=all`` is load-bearing, not decoration. GitHub's "List milestones"
    defaults ``state`` to ``open``, so without it a milestone that has been
    CLOSED (which is what happens to a sprint's milestone once the sprint ends)
    is invisible here — apply_plan would read that as "no such milestone",
    create a SECOND one with the same title, and orphan the issues filed under
    the first. That is precisely the duplicate-filing this function exists to
    prevent. It goes in the path as a query string: ``-f state=all`` would flip
    gh's auto-method from GET to POST.

    ``&per_page=100`` is equally load-bearing. ``gh api`` pagination is
    opt-in — without an explicit ``per_page``, GitHub returns only the first
    page (30 items) of milestones. A repo with more than 30 milestones would
    silently drop the older ones from this scan, reintroducing the exact
    duplicate-milestone bug ``?state=all`` was added to prevent.

    ``ok=False`` means the gh call itself failed (non-zero exit, OSError,
    timeout) — the caller cannot conclude the milestone is absent, only that it
    could not check. ``ok=True`` with ``number=None`` is a genuine miss: the
    call succeeded and no milestone with this title exists yet. Conflating
    these two is exactly what breaks idempotency on a re-run after a transient
    gh failure (see ``cw.sprint.apply_plan``).
    """
    try:
        result = _sp.run(
            [
                "gh", "api",
                "repos/{owner}/{repo}/milestones?state=all&per_page=100",
                "--jq", ".[] | {number, title}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("title") == title and isinstance(entry.get("number"), int):
            number: int = entry["number"]
            return number, True
    return None, True


def milestone_issue_titles(
    milestone: int, *, timeout: int = _CREATE_TIMEOUT
) -> tuple[dict[str, int] | None, bool]:
    """Return ({issue title: number}, ok) for every issue on *milestone*.

    ``ok=False`` means the gh call itself failed (non-zero exit, OSError,
    timeout, unparseable JSON) — this is what makes ``cw sprint apply``
    idempotent: a caller must not read a failed lookup as "milestone has no
    issues yet" and re-file everything as duplicates. On success the dict may
    legitimately be empty (milestone exists but nothing has been filed under it
    yet); that is ``({}, True)``, not ``(None, True)``.

    Note: if two issues under *milestone* share the exact same title, this dict
    comprehension keeps only the last one seen. The idempotent re-entry check
    in ``apply_plan`` assumes ticket/epic titles are unique within a milestone.

    Note also the ``--limit 200`` cap below: only the 200 most recent issues
    under *milestone* are considered. A milestone with more than 200 issues
    filed against it will silently omit the older ones from the returned dict.

    Passing the numeric id to ``gh issue list --milestone`` is correct here and
    is NOT the same bug as in ``create_issue``: list documents its flag as
    "Filter by milestone number or title", whereas ``issue create``/``issue
    edit`` take a name only. Do not "fix" this one.
    """
    try:
        result = _sp.run(
            [
                "gh", "issue", "list",
                "--milestone", str(milestone),
                "--state", "all",
                "--limit", "200",
                "--json", "number,title",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(payload, list):
        return None, False
    titles = {
        str(item["title"]): int(item["number"])
        for item in payload
        if isinstance(item, dict) and "title" in item and "number" in item
    }
    return titles, True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gh.py -v`
Expected: PASS (existing tests still green — the new helpers are additive)

- [ ] **Step 5: Run the gates and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ && uv run mypy --strict src/
git add src/cw/gh.py tests/test_gh.py
git commit -m "feat(gh): issue and milestone creation helpers, bodies via --body-file"
```

---

### Task 4: Apply — the four-pass dance, idempotent

**Files:**
- Modify: `src/cw/sprint.py`
- Modify: `src/cw/exceptions.py`
- Modify: `tests/test_sprint.py`

**Interfaces:**
- Consumes: `cw.sprint.BuildoutPlan` (Task 2); every helper from Task 3.
- Produces:
  - `cw.sprint.GhSurface` (Protocol) and `cw.sprint.GhClient` (its default `cw.gh`-backed impl)
  - `cw.sprint.AppliedBuildout` (Pydantic model: `milestone_number: int`, `epic_numbers: dict[str, int]`, `ticket_numbers: dict[str, int]`, `created: list[str]`, `skipped: list[str]`). `created`/`skipped` are epic-or-ticket codes, partitioned by whether `_create_or_skip` created a new issue or reused one matched by title against the milestone's existing issues.
  - `cw.sprint.apply_plan(plan: BuildoutPlan, *, client: GhSurface | None = None) -> AppliedBuildout` — raises `SprintApplyError` on any unrecoverable `gh` failure
  - `cw.exceptions.SprintApplyError(CwError)` — carries the partial `AppliedBuildout` (as `applied`) so the CLI can report what was created before the failure

`GhSurface.find_milestone` and `.milestone_issue_titles` mirror Task 3's
`(value, ok)` tuple returns exactly — `apply_plan` must refuse
(`SprintApplyError`) rather than proceed when `ok` is `False`, since proceeding
would read a failed lookup as "nothing exists yet" and re-file duplicates.

## Touch-point Contract

- Touch-point: `src/cw/sprint.py` import-block insertion point
  File:line: `src/cw/sprint.py:10-23`
  Read: `from __future__ import annotations` (10) / (blank) / `import re` (12) /
  `import subprocess as _sp` (13) / `from typing import TYPE_CHECKING` (14) /
  (blank) / `from pydantic import BaseModel, Field` (16) / (blank) /
  `from cw.exceptions import RfcContractError` (18) /
  `from cw.tracker import load_project_config_dict` (19) / (blank) /
  `if TYPE_CHECKING:` (21) / `    from collections.abc import Iterator` (22) /
  `    from pathlib import Path` (23) — this is the current, already-merged
  header (Tasks 1-2 are on `origin/main`); the file is 545 lines total.
  Plan asserts: Task 4's new imports (`Protocol`, `runtime_checkable`; `from cw
  import gh`; `SprintApplyError`) must land in this existing header — `Protocol,
  runtime_checkable` added onto the existing line-14 `from typing import
  TYPE_CHECKING` import, `SprintApplyError` merged into the existing line-18
  `from cw.exceptions import RfcContractError` import, `from cw import gh` added
  as its own line — not appended near `apply_plan` at the end of the file, to
  avoid tripping ruff's E402 (and I001 if merely moved to the top unsorted).
  Match: CONFIRMED

- Touch-point: `src/cw/gh.py`'s 5 real signatures `GhSurface` must structurally match
  File:line: `src/cw/gh.py:510-517` (`create_issue`), `587-589`
  (`update_issue_body`), `604` (`create_milestone`), `631-633`
  (`find_milestone`), `686-688` (`milestone_issue_titles`)
  Read:
  `def create_issue(title: str, body: str, *, labels: list[str], milestone: int, timeout: int = _CREATE_TIMEOUT) -> int | None:`
  `def update_issue_body(number: int, body: str, *, timeout: int = _CREATE_TIMEOUT) -> bool:`
  `def create_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> int | None:`
  `def find_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> tuple[int | None, bool]:`
  `def milestone_issue_titles(milestone: int, *, timeout: int = _CREATE_TIMEOUT) -> tuple[dict[str, int] | None, bool]:`
  Plan asserts: `GhSurface`'s 5 Protocol methods (and `GhClient`'s delegating
  bodies) cover a narrower call surface than the real functions — none of them
  declare the real `*, timeout: int = ...` keyword-only parameter, since
  `apply_plan` never needs a non-default timeout. This is a deliberate subset,
  not a mismatch: a class satisfies a `Protocol` by being call-compatible with
  what the Protocol declares, and `GhClient`'s bodies (`gh.find_milestone(title)`
  etc.) rely on the real functions' `timeout` default rather than forwarding one.
  Every other parameter name, order, and return-type shape (including the
  `tuple[T | None, bool]` split on the two lookup helpers) matches exactly.
  Match: CONFIRMED

- Touch-point: `src/cw/exceptions.py`'s `CwError.__slots__ = ()` convention
  File:line: `src/cw/exceptions.py:18-21`
  Read: `class CwError(Exception):` (18) / `"""Base exception for all cw
  errors."""` (19) / (blank) / `    __slots__ = ()` (21) — every subclass in the
  file (`WorktreeError`, `RfcContractError`, etc.) repeats `__slots__ = ()`
  unchanged, since none of them carry their own attributes.
  Plan asserts: `SprintApplyError` follows the same `__slots__` convention but
  is the first exception in the file to override it to `("applied",)`, because
  — unlike every existing subclass — it carries an instance attribute (the
  partial `AppliedBuildout`) that a plain `()` slots tuple would reject.
  Match: CONFIRMED

## Pre-flight Resolution Conformance

- R1: The `(value, ok)` tuple discipline on `find_milestone` and
  `milestone_issue_titles` extends into `apply_plan`: `ok=False` is treated as
  an unrecoverable failure, never as "nothing exists yet" (the duplicate-filing
  guard). `_resolve_milestone` raises `SprintApplyError("could not determine
  whether milestone exists...")` when `find_milestone`'s `ok` is `False`, and
  `apply_plan` raises `SprintApplyError("could not list existing issues...")`
  when `milestone_issue_titles`'s `ok` is `False`. Covered by
  `test_apply_plan_raises_when_the_milestone_lookup_itself_fails` and
  `test_apply_plan_raises_when_the_milestone_issue_lookup_fails`, both of which
  also assert no `create_issue` call leaked through before the raise.
  [SATISFIED]
- R2: `GhSurface` is a `@runtime_checkable typing.Protocol`, not the `cw.gh`
  module itself. `apply_plan(plan, *, client: GhSurface | None = None)` types
  the parameter as the Protocol, and `GhSurface`'s docstring states why:
  `mypy --strict` cannot verify a module object against a call site without a
  suppression, which this repo does not permit. [SATISFIED]
- R3: `SprintApplyError` carries the partial `AppliedBuildout` (as `applied`)
  so a mid-run failure is diagnosable rather than opaque. `_create_or_skip` and
  `_backfill_children` both raise with `applied=applied`;
  `test_sprint_apply_error_carries_the_partial_applied_state` asserts
  `exc_info.value.applied.milestone_number == 11` and that both tickets already
  created survive on the partial object. [SATISFIED]
- R4: The epic-ref rewrite (`Epic #<key>` → `Epic #<number>`) is a
  boundary-safe regex, not a plain substring replace — Epic I's replacement
  must not corrupt an Epic II reference (`"Epic #I"` is a literal prefix of
  `"Epic #II"`). `_resolve_epic_refs` uses
  `re.sub(rf"Epic #{re.escape(key)}(?!\w)", ...)`;
  `test_apply_plan_resolves_epic_ii_refs_without_corruption_from_epic_i`
  exercises exactly this failure mode with the two-epic fixture. [SATISFIED]
- R5: An epic-less ticket's footer keeps no `Epic #` clause at all — this is
  not a template substitution with an empty placeholder, it is the absence of
  the clause. `_resolve_epic_refs`'s loop is a no-op when the body has no
  `Epic #` substring to match, so the body passes through byte-for-byte;
  `test_apply_plan_leaves_the_epic_less_ticket_footer_epic_free` asserts
  `"Epic #" not in s1_body`. [SATISFIED]

- [ ] **Step 1: Write the failing tests**

The point of these tests is **call order** and **idempotency** — the two things
the hand-driven session got wrong. GitHub assigns numbers on creation, so epics
must exist before children can reference them, and the checklist can only be
written once the children have numbers.

Append to `tests/test_sprint.py`:

```python
# Merge into tests/test_sprint.py's EXISTING top-of-file import block — do NOT
# append these as new lines near the classes/functions below (that trips
# ruff's E402; and even placed at top as unsorted separate lines, I001).
# The existing header is:
#     from cw import sprint
#     from cw.exceptions import RfcContractError
#     from cw.sprint import (
#         BuildoutConfig,
#         build_plan,
#         load_buildout_config,
#         load_rfc_text,
#         parse_rfc,
#     )
#     from tests.conftest import _write_project_config_yaml
# `_plan()` below is annotated `-> BuildoutPlan`, but BuildoutPlan was never
# imported by name — Task 2's append only imported BuildoutConfig/build_plan/
# load_buildout_config. `AppliedBuildout` and `GhSurface` are needed by the
# new tests further down (partial-state narrowing and Protocol conformance);
# `gh` is needed by the client=None default-path test. Without these, the
# block NameErrors.
from cw import gh, sprint
from cw.exceptions import RfcContractError, SprintApplyError
from cw.sprint import (
    AppliedBuildout,
    BuildoutConfig,
    BuildoutPlan,
    GhSurface,
    apply_plan,
    build_plan,
    load_buildout_config,
    load_rfc_text,
    parse_rfc,
)
from tests.conftest import _write_project_config_yaml


class FakeGh:
    """A GhSurface test double. Records call order; hands out issue numbers."""

    def __init__(
        self,
        *,
        existing: dict[str, int] | None = None,
        milestone_exists: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.bodies: dict[int, str] = {}
        self.existing = existing or {}
        # Decoupled from `existing` on purpose: a milestone can exist with zero
        # issues yet (a partial run that died between pass 1 and pass 2).
        self.milestone_exists = milestone_exists or bool(self.existing)
        self._next = 100

    def find_milestone(self, title: str) -> tuple[int | None, bool]:
        self.calls.append(f"find_milestone:{title}")
        return (11, True) if self.milestone_exists else (None, True)

    def create_milestone(self, title: str) -> int | None:
        self.calls.append(f"create_milestone:{title}")
        return 11

    def milestone_issue_titles(
        self, milestone: int
    ) -> tuple[dict[str, int] | None, bool]:
        self.calls.append(f"list:{milestone}")
        return dict(self.existing), True

    def create_issue(
        self, title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None:
        self.calls.append(f"create_issue:{title}")
        self._next += 1
        self.bodies[self._next] = body
        return self._next

    def update_issue_body(self, number: int, body: str) -> bool:
        self.calls.append(f"update_issue_body:{number}")
        self.bodies[number] = body
        return True


def _plan() -> BuildoutPlan:
    return build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")


TWO_EPIC_RFC = """\
# RFC 0011 — Availability- & Counterparty-Aware Holding

## Summary

Body.

## Design

### Epic I — Availability-aware holding (inward)

Hold work when the environment cannot carry it.

### Epic II — Counterparty-aware collaboration (outward)

Collaborate when the counterparty can't proceed.

## Phasing

| Wave | Track A | Track B |
|------|---------|---------|
| 0 | S1 | |

## Resolved decisions

- **D-S1 — Counterparty derivation.** Derive at pr_hydrate, no stored field.
- **D-A1 — Park-class shape.** New blocker.reason value.
- **D-B1 — Idle exemption.** Idle counterparties are exempt from holds.

## Tickets

### S1 — counterparty axis + self-identity

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** The shared seam both epics build on.
- **Scope:** D-S1
- **Acceptance:**
  - Counterparty resolves to self when no PR exists.

### A1 — park class (keystone)

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** The keystone park class.
- **Scope:** D-A1
- **Acceptance:**
  - A held task routes to BLOCKED_ON_USER.

### B1 — idle exemption

- **Epic:** II
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** Idle counterparties should not hold work.
- **Scope:** D-B1
- **Acceptance:**
  - An idle counterparty's ticket is exempt from the hold.

## References

- `src/cw/pr_hydrate.py:257` — where counterparty derivation lands

## Issues

Issues: _(filled by `/sprint-buildout`)_
"""


def test_apply_plan_creates_milestone_then_epics_then_tickets_then_backfills() -> None:
    fake = FakeGh()
    applied = apply_plan(_plan(), client=fake)

    kinds = [c.split(":")[0] for c in fake.calls]
    assert kinds == [
        "find_milestone",
        "create_milestone",
        "list",
        "create_issue",       # epic I
        "create_issue",       # ticket S1
        "create_issue",       # ticket A1
        "update_issue_body",  # epic I checklist backfill
    ]
    assert applied.milestone_number == 11
    assert set(applied.ticket_numbers) == {"S1", "A1"}


def test_apply_plan_backfills_the_children_checklist_into_the_marker() -> None:
    fake = FakeGh()
    applied = apply_plan(_plan(), client=fake)

    epic_body = fake.bodies[applied.epic_numbers["I"]]
    assert "<!-- children -->" not in epic_body
    assert f"- [ ] #{applied.ticket_numbers['A1']}" in epic_body


def test_apply_plan_rewrites_the_ticket_footer_with_the_real_epic_number() -> None:
    fake = FakeGh()
    applied = apply_plan(_plan(), client=fake)

    a1_body = fake.bodies[applied.ticket_numbers["A1"]]
    assert f"Epic #{applied.epic_numbers['I']}" in a1_body
    assert "Epic #I" not in a1_body


def test_apply_plan_is_idempotent_and_skips_issues_that_already_exist() -> None:
    plan = _plan()
    existing = {plan.tickets[0].title: 1153}  # S1 already filed by a partial run
    fake = FakeGh(existing=existing)

    applied = apply_plan(plan, client=fake)

    assert applied.ticket_numbers["S1"] == 1153
    assert "S1" in applied.skipped
    assert f"create_issue:{plan.tickets[0].title}" not in fake.calls


def test_apply_plan_raises_when_milestone_creation_fails() -> None:
    class DeadGh(FakeGh):
        def create_milestone(self, title: str) -> int | None:
            return None

    with pytest.raises(SprintApplyError, match="could not create milestone"):
        apply_plan(_plan(), client=DeadGh())


def test_apply_plan_raises_when_the_milestone_lookup_itself_fails() -> None:
    """A non-zero gh exit on find_milestone is NOT a genuine miss — apply_plan
    must refuse rather than try to create a (possibly duplicate) milestone."""

    class FlakyGh(FakeGh):
        def find_milestone(self, title: str) -> tuple[int | None, bool]:
            self.calls.append(f"find_milestone:{title}")
            return None, False

    fake = FlakyGh()
    with pytest.raises(
        SprintApplyError, match="could not determine whether milestone exists"
    ):
        apply_plan(_plan(), client=fake)

    assert not any(c.startswith("create_issue") for c in fake.calls)


def test_apply_plan_raises_when_the_milestone_issue_lookup_fails() -> None:
    """Same principle for `milestone_issue_titles`: ok=False must not be read
    as "the milestone has no issues yet"."""

    class FlakyGh(FakeGh):
        def milestone_issue_titles(
            self, milestone: int
        ) -> tuple[dict[str, int] | None, bool]:
            self.calls.append(f"list:{milestone}")
            return None, False

    fake = FlakyGh()
    with pytest.raises(SprintApplyError, match="could not list existing issues"):
        apply_plan(_plan(), client=fake)

    assert not any(c.startswith("create_issue") for c in fake.calls)


def test_apply_plan_reuses_a_milestone_that_exists_with_zero_issues_yet() -> None:
    """A prior partial run created the milestone but died before pass 2 (epic
    creation) ever ran — `milestone_issue_titles` legitimately returns an empty
    dict for an existing milestone. `milestone_exists` is decoupled from
    `existing` on FakeGh precisely so this re-entry shape is testable."""
    fake = FakeGh(milestone_exists=True)
    applied = apply_plan(_plan(), client=fake)

    kinds = [c.split(":")[0] for c in fake.calls]
    assert kinds[0] == "find_milestone"
    assert "create_milestone" not in kinds
    assert applied.milestone_number == 11
    assert set(applied.ticket_numbers) == {"S1", "A1"}


def test_sprint_apply_error_carries_the_partial_applied_state() -> None:
    class DeadBackfillGh(FakeGh):
        def update_issue_body(self, number: int, body: str) -> bool:
            self.calls.append(f"update_issue_body:{number}")
            return False

    fake = DeadBackfillGh()
    with pytest.raises(SprintApplyError) as exc_info:
        apply_plan(_plan(), client=fake)

    applied = exc_info.value.applied
    # `.applied` is typed `object | None` on SprintApplyError (avoids an import
    # cycle — see the exception's docstring), so mypy cannot narrow attribute
    # access without this isinstance check.
    assert isinstance(applied, AppliedBuildout)
    assert applied.milestone_number == 11
    assert set(applied.ticket_numbers) == {"S1", "A1"}


def test_apply_plan_raises_when_epic_creation_fails() -> None:
    """`_create_or_skip`'s `create_issue returns None` branch is shared by both
    epic and ticket creation, but no test above exercises the epic side —
    every DeadGh/FlakyGh variant kills a different call. This kills epic
    creation specifically: the partial `AppliedBuildout` must carry the
    milestone number but no epic numbers yet, since the raise happens before
    `applied.epic_numbers[epic.code]` is ever assigned for the failing epic."""

    class DeadEpicGh(FakeGh):
        def create_issue(
            self, title: str, body: str, *, labels: list[str], milestone: int
        ) -> int | None:
            self.calls.append(f"create_issue:{title}")
            return None

    fake = DeadEpicGh()
    with pytest.raises(SprintApplyError, match="could not create issue") as exc_info:
        apply_plan(_plan(), client=fake)

    applied = exc_info.value.applied
    assert isinstance(applied, AppliedBuildout)
    assert applied.milestone_number == 11
    assert applied.epic_numbers == {}


def test_apply_plan_is_idempotent_and_skips_an_epic_that_already_exists() -> None:
    """Sibling of `test_apply_plan_is_idempotent_and_skips_issues_that_already_exist`,
    which only exercises the skip branch for a ticket. `_create_or_skip` is the
    same shared helper for epics — this covers the other caller."""
    plan = _plan()
    epic_title = plan.epics[0].title
    existing = {epic_title: 500}  # Epic I already filed by a partial run
    fake = FakeGh(existing=existing)

    applied = apply_plan(plan, client=fake)

    assert applied.epic_numbers["I"] == 500
    assert "I" in applied.skipped
    assert f"create_issue:{epic_title}" not in fake.calls


def test_apply_plan_uses_ghclient_by_default_and_forwards_calls_to_cw_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test above passes an explicit `client=fake`, so `GhClient`'s 5
    delegating methods and the `else GhClient()` branch in `apply_plan` are
    0% covered. Monkeypatch `cw.gh`'s 5 module functions directly and call
    `apply_plan` with no `client=` at all."""
    calls: list[str] = []

    def fake_find_milestone(title: str) -> tuple[int | None, bool]:
        calls.append(f"find_milestone:{title}")
        return None, True

    def fake_create_milestone(title: str) -> int | None:
        calls.append(f"create_milestone:{title}")
        return 11

    def fake_milestone_issue_titles(
        milestone: int,
    ) -> tuple[dict[str, int] | None, bool]:
        calls.append(f"list:{milestone}")
        return {}, True

    def fake_create_issue(
        title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None:
        calls.append(f"create_issue:{title}")
        return 200 + len(calls)

    def fake_update_issue_body(number: int, body: str) -> bool:
        calls.append(f"update_issue_body:{number}")
        return True

    monkeypatch.setattr(gh, "find_milestone", fake_find_milestone)
    monkeypatch.setattr(gh, "create_milestone", fake_create_milestone)
    monkeypatch.setattr(gh, "milestone_issue_titles", fake_milestone_issue_titles)
    monkeypatch.setattr(gh, "create_issue", fake_create_issue)
    monkeypatch.setattr(gh, "update_issue_body", fake_update_issue_body)

    applied = apply_plan(_plan())

    assert applied.milestone_number == 11
    assert set(applied.ticket_numbers) == {"S1", "A1"}
    kinds = [c.split(":")[0] for c in calls]
    assert kinds[:3] == ["find_milestone", "create_milestone", "list"]
    assert "update_issue_body" in kinds


def test_fake_gh_satisfies_the_ghsurface_protocol() -> None:
    """`@runtime_checkable` conformance check — cheap, and catches drift if
    `GhSurface`'s method set changes without `FakeGh` keeping pace."""
    assert isinstance(FakeGh(), GhSurface)


def test_backfill_children_handles_an_epic_with_zero_children() -> None:
    """A valid RFC shape: an epic exists but no ticket cites it yet (e.g. a
    keystone epic filed ahead of its first child). `_backfill_children`'s
    checklist generator over `plan.epic_children.get(epic.code, [])` must
    produce an empty checklist, not raise, for that epic."""
    rfc = MINIMAL_RFC.replace(
        "### Epic I — Availability-aware holding (inward)\n\n"
        "Hold work when the environment cannot carry it.\n",
        "### Epic I — Availability-aware holding (inward)\n\n"
        "Hold work when the environment cannot carry it.\n\n"
        "### Epic II — Counterparty-aware collaboration (outward)\n\n"
        "Collaborate when the counterparty can't proceed.\n",
    )
    plan = build_plan(parse_rfc(rfc), _config(), version="1.20.0")
    fake = FakeGh()
    applied = apply_plan(plan, client=fake)

    epic_ii_body = fake.bodies[applied.epic_numbers["II"]]
    assert "<!-- children -->" not in epic_ii_body


def test_apply_plan_resolves_epic_ii_refs_without_corruption_from_epic_i() -> None:
    """Epic keys are Roman numerals, so "Epic #I" is a literal prefix of
    "Epic #II". A plain substring replace over Epic I's number first corrupts
    B1's footer into "Epic #101I" — verified by execution before the fix. The
    single-epic MINIMAL_RFC fixture cannot catch this; it needs two epics."""
    plan = build_plan(parse_rfc(TWO_EPIC_RFC), _config(), version="1.20.0")
    fake = FakeGh()
    applied = apply_plan(plan, client=fake)

    b1_body = fake.bodies[applied.ticket_numbers["B1"]]
    assert f"Epic #{applied.epic_numbers['II']}" in b1_body
    assert "Epic #I" not in b1_body
    assert "Epic #II" not in b1_body


def test_apply_plan_leaves_the_epic_less_ticket_footer_epic_free() -> None:
    """S1 has no epic. `_resolve_epic_refs` must leave its footer untouched —
    no `Epic #` substring should appear, resolved or otherwise."""
    fake = FakeGh()
    applied = apply_plan(_plan(), client=fake)

    s1_body = fake.bodies[applied.ticket_numbers["S1"]]
    assert "Epic #" not in s1_body
    assert "Part of RFC 0011 Wave 0 (Sprint 0)" in s1_body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sprint.py -v -k apply`
Expected: FAIL — `ImportError: cannot import name 'apply_plan' from 'cw.sprint'`

- [ ] **Step 3: Add the exception**

Append to `src/cw/exceptions.py`:

```python
class SprintApplyError(CwError):
    """``cw sprint apply`` could not complete a GitHub mutation.

    Raised rather than silently half-applying. ``apply_plan`` is idempotent, so
    the operator's remedy is always the same: fix the cause (auth, rate limit)
    and re-run — already-created issues are skipped, not duplicated.

    Carries the partial ``AppliedBuildout`` as ``applied`` (default ``None``) so
    the CLI can report what was created before the failure. Typed as
    ``object | None`` here rather than ``AppliedBuildout | None`` to avoid an
    import cycle — ``cw.sprint`` imports from ``cw.exceptions``, not the other
    way around. Callers that need the concrete type narrow it themselves (e.g.
    ``isinstance(e.applied, AppliedBuildout)``).
    """

    __slots__ = ("applied",)

    def __init__(self, message: str, *, applied: object | None = None) -> None:
        super().__init__(message)
        self.applied = applied
```

- [ ] **Step 4: Implement apply**

Edit `src/cw/sprint.py`'s EXISTING top-of-file import block (do NOT append
these lines near the code below — that trips ruff's E402; and even placed at
top as unsorted separate lines, I001). This append raises `SprintApplyError`
throughout (`_resolve_milestone`, `_create_or_skip`, `_backfill_children`) but
Task 1/2's header only imported `RfcContractError`, and it calls `gh.*` but
never imports the module — without both, the block NameErrors. Replace the
current header (`from __future__ import annotations` through the
`if TYPE_CHECKING:` block, `src/cw/sprint.py:10-23`) with:

```python
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
```

Then append the following to `src/cw/sprint.py` (no import lines in this
fence — they are all already merged into the header above):

```python
@runtime_checkable
class GhSurface(Protocol):
    """The slice of `gh` that apply_plan needs.

    A Protocol, not the module itself: `mypy --strict` cannot verify a module
    object against a call site, and the alternative — `gh_mod: object` plus a
    `# type: ignore[union-attr]` on every call — is a suppression, which this repo
    does not permit. The Protocol gives real type-checking AND a clean test double.
    `@runtime_checkable` matches every other Protocol in this codebase
    (`executor.StageExecutor`, `native_daemon.NativeDaemonClient`, etc.).
    """

    def find_milestone(self, title: str) -> tuple[int | None, bool]: ...
    def create_milestone(self, title: str) -> int | None: ...
    def milestone_issue_titles(
        self, milestone: int
    ) -> tuple[dict[str, int] | None, bool]: ...
    def create_issue(
        self, title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None: ...
    def update_issue_body(self, number: int, body: str) -> bool: ...


class GhClient:
    """Default GhSurface — delegates to :mod:`cw.gh`."""

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
    """What `cw sprint apply` actually did. Printed for the operator."""

    milestone_number: int
    epic_numbers: dict[str, int] = Field(default_factory=dict)
    ticket_numbers: dict[str, int] = Field(default_factory=dict)
    created: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


def _resolve_milestone(plan: BuildoutPlan, client: GhSurface) -> int:
    existing, ok = client.find_milestone(plan.milestone_title)
    if not ok:
        msg = f"could not determine whether milestone exists: {plan.milestone_title}"
        raise SprintApplyError(msg)
    if existing is not None:
        return existing
    created = client.create_milestone(plan.milestone_title)
    if created is None:
        msg = f"could not create milestone: {plan.milestone_title}"
        raise SprintApplyError(msg)
    return created


def _create_or_skip(
    draft: IssueDraft,
    milestone: int,
    existing: dict[str, int],
    client: GhSurface,
    applied: AppliedBuildout,
) -> int:
    """Create *draft*, or reuse the issue a prior partial run already filed."""
    if draft.title in existing:
        applied.skipped.append(draft.code)
        return existing[draft.title]
    number = client.create_issue(
        draft.title, draft.body, labels=draft.labels, milestone=milestone
    )
    if number is None:
        msg = f"could not create issue: {draft.title}"
        raise SprintApplyError(msg, applied=applied)
    applied.created.append(draft.code)
    return number


def _resolve_epic_refs(ticket: IssueDraft, epic_numbers: dict[str, int]) -> IssueDraft:
    """Rewrite the body's ``Epic #<key>`` placeholder with the real issue number.

    build_plan renders ``Epic #I`` because the epic's number does not exist until
    apply creates it — GitHub assigns numbers at creation time.

    Uses a boundary-safe regex, not a plain substring replace: epic keys are
    Roman numerals, so ``"Epic #I"`` is a literal *prefix* of ``"Epic #II"``. A
    naive ``body.replace(f"Epic #{key}", ...)`` loop corrupts every Epic II
    ticket into ``"Epic #101I"`` once Epic I's replacement runs first — verified
    by execution. The ``(?!\\w)`` negative lookahead stops the match before it
    can swallow the next Roman-numeral character. An epic-less ticket's body
    has no ``Epic #`` substring at all, so this loop is a no-op for it.
    """
    body = ticket.body
    for key, number in epic_numbers.items():
        body = re.sub(rf"Epic #{re.escape(key)}(?!\w)", f"Epic #{number}", body)
    return ticket.model_copy(update={"body": body})


def _backfill_children(
    plan: BuildoutPlan, applied: AppliedBuildout, client: GhSurface
) -> None:
    """Replace each epic's marker with a checklist of its real child numbers."""
    for epic in plan.epics:
        checklist = "\n".join(
            f"- [ ] #{applied.ticket_numbers[code]} — {code}"
            for code in plan.epic_children.get(epic.code, [])
            if code in applied.ticket_numbers
        )
        body = epic.body.replace(plan.children_marker, checklist)
        if not client.update_issue_body(applied.epic_numbers[epic.code], body):
            msg = f"could not backfill children checklist on epic: {epic.title}"
            raise SprintApplyError(msg, applied=applied)


def apply_plan(
    plan: BuildoutPlan, *, client: GhSurface | None = None
) -> AppliedBuildout:
    """Execute the plan against GitHub. Idempotent; safe to re-run after a failure.

    The pass order is forced by GitHub assigning issue numbers at creation time:
    epics must exist before a child body can name its epic, and children must exist
    before they can appear in the epic's checklist. Hence: milestone → epics →
    tickets → backfill.
    """
    gh_client: GhSurface = client if client is not None else GhClient()
    milestone = _resolve_milestone(plan, gh_client)
    applied = AppliedBuildout(milestone_number=milestone)

    existing, ok = gh_client.milestone_issue_titles(milestone)
    if not ok:
        msg = f"could not list existing issues on milestone #{milestone}"
        raise SprintApplyError(msg, applied=applied)
    existing = existing or {}

    for epic in plan.epics:
        applied.epic_numbers[epic.code] = _create_or_skip(
            epic, milestone, existing, gh_client, applied
        )

    for ticket in plan.tickets:
        resolved = _resolve_epic_refs(ticket, applied.epic_numbers)
        applied.ticket_numbers[ticket.code] = _create_or_skip(
            resolved, milestone, existing, gh_client, applied
        )

    _backfill_children(plan, applied, gh_client)
    return applied
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_sprint.py -v`
Expected: PASS

- [ ] **Step 6: Run the gates and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ && uv run mypy --strict src/
git add src/cw/sprint.py src/cw/exceptions.py tests/test_sprint.py
git commit -m "feat(sprint): idempotent apply — milestone, epics, tickets, checklist backfill"
```

---

### Task 5: CLI — `cw sprint plan|apply`

**Files:**
- Create: `src/cw/cli/sprint.py`
- Modify: `src/cw/cli/__init__.py`
- Modify: `src/cw/gh.py`
- Modify: `tests/test_gh.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4; `cw.cli._base.main` and `handle_errors`
  (existing); `cw.gh.find_milestone`'s `(value, ok)` pattern (existing, Task 3)
  as the shape the new gh helper mirrors.
- Produces:
  - `cw.gh.latest_release_tag(*, timeout: int = _CREATE_TIMEOUT) -> tuple[str | None, bool]`
    — a policy-free `gh release view` wrapper, added to `src/cw/gh.py` so the
    CLI never shells out to `gh` directly (this repo's standing rule: reuse
    `cw.gh` for every `gh` subprocess call). Never logs, raises, or falls back
    to a default version — that decision belongs to the caller.
  - the `cw sprint` command group: `cw sprint plan <rfc-path> --out <file>`;
    `cw sprint apply <plan-file>`.

## Touch-point Contract

- Touch-point: `src/cw/cli/__init__.py`'s submodule import tuple and `__all__`
  File:line: `src/cw/cli/__init__.py:16-26` (import tuple), `:90-99` (`__all__` tail)
  Read: the real import tuple is `channels, config_cmds, guard, maintenance,
  queues, review, session_inspect, watchdog, worktree,` (line 16-26) — `review`
  IS present (registers the `cw review` group by import side-effect; dropping
  it breaks `tests/test_cli_review.py`). The real `__all__` tail (alphabetical
  over the whole list, not just submodule names) is `..., "review",
  "session_group", "session_inspect", "watchdog", "worktree",`.
  Plan asserts: `sprint` is inserted into BOTH lists in its true alphabetical
  slot — between `session_inspect` and `watchdog` in each, NOT between
  `review` and `session_inspect`. Verified directly:
  `sorted(["session_inspect", "sprint", "watchdog"])` orders
  `session_inspect` before `sprint` (comparing the two names character by
  character, `"session_inspect"[1] == "e"` sorts before `"sprint"[1] == "p"`,
  so `session_inspect < sprint`), and `sprint < watchdog` trivially (`s <
  w`). `review` is untouched — it stays exactly where it already is, in both
  lists.
  Match: CONFIRMED

- Touch-point: `src/cw/cli/_base.py`'s `handle_errors` boundary decorator
  File:line: `src/cw/cli/_base.py:40-50`
  Read:

```python
def handle_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Convert CwError exceptions to click.ClickException at the CLI boundary."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except CwError as e:
            raise click.ClickException(str(e)) from e

    return wrapper
```

  Plan asserts: both `sprint plan` and `sprint apply` rely on this existing
  boundary, not a bespoke one. `@handle_errors` on `sprint_plan` converts any
  `RfcContractError` raised inside `parse_rfc`/`load_buildout_config` (both
  `CwError` subclasses) into a clean `ClickException`, giving `sprint plan` a
  nonzero exit with the message on stdout —
  `test_sprint_plan_reports_a_contract_violation_as_a_clean_cli_error` asserts
  exactly this. `sprint_apply`'s inner `try/except SprintApplyError: ... raise`
  re-raises inside the SAME function body that `@handle_errors` wraps, so the
  re-raised `SprintApplyError` (a `CwError` subclass, per Task 4) still passes
  through `except CwError as e` in `wrapper` and becomes
  `ClickException(str(e))` — the nonzero-exit path is uniform whether the
  error surfaces before or after the partial-progress echo.
  Match: CONFIRMED

- Touch-point: `tests/test_sprint.py`'s exported fixtures and
  `tests/conftest.py`'s `_write_project_config_yaml`
  File:line: `tests/test_sprint.py:21` (`MINIMAL_RFC`), `:262` (`CONFIG_YAML`),
  `:295` (`_config()`); `tests/conftest.py:99-111`
  (`_write_project_config_yaml`)
  Read: `tests/test_sprint.py` imports config-writing via
  `from tests.conftest import _write_project_config_yaml` at its own top —
  it does NOT define, wrap, or re-export a `_write_config` name anywhere in
  the file. `tests/conftest.py:99` defines
  `def _write_project_config_yaml(root: Path, content: str) -> None:`, whose
  docstring states it is "the canonical version new tests should import
  instead of adding a fourth copy."
  Plan asserts: Task 5's cross-file test import is
  `from tests.test_sprint import CONFIG_YAML, MINIMAL_RFC, _config` plus a
  separate `from tests.conftest import _write_project_config_yaml` — call
  sites use `_write_project_config_yaml(...)`, never `_write_config(...)`
  (which does not exist anywhere in the test suite and would `ImportError` at
  collection time, failing every test in `tests/test_cli.py`, not just the
  sprint ones).
  Match: CONFIRMED

- [ ] **Step 1: Add `latest_release_tag` to `cw.gh`, with tests**

`_resolve_version` (Step 5 below) needs the latest release tag to derive the
next milestone version. Per this repo's standing rule — reuse `cw.gh` for
every `gh` subprocess call, never shell out from a CLI module directly — that
`gh` invocation belongs in `src/cw/gh.py`, as a new helper mirroring
`find_milestone`'s `(value, ok)` shape.

Add to `src/cw/gh.py`, next to the other milestone/release helpers:

```python
def latest_release_tag(*, timeout: int = _CREATE_TIMEOUT) -> tuple[str | None, bool]:
    """Return (tag, ok) for the repo's latest GitHub release.

    Mirrors ``find_milestone``'s ``(value, ok)`` shape: ``ok=False`` means the
    ``gh`` call itself failed (non-zero exit, OSError, timeout) — the caller
    cannot conclude there is no release, only that it could not check.
    Conflating the two would make a version-bump computation silently fall
    back to ``0.0.0`` after a transient ``gh`` failure, exactly the kind of
    trap ``find_milestone``'s docstring warns about for milestones.

    Policy-free by design: never logs, raises, or falls back to a default
    version. That decision belongs to the caller
    (``cw.cli.sprint._resolve_version``), which is the one place that knows
    what a missing tag should become.
    """
    try:
        result = _sp.run(
            ["gh", "release", "view", "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    tag = result.stdout.decode("utf-8", "replace").strip()
    if not tag:
        return None, False
    return tag, True
```

Append to `tests/test_gh.py`, following the existing `TestFindMilestone`
class-per-helper, `monkeypatch.setattr(gh._sp, "run", fake_run)` pattern
(see `tests/test_gh.py:1233` on `origin/main`):

```python
class TestLatestReleaseTag:
    """Tests for latest_release_tag."""

    def test_returns_the_tag_and_ok_true_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 0, b"v1.4.0\n", b"")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.latest_release_tag() == ("v1.4.0", True)

    def test_returns_none_false_when_the_gh_call_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(cmd, 1, b"", b"gh: no releases found")

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.latest_release_tag() == (None, False)

    def test_returns_none_false_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(gh._sp, "run", fake_run)
        assert gh.latest_release_tag() == (None, False)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_gh.py -v -k LatestReleaseTag`
Expected: PASS

- [ ] **Step 3: Write the failing CLI tests**

Append to `tests/test_cli.py`, following its existing `CliRunner` pattern. Reuse
the sprint fixtures rather than duplicating them — `tests` is a package and
cross-file test imports are already used here (see
`tests/test_reconcile_review_recipes.py`, which imports from `tests.test_pr_hydrate`):

```python
# Merge into tests/test_cli.py's EXISTING top-of-file import block — do NOT
# append these as new lines near the test functions below (the file already
# has ~9000 lines of class/function code by this point, so a bare
# module-level import statement placed there is ruff's E402; and even
# hoisted-but-unsorted at the top, I001). The existing header is:
#     from cw.cli import (
#         _complete_client,
#         _complete_session,
#         _configure_logging,
#         _display_sessions,
#         _display_status,
#         main,
#     )
#     from cw.config import load_clients, load_state, save_state
#     from cw.events import read_events
#     from cw.exceptions import CwError
#     from cw.models import (
#         ClientConfig,
#         CwState,
#         OrchestratorEventType,
#         Session,
#         SessionOrigin,
#         SessionPurpose,
#         SessionStatus,
#         Stage,
#         TicketTask,
#     )
#     from tests.test_result import _valid_payload
# `_resolve_version` needs `cw.cli.sprint` imported by name; `SprintApplyError`
# merges into the existing `cw.exceptions` import (do not add a second
# `from cw.exceptions import` line — merge into one, alphabetized:
# `CwError, SprintApplyError`); `AppliedBuildout`/`BuildoutPlan`/`build_plan`/
# `parse_rfc` are new from `cw.sprint`; `_write_project_config_yaml` is new
# from `tests.conftest`; `CONFIG_YAML`/`MINIMAL_RFC`/`_config` are new from
# `tests.test_sprint`. Resulting first-party block, alphabetized in full
# (ruff's isort treats both `src` and `tests` as first-party per this repo's
# `pyproject.toml`, so `cw.*` and `tests.*` sort together):
#     from cw.cli import (
#         _complete_client,
#         _complete_session,
#         _configure_logging,
#         _display_sessions,
#         _display_status,
#         main,
#     )
#     from cw.cli.sprint import _resolve_version
#     from cw.config import load_clients, load_state, save_state
#     from cw.events import read_events
#     from cw.exceptions import CwError, SprintApplyError
#     from cw.models import (
#         ClientConfig,
#         CwState,
#         OrchestratorEventType,
#         Session,
#         SessionOrigin,
#         SessionPurpose,
#         SessionStatus,
#         Stage,
#         TicketTask,
#     )
#     from cw.sprint import AppliedBuildout, BuildoutPlan, build_plan, parse_rfc
#     from tests.conftest import _write_project_config_yaml
#     from tests.test_result import _valid_payload
#     from tests.test_sprint import CONFIG_YAML, MINIMAL_RFC, _config
# Only the new test functions below are actually appended near the bottom of
# the file — no import statements down there.


def test_sprint_plan_writes_a_plan_file_and_prints_a_summary(
    tmp_path, monkeypatch
) -> None:
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text(MINIMAL_RFC, encoding="utf-8")
    _write_project_config_yaml(tmp_path, CONFIG_YAML)
    monkeypatch.setattr("cw.cli.sprint._resolve_version", lambda _root: "1.20.0")

    out = tmp_path / "plan.json"
    result = CliRunner().invoke(
        main,
        [
            "sprint",
            "plan",
            "docs/rfcs/0011-x.md",
            "--out",
            str(out),
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "2 tickets" in result.output
    assert "v1.20.0 — Availability- & Counterparty-Aware Holding" in result.output
    plan = BuildoutPlan.model_validate_json(out.read_text(encoding="utf-8"))
    assert [t.code for t in plan.tickets] == ["S1", "A1"]


def test_sprint_plan_reports_a_contract_violation_as_a_clean_cli_error(
    tmp_path, monkeypatch
) -> None:
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text(MINIMAL_RFC.replace("## Tickets", "## Nope"), encoding="utf-8")
    _write_project_config_yaml(tmp_path, CONFIG_YAML)

    result = CliRunner().invoke(
        main,
        [
            "sprint",
            "plan",
            "docs/rfcs/0011-x.md",
            "--out",
            str(tmp_path / "p.json"),
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "missing section: ## Tickets" in result.output


def test_sprint_plan_version_override_wins_over_resolve_version(
    tmp_path, monkeypatch
) -> None:
    """--version must short-circuit before _resolve_version is ever called —
    ``version_override or _resolve_version(root)`` only evaluates the second
    operand when the first is falsy, so a real gh subprocess call never
    happens when an operator supplies an explicit version."""
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text(MINIMAL_RFC, encoding="utf-8")
    _write_project_config_yaml(tmp_path, CONFIG_YAML)

    out = tmp_path / "plan.json"
    result = CliRunner().invoke(
        main,
        [
            "sprint",
            "plan",
            "docs/rfcs/0011-x.md",
            "--out",
            str(out),
            "--root",
            str(tmp_path),
            "--version",
            "9.9.9",
        ],
    )

    assert result.exit_code == 0
    assert "v9.9.9" in result.output


def test_resolve_version_minor_bumps_the_latest_release_tag(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "cw.cli.sprint.latest_release_tag", lambda **_: ("v1.4.0", True)
    )
    assert _resolve_version(tmp_path) == "1.5.0"


def test_resolve_version_falls_back_when_the_gh_call_fails(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("cw.cli.sprint.latest_release_tag", lambda **_: (None, False))
    assert _resolve_version(tmp_path) == "0.0.0"


def test_resolve_version_falls_back_on_a_malformed_tag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "cw.cli.sprint.latest_release_tag", lambda **_: ("not-a-version", True)
    )
    assert _resolve_version(tmp_path) == "0.0.0"


def test_sprint_apply_dry_run_makes_no_gh_calls(tmp_path, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        "cw.gh.create_milestone", lambda *_a, **_k: called.append("boom")
    )

    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(main, ["sprint", "apply", str(plan_file), "--dry-run"])

    assert result.exit_code == 0
    assert called == []
    assert "would create" in result.output.lower()


def test_sprint_apply_prints_partial_progress_on_a_mid_run_failure(
    tmp_path, monkeypatch
) -> None:
    def fake_apply_plan(plan: BuildoutPlan) -> AppliedBuildout:
        msg = "could not backfill children checklist on epic: epic: x"
        raise SprintApplyError(
            msg,
            applied=AppliedBuildout(milestone_number=11, ticket_numbers={"S1": 101}),
        )

    monkeypatch.setattr("cw.cli.sprint.apply_plan", fake_apply_plan)

    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(main, ["sprint", "apply", str(plan_file)])

    assert result.exit_code != 0
    assert "Partial progress before failure" in result.output
    assert "S1: #101" in result.output


def test_sprint_apply_error_without_partial_state_prints_no_partial_banner(
    tmp_path, monkeypatch
) -> None:
    """SprintApplyError.applied defaults to None — e.g. a failure before the
    milestone itself could even be created/found. isinstance(None,
    AppliedBuildout) is False, so the "Partial progress" banner must not
    print; there is nothing partial to report."""

    def fake_apply_plan(plan: BuildoutPlan) -> AppliedBuildout:
        msg = "could not create milestone: boom"
        raise SprintApplyError(msg, applied=None)

    monkeypatch.setattr("cw.cli.sprint.apply_plan", fake_apply_plan)

    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(main, ["sprint", "apply", str(plan_file)])

    assert result.exit_code != 0
    assert "Partial progress" not in result.output


def test_sprint_apply_prints_the_created_issue_numbers_on_success(
    tmp_path, monkeypatch
) -> None:
    def fake_apply_plan(plan: BuildoutPlan) -> AppliedBuildout:
        return AppliedBuildout(
            milestone_number=11,
            epic_numbers={"I": 100},
            ticket_numbers={"S1": 101},
            created=["I", "S1"],
            skipped=[],
        )

    monkeypatch.setattr("cw.cli.sprint.apply_plan", fake_apply_plan)

    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(main, ["sprint", "apply", str(plan_file)])

    assert result.exit_code == 0
    assert "Milestone #11" in result.output
    assert "S1: #101" in result.output
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k sprint`
Expected: FAIL — `Error: No such command 'sprint'` (and `ImportError` on
`cw.cli.sprint`, which does not exist yet)

- [ ] **Step 5: Implement the CLI**

Create `src/cw/cli/sprint.py`:

```python
"""``cw sprint`` — RFC → ticketed sprint block (see /sprint-buildout)."""

from __future__ import annotations

from pathlib import Path

import click

from cw.cli._base import handle_errors, main
from cw.exceptions import SprintApplyError
from cw.gh import latest_release_tag
from cw.sprint import (
    AppliedBuildout,
    BuildoutPlan,
    apply_plan,
    build_plan,
    load_buildout_config,
    load_rfc_text,
    parse_rfc,
)

_VERSION_TIMEOUT = 10
_FALLBACK_VERSION = "0.0.0"
_SEMVER_PARTS = 3  # major.minor.patch — ruff PLR2004 forbids the bare `3`


def _resolve_version(_root: Path) -> str:
    """Derive the milestone version from the latest release tag, minor-bumped.

    ``_root`` is accepted but unused: it keeps this a drop-in replacement for
    ``sprint_plan``'s ``root`` argument, but ``cw.gh.latest_release_tag`` —
    like every other ``cw.gh`` helper — never threads a working directory
    through. ``gh`` targets the repo from the process's own cwd; there is
    nothing left for a repo root to do here once the subprocess call lives in
    ``cw.gh`` instead of being shelled out to directly from this module.
    """
    tag, ok = latest_release_tag(timeout=_VERSION_TIMEOUT)
    if not ok or tag is None:
        return _FALLBACK_VERSION
    tag = tag.lstrip("v")
    parts = tag.split(".")
    if len(parts) != _SEMVER_PARTS or not all(p.isdigit() for p in parts):
        return _FALLBACK_VERSION
    return f"{parts[0]}.{int(parts[1]) + 1}.0"


@main.group()
def sprint() -> None:
    """Build a ticketed sprint block from an RFC."""


@sprint.command("plan")
@click.argument("rfc_path")
@click.option(
    "--out",
    required=True,
    type=click.Path(path_type=Path),
    help="Where to write plan JSON.",
)
@click.option("--root", default=".", type=click.Path(path_type=Path), help="Repo root.")
@click.option(
    "--version",
    "version_override",
    default=None,
    help="Override the milestone version.",
)
@handle_errors
def sprint_plan(
    rfc_path: str, out: Path, root: Path, version_override: str | None
) -> None:
    """Parse RFC_PATH and draft every issue. Makes no changes to GitHub."""
    cfg = load_buildout_config(root)
    doc = parse_rfc(load_rfc_text(rfc_path, root))
    version = version_override or _resolve_version(root)
    plan = build_plan(doc, cfg, version=version)
    out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    click.echo(f"Milestone: {plan.milestone_title}")
    click.echo(f"{len(plan.epics)} epics, {len(plan.tickets)} tickets")
    for sprint_num in sorted(plan.sprint_map):
        codes = ", ".join(plan.sprint_map[sprint_num])
        click.echo(f"  Sprint {sprint_num}: {codes}")
    click.echo(f"\nPlan written to {out}. Review it, then: cw sprint apply {out}")


def _echo_applied(applied: AppliedBuildout) -> None:
    for code, number in applied.epic_numbers.items():
        click.echo(f"  epic {code}: #{number}")
    for code, number in applied.ticket_numbers.items():
        click.echo(f"  {code}: #{number}")
    if applied.skipped:
        click.echo(f"Skipped (already existed): {', '.join(applied.skipped)}")


@sprint.command("apply")
@click.argument("plan_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--dry-run", is_flag=True, help="Print what would be created; touch nothing."
)
@handle_errors
def sprint_apply(plan_file: Path, dry_run: bool) -> None:
    """Execute PLAN_FILE against GitHub. Idempotent — safe to re-run."""
    plan = BuildoutPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))

    if dry_run:
        click.echo(f"Would create milestone: {plan.milestone_title}")
        for draft in [*plan.epics, *plan.tickets]:
            click.echo(f"  would create {draft.kind}: {draft.title}")
        return

    try:
        applied = apply_plan(plan)
    except SprintApplyError as exc:
        # A mid-run failure still leaves useful state behind (what got created
        # before the gh call that failed) — surface it before handle_errors
        # converts the exception to a plain ClickException(str(exc)).
        if isinstance(exc.applied, AppliedBuildout):
            click.echo("Partial progress before failure:")
            _echo_applied(exc.applied)
        raise

    click.echo(f"Milestone #{applied.milestone_number}")
    _echo_applied(applied)
```

- [ ] **Step 6: Register the group**

In `src/cw/cli/__init__.py`, add `sprint` to the submodule import block (imported
for its registration side effect) and to `__all__`, keeping both alphabetical.
`review` stays exactly where it already is — `sprint` slots in between
`session_inspect` and `watchdog` (see the Touch-point Contract above for why
that slot, not the one adjacent to `review`):

```python
from cw.cli import (
    channels,
    config_cmds,
    guard,
    maintenance,
    queues,
    review,
    session_inspect,
    sprint,
    watchdog,
    worktree,
)
```

And in the same file's `__all__` list, insert `"sprint",` between
`"session_inspect",` and `"watchdog",` (excerpt — the tail of the existing
list, not the whole thing):

```text
    "review",
    "session_group",
    "session_inspect",
    "sprint",
    "watchdog",
    "worktree",
]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v -k sprint && uv run cw sprint --help`
Expected: PASS; help lists `plan` and `apply`.

- [ ] **Step 8: Run the gates and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ && uv run mypy --strict src/
git add src/cw/cli/sprint.py src/cw/cli/__init__.py src/cw/gh.py tests/test_gh.py tests/test_cli.py
git commit -m "feat(cli): cw sprint plan|apply"
```

---

### Task 6: End-to-end acceptance against the real RFC 0011 buildout

**Files:**
- Create: `tests/fixtures/rfc-0011-tickets.md`
- Modify: `tests/test_sprint.py`

**Interfaces:**
- Consumes: `parse_rfc`, `build_plan` (Tasks 1–2).
- Produces: nothing new — this is the acceptance bar from the spec.

This is the task that proves the design. The RFC 0011 buildout really produced
2 epics and 13 children; if `cw sprint plan` cannot reproduce that from a
conforming RFC, either the template or the parser is wrong, and we want to know
before the next RFC — not during it.

## Touch-point Contract

- Touch-point: `build_plan`'s `sprint_map`/`epic_children` construction — document
  order, not sorted
  File:line: `src/cw/sprint.py:528-531`
  Read: `for ticket in doc.tickets:` (528) / `    sprint_map.setdefault(ticket.sprint, []).append(ticket.code)` (529) / `    if ticket.epic is not None:` (530) / `        epic_children.setdefault(ticket.epic, []).append(ticket.code)` (531) —
  a plain `.setdefault(...).append(...)` over `doc.tickets` in the order `parse_rfc`
  produced them (document order — `_split_ticket_blocks`'s docstring: "Return
  [(code, name, block_body)] in document order"). Nothing sorts `sprint_map`'s
  per-key lists.
  Plan asserts: THIS IS LOAD-BEARING for the fixture. The acceptance test (Step 2)
  asserts `plan.sprint_map[0] == ["S1", "S2"]` — an *unsorted* equality check, not
  `sorted(...)`. That only passes if the fixture's `### S1` ticket block precedes
  its `### S2` block in `## Tickets`. (`sprint_map[1]` and `sprint_map[2]` are
  asserted via `sorted(...)`, so intra-sprint ordering doesn't matter there —
  only the Sprint-0 pair is order-sensitive, because it's the only sprint
  asserted unsorted.)
  Match: CONFIRMED

- Touch-point: `parse_rfc`'s required-sections and required-per-ticket-fields
  contract
  File:line: `src/cw/sprint.py:45-59`
  Read: `_REQUIRED_SECTIONS = (` (45) `_SEC_DESIGN, _SEC_TICKETS, _SEC_RESOLVED_DECISIONS, _SEC_REFERENCES,` (46-49) `)` (50) — the fixture must contain all four `##` sections (`## Design`, `## Tickets`, `## Resolved decisions`, `## References`; `_section()`'s prefix-tolerant match, `sprint.py:126-130`, accepts the real RFC's annotated heading `## Resolved decisions (hardening pass, operator, 2026-07-12)` verbatim). `_REQUIRED_TICKET_FIELDS = (` (51) `"Epic", "Wave", "Sprint", "Depends on", "Context", "Scope", "Acceptance",` (52-58) `)` (59) — every one of the 13 ticket blocks must carry all seven `- **Field:** value` bullets or `_ticket_fields` raises `RfcContractError: ticket {code}: missing field: {required}`.
  Plan asserts: the fixture must not omit any required section or any required
  per-ticket field, even for tickets whose `Depends on`/`Scope` value is the
  literal `none` (see `_NONE_VALUES`, `sprint.py:59` — `"none"`, `"-"`, `"—"`,
  `""` are the only accepted null spellings; the field itself must still be
  present).
  Match: CONFIRMED

- Touch-point: milestone-title format — test fixture vs. real project config
  File:line: `tests/test_sprint.py:295-312` (`_config()`) vs. `.claude/project-config.yaml:58-69` (`sprint_buildout:` block)
  Read: `_config()`'s `"milestone": {"title_pattern": "v{version} — {rfc_title}"}` (test_sprint.py:301) is byte-for-byte identical to `.claude/project-config.yaml:60`'s `title_pattern: "v{version} — {rfc_title}"`; likewise `_config()`'s `epic`/`ticket` blocks (test_sprint.py:302-311) match `project-config.yaml:61-69` field-for-field (the `notion:` block at `project-config.yaml:70-76` has no counterpart in `_config()` — `BuildoutConfig.notion` is optional and `_config()` omits it deliberately).
  Plan asserts: `_config()` is not an arbitrary test fixture — it is the real
  `sprint_buildout` config transcribed, so `plan.milestone_title` in the Step 2
  acceptance test resolves exactly as `cw sprint plan` would against the live
  config: `"v1.20.0 — Availability- & Counterparty-Aware Holding"`. The
  `{rfc_title}` half is the real RFC 0011 title line 1 verbatim
  (`docs/rfcs/0011-availability-and-counterparty-aware-holding.md:1` —
  `# RFC 0011 — Availability- & Counterparty-Aware Holding`), so the fixture's
  own `# RFC 0011 — ...` line must reproduce that title exactly or the assertion
  fails on the `{rfc_title}` substitution, not on anything sprint-map-related.
  Match: CONFIRMED

- [ ] **Step 1: Write the fixture**

Create `tests/fixtures/rfc-0011-tickets.md`: RFC 0011's real content
(`docs/rfcs/0011-availability-and-counterparty-aware-holding.md` — tracked, read
it directly for the `## Summary`/`## Design`/`## Resolved constraints`/
`## Explicitly out of scope` prose) with a `## Tickets` section back-filled to
describe what that session actually filed, since the real RFC predates the
`## Tickets` contract and doesn't have one yet.

**Provenance:** the codes/epics/waves/dependencies below are transcribed from
RFC 0011's own `## Phasing` table
(`docs/rfcs/0011-availability-and-counterparty-aware-holding.md:317-321`) and
`## Resolved decisions` section (same file, lines 326-357) — both tracked and
on `origin/main`. A local handoff document
(`.handoffs/handoff-sprint-kickoff-2026-07-13-1306.md`) also records this
buildout, but **it is `.gitignore`d and does not exist on a fresh worktree — it
is local-only provenance, not required reading.** Everything the worker needs
is inlined in the table below; do not `Read` the handoff path.

**Fixture ordering requirement (load-bearing — see Touch-point Contract):** the
`### S1` ticket block MUST appear before the `### S2` ticket block in the
fixture's `## Tickets` section. `sprint_map[0]` is asserted unsorted
(`["S1", "S2"]`); document order is the only thing that produces that order.
All other tickets may appear in any order — their sprints are asserted via
`sorted(...)`.

**Wave == Sprint for every ticket in this RFC.** RFC 0011's `## Phasing` table
has a single "Wave" axis (0/1/2) that both epics share; every ticket's `Wave:`
field value equals its `Sprint:` field value. Do not treat these as
independently-chosen numbers — for this RFC they are always the same integer.

**Per-ticket data** (13 tickets, 2 epics — all fields required by
`_REQUIRED_TICKET_FIELDS`; `Depends on` and `Scope` use the literal value
`none` where there is nothing to cite, per `_NONE_VALUES`):

| Code | Name | Epic | Wave | Sprint | Depends on | Scope |
|------|------|------|------|--------|------------|-------|
| S1 | counterparty axis + self-identity | none | 0 | 0 | none | D-S1 |
| S2 | native review-request register | none | 0 | 0 | S1 | D-S2a, D-S2b |
| A1 | park class (keystone) | I | 1 | 1 | S1 | D-A1 |
| A2 | unavailability detector | I | 1 | 1 | A1 | D-A2 |
| A5 | availability preflight probe | I | 1 | 1 | A1 | D-A5 |
| A3 | stop-before-finalize hold | I | 2 | 2 | none | D-A3 |
| A4 | auto-resume-on-return | I | 2 | 2 | none | none |
| A6 | digest / batch on attention channel | I | 2 | 2 | none | D-A6 |
| B1 | teammate-review idle-reap exemption | II | 1 | 1 | S1 | none |
| B2 | two-party consent gate | II | 1 | 1 | S1 | none |
| B3 | individual re-request gate | II | 2 | 2 | none | none |
| B4 | response contract | II | 2 | 2 | none | D-B4 |
| B5 | graceful rejection | II | 2 | 2 | none | none |

Epics (`### Epic <key> — <name>` under `## Design`):

| Key | Name |
|-----|------|
| I | Availability-aware holding (inward) |
| II | Counterparty-aware collaboration (outward) |

This `Scope:` mapping was cross-checked against RFC 0011's actual
`## Resolved decisions` list (D-S1, D-S2a, D-S2b, D-A1, D-A2, D-A3, D-A5, D-A6,
D-B4 — nine decisions, no more, no fewer): every decision id is cited by
exactly one ticket above, and the five tickets with no matching decision
(A4, B1, B2, B3, B5) cite `none`. If the RFC's decisions list is ever amended,
re-verify this table against it before trusting the fixture.

Each ticket's `Context:` and `Acceptance:` bullets come from the corresponding
RFC prose in `## Design` (the `#### <Code> — ...` subsection for that ticket,
or the `### S1`/`### S2` prose for the two seam tickets) — write them from that
tracked source, not from the handoff.

- [ ] **Step 2: Write the failing test**

```python
FIXTURES = Path(__file__).parent / "fixtures"


def test_rfc_0011_fixture_reproduces_the_real_buildout() -> None:
    """The acceptance bar: a conforming RFC 0011 yields the block that was filed."""
    doc = parse_rfc((FIXTURES / "rfc-0011-tickets.md").read_text(encoding="utf-8"))
    plan = build_plan(doc, _config(), version="1.20.0")

    assert plan.milestone_title == (
        "v1.20.0 — Availability- & Counterparty-Aware Holding"
    )
    assert [e.code for e in plan.epics] == ["I", "II"]
    assert len(plan.tickets) == 13

    assert plan.sprint_map[0] == ["S1", "S2"]
    assert sorted(plan.sprint_map[1]) == ["A1", "A2", "A5", "B1", "B2"]
    assert sorted(plan.sprint_map[2]) == ["A3", "A4", "A6", "B3", "B4", "B5"]

    assert sorted(plan.epic_children["I"]) == ["A1", "A2", "A3", "A4", "A5", "A6"]
    assert sorted(plan.epic_children["II"]) == ["B1", "B2", "B3", "B4", "B5"]

    # The dependency the RFC's Phasing table calls out: S2 rides S1.
    s2 = next(t for t in doc.tickets if t.code == "S2")
    assert s2.depends_on == ["S1"]
```

- [ ] **Step 3: Run the test**

Run: `uv run pytest tests/test_sprint.py::test_rfc_0011_fixture_reproduces_the_real_buildout -v`
Expected: initially FAIL (fixture incomplete), then PASS once the fixture is
faithful. **If it cannot be made to pass without changing the parser, stop and
report** — that means the template is missing something the RFC genuinely needs,
which is a design finding, not an implementation bug.

- [ ] **Step 4: Run all seven CI gates and commit**

Per this plan's Done criteria ("all seven CI gates pass"), run all seven, in the
order `CLAUDE.md`'s Quality Gates section specifies — not just the first three
plus coverage:

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy --strict src/
uv run pre-commit run --all-files
uv run --extra mcp pytest tests/ -m 'not integration' --cov=cw --cov-report=xml --cov-fail-under=88
uv run pytest tests/ -m integration
uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90
git add tests/fixtures/rfc-0011-tickets.md tests/test_sprint.py
git commit -m "test(sprint): acceptance — RFC 0011 fixture reproduces the real buildout"
```

---

### Task 7: The skill — `/sprint-buildout`

**Files:**
- Create: `.claude/skills/sprint-buildout/SKILL.md`

**Interfaces:**
- Consumes: `cw sprint plan|apply` (Task 5).
- Produces: the `/sprint-buildout` skill. `scripts/install-skills.sh` syncs every
  directory under `.claude/skills/` to `~/.claude/skills/` with a manifest-scoped
  prune, so no install plumbing is needed.

- [ ] **Step 1: Write `SKILL.md`**

Frontmatter `name: sprint-buildout` and a `description:` that triggers on the real
phrasings: "turn this RFC into tickets", "build out the sprint", "ticket up RFC
NNNN", "break this RFC into sprints".

The body specifies the pipeline. Keep the division of labor explicit, because it
is the whole point of the design:

1. **`cw sprint plan <rfc-path> --out <scratch>/plan.json`.** If it refuses
   (`missing section: ## Tickets`), do **not** work around it — report the defect
   and offer to fix the RFC against `docs/rfcs/TEMPLATE.md`. Silently inferring
   what the RFC failed to say is the exact failure this design exists to prevent.
2. **Adjacent-bug scan.** Spawn one `model: sonnet` subagent: given the RFC's
   `## References` file:line refs, list open bugs whose touched paths overlap.
   It returns candidates plus the overlap evidence — not file dumps. (The RFC 0011
   session pulled in #1149 this way: both its fixes touched the reap decision path
   that A1/B1 build on.)
3. **The single operator gate.** Present, in one message: the milestone title, the
   sprint→ticket map, every issue title, and the pull-in candidates with their
   evidence and your recommendation. Offer to show any body on request. Then wait.
   Do not create anything before the operator approves. `gh issue create` has no
   draft mode — a typo ships instantly and 15 issues is a lot to clean up.
4. **`cw sprint apply <scratch>/plan.json`.** Report the real numbers.
5. **Pulled-in bugs** the operator accepted: set the milestone and post a rationale
   comment naming the shared code path.
6. **Notion phase** (only if `sprint_buildout.notion` is configured; otherwise skip
   silently and say so). One page per sprint in the configured data source, with
   the configured properties. The page's Goal / risk-annotated ticket list / exit
   bar / dependency chain is prose you write; the skeleton and properties come from
   config.
7. **RFC footer PR.** Back-fill the RFC's `Issues:` footer with the real issue
   numbers from step 4. No pipeline step returns a milestone URL — construct it
   yourself from the milestone number `cw sprint apply` reported and the repo's
   `owner/repo` (`gh repo view --json nameWithOwner --jq .nameWithOwner`):
   `https://github.com/<owner>/<repo>/milestone/<number>`. Open a docs-only PR.

Also state what the skill must **not** do: decide the wave→sprint granularity (the
RFC's `Sprint:` fields decide it), decide the pull-in (the operator decides), or
resolve anything the RFC defers to ticket-hardening (that is `/harden-ticket`'s job
at dispatch time, not buildout's).

- [ ] **Step 2: Verify it installs**

Run: `./scripts/install-skills.sh && ls ~/.claude/skills/sprint-buildout/`
Expected: `SKILL.md` present; the summary line reports one more skill dir synced
and zero orphans pruned.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sprint-buildout/SKILL.md
git commit -m "feat(skill): /sprint-buildout — RFC to ticketed sprint block"
```

---

## Done criteria

- `cw sprint plan` on the RFC 0011 fixture reproduces 2 epics + 13 children with
  the correct sprint map, epic membership, and dependency edges.
- `cw sprint plan` refuses every malformed RFC with a message naming the exact
  defect.
- `cw sprint apply` is idempotent: re-running after a partial failure skips what
  exists rather than duplicating it.
- All seven CI gates pass, including patch coverage ≥90%.
- `/sprint-buildout` is installed and its one operator gate precedes every GitHub
  mutation.

## Follow-on (explicitly not in this plan)

`/sprint-advance` — the sprint *boundary* (close milestone N, roll unfinished
tickets to N+1, flip Notion status, spin up the next wave). At a ~2-day cadence
this is the higher-frequency repetition, but no session has been retro'd yet, so
specifying it now would be speculation. Run one boundary by hand, retro it, then
spec it. This plan leaves it room: the config block, the `cw.gh` creation helpers,
and the Notion IDs are all shared surface it will want.
