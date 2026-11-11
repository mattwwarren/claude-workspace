"""Guard tests: docs and skills describe the per-session cw-operator wiring (#3170).

Pure-markdown assertions in the style of
``tests/test_orchestration_skill_discipline.py``: ``cw orchestrator-start``
subscribes its session to ``cw-operator`` through a per-session
``--mcp-config`` file, and the watch Monitor stays the fallback for sessions
started any other way.
"""

from __future__ import annotations

from pathlib import Path

from tests._shared.workflow_text import _norm

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".claude" / "skills"
OPERATOR_DOC = ROOT / "docs" / "operator-channel.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    """Normalized text of *content* from *start* up to *end*."""
    begin = content.index(start)
    return _norm(content[begin : content.index(end, begin)])


def test_operator_channel_doc_documents_per_session_mcp_config() -> None:
    doc = _norm(_read(OPERATOR_DOC))
    assert "Per-session subscription (orchestrator)" in doc
    assert "--mcp-config" in doc
    assert "cw orchestrator-start" in doc
    assert "server:cw-operator" in doc
    assert "all-sessions alternative" in doc


def test_operator_channel_doc_no_longer_defers_skill_adoption() -> None:
    doc = _norm(_read(OPERATOR_DOC))
    assert "Skill/runbook adoption of this channel is deferred" not in doc


def test_cw_orchestration_watching_names_orchestrator_start_keeps_monitor() -> None:
    content = _read(SKILLS / "cw-orchestration" / "SKILL.md")
    watching = _between(content, "- **Watching.**", "- **Posting tracker comments.**")
    assert "cw orchestrator-start" in watching
    assert "cw-operator" in watching
    assert "timeout_ms: 1800000" in watching
    assert "re-arm only when it expires" in watching


def test_orchestrate_sprint_phase4_mentions_orchestrator_start() -> None:
    content = _read(SKILLS / "orchestrate-sprint" / "SKILL.md")
    phase4 = _between(content, "### Phase 4", "### Phase 5")
    assert "cw orchestrator-start" in phase4
    assert '<channel source="cw-operator">' in phase4
    assert "timeout_ms: 1800000" in phase4
    assert "re-arm only when it expires" in phase4
