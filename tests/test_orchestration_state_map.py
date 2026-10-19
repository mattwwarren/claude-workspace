"""Structural guard: the ``cw-orchestration`` skill's state-to-command map (#2698).

The skill's ``## State to command`` section tells an orchestrator session which
``cw`` command releases each queue state. Prose drifts from code silently, so
this file pins it to the live sources it restates:

- every ``cw ...`` inline span in the map, the command semantics and the
  hazards block resolves to a real leaf command and real flags in the live
  Click tree (the same walk as ``tests/test_dev_queue_next_commands.py``);
- every state key (a backticked span in an entry's bold prefix) is a member of
  the queue, sentinel, blocker, disposition or skip-reason vocabulary;
- the first command the map gives for a common state matches the first
  command ``cw.dev_queue.next_commands`` emits for it (``CASES``);
- the human-kept gates are marked operator-only and spell no release command.

The version pin is advisory: it is checked only for shape and for not being
ahead of ``pyproject.toml``, so a release bump never reddens this file.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import click
import pytest

import cw.dev_queue as dev_queue_mod
from cw.auto_dev_result import KNOWN_BLOCKER_REASONS, Status
from cw.cli import main
from cw.dev_queue import DRAIN_DISPOSITIONS, HOLD_DISPOSITIONS
from cw.dev_queue.next_commands import (
    LANE_PAUSED_SKIP_CODE,
    RESOLVE_COMMAND,
    next_commands,
)
from cw.models import DispatchSkipReason, QueueItemStatus
from tests._ticket_view_helpers import ACME, ACME_TICKET, CASES
from tests.conftest import walk_click_tree

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".claude" / "skills"
SKILL_NAME = "cw-orchestration"

STATE_HEADING = "## State to command"
SEMANTICS_HEADING = "## Command semantics"
HAZARDS_HEADING = "## Known cw hazards"
VERSION_HEADING = "## Version"

WHO_ORCHESTRATOR = "orchestrator"
WHO_OPERATOR = "operator only"
SCOPE_HINT_LARGE = "scope_hint: large"
CATCH_ALL_PREFIX = "any other state"
SHOW_COMMAND = "cw dev-queue show <T> -c <C>"
TICKET_PLACEHOLDER = "<T>"
CLIENT_PLACEHOLDER = "<C>"
HELP_FLAG = "--help"
RELEASE_VERBS = ("cw dev-queue requeue", "cw dev-queue approve")
OPERATOR_ONLY_KEYS = (
    "awaiting_operator_signoff",
    "finalize_gate_held",
    "scope_hint: large",
)
CATCH_ALL_STATES = (
    "scope_exceeded",
    "forbidden_area",
    "plan_unreviewable",
    "plan_unsound",
    "agent_block",
    "tool_denied",
    "empty_diff_blocked",
    "merge_pending",
)
RESOLVE_KEYS = ("ambiguities_pending_resolution", "premises_pending_verification")
VOCABULARY_ROWS = (
    "cw preflight <T> -c <C>",
    "cw lane set <C> <lane> max_parallel=N",
    "cw config concurrency set ceiling=N -c <C>",
    'cw review settle <file> --ticket <T> --reason "<why>" -c <C>',
)
LANE_SET_KEYS = "settable keys are `max_parallel` and `attempt_ceiling` only"

# Table state key -> the CASES row whose first next command the entry must give.
PARITY: dict[str, str] = {
    "attempt_cap_blocked": "rule6b_attempt_cap_blocked",
    "plan_scope_drift": "rule5_scope_drift_no_session",
    "codex_must_fix_findings": "rule6_must_fix_no_session",
    "pending": "rule2_pending",
    "running": "rule3_running",
    "failed": "rule4_failed",
    "branch_behind_main": "rule11_branch_behind_main",
    "awaiting_operator": "rule8_awaiting_operator",
    "plan_pending_approval": "rule9_plan_gate",
    "review_pending_approval": "rule9_review_gate",
}

_ENTRY_START = "- **"
_BOLD_PREFIX_RE = re.compile(r"^- \*\*(?P<prefix>.+?)\*\*:")
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_WHO_RE = re.compile(r"Who: (orchestrator|operator only)\b")
_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>")
_HAZARD_CITE_RE = re.compile(r"#\d+|\b(?:src|docs|tests)/[\w./-]+")
_VERSION_RE = re.compile(r"Written against cw (\d+)\.(\d+)\.(\d+)\b")
_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_CW_PREFIX = "cw "
_ALTERNATION = "|"
_PLACEHOLDER_VALUE = "X"


@dataclass(frozen=True)
class Entry:
    """One ``- **<keys>**: <body>`` bullet of the state-to-command map."""

    prefix: str
    text: str

    @property
    def keys(self) -> list[str]:
        return _SPAN_RE.findall(self.prefix)

    @property
    def commands(self) -> list[str]:
        return [s for s in _SPAN_RE.findall(self.text) if s.startswith(_CW_PREFIX)]


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _section(content: str, heading: str) -> str:
    """*content* from *heading* up to the next ``## `` heading."""
    start = content.index(heading) + len(heading)
    rest = content[start:]
    following = _NEXT_HEADING_RE.search(rest)
    return rest if following is None else rest[: following.start()]


def _content() -> str:
    return _skill(SKILL_NAME)


def _bullets(section: str) -> list[str]:
    """Top-level ``- `` bullets of *section*, continuation lines joined."""
    bullets: list[str] = []
    for line in section.splitlines():
        if line.startswith("- "):
            bullets.append(line)
        elif bullets and line.startswith(" ") and line.strip():
            bullets[-1] = f"{bullets[-1]} {line.strip()}"
    return bullets


def _entries() -> list[Entry]:
    entries: list[Entry] = []
    for bullet in _bullets(_section(_content(), STATE_HEADING)):
        if not bullet.startswith(_ENTRY_START):
            continue
        match = _BOLD_PREFIX_RE.match(bullet)
        assert match is not None, f"entry without a `**...**:` prefix: {bullet}"
        entries.append(Entry(prefix=match.group("prefix"), text=bullet))
    return entries


def _entry_for(key: str) -> Entry:
    hits = [entry for entry in _entries() if key in entry.keys]
    assert len(hits) == 1, f"{key!r} keys {len(hits)} entries"
    return hits[0]


def _known_vocabulary() -> set[str]:
    dispositions = {
        value
        for name, value in vars(dev_queue_mod).items()
        if name.endswith("_DISPOSITION") and isinstance(value, str)
    }
    return (
        {status.value for status in QueueItemStatus}
        | set(get_args(Status))
        | set(KNOWN_BLOCKER_REASONS)
        | dispositions
        | set(HOLD_DISPOSITIONS)
        | set(DRAIN_DISPOSITIONS)
        | {reason.value for reason in DispatchSkipReason}
        | {LANE_PAUSED_SKIP_CODE, SCOPE_HINT_LARGE}
    )


def _click_paths() -> dict[tuple[str, ...], click.Command]:
    return dict(walk_click_tree(main))


def _leaf_options(leaf: click.Command) -> set[str]:
    opts = {
        opt
        for param in leaf.params
        if isinstance(param, click.Option)
        for opt in (*param.opts, *param.secondary_opts)
    }
    return opts | {HELP_FLAG}


def _resolve(span: str, paths: dict[tuple[str, ...], click.Command]) -> list[str]:
    """Problems resolving *span* against the Click tree (empty when it resolves)."""
    if _ALTERNATION in span:
        return [f"{span}: `|` alternation; spell each command separately"]
    tokens = shlex.split(_PLACEHOLDER_RE.sub(_PLACEHOLDER_VALUE, span))[1:]
    prefix_len = max(
        (n for n in range(1, len(tokens) + 1) if tuple(tokens[:n]) in paths),
        default=0,
    )
    leaf = paths.get(tuple(tokens[:prefix_len]))
    if prefix_len == 0 or leaf is None or isinstance(leaf, click.Group):
        return [f"{span}: does not name a leaf command"]
    options = _leaf_options(leaf)
    flags = [t.split("=", 1)[0] for t in tokens[prefix_len:] if t.startswith("-")]
    return [f"{span}: unknown flag {flag}" for flag in flags if flag not in options]


def _resolvable_spans() -> list[str]:
    content = _content()
    spans: list[str] = []
    for heading in (STATE_HEADING, SEMANTICS_HEADING, HAZARDS_HEADING):
        section = _section(content, heading)
        spans.extend(s for s in _SPAN_RE.findall(section) if s.startswith(_CW_PREFIX))
    return spans


def _placeholdered(command: str) -> str:
    return command.replace(f" {ACME_TICKET} ", f" {TICKET_PLACEHOLDER} ").replace(
        f"-c {ACME}", f"-c {CLIENT_PLACEHOLDER}"
    )


# --- tests ---


def test_skill_frontmatter() -> None:
    content = _content()
    assert content.startswith("---\n")
    frontmatter = content.split("---", 2)[1]
    assert f"name: {SKILL_NAME}" in frontmatter
    assert "description:" in frontmatter
    assert "orchestrate-sprint" in frontmatter


def test_map_has_entries() -> None:
    assert len(_entries()) >= len(PARITY)


def test_map_defers_to_show_next() -> None:
    section = _section(_content(), STATE_HEADING)
    assert f"`{SHOW_COMMAND}`" in section
    assert "authoritative" in section


def test_every_entry_declares_who() -> None:
    for entry in _entries():
        assert len(_WHO_RE.findall(entry.text)) == 1, entry.text
        assert entry.text.count("Who:") == 1, entry.text


@pytest.mark.parametrize("key", OPERATOR_ONLY_KEYS)
def test_human_kept_gates_are_operator_only(key: str) -> None:
    entry = _entry_for(key)
    assert _WHO_RE.findall(entry.text) == [WHO_OPERATOR]
    releases = [c for c in entry.commands if c.startswith(RELEASE_VERBS)]
    assert releases == [], entry.text


def test_other_entries_are_orchestrator_run() -> None:
    operator_entries = {_entry_for(key).text for key in OPERATOR_ONLY_KEYS}
    for entry in _entries():
        if entry.text not in operator_entries:
            assert _WHO_RE.findall(entry.text) == [WHO_ORCHESTRATOR], entry.text


def test_table_commands_resolve_in_click_tree() -> None:
    paths = _click_paths()
    spans = _resolvable_spans()
    assert spans
    problems = [problem for span in spans for problem in _resolve(span, paths)]
    assert problems == [], "\n".join(problems)


def test_resolver_rejects_groups_unknown_flags_and_alternation() -> None:
    paths = _click_paths()
    assert _resolve("cw dev-queue requeue <T> -c <C> --from-failed", paths) == []
    assert _resolve("cw dev-queue", paths)
    assert _resolve("cw dev-queue requeue <T> --no-such-flag", paths)
    assert _resolve("cw dev-queue requeue <T> --from-failed|--from-cancelled", paths)
    assert _resolve("cw no-such-verb", paths)


def test_state_keys_are_known_vocabulary() -> None:
    known = _known_vocabulary()
    keys = [key for entry in _entries() for key in entry.keys]
    assert keys
    unknown = sorted(set(keys) - known)
    assert unknown == []


def test_body_backticks_are_not_keys() -> None:
    entry = _entry_for("plan_pending_approval")
    assert "plan_requeued" in entry.text
    assert "plan_requeued" not in entry.keys


@pytest.mark.parametrize(("key", "case_id"), sorted(PARITY.items()))
def test_first_command_parity_with_next_commands(key: str, case_id: str) -> None:
    case = CASES[case_id]
    first = next_commands(case.task, session=case.session())[0].command
    tokens = shlex.split(first)
    entry = _entry_for(key)
    verb = " ".join(tokens[:3])
    assert f"{verb} {TICKET_PLACEHOLDER} -c {CLIENT_PLACEHOLDER}" in entry.text, (
        f"{key}: expected {_placeholdered(first)!r}"
    )
    for flag in (t for t in tokens if t.startswith("--")):
        assert flag in entry.text, f"{key}: missing {flag} of {first!r}"


def test_catch_all_row_names_show_and_next() -> None:
    hits = [e for e in _entries() if e.prefix == CATCH_ALL_PREFIX]
    assert len(hits) == 1
    entry = hits[0]
    assert entry.keys == []
    assert f"`{SHOW_COMMAND}`" in entry.text
    assert "`next:`" in entry.text
    for state in CATCH_ALL_STATES:
        assert f"`{state}`" in entry.text, state


@pytest.mark.parametrize("row", VOCABULARY_ROWS)
def test_vocabulary_rows_present(row: str) -> None:
    content = _content()
    body = _section(content, STATE_HEADING) + _section(content, SEMANTICS_HEADING)
    assert f"`{row}`" in body
    assert _resolve(row, _click_paths()) == []


def test_vocabulary_rows_state_setter_limits() -> None:
    semantics = " ".join(_section(_content(), SEMANTICS_HEADING).split())
    assert LANE_SET_KEYS in semantics
    assert "are refused" in semantics
    assert "`--ticket` and `--reason` are both required" in semantics


@pytest.mark.parametrize("key", RESOLVE_KEYS)
def test_resolve_entry_matches_resolve_command(key: str) -> None:
    entry = _entry_for(key)
    verb, _, flag_part = RESOLVE_COMMAND.partition(" --")
    assert verb in entry.text
    assert f"--{flag_part.split()[0]}" in entry.text


def test_version_pin_well_formed_and_not_ahead() -> None:
    section = _section(_content(), VERSION_HEADING)
    pins = _VERSION_RE.findall(section)
    assert len(pins) == 1
    pinned = tuple(int(part) for part in pins[0])
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project_version = tomllib.load(handle)["project"]["version"]
    current = tuple(int(part) for part in project_version.split(".")[:3])
    assert pinned <= current


def test_hazards_block_present() -> None:
    bullets = _bullets(_section(_content(), HAZARDS_HEADING))
    assert bullets
    for bullet in bullets:
        assert _HAZARD_CITE_RE.search(bullet), bullet
