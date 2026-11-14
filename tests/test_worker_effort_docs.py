"""Doc conformance for ``worker_effort`` (#2350).

Pins the operator-facing strings: the CONFIG_REFERENCE field row and gating
sentence, the cost-control example, and the README row. Needles are matched
after :func:`_norm`, so line wrapping does not matter.
"""

from __future__ import annotations

from pathlib import Path

from tests._shared.workflow_text import _norm

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _ROOT / "config" / "CONFIG_REFERENCE.md"
_README = _ROOT / "README.md"

_GATING = (
    "The default applies to full model ids starting `claude-opus-5-5`, "
    "not aliases such as `opus`; set `worker_effort` explicitly for an alias."
)


def _row(text: str, field: str) -> str:
    prefix = f"| `{field}` |"
    lines = [line for line in text.splitlines() if line.startswith(prefix)]
    assert lines, f"no table row for `{field}`"
    return lines[0]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def test_config_reference_row_states_default_and_inherit() -> None:
    row = _row(_CONFIG.read_text(encoding="utf-8"), "worker_effort")
    assert "none (medium on claude-opus-5-5 pins)" in row
    assert "`inherit`" in row


def test_config_reference_cost_control_has_effort_example() -> None:
    section = _section(
        _CONFIG.read_text(encoding="utf-8"), "### Cost Control — Pin Worker Model"
    )
    normalized = _norm(section)
    assert _norm("worker_model: claude-opus-5-5") in normalized
    assert _norm("worker_effort: high") in normalized


def test_config_reference_states_gating_sentence() -> None:
    assert _norm(_GATING) in _norm(_CONFIG.read_text(encoding="utf-8"))


def test_readme_row_mentions_worker_effort_and_inherit() -> None:
    row = _row(_README.read_text(encoding="utf-8"), "worker_effort")
    assert "`inherit`" in row
