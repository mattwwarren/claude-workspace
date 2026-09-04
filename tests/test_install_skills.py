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
_REAL_EXCLUDED_COMMANDS_FILE = (
    Path(__file__).parent.parent / "scripts" / "excluded-commands.txt"
)
_REAL_EXCLUDED_SCRIPTS_FILE = (
    Path(__file__).parent.parent / "scripts" / "excluded-scripts.txt"
)


def _scaffold_fake_install(tmp_path: Path, fake_repo: Path) -> Path:
    """Mirror the scripts/ directory layout expected by install-skills.sh.

    install-skills.sh derives PROJECT_DIR as dirname(dirname(BASH_SOURCE[0])).
    We place a copy of the script at <fake_repo>/scripts/install-skills.sh so
    that PROJECT_DIR resolves to <fake_repo>, which already has .claude/ set up
    by the fake_repo fixture. The real excluded-commands.txt is copied
    alongside it so EXCLUDED_COMMANDS reads the same data the real installer
    would — without this, every fixture run installs ship-it.md (the array
    reads empty) and TestProjectScopedCommandsExcluded breaks.

    Returns the path to the executable script copy.
    """
    if not _REAL_SCRIPT.exists():
        pytest.fail(f"install-skills.sh not found at {_REAL_SCRIPT}")
    scripts_dir = fake_repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_copy = scripts_dir / "install-skills.sh"
    shutil.copy2(str(_REAL_SCRIPT), str(script_copy))
    script_copy.chmod(0o755)
    if _REAL_EXCLUDED_COMMANDS_FILE.exists():
        shutil.copy2(
            str(_REAL_EXCLUDED_COMMANDS_FILE),
            str(scripts_dir / "excluded-commands.txt"),
        )
    if _REAL_EXCLUDED_SCRIPTS_FILE.exists():
        shutil.copy2(
            str(_REAL_EXCLUDED_SCRIPTS_FILE),
            str(scripts_dir / "excluded-scripts.txt"),
        )
    return script_copy


def _run(
    script: Path, fake_home: Path, *extra_args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *extra_args],
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
    """Minimal fake repo .claude tree: 2 installable commands + 1 skill dir.

    ship-it.md is present in source but is project-scoped, so it must never be
    installed globally — the counts below deliberately exclude it.
    """
    repo = tmp_path / "repo"
    commands = repo / ".claude" / "commands"
    commands.mkdir(parents=True)
    (commands / "auto-dev.md").write_text("# auto-dev\n")
    (commands / "review.md").write_text("# review\n")
    (commands / "ship-it.md").write_text("# ship-it (project-scoped)\n")

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


@pytest.fixture
def fake_repo_with_agents(fake_repo: Path) -> Path:
    """fake_repo plus an agents dir: 1 installable agent + 1 excluded spike.

    spike-isolated.md is present in source but is experiment-scoped (#107),
    so it must never be installed globally.
    """
    agents = fake_repo / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-quality-reviewer.md").write_text("# code quality reviewer\n")
    (agents / "spike-isolated.md").write_text("# spike (excluded)\n")
    return fake_repo


@pytest.fixture
def fake_repo_with_scripts(fake_repo: Path) -> Path:
    """fake_repo plus a .claude/scripts tree shaped like the real one.

    Two top-level scripts, a nested utils/ package (the real
    prep_pr_finalize.py imports it via ``Path(__file__).parent``), the
    repo-scoped check_imports.py that must never be installed, and a
    __pycache__ that must never be installed either.
    """
    scripts = fake_repo / ".claude" / "scripts"
    (scripts / "utils").mkdir(parents=True)
    (scripts / "__pycache__").mkdir()
    (scripts / "prep_pr_state.py").write_text("# fresh: gate-timeout\n")
    (scripts / "review_monitor.py").write_text("# review monitor\n")
    (scripts / "check_imports.py").write_text("# repo-scoped CI gate\n")
    (scripts / "utils" / "__init__.py").write_text("")
    (scripts / "utils" / "runtime_paths.py").write_text("# runtime paths\n")
    (scripts / "utils" / "local_only.py").write_text("# nested, repo-scoped\n")
    (scripts / "__pycache__" / "prep_pr_state.cpython-313.pyc").write_bytes(b"\0")
    return fake_repo


@pytest.fixture
def fake_repo_with_nested_skill(fake_repo: Path) -> Path:
    """fake_repo plus a skill dir with a nested scripts/ subdirectory.

    A skill directory is not always flat (SKILL.md only) — some ship a
    scripts/ subdir alongside SKILL.md.
    """
    skill_dir = fake_repo / ".claude" / "skills" / "some-skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# some-skill\n")
    (scripts_dir / "helper.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    return fake_repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInstallSkillsFirstRun:
    """First run with no prior manifest: files installed, manifest created."""

    def test_commands_copied(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        installed = fake_home / ".claude" / "commands" / "auto-dev.md"
        assert installed.exists()
        assert (fake_home / ".claude" / "commands" / "review.md").exists()
        assert "commands synced : 2" in result.stdout

        # Commands are symlinked, not copied — one copy on disk.
        assert installed.is_symlink()
        src = script.parent.parent / ".claude" / "commands" / "auto-dev.md"
        assert installed.readlink() == src.resolve()

    def test_skill_dir_copied(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        skill_dir = fake_home / ".claude" / "skills" / "cw-fanout"
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.exists()

        # Skill dirs are symlinked, not recursively copied.
        assert skill_dir.is_symlink()
        src = script.parent.parent / ".claude" / "skills" / "cw-fanout"
        assert skill_dir.readlink() == src.resolve()

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


class TestInstallSkillsNestedFiles:
    """Generic skill-install loop recurses into nested subdirectories.

    The skill directory is a single symlink, so nested files are visible for
    free without any recursive copy step.
    """

    def test_nested_subdirectory_file_installed(
        self, script: Path, fake_repo_with_nested_skill: Path, fake_home: Path
    ) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        nested = (
            fake_home / ".claude" / "skills" / "some-skill" / "scripts" / "helper.sh"
        )
        assert nested.exists()


class TestInstallSkillsSymlinkMigration:
    """A prior copy-based install (regular file/dir, or a stale symlink) is
    corrected in place on the next run — no manual cleanup required.
    """

    def test_command_migrated_from_copy_to_symlink(
        self, script: Path, fake_home: Path
    ) -> None:
        commands_dst = fake_home / ".claude" / "commands"
        commands_dst.mkdir(parents=True, exist_ok=True)
        stale = commands_dst / "auto-dev.md"
        stale.write_text("# stale copy content\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        installed = commands_dst / "auto-dev.md"
        assert installed.is_symlink()
        src = script.parent.parent / ".claude" / "commands" / "auto-dev.md"
        assert installed.readlink() == src.resolve()
        assert installed.read_text() == "# auto-dev\n"

    def test_skill_dir_migrated_from_copy_to_symlink(
        self, script: Path, fake_home: Path
    ) -> None:
        skills_dst = fake_home / ".claude" / "skills"
        skills_dst.mkdir(parents=True, exist_ok=True)
        stale_dir = skills_dst / "cw-fanout"
        stale_dir.mkdir()
        (stale_dir / "SKILL.md").write_text("# stale skill content\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        installed = skills_dst / "cw-fanout"
        assert installed.is_symlink()
        src = script.parent.parent / ".claude" / "skills" / "cw-fanout"
        assert installed.readlink() == src.resolve()
        assert (installed / "SKILL.md").read_text() == "# cw-fanout skill\n"

    def test_skill_dir_symlink_wrong_target_repointed(
        self, script: Path, fake_home: Path, tmp_path: Path
    ) -> None:
        skills_dst = fake_home / ".claude" / "skills"
        skills_dst.mkdir(parents=True, exist_ok=True)
        decoy = tmp_path / "decoy-skill"
        decoy.mkdir()
        (decoy / "SKILL.md").write_text("# decoy content\n")
        (skills_dst / "cw-fanout").symlink_to(decoy)

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        installed = skills_dst / "cw-fanout"
        assert installed.is_symlink()
        src = script.parent.parent / ".claude" / "skills" / "cw-fanout"
        assert installed.readlink() == src.resolve()
        assert (installed / "SKILL.md").read_text() == "# cw-fanout skill\n"

    def test_command_symlink_wrong_target_repointed(
        self, script: Path, fake_home: Path, tmp_path: Path
    ) -> None:
        commands_dst = fake_home / ".claude" / "commands"
        commands_dst.mkdir(parents=True, exist_ok=True)
        decoy = tmp_path / "decoy.md"
        decoy.write_text("# decoy\n")
        (commands_dst / "auto-dev.md").symlink_to(decoy)

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        installed = commands_dst / "auto-dev.md"
        assert installed.is_symlink()
        src = script.parent.parent / ".claude" / "commands" / "auto-dev.md"
        assert installed.readlink() == src.resolve()
        assert installed.read_text() == "# auto-dev\n"

    def test_skill_dir_reinstall_over_already_correct_symlink_is_idempotent(
        self, script: Path, fake_home: Path
    ) -> None:
        """Re-running the installer when nothing changed must succeed and
        must not plant a stray symlink inside the source skill directory.

        Regression test: `ln -s` alone, run unconditionally after a guard
        that (correctly) skips clearing an already-correct symlink, still
        has an existing destination to contend with. Since the destination
        resolves through the symlink to an existing directory, `ln -s`
        silently installs a new link *inside* that directory instead of
        erroring — corrupting the tracked source tree on the second and
        every subsequent run (#1535 review).
        """
        skills_dst = fake_home / ".claude" / "skills"
        skill_src = script.parent.parent / ".claude" / "skills" / "cw-fanout"

        # Run 1: fresh install.
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr
        installed = skills_dst / "cw-fanout"
        assert installed.is_symlink()
        assert installed.readlink() == skill_src.resolve()

        # Run 2: nothing changed — must still succeed, symlink must be
        # unchanged, and no stray entry may appear inside the real source
        # skill directory.
        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr
        assert installed.is_symlink()
        assert installed.readlink() == skill_src.resolve()
        assert sorted(p.name for p in skill_src.iterdir()) == ["SKILL.md"], (
            "install-skills.sh planted an unexpected entry inside the real "
            "source skill directory on a steady-state re-run"
        )


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
        # This scenario's prune target (review.md) is a command file, so it
        # exercises the symlink-to-file prune path only. The prune block's
        # rm -rf on a symlinked skill DIRECTORY (unlinks only the link, never
        # recurses into the real target) is not exercised by this test.
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


class TestInstallSkillsAgents:
    """Agents install alongside commands/skills; spike-isolated.md stays excluded."""

    def test_agents_copied(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        agents_dst = fake_home / ".claude" / "agents"
        assert (agents_dst / "code-quality-reviewer.md").exists()
        assert "agents synced   : 1" in result.stdout

    def test_spike_isolated_excluded(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        installed = fake_home / ".claude" / "agents" / "spike-isolated.md"
        assert not installed.exists(), (
            "spike-isolated.md is a throwaway spike probe (#107) and must "
            "never be installed globally"
        )
        assert "agents skipped  : 1 (experiment-scoped)" in result.stdout

    def test_spike_isolated_not_in_manifest(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        entries = manifest.read_text().splitlines()
        assert "agents/spike-isolated.md" not in entries
        assert "agents/code-quality-reviewer.md" in entries

    def test_foreign_agent_survives_prune(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        """Manifest-scoped prune must never remove an agent that exists only
        in global-claude (i.e. was never installed by cw from this repo).
        """
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        # Plant a foreign agent NOT from cw (not in the manifest) -- mirrors
        # an agent that lives only in global-claude.
        foreign_agent = fake_home / ".claude" / "agents" / "foreign-reviewer.md"
        foreign_agent.write_text("# foreign, global-claude-only\n")

        # Drop code-quality-reviewer.md from repo source to trigger a prune.
        agents_src = fake_repo_with_agents / ".claude" / "agents"
        (agents_src / "code-quality-reviewer.md").unlink()

        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr

        assert foreign_agent.exists(), (
            "An agent that exists only in global-claude (never installed by "
            "cw) was deleted by the manifest-scoped prune"
        )
        pruned = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        assert not pruned.exists()

    def test_no_agents_dir_is_a_noop(self, script: Path, fake_home: Path) -> None:
        """Repos without .claude/agents/ still install cleanly (guarded loop)."""
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert "agents synced   : 0" in result.stdout


class TestInstallSkillsAgentOverwriteSafety:
    """Agent copies must refuse to clobber a hand-edited destination (#1784).

    A baseline shadow-copy store at ~/.claude/.cw-agents-baseline/ records the
    exact content cw itself last wrote for each agent. On divergence between
    the destination and BOTH the source and the baseline, install must refuse
    (non-zero exit) instead of silently overwriting — unless --force is
    passed.
    """

    def test_agent_hand_edit_is_not_clobbered(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        assert dest.read_text() == "# code quality reviewer\n"

        # Simulate a hand-edit directly on the destination; source untouched.
        dest.write_text("# hand-edited content B\n")

        r2 = _run(script, fake_home)
        assert r2.returncode != 0, (
            "install must refuse when destination diverges from both source "
            "and baseline"
        )
        assert dest.read_text() == "# hand-edited content B\n", (
            "hand-edited destination must survive untouched on refusal, not "
            "be silently reverted to the source content"
        )

    def test_conflict_message_names_file_and_both_paths_and_remediation(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        dest.write_text("# hand-edited content B\n")
        src = fake_repo_with_agents / ".claude" / "agents" / "code-quality-reviewer.md"

        r2 = _run(script, fake_home)
        assert r2.returncode != 0

        assert "code-quality-reviewer.md" in r2.stderr
        assert str(src.resolve()) in r2.stderr
        assert str(dest.resolve()) in r2.stderr
        assert "--force" in r2.stderr

    def test_cw_copy_legitimately_newer_installs_without_friction(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        # cw's own source is updated; destination untouched by anyone else.
        src = fake_repo_with_agents / ".claude" / "agents" / "code-quality-reviewer.md"
        src.write_text("# code quality reviewer v2\n")

        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr
        assert "ERROR" not in r2.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        assert dest.read_text() == "# code quality reviewer v2\n"

    def test_preexisting_identical_destination_installs_cleanly(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        # Destination pre-populated byte-identical to source, with no prior
        # baseline on disk at all (simulates the #1774-mirror case).
        agents_dst = fake_home / ".claude" / "agents"
        agents_dst.mkdir(parents=True, exist_ok=True)
        (agents_dst / "code-quality-reviewer.md").write_text(
            "# code quality reviewer\n"
        )

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert "ERROR" not in result.stderr
        assert (
            agents_dst / "code-quality-reviewer.md"
        ).read_text() == "# code quality reviewer\n"

    def test_force_flag_overwrites_hand_edit(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        dest.write_text("# hand-edited content B\n")

        r2 = _run(script, fake_home, "--force")
        assert r2.returncode == 0, r2.stderr
        assert dest.read_text() == "# code quality reviewer\n"

    def test_conflict_does_not_write_manifest_or_prune(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        dest.write_text("# hand-edited content B\n")

        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        manifest_before = manifest.read_text()

        r2 = _run(script, fake_home)
        assert r2.returncode != 0

        assert manifest.read_text() == manifest_before, (
            "manifest must not be rewritten on a run that aborts due to a conflict"
        )
        assert dest.exists(), (
            "the manifest-scoped prune step must not delete the hand-edited "
            "file as a false orphan"
        )
        assert dest.read_text() == "# hand-edited content B\n"

    def test_other_agents_still_install_when_one_conflicts(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        agents_src = fake_repo_with_agents / ".claude" / "agents"
        (agents_src / "test-generator.md").write_text("# test generator\n")

        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        dest.write_text("# hand-edited content B\n")

        # Update the *other* agent's source so its install is observable.
        (agents_src / "test-generator.md").write_text("# test generator v2\n")

        r2 = _run(script, fake_home)
        assert r2.returncode != 0, "run must still exit non-zero overall"

        other_dest = fake_home / ".claude" / "agents" / "test-generator.md"
        assert other_dest.read_text() == "# test generator v2\n", (
            "an uninvolved sibling agent must still install normally even "
            "though the run overall fails"
        )

    def test_agent_baseline_removed_when_agent_pruned(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        baseline = (
            fake_home / ".claude" / ".cw-agents-baseline" / "code-quality-reviewer.md"
        )
        assert baseline.exists(), "baseline shadow-copy must be written on install"

        agents_src = fake_repo_with_agents / ".claude" / "agents"
        (agents_src / "code-quality-reviewer.md").unlink()

        r2 = _run(script, fake_home)
        assert r2.returncode == 0, r2.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        assert not dest.exists()
        assert not baseline.exists(), (
            "a pruned agent's baseline entry must be removed too, so a "
            "future re-add under the same filename doesn't inherit stale "
            "baseline state"
        )

    def test_conflicting_agent_run_still_installs_skills(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        """Regression test for the abort-placement bug (#1784 round 3): the
        agent_conflicts deferred-abort block used to sit BEFORE the skills
        loop, so a conflicting run exited before a single skill installed.
        """
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        dest.write_text("# hand-edited content B\n")

        # Remove the skill symlink so its presence after run 2 can only be
        # explained by the skills loop having actually run during run 2 —
        # not by leftover state from run 1.
        skill_dst = fake_home / ".claude" / "skills" / "cw-fanout"
        skill_dst.unlink()

        r2 = _run(script, fake_home)
        assert r2.returncode != 0, "agent conflict must still abort the run"

        skill_src = fake_repo_with_agents / ".claude" / "skills" / "cw-fanout"
        assert skill_dst.is_symlink(), (
            "the skills loop must still run (and reinstall the skill) even "
            "though the agent conflict aborts the overall script — proves "
            "the abort now happens after the skills loop, not before it"
        )
        assert skill_dst.readlink() == skill_src.resolve()

    def test_prune_refuses_to_delete_hand_edited_orphaned_agent(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        baseline = (
            fake_home / ".claude" / ".cw-agents-baseline" / "code-quality-reviewer.md"
        )
        assert baseline.exists()

        # Hand-edit the destination directly — diverges from both source and
        # the recorded baseline.
        dest.write_text("# hand-edited content B\n")

        # Drop the agent from repo source entirely so this run's source scan
        # no longer sees it, firing the manifest-scoped prune's "old entry
        # absent from new set" condition.
        agents_src = fake_repo_with_agents / ".claude" / "agents"
        (agents_src / "code-quality-reviewer.md").unlink()

        r2 = _run(script, fake_home)
        assert r2.returncode != 0, (
            "prune must refuse to delete an agent whose destination "
            "diverges from its baseline"
        )

        assert dest.exists(), "hand-edited destination must not be deleted"
        assert dest.read_text() == "# hand-edited content B\n"
        assert baseline.exists(), "baseline entry must not be removed on refusal"

        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        entries = manifest.read_text().splitlines()
        assert "agents/code-quality-reviewer.md" in entries, (
            "a refused prune entry must be carried forward into the new "
            "manifest so it is reconsidered next run"
        )

    def test_prune_force_deletes_hand_edited_orphaned_agent(
        self, script: Path, fake_repo_with_agents: Path, fake_home: Path
    ) -> None:
        r1 = _run(script, fake_home)
        assert r1.returncode == 0, r1.stderr

        dest = fake_home / ".claude" / "agents" / "code-quality-reviewer.md"
        baseline = (
            fake_home / ".claude" / ".cw-agents-baseline" / "code-quality-reviewer.md"
        )

        dest.write_text("# hand-edited content B\n")

        agents_src = fake_repo_with_agents / ".claude" / "agents"
        (agents_src / "code-quality-reviewer.md").unlink()

        r2 = _run(script, fake_home, "--force")
        assert r2.returncode == 0, r2.stderr

        assert not dest.exists(), (
            "--force must bypass the prune-time divergence check too"
        )
        assert not baseline.exists(), "baseline entry must be cleaned up too"

        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        entries = manifest.read_text().splitlines()
        assert "agents/code-quality-reviewer.md" not in entries, (
            "a force-pruned entry must not be carried forward"
        )


class TestProjectScopedCommandsExcluded:
    """Project-scoped commands must never reach ~/.claude/commands.

    /prep-pr resolves /ship-it against the current project's
    .claude/commands/ship-it.md and treats its absence as a BLOCK. A global
    copy makes every unrelated repo appear to have one, then ships it with
    claude-workspace's base branch, test plan, and finalize scripts.
    """

    def test_ship_it_not_installed(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        installed = fake_home / ".claude" / "commands" / "ship-it.md"
        assert not installed.exists(), (
            "ship-it.md is project-scoped and must never be installed into "
            "~/.claude/commands — a global copy hijacks /ship-it in every "
            "other repo."
        )

    def test_ship_it_not_in_manifest(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        entries = manifest.read_text().splitlines()
        assert "commands/ship-it.md" not in entries

    def test_installable_commands_still_sync(
        self, script: Path, fake_home: Path
    ) -> None:
        """Excluding ship-it.md must not suppress its siblings."""
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        assert (fake_home / ".claude" / "commands" / "auto-dev.md").exists()
        assert (fake_home / ".claude" / "commands" / "review.md").exists()
        assert "commands synced : 2" in result.stdout
        assert "commands skipped: 1 (project-scoped)" in result.stdout

    def test_previously_installed_ship_it_is_pruned(
        self, script: Path, fake_home: Path
    ) -> None:
        """A ship-it.md installed by an older cw is cleaned up on next run.

        The manifest-scoped prune owns the entry, so dropping it from the
        install set is self-healing — no manual rm required.
        """
        commands_dst = fake_home / ".claude" / "commands"
        commands_dst.mkdir(parents=True, exist_ok=True)
        stale = commands_dst / "ship-it.md"
        stale.write_text("# stale global ship-it\n")

        manifest = fake_home / ".claude" / ".cw-skills-manifest"
        manifest.write_text("commands/ship-it.md\ncommands/auto-dev.md\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        assert not stale.exists(), (
            "A previously-installed global ship-it.md should be pruned once it "
            "is excluded from the install set"
        )
        # Pin the removal to the manifest-scoped prune specifically, not to any
        # code path that happens to delete the file (e.g. a wipe-and-repopulate
        # refactor would silently lose the foreign-file safety invariant).
        assert "orphans pruned  : 1" in result.stdout
        assert "commands/ship-it.md" not in manifest.read_text().splitlines()


class TestInstallSkillsExclusionFile:
    """The installer's project-scoped exclusion list is data, not code — a
    hardcoded EXCLUDED_COMMANDS array would pass TestProjectScopedCommandsExcluded
    but fail this test, since it only ever excludes ship-it.md regardless of
    what the data file says.
    """

    def test_installer_reads_exclusion_file_not_hardcoded(
        self, script: Path, fake_home: Path
    ) -> None:
        exclusion_file = script.parent / "excluded-commands.txt"
        exclusion_file.write_text("review.md\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr

        commands_dst = fake_home / ".claude" / "commands"
        assert not (commands_dst / "review.md").exists(), (
            "review.md was listed in the fixture's excluded-commands.txt and "
            "must not be installed"
        )
        assert (commands_dst / "ship-it.md").exists(), (
            "ship-it.md is only excluded via the data file; overwriting the "
            "fixture's excluded-commands.txt to list review.md instead proves "
            "the installer reads the file rather than a hardcoded array"
        )


class TestScriptsInstalled:
    """`.claude/scripts/` is installed file-by-file into ~/.claude/scripts.

    The motivating incident (#2090): /prep-pr's backing script lives in this
    repo, but nothing installed it, so the `~/.claude/scripts/` copy came from
    a separate checkout and silently went stale. Files (never directories) are
    linked so a scripts/ or utils/ that also holds foreign files keeps them.
    """

    def test_scripts_symlinked_per_file_including_nested_utils(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        dst = fake_home / ".claude" / "scripts"
        src = fake_repo_with_scripts / ".claude" / "scripts"

        for rel in ("prep_pr_state.py", "review_monitor.py", "utils/runtime_paths.py"):
            installed = dst / rel
            assert installed.is_symlink(), rel
            assert installed.readlink() == (src / rel).resolve()
        # utils/ itself is a real directory, not a link to the source dir.
        assert (dst / "utils").is_dir()
        assert not (dst / "utils").is_symlink()
        assert "scripts synced  : 5" in result.stdout

    def test_repo_scoped_script_and_pycache_never_installed(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        dst = fake_home / ".claude" / "scripts"
        assert not (dst / "check_imports.py").exists()
        assert not (dst / "__pycache__").exists()
        assert "scripts skipped : 1 (repo-scoped)" in result.stdout
        entries = (fake_home / ".claude" / ".cw-skills-manifest").read_text()
        assert "scripts/check_imports.py" not in entries
        assert "scripts/prep_pr_state.py" in entries.splitlines()
        assert "scripts/utils/runtime_paths.py" in entries.splitlines()

    def test_stale_regular_file_replaced_named_and_backed_up(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """A pre-existing copy (global-claude's stale duplicate) becomes the
        symlink and the summary names it so it can be untracked over there.
        Its differing bytes survive beside the link — ln -sf would otherwise
        have unlinked them with no reversal path but git history.
        """
        dst = fake_home / ".claude" / "scripts"
        dst.mkdir()
        stale = dst / "prep_pr_state.py"
        stale.write_text("# stale: no gate-timeout\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert stale.is_symlink()
        assert "gate-timeout" in stale.read_text()
        backup = dst / "prep_pr_state.py.pre-symlink.bak"
        assert not backup.is_symlink()
        assert backup.read_text() == "# stale: no gate-timeout\n"
        assert "scripts replaced (regular file -> symlink):" in result.stdout
        assert "    - scripts/prep_pr_state.py" in result.stdout
        assert "git rm --cached" in result.stdout
        assert (
            "replaced copies that differed from cw's source were kept beside the link:"
            in result.stdout
        )
        assert f"    - {backup}" in result.stdout
        # Only the replaced file is named — fresh installs are not.
        assert "    - scripts/review_monitor.py" not in result.stdout
        # The backup is not cw's to prune: it never enters the manifest.
        entries = (fake_home / ".claude" / ".cw-skills-manifest").read_text()
        assert "pre-symlink.bak" not in entries

    def test_second_divergent_replacement_keeps_earlier_backup(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """Until the global-claude hand-over is done, a checkout there can
        restore the tracked file over cw's link; a later run must not clobber
        the first generation's backup with the second's.
        """
        dst = fake_home / ".claude" / "scripts"
        dst.mkdir()
        target = dst / "prep_pr_state.py"
        target.write_text("# generation one\n")
        first = _run(script, fake_home)
        assert first.returncode == 0, first.stderr

        # Simulate `git checkout` in global-claude restoring the tracked copy.
        target.unlink()
        target.write_text("# generation two\n")
        second = _run(script, fake_home)
        assert second.returncode == 0, second.stderr

        assert target.is_symlink()
        gen1 = dst / "prep_pr_state.py.pre-symlink.bak"
        gen2 = dst / "prep_pr_state.py.pre-symlink.1.bak"
        assert gen1.read_text() == "# generation one\n"
        assert gen2.read_text() == "# generation two\n"
        assert f"    - {gen2}" in second.stdout
        assert f"    - {gen1}" not in second.stdout

    def test_identical_regular_file_replaced_without_backup(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """A byte-identical copy has nothing worth keeping: replaced, named,
        no .bak dropped into the destination tree.
        """
        dst = fake_home / ".claude" / "scripts"
        dst.mkdir()
        same = dst / "prep_pr_state.py"
        same.write_text("# fresh: gate-timeout\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert same.is_symlink()
        assert "    - scripts/prep_pr_state.py" in result.stdout
        assert not (dst / "prep_pr_state.py.pre-symlink.bak").exists()
        assert "kept beside the link" not in result.stdout

    def test_no_replacement_notice_on_clean_or_repeat_run(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        first = _run(script, fake_home)
        assert first.returncode == 0, first.stderr
        assert "scripts replaced" not in first.stdout
        second = _run(script, fake_home)
        assert second.returncode == 0, second.stderr
        assert "scripts replaced" not in second.stdout
        assert "scripts synced  : 5" in second.stdout
        assert "orphans pruned  : 0" in second.stdout

    def test_foreign_scripts_survive_install_and_prune(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """Scripts cw never installed — in scripts/ or inside utils/ — are
        untouched on install, and untouched when cw's own entries are pruned.
        """
        dst = fake_home / ".claude" / "scripts"
        (dst / "utils").mkdir(parents=True)
        foreign = dst / "generate_handoff.py"
        foreign.write_text("# global-claude only\n")
        foreign_util = dst / "utils" / "other.py"
        foreign_util.write_text("# global-claude only\n")

        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert foreign.read_text() == "# global-claude only\n"
        assert foreign_util.read_text() == "# global-claude only\n"

        # Drop every cw script from source: cw's links are pruned, foreign stay.
        shutil.rmtree(fake_repo_with_scripts / ".claude" / "scripts")
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert not (dst / "prep_pr_state.py").exists()
        assert not (dst / "utils" / "runtime_paths.py").exists()
        assert foreign.exists()
        assert foreign_util.exists()
        assert "orphans pruned  : 5" in result.stdout

    def test_no_scripts_dir_is_a_noop(self, script: Path, fake_home: Path) -> None:
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        assert "scripts synced  : 0" in result.stdout
        assert not (fake_home / ".claude" / "scripts").exists()

    def test_installer_reads_scripts_exclusion_file_not_hardcoded(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """Swapping the fixture's excluded-scripts.txt changes what is skipped."""
        (script.parent / "excluded-scripts.txt").write_text("review_monitor.py\n")
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        dst = fake_home / ".claude" / "scripts"
        assert (dst / "check_imports.py").is_symlink()
        assert not (dst / "review_monitor.py").exists()

    def test_nested_exclusion_entry_matches_full_relative_path(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """Exclusion entries are paths relative to .claude/scripts/, so a
        `utils/<name>` entry skips exactly that nested file — not its
        basename anywhere, and not the rest of utils/.
        """
        (script.parent / "excluded-scripts.txt").write_text(
            "check_imports.py\nutils/local_only.py\n"
        )
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        dst = fake_home / ".claude" / "scripts"
        assert not (dst / "utils" / "local_only.py").exists()
        assert (dst / "utils" / "runtime_paths.py").is_symlink()
        assert "scripts skipped : 2 (repo-scoped)" in result.stdout
        entries = (fake_home / ".claude" / ".cw-skills-manifest").read_text()
        assert "scripts/utils/local_only.py" not in entries.splitlines()

    def test_basename_only_exclusion_does_not_match_nested_file(
        self, script: Path, fake_repo_with_scripts: Path, fake_home: Path
    ) -> None:
        """The complement: a bare `local_only.py` entry names a top-level file
        that does not exist, so the nested utils/local_only.py still installs.
        """
        (script.parent / "excluded-scripts.txt").write_text("local_only.py\n")
        result = _run(script, fake_home)
        assert result.returncode == 0, result.stderr
        dst = fake_home / ".claude" / "scripts"
        assert (dst / "utils" / "local_only.py").is_symlink()
        assert "scripts skipped : 0 (repo-scoped)" in result.stdout
