# sprint-buildout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an RFC into a fully-ticketed sprint block (milestone + epics + children + Notion pages + RFC footer PR) via `cw sprint plan|apply` behind a single operator confirmation, replacing a ~1-hour hand-driven pipeline that re-derives its conventions from prior issues every time.

**Architecture:** Three layers split on one rule — *the code owns anything with a shell-quoting hazard or an ordering constraint; the skill owns anything requiring a judgment call.* `docs/rfcs/TEMPLATE.md` is a strict input contract (a `## Tickets` section is the sole parse source, so buildout is transcription with zero inference). `src/cw/sprint.py` parses + builds a plan (pure, unit-testable) and applies it (the three-pass `gh` dance GitHub's number assignment forces). `.claude/skills/sprint-buildout/SKILL.md` drives the pipeline, runs the adjacent-bug scan, and owns the one operator gate.

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
inference.

````markdown
# RFC NNNN — <Title>

## Summary

<Two or three paragraphs. What changes and why.>

## Motivation

<The problem. Evidence, not assertion.>

## Design

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
that belong to no epic. Buildout transcribes these verbatim — it infers nothing.>

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

import pytest

from cw.exceptions import RfcContractError
from cw.sprint import parse_rfc

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
    ["## Tickets", "## Resolved decisions", "## References"],
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

_REQUIRED_SECTIONS = ("## Tickets", "## Resolved decisions", "## References")
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
    back to the working tree when the path is not on origin/main (a brand-new,
    unmerged RFC).
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
    """Return the body of a ``## Heading`` section, or raise RfcContractError."""
    pattern = re.compile(
        rf"^{re.escape(heading)}\s*$(?P<body>.*?)(?=^##\s|\Z)", re.M | re.S
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


def _parse_ticket(code: str, name: str, block: str) -> TicketSpec:
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
    matches = list(_EPIC_RE.finditer(text))
    epics: list[EpicSpec] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        intent = text[match.end() : end]
        # Stop the intent at the next ## section so it never swallows ## Phasing.
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
        epics=_parse_epics(text),
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
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint}), Epic #{epic}"
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
from pathlib import Path

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
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint}), Epic #{epic}"
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
                "footer_pattern": (
                    "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint}), Epic #{epic}"
                ),
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
        epic=ticket.epic or "—",
    )
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

Note: the ticket footer's `Epic #{epic}` renders the epic *key* (`I`) at plan
time, not an issue number — the number does not exist until `apply` creates the
epic. `apply` rewrites it via `_resolve_epic_refs` in Task 4.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_sprint.py -v`
Expected: PASS

- [ ] **Step 6: Document the config block**

Add a "Sprint Buildout Config" section to `config/CONFIG_REFERENCE.md`, matching
the style of the existing "Review Strategy Config" section: show the full block,
state that every key is required except `notion:`, and state the consequence of
omitting `notion:` (the skill's Notion phase silently skips). Say plainly that
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
  - `cw.gh.find_milestone(title: str, *, timeout: int = ...) -> int | None`
  - `cw.gh.create_issue(title: str, body: str, *, labels: list[str], milestone: int, timeout: int = ...) -> int | None`
  - `cw.gh.update_issue_body(number: int, body: str, *, timeout: int = ...) -> bool`
  - `cw.gh.milestone_issue_titles(milestone: int, *, timeout: int = ...) -> dict[str, int] | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gh.py`, following that file's existing monkeypatch-of-`_sp.run` pattern:

```python
def test_create_issue_writes_the_body_to_a_temp_file_not_the_argv(monkeypatch) -> None:
    """Bodies never ride argv: RFC titles carry em-dashes and ampersands."""
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen["cmd"] = cmd
        body_file = Path(cmd[cmd.index("--body-file") + 1])
        seen["body"] = body_file.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, b"https://github.com/o/r/issues/42\n", b"")

    monkeypatch.setattr(gh._sp, "run", fake_run)
    number = gh.create_issue(
        "RFC 0011 A1 — park class", "## Context\n\nBody & more.", labels=["feature"], milestone=11
    )

    assert number == 42
    assert "--body" not in seen["cmd"]  # only --body-file
    assert seen["body"] == "## Context\n\nBody & more."


def test_create_issue_returns_none_when_gh_fails(monkeypatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 1, b"", b"gh: not authenticated")

    monkeypatch.setattr(gh._sp, "run", fake_run)
    assert gh.create_issue("t", "b", labels=[], milestone=1) is None


def test_create_milestone_returns_the_number(monkeypatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, b'{"number": 11}', b"")

    monkeypatch.setattr(gh._sp, "run", fake_run)
    assert gh.create_milestone("v1.20.0 — Availability & Counterparty") == 11


def test_milestone_issue_titles_maps_title_to_number(monkeypatch) -> None:
    payload = b'[{"number": 1155, "title": "RFC 0011 A1 — park class"}]'

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, payload, b"")

    monkeypatch.setattr(gh._sp, "run", fake_run)
    assert gh.milestone_issue_titles(11) == {"RFC 0011 A1 — park class": 1155}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gh.py -v -k "create or milestone"`
Expected: FAIL — `AttributeError: module 'cw.gh' has no attribute 'create_issue'`

- [ ] **Step 3: Implement the helpers**

Append to `src/cw/gh.py`. Match the file's established shape: module-level timeout
constants, `_sp.run(..., capture_output=True, check=False)`, `except (OSError,
_sp.TimeoutExpired)` → `None`.

```python
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

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
    """Create an issue via ``gh issue create``; return its number, or None on failure."""
    cmd = ["gh", "issue", "create", "--title", title, "--milestone", str(milestone)]
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
    return int(match.group(1)) if match else None


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


def find_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> int | None:
    """Return the number of an existing open milestone titled *title*, else None."""
    try:
        result = _sp.run(
            ["gh", "api", "repos/{owner}/{repo}/milestones", "--jq", ".[] | {number, title}"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("title") == title and isinstance(entry.get("number"), int):
            number: int = entry["number"]
            return number
    return None


def milestone_issue_titles(milestone: int, *, timeout: int = _CREATE_TIMEOUT) -> dict[str, int] | None:
    """Return {issue title: number} for every issue on *milestone*, or None on failure.

    This is what makes ``cw sprint apply`` idempotent: a re-run after a partial
    failure skips issues that already exist rather than filing duplicates.
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
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return {
        str(item["title"]): int(item["number"])
        for item in payload
        if isinstance(item, dict) and "title" in item and "number" in item
    }
```

Add `import re` to `gh.py`'s imports if absent.

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

### Task 4: Apply — the three-pass dance, idempotent

**Files:**
- Modify: `src/cw/sprint.py`
- Modify: `tests/test_sprint.py`

**Interfaces:**
- Consumes: `cw.sprint.BuildoutPlan` (Task 2); every helper from Task 3.
- Produces:
  - `cw.sprint.GhSurface` (Protocol) and `cw.sprint.GhClient` (its default `cw.gh`-backed impl)
  - `cw.sprint.AppliedBuildout` (Pydantic model: `milestone_number: int`, `epic_numbers: dict[str, int]`, `ticket_numbers: dict[str, int]`, `created: list[str]`, `skipped: list[str]`)
  - `cw.sprint.apply_plan(plan: BuildoutPlan, *, client: GhSurface | None = None) -> AppliedBuildout` — raises `SprintApplyError` on any unrecoverable `gh` failure
  - `cw.exceptions.SprintApplyError(CwError)`

- [ ] **Step 1: Write the failing tests**

The point of these tests is **call order** and **idempotency** — the two things
the hand-driven session got wrong. GitHub assigns numbers on creation, so epics
must exist before children can reference them, and the checklist can only be
written once the children have numbers.

Append to `tests/test_sprint.py`:

```python
from cw.exceptions import SprintApplyError
from cw.sprint import apply_plan


class FakeGh:
    """A GhSurface test double. Records call order; hands out issue numbers."""

    def __init__(self, *, existing: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.bodies: dict[int, str] = {}
        self.existing = existing or {}
        self._next = 100

    def find_milestone(self, title: str) -> int | None:
        self.calls.append(f"find_milestone:{title}")
        return 11 if self.existing else None

    def create_milestone(self, title: str) -> int | None:
        self.calls.append(f"create_milestone:{title}")
        return 11

    def milestone_issue_titles(self, milestone: int) -> dict[str, int] | None:
        self.calls.append(f"list:{milestone}")
        return dict(self.existing)

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
    """
```

- [ ] **Step 4: Implement apply**

Append to `src/cw/sprint.py`:

```python
from typing import Protocol

from cw import gh


class GhSurface(Protocol):
    """The slice of `gh` that apply_plan needs.

    A Protocol, not the module itself: `mypy --strict` cannot verify a module
    object against a call site, and the alternative — `gh_mod: object` plus a
    `# type: ignore[union-attr]` on every call — is a suppression, which this repo
    does not permit. The Protocol gives real type-checking AND a clean test double.
    """

    def find_milestone(self, title: str) -> int | None: ...
    def create_milestone(self, title: str) -> int | None: ...
    def milestone_issue_titles(self, milestone: int) -> dict[str, int] | None: ...
    def create_issue(
        self, title: str, body: str, *, labels: list[str], milestone: int
    ) -> int | None: ...
    def update_issue_body(self, number: int, body: str) -> bool: ...


class GhClient:
    """Default GhSurface — delegates to :mod:`cw.gh`."""

    def find_milestone(self, title: str) -> int | None:
        return gh.find_milestone(title)

    def create_milestone(self, title: str) -> int | None:
        return gh.create_milestone(title)

    def milestone_issue_titles(self, milestone: int) -> dict[str, int] | None:
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
    existing = client.find_milestone(plan.milestone_title)
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
        raise SprintApplyError(msg)
    applied.created.append(draft.code)
    return number


def _resolve_epic_refs(ticket: IssueDraft, epic_numbers: dict[str, int]) -> IssueDraft:
    """Rewrite the body's ``Epic #<key>`` placeholder with the real issue number.

    build_plan renders ``Epic #I`` because the epic's number does not exist until
    apply creates it — GitHub assigns numbers at creation time.
    """
    body = ticket.body
    for key, number in epic_numbers.items():
        body = body.replace(f"Epic #{key}", f"Epic #{number}")
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
            raise SprintApplyError(msg)


def apply_plan(plan: BuildoutPlan, *, client: GhSurface | None = None) -> AppliedBuildout:
    """Execute the plan against GitHub. Idempotent; safe to re-run after a failure.

    The pass order is forced by GitHub assigning issue numbers at creation time:
    epics must exist before a child body can name its epic, and children must exist
    before they can appear in the epic's checklist. Hence: milestone → epics →
    tickets → backfill.
    """
    gh_client: GhSurface = client if client is not None else GhClient()
    milestone = _resolve_milestone(plan, gh_client)
    existing = gh_client.milestone_issue_titles(milestone) or {}
    applied = AppliedBuildout(milestone_number=milestone)

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
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4; `cw.cli._base.main` and `handle_errors` (existing).
- Produces: the `cw sprint` command group. `cw sprint plan <rfc-path> --out <file>`; `cw sprint apply <plan-file>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`, following its existing `CliRunner` pattern. Reuse
the sprint fixtures rather than duplicating them — `tests` is a package and
cross-file test imports are already used here (see
`tests/test_reconcile_review_recipes.py`, which imports from `tests.test_pr_hydrate`):

```python
from cw.sprint import BuildoutPlan, build_plan, parse_rfc
from tests.test_sprint import CONFIG_YAML, MINIMAL_RFC, _config, _write_config


def test_sprint_plan_writes_a_plan_file_and_prints_a_summary(tmp_path, monkeypatch) -> None:
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text(MINIMAL_RFC, encoding="utf-8")
    _write_config(tmp_path, CONFIG_YAML)
    monkeypatch.setattr("cw.cli.sprint._resolve_version", lambda root: "1.20.0")

    out = tmp_path / "plan.json"
    result = CliRunner().invoke(
        main, ["sprint", "plan", "docs/rfcs/0011-x.md", "--out", str(out), "--root", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "2 tickets" in result.output
    assert "v1.20.0 — Availability- & Counterparty-Aware Holding" in result.output
    plan = BuildoutPlan.model_validate_json(out.read_text(encoding="utf-8"))
    assert [t.code for t in plan.tickets] == ["S1", "A1"]


def test_sprint_plan_reports_a_contract_violation_as_a_clean_cli_error(tmp_path, monkeypatch) -> None:
    rfc = tmp_path / "docs" / "rfcs" / "0011-x.md"
    rfc.parent.mkdir(parents=True)
    rfc.write_text(MINIMAL_RFC.replace("## Tickets", "## Nope"), encoding="utf-8")
    _write_config(tmp_path, CONFIG_YAML)

    result = CliRunner().invoke(
        main, ["sprint", "plan", "docs/rfcs/0011-x.md", "--out", str(tmp_path / "p.json"), "--root", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "missing section: ## Tickets" in result.output


def test_sprint_apply_dry_run_makes_no_gh_calls(tmp_path, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr("cw.gh.create_milestone", lambda *a, **k: called.append("boom"))

    plan = build_plan(parse_rfc(MINIMAL_RFC), _config(), version="1.20.0")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(main, ["sprint", "apply", str(plan_file), "--dry-run"])

    assert result.exit_code == 0
    assert called == []
    assert "would create" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k sprint`
Expected: FAIL — `Error: No such command 'sprint'`

- [ ] **Step 3: Implement the CLI**

Create `src/cw/cli/sprint.py`:

```python
"""``cw sprint`` — RFC → ticketed sprint block (see /sprint-buildout)."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

import click

from cw.cli._base import handle_errors, main
from cw.sprint import (
    BuildoutPlan,
    apply_plan,
    build_plan,
    load_buildout_config,
    load_rfc_text,
    parse_rfc,
)

_VERSION_TIMEOUT = 10
_FALLBACK_VERSION = "0.0.0"


def _resolve_version(root: Path) -> str:
    """Derive the milestone version from the latest release tag, minor-bumped."""
    try:
        result = _sp.run(
            ["gh", "release", "view", "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            cwd=root,
            timeout=_VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return _FALLBACK_VERSION
    if result.returncode != 0:
        return _FALLBACK_VERSION
    tag = result.stdout.decode("utf-8", "replace").strip().lstrip("v")
    parts = tag.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return _FALLBACK_VERSION
    return f"{parts[0]}.{int(parts[1]) + 1}.0"


@main.group()
def sprint() -> None:
    """Build a ticketed sprint block from an RFC."""


@sprint.command("plan")
@click.argument("rfc_path")
@click.option("--out", required=True, type=click.Path(path_type=Path), help="Where to write plan JSON.")
@click.option("--root", default=".", type=click.Path(path_type=Path), help="Repo root.")
@click.option("--version", "version_override", default=None, help="Override the milestone version.")
@handle_errors
def sprint_plan(rfc_path: str, out: Path, root: Path, version_override: str | None) -> None:
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


@sprint.command("apply")
@click.argument("plan_file", type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Print what would be created; touch nothing.")
@handle_errors
def sprint_apply(plan_file: Path, dry_run: bool) -> None:
    """Execute PLAN_FILE against GitHub. Idempotent — safe to re-run."""
    plan = BuildoutPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))

    if dry_run:
        click.echo(f"Would create milestone: {plan.milestone_title}")
        for draft in [*plan.epics, *plan.tickets]:
            click.echo(f"  would create {draft.kind}: {draft.title}")
        return

    applied = apply_plan(plan)
    click.echo(f"Milestone #{applied.milestone_number}")
    for code, number in applied.epic_numbers.items():
        click.echo(f"  epic {code}: #{number}")
    for code, number in applied.ticket_numbers.items():
        click.echo(f"  {code}: #{number}")
    if applied.skipped:
        click.echo(f"Skipped (already existed): {', '.join(applied.skipped)}")
```

- [ ] **Step 4: Register the group**

In `src/cw/cli/__init__.py`, add `sprint` to the submodule import block (imported
for its registration side effect) and to `__all__`, keeping both alphabetical:

```python
from cw.cli import (
    channels,
    config_cmds,
    guard,
    maintenance,
    queues,
    session_inspect,
    sprint,
    watchdog,
    worktree,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v -k sprint && uv run cw sprint --help`
Expected: PASS; help lists `plan` and `apply`.

- [ ] **Step 6: Run the gates and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ && uv run mypy --strict src/
git add src/cw/cli/sprint.py src/cw/cli/__init__.py tests/test_cli.py
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

- [ ] **Step 1: Write the fixture**

Create `tests/fixtures/rfc-0011-tickets.md`: RFC 0011's real content with a
`## Tickets` section back-filled to describe what that session actually filed.
Source the codes, epics, waves, and dependencies from the buildout handoff
(`.handoffs/handoff-sprint-kickoff-2026-07-13-1306.md`), which records them:

- Sprint 0 (Wave 0), no epic: `S1` (counterparty axis + self-identity),
  `S2` (native review-request register; depends on S1)
- Epic I (`## Design` → `### Epic I — Availability-aware holding (inward)`):
  Sprint 1 — `A1` (park class, keystone; depends on S1), `A2` (detector; depends
  on A1), `A5` (probe; depends on A1). Sprint 2 — `A3` (stop-before-finalize),
  `A4` (auto-resume), `A6` (digest).
- Epic II (`### Epic II — Counterparty-aware collaboration (outward)`):
  Sprint 1 — `B1` (idle exemption; depends on S1), `B2` (consent gate; depends on
  S1). Sprint 2 — `B3` (individual re-request), `B4` (response contract),
  `B5` (graceful rejection).

Each ticket's `Context:` and `Acceptance:` come from the corresponding RFC prose;
each `Scope:` cites the `D-*` ids that already exist in RFC 0011's "Resolved
decisions" section (D-S1, D-S2a, D-S2b, D-A1, D-A2, D-A3, D-A5, D-A6, D-B4).

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

    # The dependency the handoff calls out: S2 rides S1.
    s2 = next(t for t in doc.tickets if t.code == "S2")
    assert s2.depends_on == ["S1"]
```

- [ ] **Step 3: Run the test**

Run: `uv run pytest tests/test_sprint.py::test_rfc_0011_fixture_reproduces_the_real_buildout -v`
Expected: initially FAIL (fixture incomplete), then PASS once the fixture is
faithful. **If it cannot be made to pass without changing the parser, stop and
report** — that means the template is missing something the RFC genuinely needs,
which is a design finding, not an implementation bug.

- [ ] **Step 4: Run the full gates and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy --strict src/
uv run --extra mcp pytest tests/ -m 'not integration' --cov=cw --cov-report=xml --cov-fail-under=88
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
7. **RFC footer PR.** Back-fill the RFC's `Issues:` footer with the real numbers
   and the milestone URL; open a docs-only PR.

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
