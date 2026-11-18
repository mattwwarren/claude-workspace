"""Needle test: operator docs describe the harness-capabilities check (#3708)."""

from __future__ import annotations

from pathlib import Path

from tests._shared.workflow_text import _norm

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_doc_lists_the_check_as_declared_not_enforced() -> None:
    text = _norm((_REPO_ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8"))
    assert "`[OK] harness-capabilities/<client>/<lane>/<backend>`" in text
    assert "declared, not enforced" in text


def test_config_reference_describes_check_schema_and_sandbox_rule() -> None:
    text = _norm(
        (_REPO_ROOT / "config" / "CONFIG_REFERENCE.md").read_text(encoding="utf-8")
    )
    assert "harness-capabilities/<client>/<lane>/<backend>" in text
    assert "cw schema show harness-capabilities" in text
    assert "harness manages its own sandbox" in text
