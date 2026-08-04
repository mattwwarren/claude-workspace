"""Guard test (#1634): the changelog-gate detection snippet duplicated across
`.claude/commands/ship-it.md` (Step 3a) and its two SKILL.md replicas
(`cw-followup`, `cw-session-watch`) must stay byte-identical to
`.github/workflows/changelog-gate.yml`'s actual gate conditions.

Stage-3 review on #1634 flagged the triplicated `GATE_FIRES` snippet as having
no automated tripwire: if the workflow's title/path conditions ever change,
nothing forces the three markdown copies to follow. This pins all three
against the workflow's literal grep patterns so a missed copy fails loudly
instead of silently reintroducing the gap #1634 was filed to close.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "changelog-gate.yml"
SHIP_IT_PATH = ROOT / ".claude" / "commands" / "ship-it.md"
CW_FOLLOWUP_PATH = ROOT / ".claude" / "skills" / "cw-followup" / "SKILL.md"
CW_SESSION_WATCH_PATH = ROOT / ".claude" / "skills" / "cw-session-watch" / "SKILL.md"

TITLE_PATTERN_LITERAL = r"grep -qE '^(feat|fix)\('"
SRC_PATH_PATTERN_LITERAL = r"grep -q '^src/'"

SOURCES = {
    "changelog-gate.yml": WORKFLOW_PATH,
    "ship-it.md": SHIP_IT_PATH,
    "cw-followup/SKILL.md": CW_FOLLOWUP_PATH,
    "cw-session-watch/SKILL.md": CW_SESSION_WATCH_PATH,
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_title_prefix_regex_matches_workflow_in_every_copy() -> None:
    for name, path in SOURCES.items():
        text = _read(path)
        assert TITLE_PATTERN_LITERAL in text, (
            f"{name} is missing (or has drifted from) the gate's title-prefix "
            f"check {TITLE_PATTERN_LITERAL!r}"
        )


def test_src_path_regex_matches_workflow_in_every_copy() -> None:
    for name, path in SOURCES.items():
        text = _read(path)
        assert SRC_PATH_PATTERN_LITERAL in text, (
            f"{name} is missing (or has drifted from) the gate's src/ path "
            f"check {SRC_PATH_PATTERN_LITERAL!r}"
        )
