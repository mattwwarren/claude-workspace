"""Tests for scripts/install-skills.sh prune safety invariant.

Verifies that the manifest-scoped prune never removes paths that cw did not
itself install (the "foreign skill safety" guarantee).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REAL_SCRIPT = Path(__file__).parent.parent / "scripts" / "install-skills.sh"


def _scaffold_fake_install(tmp_path: Path, fake_repo: Path) -> Path:
    """Mirror the scripts/ directory layout expected by install-skills.sh.

    install-skills.sh derives PROJECT_DIR as dirname(dirname(BASH_SOURCE[0])).
    We place a copy of the script at <fake_repo>/scripts/install-skills.sh so
    that PROJECT_DIR resolves to <fake_repo>, which already has .claude/ set up
    by the fake_repo fixture.

    Returns the path to the executable script copy.
    """
    if not _REAL_SCRIPT.exists():
        pytest.fail(f"install-skills.sh not found at {_REAL_SCRIPT}")
    scripts_dir = fake_repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_copy = scripts_dir / "install-skills.sh"
    shutil.copy2(str(_REAL_SCRIPT), str(script_copy))
    script_copy.chmod(0o755)
    return script_copy


def _run(script: Path, fake_home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(fake_home)},
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Minimal fake repo .claude tree: 2 commands + 1 skill dir."""
    repo = tmp_path / "repo"
    commands = repo / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "auto-dev.md").write_text("# auto-dev\n")
    (commands / "review.md").write_text("# review\n")

    skill_dir = repo / ".claude" / "skills" / "cw-fanout"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# cw-fanout skill\n")

    return repo


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """Fake HOME directory with .claude subdir."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    return home


@pytest.fixture
def script(fake_repo: Path) -> Path:
    """Script copy placed so PROJECT_DIR === fake_repo."""
    return _scaffold_fake_install(fake_repo.parent, fake_repo)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInstallSkillsFirstRun:
    """First run with no prior manifest: files installed, manifest created."""

    def test_commands_copied(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert (fake_home / ".claude" / "commands" / "auto-dev.md").exists()
        assert (fake_home / ".claude" / "commands" / "review.md").exists()
        assert "commands synced : 2" in result.stdout

    def test_skill_dir_copied(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        skill_md = fake_home / ".claude" / "skills" / "cw-fanout" / "SKILL.md"
        assert skill_md.exists()

    def test_manifest_written(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        assert manifest.exists()
        entries = manifest.read_text().splitlines()
        assert "commands/auto-dev.md" in entries
        assert "commands/review.md" in entries
        assert "skills/cw-fanout" in entries

    def test_no_prune_on_first_run(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert "orphans pruned  : 0" in result.stdout


class TestInstallSkillsPruneSafety:
    """KEY SAFETY TEST: foreign skills are never pruned.

    Scenario:
    1. Run 1: install auto-dev.md + review.md + cw-fanout skill.
    2. A *foreign* skill (peon-ping-fake) appears in ~/.claude/skills/ —
       NOT from cw, so it is NOT in the manifest.
    3. Remove review.md from the repo source (simulating a dropped command).
    4. Run 2: review.md should be pruned; peon-ping-fake MUST survive.
    """

    def test_foreign_skill_survives_prune(
        self, script: Path, fake_repo: Path, fake_home: Path
    ) -> None:
        # Run 1: install everything
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        # Plant a foreign skill NOT from cw (not in the manifest)
        foreign_skill = fake_home / ".claude" / "skills" / "peon-ping-fake"
        foreign_skill.mkdir(parents=True)
        foreign_skill_md = foreign_skill / "SKILL.md"
        foreign_skill_md.write_text("# foreign\n")

        # Drop review.md from the repo source to trigger a prune on run 2
        (fake_repo / ".claude" / "commands" / "review.md").unlink()

        # Run 2: should prune review.md but NOT peon-ping-fake
        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr

        # --- THE KEY SAFETY ASSERTION ---
        assert foreign_skill_md.exists(), (
            "Foreign skill peon-ping-fake/SKILL.md was deleted by cw prune — "
            "this violates the manifest-scoped prune safety invariant: cw must "
            "never remove paths it did not itself install."
        )

        # review.md was in the old manifest → must be pruned
        pruned = fake_home / ".claude" / "commands" / "review.md"
        assert not pruned.exists(), (
            "review.md (dropped from repo source) should have been pruned"
        )

        # auto-dev.md still in source → must survive
        assert (fake_home / ".claude" / "commands" / "auto-dev.md").exists()

        # exactly 1 orphan pruned (review.md only)
        assert "orphans pruned  : 1" in r2.stdout

    def test_manifest_updated_after_prune(
        self, script: Path, fake_repo: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        (fake_repo / ".claude" / "commands" / "review.md").unlink()

        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr

        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        entries = manifest.read_text().splitlines()
        assert "commands/review.md" not in entries
        assert "commands/auto-dev.md" in entries
        assert "skills/cw-fanout" in entries

    def test_idempotent_second_run(self, script: Path, fake_home: Path) -> None:
        """Running twice with no changes is a no-op (zero orphans pruned)."""
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr

        assert "orphans pruned  : 0" in r2.stdout
        assert (fake_home / ".claude" / "commands" / "auto-dev.md").exists()
        assert (fake_home / ".claude" / "skills" / "cw-fanout" / "SKILL.md").exists()
