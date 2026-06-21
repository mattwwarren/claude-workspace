"""Tests for .claude/scripts/prep_pr_state.py gate detection.

Uses importlib to load the script directly (it lives outside the src/ tree).
All fixtures are deterministic string literals — the live CLAUDE.md is never read.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Protocol, cast

# ---------------------------------------------------------------------------
# Protocol for dynamically-loaded Gate objects
# ---------------------------------------------------------------------------


class _GateP(Protocol):
    """Structural type for Gate dataclass instances loaded via importlib."""

    name: str
    command: str
    autofix: str | None


# ---------------------------------------------------------------------------
# Script loader
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "prep_pr_state.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("prep_pr_state", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("prep_pr_state", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
_raw_parse = _mod._parse_claude_md_gates


def _parse_claude_md_gates(path: Path) -> list[_GateP]:
    return cast("list[_GateP]", _raw_parse(path))


# ---------------------------------------------------------------------------
# Fixtures: deterministic CLAUDE.md content strings
# ---------------------------------------------------------------------------

# (a) Bullet-only: classic format
_BULLET_ONLY = """\
# Project

## Quality Gates

- ruff: uv run ruff check . | uv run ruff check --fix .
- mypy: uv run mypy .
- pytest: uv run pytest

## Other Section

Some content.
"""

# (b) Bash-block-only: mirrors the real CLAUDE.md structure (all 7 gates,
#     two-line diff-cover and two-line pytest with --extra mcp)
_BASH_BLOCK_ONLY = """\
# Project

## Quality Gates

Before committing run every gate CI enforces:

```bash
uv run ruff check src/ tests/                                    # 1. Lint
uv run ruff format --check src/ tests/                           # 2. Format
uv run mypy --strict src/                                        # 3. Type check
uv run pre-commit run --all-files                                # 4. Hooks
uv run --extra mcp pytest tests/ -m 'not integration' \\
  --cov=cw --cov-report=xml --cov-fail-under=88  # 5. Unit + total cov >=88%
uv run pytest tests/ -m integration                # 6. tmux integration
uv run diff-cover coverage.xml --compare-branch=origin/main \\
  --fail-under=90  # 7. Patch coverage >=90%
```

Requirements section.
"""

# (c) Mixed: bullet gates + bash-block gates; bash-block overrides same-name bullet
_MIXED = """\
# Project

## Quality Gates

- ruff: uv run ruff check .
- mypy: uv run mypy .

```bash
uv run ruff check src/ tests/
uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90
```
"""

# (d) No Quality Gates section
_NO_SECTION = """\
# Project

## Setup

Run `make install`.

## Other

Nothing here about gates.
"""

# (e) Missing CLAUDE.md — tested via a non-existent path

# (f) Unclosed fence (malformed)
_UNCLOSED_FENCE = """\
# Project

## Quality Gates

```bash
uv run ruff check .
uv run mypy .
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write_claude_md(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "CLAUDE.md"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Case (a): bullet-only — existing behaviour unchanged
# ---------------------------------------------------------------------------


class TestBulletOnly:
    def test_returns_all_bullet_gates(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _BULLET_ONLY)
        gates = _parse_claude_md_gates(path)
        names = [g.name for g in gates]
        assert names == ["ruff", "mypy", "pytest"]

    def test_autofix_preserved(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _BULLET_ONLY)
        gates = _parse_claude_md_gates(path)
        ruff = next(g for g in gates if g.name == "ruff")
        assert ruff.autofix == "uv run ruff check --fix ."

    def test_command_preserved(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _BULLET_ONLY)
        gates = _parse_claude_md_gates(path)
        mypy = next(g for g in gates if g.name == "mypy")
        assert mypy.command == "uv run mypy ."
        assert mypy.autofix is None


# ---------------------------------------------------------------------------
# Case (b): bash-block-only — all 7 gates detected, multi-line commands joined
# ---------------------------------------------------------------------------


class TestBashBlockOnly:
    def _gates(self, tmp_path: Path) -> list[_GateP]:
        path = _write_claude_md(tmp_path, _BASH_BLOCK_ONLY)
        return _parse_claude_md_gates(path)

    def test_returns_seven_gates(self, tmp_path: Path) -> None:
        assert len(self._gates(tmp_path)) == 7

    def test_names_include_diff_cover(self, tmp_path: Path) -> None:
        names = [g.name for g in self._gates(tmp_path)]
        assert "diff-cover" in names

    def test_names_include_mypy(self, tmp_path: Path) -> None:
        names = [g.name for g in self._gates(tmp_path)]
        assert "mypy" in names

    def test_names_include_pre_commit(self, tmp_path: Path) -> None:
        names = [g.name for g in self._gates(tmp_path)]
        assert "pre-commit" in names

    def test_diff_cover_command_joined(self, tmp_path: Path) -> None:
        """Multi-line diff-cover command must be joined into one command string."""
        gates = self._gates(tmp_path)
        diff_cover = next(g for g in gates if g.name == "diff-cover")
        assert "--fail-under=90" in diff_cover.command
        assert "--compare-branch=origin/main" in diff_cover.command
        assert "\\" not in diff_cover.command

    def test_pytest_extra_mcp_command_joined(self, tmp_path: Path) -> None:
        """The --extra mcp pytest continuation must be joined."""
        gates = self._gates(tmp_path)
        pytest_gates = [g for g in gates if g.name == "pytest"]
        extra_mcp = next(
            (g for g in pytest_gates if "--extra" in g.command),
            None,
        )
        assert extra_mcp is not None, "No pytest gate with --extra mcp found"
        assert "--cov-fail-under=88" in extra_mcp.command
        assert "\\" not in extra_mcp.command

    def test_pytest_name_derived_skipping_flags(self, tmp_path: Path) -> None:
        """uv run --extra mcp pytest → name must be 'pytest', not '--extra'."""
        gates = self._gates(tmp_path)
        extra_mcp = next(
            (g for g in gates if "--extra" in g.command),
            None,
        )
        assert extra_mcp is not None
        assert extra_mcp.name == "pytest"

    def test_inline_comments_stripped_from_commands(self, tmp_path: Path) -> None:
        """Trailing # comments must not appear in Gate.command."""
        gates = self._gates(tmp_path)
        for gate in gates:
            assert " # " not in gate.command, (
                f"Gate '{gate.name}' still has inline comment: {gate.command!r}"
            )

    def test_no_autofix_for_bash_block_gates(self, tmp_path: Path) -> None:
        """Bash-block gates never have an autofix field."""
        gates = self._gates(tmp_path)
        for gate in gates:
            assert gate.autofix is None, (
                f"Gate '{gate.name}' unexpectedly has autofix: {gate.autofix!r}"
            )


# ---------------------------------------------------------------------------
# Case (c): mixed bullet + bash-block — bash-block overrides same-name bullet
# ---------------------------------------------------------------------------


class TestMixedFormat:
    def _gates(self, tmp_path: Path) -> list[_GateP]:
        path = _write_claude_md(tmp_path, _MIXED)
        return _parse_claude_md_gates(path)

    def test_diff_cover_present(self, tmp_path: Path) -> None:
        names = [g.name for g in self._gates(tmp_path)]
        assert "diff-cover" in names

    def test_bash_block_ruff_overrides_bullet_ruff(self, tmp_path: Path) -> None:
        """When both bullet and bash-block have 'ruff', bash-block wins."""
        gates = self._gates(tmp_path)
        ruff_gates = [g for g in gates if g.name == "ruff"]
        # bash-block gate uses 'src/ tests/', bullet used '.'
        assert len(ruff_gates) >= 1
        # The surviving ruff gate must be the bash-block version
        assert any("src/" in g.command for g in ruff_gates), (
            "Expected bash-block ruff gate (src/ tests/) to survive dedup"
        )

    def test_bullet_only_mypy_still_present(self, tmp_path: Path) -> None:
        """mypy is only in bullet format; must still appear in results."""
        names = [g.name for g in self._gates(tmp_path)]
        assert "mypy" in names


# ---------------------------------------------------------------------------
# Case (d): no ## Quality Gates section
# ---------------------------------------------------------------------------


class TestNoSection:
    def test_returns_empty_list(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _NO_SECTION)
        assert _parse_claude_md_gates(path) == []


# ---------------------------------------------------------------------------
# Case (e): missing CLAUDE.md
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_returns_empty_list(self, tmp_path: Path) -> None:
        missing = tmp_path / "CLAUDE.md"
        assert not missing.exists()
        assert _parse_claude_md_gates(missing) == []


# ---------------------------------------------------------------------------
# Case (f): unclosed fence (malformed) — empty list, no exception
# ---------------------------------------------------------------------------


class TestUnclosedFence:
    def test_returns_empty_list_no_exception(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _UNCLOSED_FENCE)
        result = _parse_claude_md_gates(path)
        assert result == []


# ---------------------------------------------------------------------------
# Case (g): trailing continuation — last fence line ends with backslash
# ---------------------------------------------------------------------------

_TRAILING_CONTINUATION = """\
# Project

## Quality Gates

```bash
uv run mypy \\
```
"""


class TestTrailingContinuation:
    def test_pending_flushed_when_fence_ends_mid_continuation(
        self, tmp_path: Path
    ) -> None:
        """Last fence line ending with backslash must not be dropped."""
        path = _write_claude_md(tmp_path, _TRAILING_CONTINUATION)
        gates = _parse_claude_md_gates(path)
        assert len(gates) == 1
        assert gates[0].name == "mypy"
