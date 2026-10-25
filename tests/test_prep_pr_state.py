"""Tests for .claude/scripts/prep_pr_state.py gate detection.

Uses importlib to load the script directly (it lives outside the src/ tree).
All fixtures are deterministic string literals — the live CLAUDE.md is never read,
with one deliberate exception: ``TestRealClaudeMdGates`` pins the repo's own
``## Quality Gates`` list (and reads ``.github/workflows/ci.yml`` to check it
agrees) so a regression in ``detect-gates`` (or an unannounced edit to that
list) fails loudly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from cw.codex_review._context import _load_claude_md_quality_gates
from tests.conftest import (
    _bash_fences,
    _write_project_config_yaml,
    commit_tracked_file,
    git_in,
)
from tests.test_prep_pr_finalize import _python

# ---------------------------------------------------------------------------
# Protocol for dynamically-loaded Gate objects
# ---------------------------------------------------------------------------


class _GateP(Protocol):
    """Structural type for Gate dataclass instances loaded via importlib."""

    name: str
    command: str
    autofix: str | None


class _ClaudeMdGatesP(Protocol):
    """Structural type for the ClaudeMdGates dataclass loaded via importlib."""

    gates: list[_GateP]
    authoritative: bool


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
    return cast("_ClaudeMdGatesP", _raw_parse(path)).gates


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
uv run pytest tests/ -m integration                # 6. Integration
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


def _detect_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_md: str | None = None
) -> dict[str, Any]:
    """Run ``detect_gates`` in a tmp project with a ``pyproject.toml`` marker.

    When *claude_md* is given it is written as the project's CLAUDE.md.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    if claude_md is not None:
        _write_claude_md(tmp_path, claude_md)
    monkeypatch.chdir(tmp_path)
    return cast("dict[str, Any]", _mod.detect_gates())


def _gates_section(*body_lines: str) -> str:
    """Build a minimal CLAUDE.md whose ``## Quality Gates`` holds *body_lines*."""
    return "# Project\n\n## Quality Gates\n\n" + "\n".join(body_lines) + "\n"


def _bash_block(*command_lines: str) -> str:
    """Build a fenced bash block (as a CLAUDE.md fragment) of *command_lines*."""
    return "```bash\n" + "\n".join(command_lines) + "\n```"


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
        pytest_gates = [g for g in gates if g.name.startswith("pytest")]
        extra_mcp = next(
            (g for g in pytest_gates if "--extra" in g.command),
            None,
        )
        assert extra_mcp is not None, "No pytest gate with --extra mcp found"
        assert "--cov-fail-under=88" in extra_mcp.command
        assert "\\" not in extra_mcp.command

    def test_pytest_name_derived_skipping_flags(self, tmp_path: Path) -> None:
        """uv run --extra mcp pytest → name is qualified, never '--extra'.

        Two pytest gates collide on the bare name, so the ``-m`` marker
        expression qualifies each; the uv-run flag skipping itself is pinned
        directly in ``TestSplitCommand``.
        """
        gates = self._gates(tmp_path)
        extra_mcp = next(
            (g for g in gates if "--extra" in g.command),
            None,
        )
        assert extra_mcp is not None
        assert extra_mcp.name == "pytest-not-integration"

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


# ---------------------------------------------------------------------------
# #2249: gate 10 chains per-tree coverage floors onto the pytest run
# ---------------------------------------------------------------------------

# The real gate-10 layout: exactly two physical lines, one `\` continuation.
# A middle continuation line would keep its leading indent in the joined
# command (only the last line is fully stripped), so this pins the layout.
_CHAINED_FLOORS = _gates_section(
    _bash_block(
        "uv run --extra mcp pytest tests/ -m 'not integration' \\",
        "  --cov=cw --cov=.claude/scripts --cov-report=xml"
        " && uv run coverage report --include='src/cw/*' --fail-under=88"
        " && uv run coverage report --include='.claude/scripts/*' --fail-under=50"
        "  # 10. Unit + per-tree cov",
        "uv run pytest tests/ -m integration          # 11. Integration",
    )
)

_CHAINED_FLOORS_GATE_COUNT = 2


class TestChainedCoverageFloors:
    def test_chained_floors_fold_into_one_single_spaced_gate(
        self, tmp_path: Path
    ) -> None:
        path = _write_claude_md(tmp_path, _CHAINED_FLOORS)
        gates = _parse_claude_md_gates(path)
        assert len(gates) == _CHAINED_FLOORS_GATE_COUNT
        unit = gates[0]
        assert unit.name == "pytest-not-integration"
        assert (
            "&& uv run coverage report --include='src/cw/*' --fail-under=88"
            in unit.command
        )
        assert "  " not in unit.command, unit.command
        assert "\\" not in unit.command
        assert gates[1].name == "pytest-integration"


# ---------------------------------------------------------------------------
# #2187: prose bullets are not gates
# ---------------------------------------------------------------------------

_NOT_GATE_BULLETS = [
    "- Test suite: 100% pass rate required",
    "- Coverage target: total >=88%",
    "- Overview",
    "- name:  | uv run fix",
]


class TestBulletProseRejection:
    def test_ticket_prose_bullet_is_not_a_gate(self, tmp_path: Path) -> None:
        """The exact prose line from this repo's CLAUDE.md must not parse."""
        content = _gates_section(
            "- No suppressions (`# noqa`, `# type: ignore`) without explicit "
            "user approval"
        )
        path = _write_claude_md(tmp_path, content)
        assert _parse_claude_md_gates(path) == []

    def test_prose_bullet_beside_real_bullet_gate(self, tmp_path: Path) -> None:
        content = _gates_section(
            "- mypy: uv run mypy .",
            "- No suppressions (`# noqa`, `# type: ignore`) without explicit "
            "user approval",
        )
        path = _write_claude_md(tmp_path, content)
        assert [g.name for g in _parse_claude_md_gates(path)] == ["mypy"]

    @pytest.mark.parametrize("bullet", _NOT_GATE_BULLETS)
    def test_multi_word_or_malformed_bullet_is_not_a_gate(
        self, tmp_path: Path, bullet: str
    ) -> None:
        path = _write_claude_md(tmp_path, _gates_section(bullet))
        assert _parse_claude_md_gates(path) == []

    def test_backtick_fenced_command_and_autofix_unwrapped(
        self, tmp_path: Path
    ) -> None:
        content = _gates_section(
            "- ruff: `uv run ruff check .` | `uv run ruff check --fix .`"
        )
        path = _write_claude_md(tmp_path, content)
        (gate,) = _parse_claude_md_gates(path)
        assert gate.name == "ruff"
        assert gate.command == "uv run ruff check ."
        assert gate.autofix == "uv run ruff check --fix ."

    def test_inner_backticks_are_left_alone(self, tmp_path: Path) -> None:
        """Only one wrapping backtick pair is stripped, never inner ones."""
        content = _gates_section("- shell: echo `date` now")
        path = _write_claude_md(tmp_path, content)
        (gate,) = _parse_claude_md_gates(path)
        assert gate.command == "echo `date` now"


# ---------------------------------------------------------------------------
# #2187: _split_command (replaces _derive_gate_name)
# ---------------------------------------------------------------------------


class TestSplitCommand:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("uv run --extra mcp pytest tests/", ("pytest", ["tests/"])),
            ("uv run --python=3.13 ruff check", ("ruff", ["check"])),
            ("uv run", ("", [])),
            ("uv run --extra", ("", [])),
            ("", ("", [])),
            ("npx eslint .", ("eslint", ["."])),
            ("npx", ("npx", [])),
            ("uv lock --check", ("uv", ["lock", "--check"])),
            ("diff-cover coverage.xml", ("diff-cover", ["coverage.xml"])),
        ],
    )
    def test_split_command(self, command: str, expected: tuple[str, list[str]]) -> None:
        assert _mod._split_command(command) == expected


# ---------------------------------------------------------------------------
# #2187: collision-only gate name disambiguation
# ---------------------------------------------------------------------------


class TestGateNameDisambiguation:
    def _names(self, tmp_path: Path, content: str) -> list[str]:
        path = _write_claude_md(tmp_path, content)
        return [g.name for g in _parse_claude_md_gates(path)]

    def test_ruff_check_and_format_use_subcommand(self, tmp_path: Path) -> None:
        content = _gates_section(
            _bash_block(
                "uv run ruff check src/ tests/",
                "uv run ruff format --check src/ tests/",
            )
        )
        assert self._names(tmp_path, content) == ["ruff-check", "ruff-format"]

    def test_pytest_pair_uses_marker_expression(self, tmp_path: Path) -> None:
        names = self._names(tmp_path, _BASH_BLOCK_ONLY)
        assert names == [
            "ruff-check",
            "ruff-format",
            "mypy",
            "pre-commit",
            "pytest-not-integration",
            "pytest-integration",
            "diff-cover",
        ]

    def test_double_quoted_marker_expression(self, tmp_path: Path) -> None:
        content = _gates_section(
            _bash_block('uv run pytest -m "not slow"', "uv run pytest -m slow")
        )
        assert self._names(tmp_path, content) == ["pytest-not-slow", "pytest-slow"]

    def test_non_colliding_gates_keep_bare_names(self, tmp_path: Path) -> None:
        content = _gates_section(
            _bash_block("uv run ruff check src/", "uv run pytest tests/")
        )
        assert self._names(tmp_path, content) == ["ruff", "pytest"]

    def test_residual_duplicates_get_ordinals(self, tmp_path: Path) -> None:
        content = _gates_section(_bash_block("uv run mypy src/", "uv run mypy tests/"))
        assert self._names(tmp_path, content) == ["mypy", "mypy-2"]

    def test_residual_duplicates_after_qualifier_get_ordinals(
        self, tmp_path: Path
    ) -> None:
        content = _gates_section(
            _bash_block("uv run ruff check a/", "uv run ruff check b/")
        )
        assert self._names(tmp_path, content) == ["ruff-check", "ruff-check-2"]

    def test_duplicates_split_across_fences_are_disambiguated(
        self, tmp_path: Path
    ) -> None:
        content = _gates_section(
            _bash_block("uv run ruff check src/"),
            "",
            _bash_block("uv run ruff format --check src/"),
        )
        assert self._names(tmp_path, content) == ["ruff-check", "ruff-format"]


# ---------------------------------------------------------------------------
# #1432: gate-timeout / gate-elapsed liveness helpers
# ---------------------------------------------------------------------------


class TestGateTimeoutSeconds:
    def test_known_gate_returns_configured_ceiling(self) -> None:
        assert _mod.gate_timeout_seconds("mypy") == 600
        assert _mod.gate_timeout_seconds("pytest") == 600
        assert _mod.gate_timeout_seconds("pre-commit") == 480

    def test_unknown_gate_falls_back_to_default(self) -> None:
        assert _mod.gate_timeout_seconds("ruff") == _mod.GATE_TIMEOUT_FALLBACK_SECONDS

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("pytest-integration", 600),
            ("pytest-not-integration", 600),
            ("pre-commit", 480),
            ("pre-commit-hooks", 480),
            ("mypy-strict", 600),
        ],
    )
    def test_derived_name_resolves_by_longest_hyphen_prefix(
        self, name: str, expected: int
    ) -> None:
        assert _mod.gate_timeout_seconds(name) == expected

    def test_unrelated_derived_name_falls_back(self) -> None:
        assert (
            _mod.gate_timeout_seconds("ruff-check")
            == _mod.GATE_TIMEOUT_FALLBACK_SECONDS
        )

    def test_gate_timeout_cli_subcommand_json_shape(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)  # no project-config.yaml: built-in table only
        _mod.cmd_gate_timeout(argparse.Namespace(name="mypy"))
        out = json.loads(capsys.readouterr().out)
        assert set(out.keys()) == {
            "gate",
            "foreground_ceiling_s",
            "poll_ceiling_s",
            "source",
            "configured_s",
            "clamped",
            "note",
        }
        assert out["gate"] == "mypy"
        assert out["source"] == "default"
        assert out["configured_s"] is None
        assert out["clamped"] is False
        assert out["note"] is None
        assert out["foreground_ceiling_s"] == _mod.gate_timeout_seconds("mypy")
        assert out["poll_ceiling_s"] == _mod.GATE_POLL_CEILING_SECONDS


class TestGateElapsedExceedsCeiling:
    def test_not_exceeded_when_elapsed_below_ceiling(self) -> None:
        started = datetime(2026, 1, 1, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)  # +300s
        result = _mod.elapsed_exceeds_ceiling(
            started.isoformat(), ceiling_s=600, now=now
        )
        assert result["exceeded"] is False

    def test_exceeded_when_elapsed_above_ceiling(self) -> None:
        started = datetime(2026, 1, 1, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC)  # +900s
        result = _mod.elapsed_exceeds_ceiling(
            started.isoformat(), ceiling_s=600, now=now
        )
        assert result["exceeded"] is True

    def test_exceeded_at_exact_boundary_is_false(self) -> None:
        """Strict > comparison: elapsed == ceiling does not count as exceeded."""
        started = datetime(2026, 1, 1, tzinfo=UTC)
        now = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)  # +600s exactly
        result = _mod.elapsed_exceeds_ceiling(
            started.isoformat(), ceiling_s=600, now=now
        )
        assert result["exceeded"] is False

    def test_malformed_timestamp_raises_or_reports_error(self) -> None:
        with pytest.raises(ValueError, match="not-a-timestamp"):
            _mod.elapsed_exceeds_ceiling("not-a-timestamp", ceiling_s=600)

    def test_timezone_naive_timestamp_raises_value_error(self) -> None:
        """A naive timestamp (no offset) must raise ValueError, not an
        uncaught TypeError from subtracting it against a tz-aware 'now'.
        """
        naive = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None).isoformat()
        with pytest.raises(ValueError, match="timezone-aware"):
            _mod.elapsed_exceeds_ceiling(naive, ceiling_s=600)

    def test_cli_gate_elapsed_json_shape(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        started = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
        _mod.cmd_gate_elapsed(argparse.Namespace(started=started, ceiling_seconds=600))
        out = json.loads(capsys.readouterr().out)
        assert set(out.keys()) == {"elapsed_s", "ceiling_s", "exceeded"}
        assert out["ceiling_s"] == 600
        assert isinstance(out["exceeded"], bool)


# ---------------------------------------------------------------------------
# #1867: ECOSYSTEM_GATES["pyproject.toml"] default gate list — ruff-format gate
# ---------------------------------------------------------------------------


class TestEcosystemGatesPyproject:
    def test_default_gates_include_ruff_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch)
        names = [g["name"] for g in result["gates"]]
        assert "ruff-format" in names

    def test_ruff_format_gate_command_and_autofix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch)
        ruff_format = next(g for g in result["gates"] if g["name"] == "ruff-format")
        assert ruff_format["command"] == "uv run ruff format --check ."
        assert ruff_format["autofix"] == "uv run ruff format ."

    def test_ruff_check_and_ruff_format_are_distinct_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch)
        names = [g["name"] for g in result["gates"]]
        assert names.count("ruff") == 1
        assert names.count("ruff-format") == 1

    def test_existing_default_gates_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch)
        names = {g["name"] for g in result["gates"]}
        assert names == {"ruff", "ruff-format", "mypy", "pytest"}

    def test_claude_md_override_still_wins_for_ruff_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        _write_claude_md(
            tmp_path,
            "# Project\n\n## Quality Gates\n\n"
            "- ruff-format: uv run ruff format --check review_bingo_hub\n",
        )
        monkeypatch.chdir(tmp_path)
        result = cast("dict[str, Any]", _mod.detect_gates())
        ruff_format = next(g for g in result["gates"] if g["name"] == "ruff-format")
        assert ruff_format["command"] == "uv run ruff format --check review_bingo_hub"


# ---------------------------------------------------------------------------
# #2187: a CLAUDE.md bash block is authoritative — ecosystem defaults dropped
# ---------------------------------------------------------------------------

_BULLET_OVERRIDES = _gates_section(
    "- mypy: uv run mypy --strict src/",
    "- pytest: uv run pytest -q",
)

_NO_GATE_CONTENT = [
    _gates_section(_bash_block("# only a comment")),
    _UNCLOSED_FENCE,
]


class TestDetectGatesAuthoritativeBlock:
    def test_bash_block_drops_ecosystem_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch, claude_md=_BASH_BLOCK_ONLY)
        gates = result["gates"]
        assert [g["name"] for g in gates] == [
            "ruff-check",
            "ruff-format",
            "mypy",
            "pre-commit",
            "pytest-not-integration",
            "pytest-integration",
            "diff-cover",
        ]
        (ruff_format,) = (g for g in gates if g["name"] == "ruff-format")
        assert ruff_format["command"] == "uv run ruff format --check src/ tests/"
        assert all(g["command"] != "uv run ruff format --check ." for g in gates)
        assert result["detected_from"] == ["CLAUDE.md"]

    def test_bullet_only_claude_md_still_merges_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch, claude_md=_BULLET_OVERRIDES)
        by_name = {g["name"]: g for g in result["gates"]}
        assert set(by_name) == {"ruff", "ruff-format", "mypy", "pytest"}
        assert by_name["ruff-format"]["command"] == "uv run ruff format --check ."
        assert by_name["mypy"]["command"] == "uv run mypy --strict src/"
        assert by_name["pytest"]["command"] == "uv run pytest -q"
        assert result["detected_from"] == ["pyproject.toml", "CLAUDE.md"]

    @pytest.mark.parametrize("content", _NO_GATE_CONTENT)
    def test_block_with_no_gates_falls_back_to_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch, claude_md=content)
        names = {g["name"] for g in result["gates"]}
        assert names == {"ruff", "ruff-format", "mypy", "pytest"}
        assert result["detected_from"] == ["pyproject.toml"]
        parsed = _raw_parse(tmp_path / "CLAUDE.md")
        assert parsed.authoritative is False
        assert parsed.gates == []

    def test_block_with_gates_is_authoritative(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _BASH_BLOCK_ONLY)
        assert cast("_ClaudeMdGatesP", _raw_parse(path)).authoritative is True

    def test_bullet_only_is_not_authoritative(self, tmp_path: Path) -> None:
        path = _write_claude_md(tmp_path, _BULLET_OVERRIDES)
        assert cast("_ClaudeMdGatesP", _raw_parse(path)).authoritative is False

    def test_mixed_bullet_and_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _detect_gates(tmp_path, monkeypatch, claude_md=_MIXED)
        by_name = {g["name"]: g for g in result["gates"]}
        # defaults dropped; non-colliding bullet (mypy) kept; the bullet whose
        # name equals a block gate name (ruff) is overridden by the block.
        assert set(by_name) == {"mypy", "ruff", "diff-cover"}
        assert by_name["ruff"]["command"] == "uv run ruff check src/ tests/"
        assert result["detected_from"] == ["CLAUDE.md"]


# ---------------------------------------------------------------------------
# #2187: pin the repo's own CLAUDE.md gate list (CI contract tripwire)
# ---------------------------------------------------------------------------

# The gate that runs the whole pre-commit hook suite. Located by exact command:
# the actionlint gate (#1626) also runs `pre-commit`, so the derived names are
# disambiguated (`pre-commit-run`, `pre-commit-run-2`) and neither is stable.
HOOK_SUITE_COMMAND = "uv run pre-commit run --all-files"

EXPECTED_REPO_GATES: list[tuple[str, str]] = [
    ("uv-lock", "uv lock --check"),
    ("uv-sync", "uv sync --locked --dev --extra mcp"),
    ("ruff-check", "uv run ruff check src/ tests/"),
    ("ruff-format", "uv run ruff format --check src/ tests/"),
    ("mypy", "uv run mypy --strict src/"),
    ("python", "uv run python .claude/scripts/check_imports.py"),
    ("python-2", "uv run python .claude/scripts/check_changelog_frozen.py"),
    ("pre-commit-run", "uv run pre-commit run actionlint --all-files"),
    ("pre-commit-run-2", HOOK_SUITE_COMMAND),
    (
        "pytest-not-integration",
        "uv run --extra mcp pytest tests/ -m 'not integration' "
        "--cov=cw --cov=.claude/scripts --cov-report=xml "
        "&& uv run coverage report --include='src/cw/*' --fail-under=88 "
        "&& uv run coverage report --include='.claude/scripts/*' --fail-under=50",
    ),
    ("pytest-integration", "uv run pytest tests/ -m integration"),
    (
        "diff-cover",
        "uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=90",
    ),
]

_PIN_GUIDANCE = (
    "If you intentionally added, removed, reordered or changed a gate in "
    "CLAUDE.md `## Quality Gates`, update `EXPECTED_REPO_GATES` in this file and "
    "confirm `.github/workflows/ci.yml` agrees. If you did not touch the gate "
    "list, `detect-gates` regressed: a prose bullet parsed as a gate, an "
    "ecosystem default leaked back in, or the naming scheme changed."
)


_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Index of the sync gate in the repo's list (gate 2, right after `uv lock --check`).
_SYNC_GATE_INDEX = 1


def _real_repo_gates(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the real ``detect-gates`` against this repo's CLAUDE.md (full result)."""
    monkeypatch.chdir(_REPO_ROOT)
    return cast("dict[str, Any]", _mod.detect_gates())


class TestRealClaudeMdGates:
    def test_repo_claude_md_yields_pinned_gate_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _real_repo_gates(monkeypatch)
        gates = result["gates"]
        actual = [(g["name"], g["command"]) for g in gates]
        assert actual == EXPECTED_REPO_GATES, _PIN_GUIDANCE
        assert result["detected_from"] == ["CLAUDE.md"], _PIN_GUIDANCE
        assert all("autofix" not in g for g in gates), _PIN_GUIDANCE
        assert len({name for name, _ in actual}) == len(EXPECTED_REPO_GATES)

    def test_lock_check_first_then_locked_sync(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Redundant with the exact-list pin above, kept to document intent (#2188):
        # gate 1 asserts the lock, gate 2 syncs the venv against it with --locked
        # (so it fails instead of rewriting a stale lock), both before type checks.
        gates = _real_repo_gates(monkeypatch)["gates"]
        assert len(gates) > _SYNC_GATE_INDEX, (
            f"expected at least {_SYNC_GATE_INDEX + 1} gates, got {len(gates)}. "
            + _PIN_GUIDANCE
        )
        assert gates[0]["command"] == "uv lock --check", _PIN_GUIDANCE
        sync_command = gates[_SYNC_GATE_INDEX]["command"]
        assert sync_command.startswith("uv sync"), _PIN_GUIDANCE
        assert "--locked" in sync_command.split(), _PIN_GUIDANCE
        names = [g["name"] for g in gates]
        assert "mypy" in names, "no `mypy` gate detected. " + _PIN_GUIDANCE
        assert names.index("mypy") > _SYNC_GATE_INDEX, (
            "the venv sync must run before the type check. " + _PIN_GUIDANCE
        )

    def test_sync_gate_mirrors_ci_extras(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gates = _real_repo_gates(monkeypatch)["gates"]
        assert len(gates) > _SYNC_GATE_INDEX, (
            f"expected at least {_SYNC_GATE_INDEX + 1} gates, got {len(gates)}. "
            + _PIN_GUIDANCE
        )
        ci = _CI_WORKFLOW.read_text(encoding="utf-8")
        # Anchor on the `run:` lines: the same phrases also appear earlier in ci.yml
        # comments, so a raw substring index would compare comment positions.
        m_lock = re.search(r"^\s*run: uv lock --check\s*$", ci, re.MULTILINE)
        m_sync = re.search(r"^\s*run: (uv sync .*)$", ci, re.MULTILINE)
        assert m_lock is not None, (
            "no `run: uv lock --check` step in ci.yml (comments are not matched)"
        )
        assert m_sync is not None, (
            "no `run: uv sync ...` step in ci.yml (comments are not matched)"
        )
        assert m_lock.start() < m_sync.start(), (
            "ci.yml must assert the lock before it syncs the venv"
        )
        ci_extras = re.findall(r"--extra (\S+)", m_sync.group(1))
        assert ci_extras, "ci.yml's `uv sync` step installs no `--extra`"
        sync_command = gates[_SYNC_GATE_INDEX]["command"]
        for extra in ci_extras:
            assert f"--extra {extra}" in sync_command, (
                f"CI syncs `--extra {extra}` but the CLAUDE.md sync gate "
                f"({sync_command!r}) does not. " + _PIN_GUIDANCE
            )

    def test_install_md_dev_setup_uses_sync_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # INSTALL.md's contributor setup must run the exact CLAUDE.md gate-2
        # command: a bare `uv sync` is exact and removes the mcp extra, so a
        # venv predating an `mcp` bump then fails mypy with `[arg-type]` (#2242).
        sync_command = _real_repo_gates(monkeypatch)["gates"][_SYNC_GATE_INDEX][
            "command"
        ]
        install_md = (_REPO_ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        fences = _bash_fences(install_md)
        dev_fences = [fence for fence in fences if "uv run cw --help" in fence]
        assert len(dev_fences) == 1, (
            "expected exactly one INSTALL.md bash fence containing "
            f"`uv run cw --help` (the dev setup), got {len(dev_fences)}"
        )
        dev_lines = [
            line.split("#", 1)[0].strip() for line in dev_fences[0].splitlines()
        ]
        assert sync_command in dev_lines, (
            f"INSTALL.md dev setup must run the CLAUDE.md sync gate {sync_command!r}"
        )
        bare_sync = [
            line
            for fence in fences
            for line in (raw.split("#", 1)[0].strip() for raw in fence.splitlines())
            if line == "uv sync"
        ]
        assert not bare_sync, "INSTALL.md has a bare `uv sync` in a bash fence"

    def test_gate_number_prose_matches_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Renumbering a gate silently invalidates prose that names gates by number
        # (the drift class #1565 fixed by hand). Assert derived facts, not wording.
        section = _load_claude_md_quality_gates(_REPO_ROOT)
        assert section is not None, "CLAUDE.md has no `## Quality Gates` section"
        fences = _bash_fences(section)
        assert len(fences) == 1, (
            f"expected exactly one bash fence in `## Quality Gates`, got {len(fences)}"
        )
        # Only the fenced block: the prose holds other `#N`-shaped tokens (#436, #1).
        numbers = [int(n) for n in re.findall(r"#\s+(\d+)\.\s", fences[0])]
        assert numbers == list(range(1, len(EXPECTED_REPO_GATES) + 1)), (
            f"`# N.` gate comments are not consecutive from 1: {numbers}"
        )
        collapsed = " ".join(section.split())
        m_hook = re.search(r"gates? (\d+) \*is\* the hook suite", collapsed)
        assert m_hook is not None, (
            "prose no longer says which gate `*is* the hook suite`"
        )
        commands = [g["command"] for g in _real_repo_gates(monkeypatch)["gates"]]
        assert HOOK_SUITE_COMMAND in commands, (
            f"no `{HOOK_SUITE_COMMAND}` gate detected. " + _PIN_GUIDANCE
        )
        assert int(m_hook.group(1)) == commands.index(HOOK_SUITE_COMMAND) + 1, (
            "the prose names the wrong gate number for the hook suite. " + _PIN_GUIDANCE
        )


# ---------------------------------------------------------------------------
# #2758: per-repo gate-timeout override (quality_gates.timeouts)
# ---------------------------------------------------------------------------

_BASH_CAP_S = 600
_PRE_COMMIT_DEFAULT_S = 480
_CONFIG_REFERENCE = _REPO_ROOT / "config" / "CONFIG_REFERENCE.md"


def _timeouts_yaml(*entries: str) -> str:
    """A canonical ``quality_gates.timeouts`` block holding *entries*."""
    return "quality_gates:\n  timeouts:\n" + "".join(f"    {e}\n" for e in entries)


def _config_path(root: Path) -> Path:
    return root / ".claude" / "project-config.yaml"


def _resolve(tmp_path: Path, name: str, content: str | None = None) -> dict[str, Any]:
    """Resolve *name* against a tmp project-config.yaml holding *content*."""
    if content is not None:
        _write_project_config_yaml(tmp_path, content)
    return cast(
        "dict[str, Any]",
        _mod.resolve_gate_timeout(name, config_path=_config_path(tmp_path)),
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture(params=["pyyaml", "no-pyyaml"])
def yaml_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once with PyYAML and once through the dependency-free scanner."""
    mode = cast("str", request.param)
    if mode == "no-pyyaml":
        monkeypatch.setattr(_mod, "yaml", None)
    return mode


_INVALID_TIMEOUT_VALUES = ['"fast"', "1.5", "true", "0", "-5"]

_MALFORMED_CONFIGS = [
    pytest.param("a: [unclosed\n", id="malformed-yaml"),
    pytest.param("- a\n- b\n", id="root-not-mapping"),
    pytest.param("quality_gates:\n  - 1\n  - 2\n", id="section-not-mapping"),
    pytest.param("quality_gates:\n  timeouts: 5\n", id="timeouts-not-mapping"),
]

_NON_CANONICAL_CONFIGS = [
    pytest.param("quality_gates: {timeouts: {pre-commit: 600}}\n", id="flow-root"),
    pytest.param("quality_gates:\n  timeouts: {pre-commit: 600}\n", id="flow-timeouts"),
    pytest.param(_timeouts_yaml("pre-commit 600"), id="entry-without-colon"),
]


class TestGateTimeoutConfigOverride:
    def test_exact_key_overrides_default(self, tmp_path: Path, yaml_mode: str) -> None:
        del yaml_mode
        out = _resolve(tmp_path, "pre-commit", _timeouts_yaml("pre-commit: 600"))
        assert out["foreground_ceiling_s"] == _BASH_CAP_S
        assert out["source"] == "project-config"
        assert out["configured_s"] == _BASH_CAP_S
        assert out["clamped"] is False
        assert out["note"] is None

    def test_prefix_key_covers_derived_name(
        self, tmp_path: Path, yaml_mode: str
    ) -> None:
        del yaml_mode
        out = _resolve(tmp_path, "pre-commit-run-2", _timeouts_yaml("pre-commit: 600"))
        assert out["foreground_ceiling_s"] == _BASH_CAP_S
        assert out["source"] == "project-config"

    def test_exact_key_beats_prefix_key(self, tmp_path: Path, yaml_mode: str) -> None:
        del yaml_mode
        content = _timeouts_yaml("pre-commit: 600", "pre-commit-run-2: 300")
        out = _resolve(tmp_path, "pre-commit-run-2", content)
        assert out["foreground_ceiling_s"] == 300
        assert out["configured_s"] == 300

    def test_unconfigured_gate_keeps_default(self, tmp_path: Path) -> None:
        out = _resolve(tmp_path, "mypy", _timeouts_yaml("pre-commit: 600"))
        assert out["foreground_ceiling_s"] == _mod.gate_timeout_seconds("mypy")
        assert out["source"] == "default"
        assert out["configured_s"] is None
        assert out["clamped"] is False
        assert out["gate"] == "mypy"
        assert out["poll_ceiling_s"] == _mod.GATE_POLL_CEILING_SECONDS

    def test_override_below_default_is_honoured(self, tmp_path: Path) -> None:
        out = _resolve(tmp_path, "pre-commit", _timeouts_yaml("pre-commit: 120"))
        assert out["foreground_ceiling_s"] == 120

    def test_override_above_cap_is_clamped(
        self, tmp_path: Path, yaml_mode: str
    ) -> None:
        del yaml_mode
        out = _resolve(tmp_path, "pre-commit", _timeouts_yaml("pre-commit: 900"))
        assert out["foreground_ceiling_s"] == _BASH_CAP_S
        assert out["clamped"] is True
        assert out["configured_s"] == 900
        assert "900" in out["note"]
        assert "600" in out["note"]

    def test_override_at_cap_is_not_clamped(self, tmp_path: Path) -> None:
        out = _resolve(tmp_path, "pre-commit", _timeouts_yaml("pre-commit: 600"))
        assert out["clamped"] is False

    @pytest.mark.parametrize("value", _INVALID_TIMEOUT_VALUES)
    def test_invalid_value_falls_back_with_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        yaml_mode: str,
        value: str,
    ) -> None:
        del yaml_mode
        content = _timeouts_yaml(f"pre-commit: {value}", "mypy: 300")
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit", content)
            sibling = _resolve(tmp_path, "mypy")
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert out["source"] == "default"
        assert any("pre-commit" in w for w in _warnings(caplog))
        # One bad entry does not poison a valid sibling.
        assert sibling["foreground_ceiling_s"] == 300
        assert sibling["source"] == "project-config"

    def test_missing_file_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit")
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert out["source"] == "default"
        assert _warnings(caplog) == []

    @pytest.mark.parametrize("content", _MALFORMED_CONFIGS)
    def test_malformed_config_falls_back_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit", content)
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert out["source"] == "default"
        assert _warnings(caplog)

    def test_empty_config_is_silent_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit", "")
        assert out["source"] == "default"
        assert _warnings(caplog) == []

    def test_config_without_section_is_silent_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, yaml_mode: str
    ) -> None:
        del yaml_mode
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit", "pr:\n  tool: gh\n")
            no_timeouts = _resolve(
                tmp_path, "pre-commit", "quality_gates:\n  other: 1\n"
            )
        assert out["source"] == "default"
        assert no_timeouts["source"] == "default"
        assert _warnings(caplog) == []

    def test_non_utf8_config_falls_back_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, yaml_mode: str
    ) -> None:
        del yaml_mode
        path = _config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"quality_gates:\n  timeouts:\n    pre-commit: \xff\xfe\n")
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit")
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert _warnings(caplog)

    def test_config_path_directory_falls_back_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _config_path(tmp_path).mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit")
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert _warnings(caplog)

    def test_cmd_gate_timeout_reads_cwd_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_project_config_yaml(tmp_path, _timeouts_yaml("pre-commit: 900"))
        monkeypatch.chdir(tmp_path)
        _mod.cmd_gate_timeout(argparse.Namespace(name="pre-commit-run-2"))
        out = json.loads(capsys.readouterr().out)
        assert out["foreground_ceiling_s"] == _BASH_CAP_S
        assert out["clamped"] is True
        assert out["configured_s"] == 900

    def test_live_repo_config_sets_hook_suite_to_cap(self) -> None:
        out = _mod.resolve_gate_timeout(
            "pre-commit-run-2",
            config_path=_REPO_ROOT / ".claude" / "project-config.yaml",
        )
        assert out["foreground_ceiling_s"] == _BASH_CAP_S
        assert out["source"] == "project-config"
        assert out["clamped"] is False

    def test_config_reference_documents_timeouts_key(self) -> None:
        text = _CONFIG_REFERENCE.read_text(encoding="utf-8")
        assert "quality_gates.timeouts" in text


class TestGateTimeoutScannerWithoutPyYaml:
    """The dependency-free scanner used when PyYAML is not importable."""

    @pytest.fixture(autouse=True)
    def _no_pyyaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_mod, "yaml", None)

    def test_trailing_comment_is_stripped(self, tmp_path: Path) -> None:
        out = _resolve(
            tmp_path,
            "pre-commit-run-2",
            _timeouts_yaml("pre-commit: 600  # Bash cap"),
        )
        assert out["foreground_ceiling_s"] == _BASH_CAP_S
        assert out["source"] == "project-config"

    def test_comment_line_between_keys_is_tolerated(self, tmp_path: Path) -> None:
        content = _timeouts_yaml("mypy: 300", "# why", "pre-commit: 600")
        assert _resolve(tmp_path, "mypy", content)["foreground_ceiling_s"] == 300
        assert _resolve(tmp_path, "pre-commit")["foreground_ceiling_s"] == 600

    def test_top_level_key_after_block_stops_the_scan(self, tmp_path: Path) -> None:
        content = _timeouts_yaml("mypy: 300") + "other:\n  pre-commit: 900\n"
        assert _resolve(tmp_path, "mypy", content)["foreground_ceiling_s"] == 300
        out = _resolve(tmp_path, "pre-commit")
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert out["source"] == "default"

    def test_sibling_key_does_not_leak(self, tmp_path: Path) -> None:
        content = _timeouts_yaml("mypy: 300") + "  other:\n    pre-commit: 900\n"
        out = _resolve(tmp_path, "pre-commit", content)
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert _resolve(tmp_path, "mypy")["foreground_ceiling_s"] == 300

    def test_timeouts_under_another_section_is_ignored(self, tmp_path: Path) -> None:
        content = "other:\n  timeouts:\n    pre-commit: 600\n"
        out = _resolve(tmp_path, "pre-commit", content)
        assert out["source"] == "default"

    @pytest.mark.parametrize("content", _NON_CANONICAL_CONFIGS)
    def test_non_canonical_shape_falls_back_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = _resolve(tmp_path, "pre-commit", content)
        assert out["foreground_ceiling_s"] == _PRE_COMMIT_DEFAULT_S
        assert out["source"] == "default"
        assert _warnings(caplog)


def test_dash_s_gate_timeout_reads_config_without_pyyaml(tmp_path: Path) -> None:
    """Under ``python -S`` (no site-packages, so no PyYAML) the override still
    applies: the script's own scanner reads the canonical block."""
    hidden = subprocess.run(
        [*_python(no_site=True), "-c", "import yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert hidden.returncode != 0
    assert "No module named 'yaml'" in hidden.stderr
    _write_project_config_yaml(tmp_path, _timeouts_yaml("pre-commit: 600"))
    result = subprocess.run(
        [*_python(no_site=True), str(_SCRIPT), "gate-timeout", "pre-commit-run-2"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["foreground_ceiling_s"] == _BASH_CAP_S
    assert out["source"] == "project-config"


# ---------------------------------------------------------------------------
# #2758: gate-pass stamps — skip a redundant gate re-run at an unchanged SHA
# ---------------------------------------------------------------------------

_QUOTED_PYTEST_GATE = (
    "uv run pytest tests/ -m 'not integration' "
    "&& uv run coverage report --fail-under=88"
)
_GATE_REPO_CLAUDE_MD = _gates_section(
    _bash_block("uv run mypy --strict src/", _QUOTED_PYTEST_GATE)
)
_STATE_REL = Path(".claude") / "prep-pr-state.json"


@pytest.fixture
def gate_repo(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A real git repo with a committed CLAUDE.md gate set, as the cwd.

    Every inherited ``GIT_*`` var is removed first: ``prep_pr_state._run_git``
    runs git with the inherited env, and pytest may itself run inside a git
    hook whose ``GIT_DIR`` / ``GIT_INDEX_FILE`` would redirect git away from
    this repo (green locally, red in a hook or CI). The state file is left
    untracked and unignored on purpose, so the dirty check's pathspec
    exclusion is what keeps it from dirtying the tree.
    """
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    repo = make_git_repo("repo")
    commit_tracked_file(repo, "CLAUDE.md", _GATE_REPO_CLAUDE_MD)
    monkeypatch.chdir(repo)
    return repo


def _gate_names() -> list[str]:
    return [g["name"] for g in _mod.detect_gates()["gates"]]


def _gate_pass(
    capsys: pytest.CaptureFixture[str], action: str, name: str, base: str = "main"
) -> dict[str, Any]:
    _mod.cmd_gate_pass(argparse.Namespace(action=action, name=name, base=base))
    return cast("dict[str, Any]", json.loads(capsys.readouterr().out))


def _state_json(repo: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((repo / _STATE_REL).read_text()))


def _stamps(repo: Path, name: str) -> list[dict[str, Any]]:
    return [p for p in _state_json(repo)["gate_passes"] if p["name"] == name]


class TestGatePass:
    def test_record_then_check_skips_at_same_sha(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gates = _mod.detect_gates()["gates"]
        assert any(g["command"] == _QUOTED_PYTEST_GATE for g in gates)
        head = git_in(gate_repo, "rev-parse", "HEAD")
        for name in _gate_names():
            recorded = _gate_pass(capsys, "record", name)
            assert recorded == {"gate": name, "recorded": True, "sha": head}
            checked = _gate_pass(capsys, "check", name)
            assert checked["skip"] is True
            assert checked["reason"] == "matched"
            assert checked["sha"] == head
            assert checked["stamped_sha"] == head
            assert head in checked["message"]
            assert name in checked["message"]

    def test_new_commit_is_sha_changed(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        commit_tracked_file(gate_repo, "src/new.py")
        checked = _gate_pass(capsys, "check", name)
        assert checked["skip"] is False
        assert checked["reason"] == "sha-changed"
        assert checked["message"] is None

    def test_modified_tracked_file_is_dirty(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        (gate_repo / "CLAUDE.md").write_text(_GATE_REPO_CLAUDE_MD + "\nedit\n")
        assert _gate_pass(capsys, "check", name)["reason"] == "dirty-worktree"

    def test_untracked_file_is_dirty(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        (gate_repo / "scratch.txt").write_text("x\n")
        assert _gate_pass(capsys, "check", name)["reason"] == "dirty-worktree"

    def test_other_gate_stamp_is_no_stamp(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first, second = _gate_names()
        _gate_pass(capsys, "record", first)
        checked = _gate_pass(capsys, "check", second)
        assert checked["skip"] is False
        assert checked["reason"] == "no-stamp"
        assert checked["stamped_sha"] is None

    def test_no_state_is_no_stamp_and_creates_nothing(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        checked = _gate_pass(capsys, "check", _gate_names()[0])
        assert checked["reason"] == "no-stamp"
        assert not (gate_repo / _STATE_REL).exists()

    def test_unknown_gate(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        checked = _gate_pass(capsys, "check", "no-such-gate")
        assert checked["skip"] is False
        assert checked["reason"] == "unknown-gate"
        recorded = _gate_pass(capsys, "record", "no-such-gate")
        assert recorded == {
            "gate": "no-such-gate",
            "recorded": False,
            "reason": "unknown-gate",
        }
        assert not (gate_repo / _STATE_REL).exists()

    def test_edited_stamp_command_is_command_changed(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        state = _state_json(gate_repo)
        state["gate_passes"][0]["command"] = "uv run mypy ."
        (gate_repo / _STATE_REL).write_text(json.dumps(state))
        assert _gate_pass(capsys, "check", name)["reason"] == "command-changed"

    def test_unignored_state_file_does_not_dirty_the_tree(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        # The state file really is untracked and unignored here.
        assert "?? .claude/" in git_in(gate_repo, "status", "--porcelain")
        assert _gate_pass(capsys, "check", name)["skip"] is True

    def test_ignored_state_file_behaves_the_same(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commit_tracked_file(gate_repo, ".gitignore", f"{_STATE_REL.as_posix()}\n")
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        assert git_in(gate_repo, "status", "--porcelain") == ""
        assert _gate_pass(capsys, "check", name)["skip"] is True

    def test_record_on_dirty_tree_is_refused(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (gate_repo / "scratch.txt").write_text("x\n")
        name = _gate_names()[0]
        recorded = _gate_pass(capsys, "record", name)
        assert recorded == {"gate": name, "recorded": False, "reason": "dirty-worktree"}
        assert not (gate_repo / _STATE_REL).exists()

    def test_record_creates_state_with_base_branch(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _gate_pass(capsys, "record", _gate_names()[0], base="develop")
        assert _state_json(gate_repo)["base_branch"] == "develop"

    def test_second_record_replaces_the_stamp(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        _gate_pass(capsys, "check", name)
        assert len(_stamps(gate_repo, name)[0]["skipped_at"]) == 1
        _gate_pass(capsys, "record", name)
        stamps = _stamps(gate_repo, name)
        assert len(stamps) == 1
        assert stamps[0]["skipped_at"] == []

    def test_each_skip_is_recorded_on_the_stamp(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        assert _stamps(gate_repo, name)[0]["skipped_at"] == []
        _gate_pass(capsys, "check", name)
        assert len(_stamps(gate_repo, name)[0]["skipped_at"]) == 1
        _gate_pass(capsys, "check", name)
        skipped_at = _stamps(gate_repo, name)[0]["skipped_at"]
        assert len(skipped_at) == 2
        assert all(datetime.fromisoformat(ts).tzinfo is not None for ts in skipped_at)

    def test_non_skip_check_leaves_state_untouched(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        before = (gate_repo / _STATE_REL).read_bytes()
        (gate_repo / "scratch.txt").write_text("x\n")
        assert _gate_pass(capsys, "check", name)["skip"] is False
        assert (gate_repo / _STATE_REL).read_bytes() == before

    def test_status_reports_gate_skips(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        _mod.cmd_status(argparse.Namespace())
        assert json.loads(capsys.readouterr().out)["gate_skips"] == []
        _gate_pass(capsys, "check", name)
        _gate_pass(capsys, "check", name)
        _mod.cmd_status(argparse.Namespace())
        head = git_in(gate_repo, "rev-parse", "HEAD")
        assert json.loads(capsys.readouterr().out)["gate_skips"] == [
            {"gate": name, "sha": head, "count": 2}
        ]

    def test_unresolved_head_is_head_unresolved(
        self,
        gate_repo: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del gate_repo
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)

        def _git_fails(_args: list[str], *, default: str = "") -> str:
            return default

        monkeypatch.setattr(_mod, "_run_git", _git_fails)
        assert _gate_pass(capsys, "check", name)["reason"] == "head-unresolved"
        assert _gate_pass(capsys, "record", name) == {
            "gate": name,
            "recorded": False,
            "reason": "head-unresolved",
        }

    def test_git_status_failure_reads_as_dirty(
        self,
        gate_repo: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        name = _gate_names()[0]
        _gate_pass(capsys, "record", name)
        head = git_in(gate_repo, "rev-parse", "HEAD")

        def _status_fails(args: list[str], *, default: str = "") -> str:
            return head if args[0] == "rev-parse" else default

        monkeypatch.setattr(_mod, "_run_git", _status_fails)
        assert _gate_pass(capsys, "check", name)["reason"] == "dirty-worktree"


class TestRunGitDefault:
    def test_called_process_error_returns_default(self, gate_repo: Path) -> None:
        del gate_repo
        assert (
            _mod._run_git(["rev-parse", "--verify", "no-such-ref"], default="fb")
            == "fb"
        )
        assert _mod._run_git(["rev-parse", "--verify", "no-such-ref"]) == ""

    def test_missing_git_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _no_git(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError

        monkeypatch.setattr(_mod.subprocess, "run", _no_git)
        assert _mod._run_git(["status"], default="fb") == "fb"


def _stamp(sha: str = "abc", command: str = "uv run mypy .") -> Any:
    return _mod.GatePass(name="mypy", command=command, sha=sha, recorded="t")


@pytest.mark.parametrize(
    ("stamp_fields", "head", "dirty", "expected"),
    [
        pytest.param(None, "abc", False, "no-stamp", id="no-stamp"),
        pytest.param({}, "", False, "head-unresolved", id="head-unresolved"),
        pytest.param({}, "abc", True, "dirty-worktree", id="dirty"),
        pytest.param({"sha": "old"}, "abc", False, "sha-changed", id="sha"),
        pytest.param(
            {"command": "uv run mypy src/"},
            "abc",
            False,
            "command-changed",
            id="command",
        ),
        pytest.param({}, "abc", False, "matched", id="matched"),
    ],
)
def test_gate_skip_reason_table(
    stamp_fields: dict[str, str] | None, head: str, dirty: bool, expected: str
) -> None:
    stamp = None if stamp_fields is None else _stamp(**stamp_fields)
    assert _mod.gate_skip_reason(stamp, "uv run mypy .", head, dirty) == expected


class TestGatePassPersistence:
    def test_round_trip_keeps_gate_passes(self) -> None:
        state = _mod.PrepPrState(base_branch="main")
        stamp = _stamp()
        stamp.skipped_at.append("2026-01-01T00:00:00+00:00")
        state.gate_passes.append(stamp)
        loaded = _mod.PrepPrState.from_dict(json.loads(json.dumps(state.to_dict())))
        assert loaded.gate_passes == [stamp]

    def test_legacy_state_without_gate_passes_loads(self) -> None:
        legacy = {"base_branch": "main", "snapshots": [], "gates": []}
        assert _mod.PrepPrState.from_dict(legacy).gate_passes == []

    def test_gate_pass_from_dict_defaults_skipped_at(self) -> None:
        data = {"name": "mypy", "command": "c", "sha": "abc", "recorded": "t"}
        assert _mod.GatePass.from_dict(data).skipped_at == []

    def test_clean_removes_stamps(
        self, gate_repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _gate_pass(capsys, "record", _gate_names()[0])
        assert _mod.clean_state() is True
        assert not (gate_repo / _STATE_REL).exists()


class TestGatePassMainDispatch:
    def _main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        *argv: str,
    ) -> dict[str, Any]:
        monkeypatch.setattr(sys, "argv", ["prep_pr_state.py", *argv])
        _mod.main()
        return cast("dict[str, Any]", json.loads(capsys.readouterr().out))

    def test_record_check_and_timeout_dispatch(
        self,
        gate_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name = _gate_names()[0]
        recorded = self._main(monkeypatch, capsys, "gate-pass", "record", name)
        assert recorded["recorded"] is True
        assert _state_json(gate_repo)["base_branch"] == "main"
        checked = self._main(monkeypatch, capsys, "gate-pass", "check", name)
        assert checked["skip"] is True
        timeout = self._main(monkeypatch, capsys, "gate-timeout", name)
        assert timeout["source"] == "default"

    def test_record_base_option(
        self,
        gate_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        name = _gate_names()[0]
        self._main(monkeypatch, capsys, "gate-pass", "record", name, "--base", "dev")
        assert _state_json(gate_repo)["base_branch"] == "dev"

    def test_unknown_action_exits_nonzero(
        self, gate_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del gate_repo
        monkeypatch.setattr(
            sys, "argv", ["prep_pr_state.py", "gate-pass", "bogus", "x"]
        )
        with pytest.raises(SystemExit) as excinfo:
            _mod.main()
        assert excinfo.value.code != 0
