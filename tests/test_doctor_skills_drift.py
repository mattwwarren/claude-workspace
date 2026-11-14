"""Tests for cw.doctor.skills_drift — repo-tracked skills/commands drift check.

Direct calls to ``_check_skills_commands_drift()``, monkeypatching module-level
seams (``_CLAUDE_HOME``, ``_resolve_cw_source_path``, ``_sp.run``) rather than
touching the real filesystem/environment (#1514).
"""

from __future__ import annotations

import importlib.resources
import json
import subprocess
from pathlib import Path

import pytest

from cw.doctor import skills_drift
from cw.doctor.skills_drift import _CHECK_NAME, _check_skills_commands_drift
from cw.doctor.versions import CwInstall, _InstallKind
from tests._shared.cli_preflight import _patch_cw_dist, _patch_cw_dist_not_found


def _mk_proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_module_constant_is_patchable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_CLAUDE_HOME is a module-level Path, honored when monkeypatched."""
    from cw.doctor import skills_drift

    assert isinstance(skills_drift._CLAUDE_HOME, Path)

    fake_home = tmp_path / ".claude"
    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", fake_home)
    monkeypatch.setattr(
        "cw.doctor.skills_drift._resolve_cw_source_path", lambda: tmp_path / "repo"
    )
    # fake_home / "skills" does not exist -> early skip, proving the patch stuck.
    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is False
    assert str(fake_home) in result.detail


def test_no_drift_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Identical byte-for-byte fixture files -> ok=True, warn=False, N/N match."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"

    _write(repo / ".claude/skills/foo/SKILL.md", "skill content")
    _write(repo / ".claude/commands/bar/cmd.md", "command content")
    _write(claude_home / "skills/foo/SKILL.md", "skill content")
    _write(claude_home / "commands/bar/cmd.md", "command content")

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    tracked = ".claude/skills/foo/SKILL.md\n.claude/commands/bar/cmd.md\n"
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is False
    assert "2/2" in result.detail


def test_missing_on_global_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repo-tracked file with no counterpart under _CLAUDE_HOME -> warn=True."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"

    _write(repo / ".claude/skills/foo/SKILL.md", "skill content")
    (claude_home / "skills").mkdir(parents=True)

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    tracked = ".claude/skills/foo/SKILL.md\n"
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert "missing" in result.detail
    assert ".claude/skills/foo/SKILL.md" in result.detail


def test_content_differs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Counterpart exists but bytes differ -> warn=True, detail mentions 'differ'."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"

    _write(repo / ".claude/skills/foo/SKILL.md", "repo content")
    _write(claude_home / "skills/foo/SKILL.md", "stale global content")

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    tracked = ".claude/skills/foo/SKILL.md\n"
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert "differ" in result.detail
    assert ".claude/skills/foo/SKILL.md" in result.detail


def test_counterpart_is_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Counterpart leaf path is a symlink -> warn=True, detail mentions 'symlink'."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"

    _write(repo / ".claude/skills/foo/SKILL.md", "skill content")
    (claude_home / "skills/foo").mkdir(parents=True)
    link_target = tmp_path / "elsewhere.md"
    link_target.write_text("skill content", encoding="utf-8")
    (claude_home / "skills/foo/SKILL.md").symlink_to(link_target)

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    tracked = ".claude/skills/foo/SKILL.md\n"
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert "symlink" in result.detail
    assert ".claude/skills/foo/SKILL.md" in result.detail


def test_symlink_resolves_to_tracked_path_is_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Counterpart symlink resolving to the tracked repo path is NOT drift.

    This is the expected steady state under a symlink install (#1535) —
    the previous behavior flagged this as SYMLINK unconditionally, a false
    positive this test guards against regressing.
    """
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"

    _write(repo / ".claude/skills/foo/SKILL.md", "skill content")
    (claude_home / "skills/foo").mkdir(parents=True)
    (claude_home / "skills/foo/SKILL.md").symlink_to(
        repo / ".claude/skills/foo/SKILL.md"
    )

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    tracked = ".claude/skills/foo/SKILL.md\n"
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is False
    assert "match" in result.detail


def test_mixed_drift_aggregates_one_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Combining missing + differs + symlink still yields exactly one CheckResult."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"

    _write(repo / ".claude/skills/missing.md", "a")
    _write(repo / ".claude/skills/differs.md", "repo-side")
    _write(claude_home / "skills/differs.md", "global-side")
    _write(repo / ".claude/commands/linked.md", "b")
    (claude_home / "commands").mkdir(parents=True, exist_ok=True)
    link_target = tmp_path / "elsewhere2.md"
    link_target.write_text("b", encoding="utf-8")
    (claude_home / "commands/linked.md").symlink_to(link_target)

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    tracked = (
        ".claude/skills/missing.md\n"
        ".claude/skills/differs.md\n"
        ".claude/commands/linked.md\n"
    )
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert result.name == _CHECK_NAME
    assert "missing" in result.detail
    assert "differ" in result.detail
    assert "symlink" in result.detail
    # Not a list of one CheckResult per file — this is a single aggregated result.
    assert isinstance(result.detail, str)


def test_no_local_source_repo_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry install (PackageNotFoundError) -> remapped skip CheckResult."""
    _patch_cw_dist_not_found(monkeypatch)
    result = _check_skills_commands_drift()
    assert result.name == _CHECK_NAME
    assert result.ok is True
    assert result.warn is False


def test_claude_home_skills_absent_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local source repo resolves fine, but _CLAUDE_HOME/skills is absent -> skip."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"  # not created at all

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is False


def test_git_ls_files_failure_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git binary missing (FileNotFoundError) -> ok=True, warn=True, no crash."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"
    (claude_home / "skills").mkdir(parents=True)

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)

    def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        msg = "git not found"
        raise FileNotFoundError(msg)

    monkeypatch.setattr("cw.doctor.skills_drift.run_git", _raise)

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True


def test_git_ls_files_nonzero_returncode_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git ls-files exits non-zero -> ok=True, warn=True, no crash."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"
    (claude_home / "skills").mkdir(parents=True)

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git",
        lambda *_a, **_kw: _mk_proc("", returncode=128),
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True


def test_check_included_in_run_doctor(tmp_config_dir: Path) -> None:
    """_check_skills_commands_drift is wired into run_doctor output."""
    from cw.doctor import run_doctor

    report = run_doctor()
    check_names = [c.name for c in report.checks]
    assert _CHECK_NAME in check_names


def test_examples_bounded_not_all_39(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """More than _MAX_EXAMPLES drifting files -> detail doesn't enumerate all."""
    repo = tmp_path / "repo"
    claude_home = tmp_path / ".claude"
    (claude_home / "skills").mkdir(parents=True)

    tracked_lines = []
    for i in range(6):
        relpath = f".claude/skills/missing-{i}.md"
        _write(repo / relpath, "content")
        tracked_lines.append(relpath)
    tracked = "\n".join(tracked_lines) + "\n"

    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr("cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo)
    monkeypatch.setattr(
        "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert "6 missing" in result.detail
    present = sum(1 for i in range(6) if f"missing-{i}.md" in result.detail)
    assert present < 6


class TestExcludedCommands:
    """Installer-excluded commands (e.g. ship-it.md) must not be reported
    drifting — they are deliberately never installed globally, so a git-tracked
    but absent-on-the-global-side entry for one of them is not drift.
    """

    def test_excluded_command_not_reported_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"

        _write(repo / ".claude/commands/ship-it.md", "# ship-it\n")
        _write(repo / "scripts/excluded-commands.txt", "ship-it.md\n")
        (claude_home / "skills").mkdir(parents=True)

        monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
        monkeypatch.setattr(
            "cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo
        )
        tracked = ".claude/commands/ship-it.md\n"
        monkeypatch.setattr(
            "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
        )

        result = _check_skills_commands_drift()
        assert result.ok is True
        assert result.warn is False
        assert "ship-it.md" not in result.detail

    def test_excluded_commands_file_missing_fails_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No scripts/excluded-commands.txt at all -> degrades to 'nothing
        excluded' (ship-it.md-style entries report MISSING), not a crash.
        """
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"

        _write(repo / ".claude/commands/ship-it.md", "# ship-it\n")
        (claude_home / "skills").mkdir(parents=True)

        monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
        monkeypatch.setattr(
            "cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo
        )
        tracked = ".claude/commands/ship-it.md\n"
        monkeypatch.setattr(
            "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
        )

        result = _check_skills_commands_drift()
        assert result.ok is True
        assert result.warn is True
        assert "missing" in result.detail

    def test_excluded_commands_file_blank_lines_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"

        _write(repo / ".claude/commands/ship-it.md", "# ship-it\n")
        _write(repo / "scripts/excluded-commands.txt", "ship-it.md\n\n\n")
        (claude_home / "skills").mkdir(parents=True)

        monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
        monkeypatch.setattr(
            "cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo
        )
        tracked = ".claude/commands/ship-it.md\n"
        monkeypatch.setattr(
            "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
        )

        result = _check_skills_commands_drift()
        assert result.ok is True
        assert result.warn is False
        assert "ship-it.md" not in result.detail


class TestScriptsTracked:
    """`.claude/scripts` is a tracked root (#2090): the stale-installed-script
    drift that stripped /prep-pr's gate-timeout ladder must show up here.
    """

    @staticmethod
    def _wire(
        monkeypatch: pytest.MonkeyPatch, repo: Path, claude_home: Path, tracked: str
    ) -> None:
        (claude_home / "skills").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
        monkeypatch.setattr(
            "cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo
        )
        monkeypatch.setattr(
            "cw.doctor.skills_drift.run_git", lambda *_a, **_kw: _mk_proc(tracked)
        )

    def test_git_ls_files_is_scoped_to_scripts_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[list[str]] = []

        def _run(args: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            seen.append(args)
            return _mk_proc("")

        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"
        (claude_home / "skills").mkdir(parents=True)
        monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
        monkeypatch.setattr(
            "cw.doctor.skills_drift._resolve_cw_source_path", lambda: repo
        )
        monkeypatch.setattr("cw.doctor.skills_drift.run_git", _run)

        _check_skills_commands_drift()
        assert seen[0][-3:] == [".claude/skills", ".claude/commands", ".claude/scripts"]

    def test_stale_installed_script_reports_differs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"
        _write(repo / ".claude/scripts/prep_pr_state.py", "# gate-timeout\n")
        _write(claude_home / "scripts/prep_pr_state.py", "# three versions behind\n")
        self._wire(monkeypatch, repo, claude_home, ".claude/scripts/prep_pr_state.py\n")

        result = _check_skills_commands_drift()
        assert result.warn is True
        assert "1 differ" in result.detail
        assert ".claude/scripts/prep_pr_state.py" in result.detail

    def test_nested_script_maps_to_scripts_subtree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"
        _write(repo / ".claude/scripts/utils/runtime_paths.py", "# rp\n")
        _write(claude_home / "scripts/utils/runtime_paths.py", "# rp\n")
        self._wire(
            monkeypatch, repo, claude_home, ".claude/scripts/utils/runtime_paths.py\n"
        )

        result = _check_skills_commands_drift()
        assert result.warn is False
        assert result.detail == "1/1 match"

    def test_excluded_script_not_reported_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"
        _write(repo / ".claude/scripts/check_imports.py", "# CI gate\n")
        _write(repo / ".claude/scripts/utils/local.py", "# local\n")
        _write(
            repo / "scripts/excluded-scripts.txt", "check_imports.py\nutils/local.py\n"
        )
        self._wire(
            monkeypatch,
            repo,
            claude_home,
            ".claude/scripts/check_imports.py\n.claude/scripts/utils/local.py\n",
        )

        result = _check_skills_commands_drift()
        assert result.warn is False
        assert "check_imports.py" not in result.detail

    def test_scripts_exclusion_does_not_leak_into_commands(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An excluded-scripts entry only exempts the scripts root."""
        repo = tmp_path / "repo"
        claude_home = tmp_path / ".claude"
        _write(repo / ".claude/commands/check_imports.py", "# odd but tracked\n")
        _write(repo / "scripts/excluded-scripts.txt", "check_imports.py\n")
        self._wire(
            monkeypatch, repo, claude_home, ".claude/commands/check_imports.py\n"
        )

        result = _check_skills_commands_drift()
        assert result.warn is True
        assert "1 missing" in result.detail


# ---------------------------------------------------------------------------
# Release installs (git / registry): packaged command docs and link versions
# (#3579). Installed-version and install-kind come from the shared
# FakeCwDist helper; the packaged-docs dir is a function seam.
# ---------------------------------------------------------------------------

_DOC_NAMES = (
    "auto-dev-finalize.md",
    "auto-dev-impl.md",
    "auto-dev-plan.md",
    "prep-pr.md",
)
_REPO_URL = "https://github.com/mattwwarren/claude-workspace.git"
_NOTICE = (
    "uv tool install does not install cw's skills, commands or scripts; "
    "to install them run:"
)


def _git_payload(commit_id: str | None = None, requested: str | None = None) -> str:
    vcs: dict[str, str] = {"vcs": "git"}
    if commit_id is not None:
        vcs["commit_id"] = commit_id
    if requested is not None:
        vcs["requested_revision"] = requested
    return json.dumps({"url": _REPO_URL, "vcs_info": vcs})


def _cmd(rev: str) -> str:
    return (
        f"git clone {_REPO_URL} && cd claude-workspace && "
        f"git checkout {rev} && ./scripts/install-skills.sh"
    )


def _make_packaged(root: Path, names: tuple[str, ...] = _DOC_NAMES) -> Path:
    packaged = root / "packaged"
    packaged.mkdir(parents=True)
    for name in names:
        (packaged / name).write_text(f"DOC {name}\n", encoding="utf-8")
    return packaged


def _make_checkout(root: Path, version: str, name: str = "claude-workspace") -> Path:
    checkout = root / "checkout"
    (checkout / ".claude" / "commands").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    for doc in _DOC_NAMES:
        (checkout / ".claude" / "commands" / doc).write_text(
            f"DOC {doc}\n", encoding="utf-8"
        )
    return checkout


def _wire_release(
    monkeypatch: pytest.MonkeyPatch,
    claude_home: Path,
    packaged: Path | None,
    *,
    direct_url_text: str | None,
    version: str = "1.97.0",
) -> None:
    monkeypatch.setattr("cw.doctor.skills_drift._CLAUDE_HOME", claude_home)
    monkeypatch.setattr(skills_drift, "_packaged_commands_dir", lambda: packaged)
    _patch_cw_dist(monkeypatch, direct_url_text=direct_url_text, version=version)


def _copy_docs(claude_home: Path, packaged: Path, names: tuple[str, ...]) -> None:
    (claude_home / "commands").mkdir(parents=True, exist_ok=True)
    for name in names:
        (claude_home / "commands" / name).write_bytes((packaged / name).read_bytes())


def _link_docs(claude_home: Path, checkout: Path, names: tuple[str, ...]) -> None:
    (claude_home / "commands").mkdir(parents=True, exist_ok=True)
    for name in names:
        (claude_home / "commands" / name).symlink_to(
            checkout / ".claude" / "commands" / name
        )


class TestReleaseInstallSkills:
    def test_git_install_missing_everything_warns_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        packaged = _make_packaged(tmp_path)
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.name == _CHECK_NAME
        assert result.ok is True
        assert result.warn is True
        assert result.detail == (
            f"4 of 4 shipped command docs missing from {claude_home}/commands "
            "(auto-dev-finalize.md, auto-dev-impl.md, auto-dev-plan.md, +1 more); "
            f"{claude_home}/skills not found. {_NOTICE} {_cmd('abc1234')}"
        )

    def test_all_docs_linked_and_equal_is_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        (claude_home / "skills").mkdir(parents=True)
        checkout = _make_checkout(tmp_path, "1.97.0")
        packaged = _make_packaged(tmp_path)
        _link_docs(claude_home, checkout, _DOC_NAMES)
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.name == _CHECK_NAME
        assert result.ok is True
        assert result.warn is False
        assert result.detail == (
            "4 of 4 shipped command docs match installed cw 1.97.0; "
            "skills and scripts cannot be verified without a checkout"
        )

    def test_regular_copies_equal_is_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        (claude_home / "skills").mkdir(parents=True)
        packaged = _make_packaged(tmp_path)
        _copy_docs(claude_home, packaged, _DOC_NAMES)
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.warn is False
        assert result.detail.startswith(
            "4 of 4 shipped command docs match installed cw 1.97.0"
        )

    def test_one_doc_differs_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        (claude_home / "skills").mkdir(parents=True)
        packaged = _make_packaged(tmp_path)
        _copy_docs(claude_home, packaged, _DOC_NAMES)
        (claude_home / "commands" / "auto-dev-plan.md").write_text(
            "STALE\n", encoding="utf-8"
        )
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.warn is True
        assert result.detail == (
            "1 of 4 shipped command docs differ from the copy in installed cw 1.97.0 "
            f"(auto-dev-plan.md). {_NOTICE} {_cmd('abc1234')}"
        )

    def test_links_into_older_checkout_warn_version_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        (claude_home / "skills").mkdir(parents=True)
        checkout = _make_checkout(tmp_path, "1.95.0")
        packaged = _make_packaged(tmp_path)
        _link_docs(claude_home, checkout, _DOC_NAMES)
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(requested="v1.97.0"),
        )

        result = _check_skills_commands_drift()

        assert result.warn is True
        assert result.detail == (
            "4 of 4 command doc links point at a checkout whose version differs "
            "from installed cw 1.97.0 (auto-dev-finalize.md@1.95.0, "
            "auto-dev-impl.md@1.95.0, "
            "auto-dev-plan.md@1.95.0, +1 more). "
            f"{_NOTICE} {_cmd('v1.97.0')}"
        )

    def test_dangling_link_counts_as_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        (claude_home / "skills").mkdir(parents=True)
        (claude_home / "commands").mkdir(parents=True)
        packaged = _make_packaged(tmp_path, ("prep-pr.md",))
        (claude_home / "commands" / "prep-pr.md").symlink_to(tmp_path / "gone.md")
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.warn is True
        assert "1 of 1 shipped command docs missing" in result.detail

    def test_skills_dir_absent_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        packaged = _make_packaged(tmp_path)
        _copy_docs(claude_home, packaged, _DOC_NAMES)
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.warn is True
        assert result.detail == (
            f"{claude_home}/skills not found. {_NOTICE} {_cmd('abc1234')}"
        )

    def test_registry_install_uses_canonical_url_and_tag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        packaged = _make_packaged(tmp_path, ("prep-pr.md",))
        _wire_release(monkeypatch, claude_home, packaged, direct_url_text=None)

        result = _check_skills_commands_drift()

        assert result.warn is True
        assert result.detail.endswith(_cmd("v1.97.0"))

    def test_git_without_pins_falls_back_to_tag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        packaged = _make_packaged(tmp_path, ("prep-pr.md",))
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(),
        )

        result = _check_skills_commands_drift()

        assert result.detail.endswith(_cmd("v1.97.0"))

    def test_remediation_none_url_uses_canonical_repo(self) -> None:
        install = CwInstall(
            kind=_InstallKind.GIT,
            source_path=None,
            url=None,
            requested_revision=None,
            commit_id=None,
        )

        assert skills_drift._remediation_cmd(install, "2.0.0") == _cmd("v2.0.0")

    def test_remediation_quotes_requested_revision(self) -> None:
        install = CwInstall(
            kind=_InstallKind.GIT,
            source_path=None,
            url=_REPO_URL,
            requested_revision="release/1 2",
            commit_id=None,
        )

        assert "git checkout 'release/1 2'" in skills_drift._remediation_cmd(
            install, "2.0.0"
        )

    def test_packaged_docs_absent_cannot_verify(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        _wire_release(
            monkeypatch,
            claude_home,
            None,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.ok is True
        assert result.warn is True
        assert result.detail == (
            "cannot verify command docs: installed cw 1.97.0 carries no packaged "
            f"command docs. {_NOTICE} {_cmd('abc1234')}"
        )

    def test_malformed_install_skips_with_legacy_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _wire_release(
            monkeypatch,
            tmp_path / ".claude",
            None,
            direct_url_text="{not json",
        )

        result = _check_skills_commands_drift()

        assert result.ok is True
        assert result.warn is False
        assert result.detail == "malformed direct_url.json; skipping source check"

    def test_detail_bounded_to_three_examples(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        packaged = _make_packaged(tmp_path)
        _wire_release(
            monkeypatch,
            claude_home,
            packaged,
            direct_url_text=_git_payload(commit_id="abc1234"),
        )

        result = _check_skills_commands_drift()

        assert result.detail.count(".md") == 3
        assert ", +1 more" in result.detail


class TestLinkedCheckoutVersion:
    @staticmethod
    def _link(tmp_path: Path, target: Path) -> Path:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        link = home / "doc.md"
        link.symlink_to(target)
        return link

    def test_not_a_symlink_is_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "doc.md"
        plain.write_text("x", encoding="utf-8")

        assert skills_drift._linked_checkout_version(plain) is None

    def test_no_pyproject_in_tree_is_none(self, tmp_path: Path) -> None:
        target = tmp_path / "solo" / "doc.md"
        target.parent.mkdir()
        target.write_text("x", encoding="utf-8")

        assert (
            skills_drift._linked_checkout_version(self._link(tmp_path, target)) is None
        )

    @pytest.mark.parametrize(
        "pyproject",
        [
            b"[project\n",
            b'[project]\nname = "other"\nversion = "1.0.0"\n',
            b'[project]\nname = "claude-workspace"\n',
            b'[project]\nname = "claude-workspace"\nversion = 1\n',
            b'project = "x"\n',
            b'project = ["x"]\n',
            b"\xff\xfe[project]\n",
        ],
        ids=[
            "malformed-toml",
            "other-name",
            "version-missing",
            "version-not-str",
            "project-str",
            "project-list",
            "non-utf8",
        ],
    )
    def test_unusable_pyproject_is_none(self, tmp_path: Path, pyproject: bytes) -> None:
        root = tmp_path / "root"
        target = root / "docs" / "doc.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        (root / "pyproject.toml").write_bytes(pyproject)

        assert (
            skills_drift._linked_checkout_version(self._link(tmp_path, target)) is None
        )

    def test_reads_version_from_checkout(self, tmp_path: Path) -> None:
        checkout = _make_checkout(tmp_path, "1.95.0")
        link = self._link(tmp_path, checkout / ".claude" / "commands" / "prep-pr.md")

        assert skills_drift._linked_checkout_version(link) == "1.95.0"

    def test_read_oserror_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        checkout = _make_checkout(tmp_path, "1.95.0")
        link = self._link(tmp_path, checkout / ".claude" / "commands" / "prep-pr.md")

        def _deny(_self: Path, encoding: str | None = None) -> str:
            denied = "denied"
            raise PermissionError(denied)

        monkeypatch.setattr(Path, "read_text", _deny)

        assert skills_drift._linked_checkout_version(link) is None

    def test_resolve_oserror_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        checkout = _make_checkout(tmp_path, "1.95.0")
        link = self._link(tmp_path, checkout / ".claude" / "commands" / "prep-pr.md")

        def _fail(_self: Path, strict: bool = False) -> Path:
            failed = "resolve failed"
            raise OSError(failed)

        monkeypatch.setattr(Path, "resolve", _fail)

        assert skills_drift._linked_checkout_version(link) is None

    def test_walk_stops_at_depth_bound(self, tmp_path: Path) -> None:
        """pyproject at parents[5] is reachable; parents[6] is beyond the bound."""
        inside_root = tmp_path / "inside"
        inside_target = inside_root / "a1" / "a2" / "a3" / "a4" / "a5" / "doc.md"
        inside_target.parent.mkdir(parents=True)
        inside_target.write_text("x", encoding="utf-8")
        (inside_root / "pyproject.toml").write_text(
            '[project]\nname = "claude-workspace"\nversion = "3.0.0"\n',
            encoding="utf-8",
        )
        assert (
            skills_drift._linked_checkout_version(self._link(tmp_path, inside_target))
            == "3.0.0"
        )

        outside_root = tmp_path / "outside"
        outside_target = (
            outside_root / "b1" / "b2" / "b3" / "b4" / "b5" / "b6" / "doc.md"
        )
        outside_target.parent.mkdir(parents=True)
        outside_target.write_text("x", encoding="utf-8")
        (outside_root / "pyproject.toml").write_text(
            '[project]\nname = "claude-workspace"\nversion = "3.0.0"\n',
            encoding="utf-8",
        )
        other_home = tmp_path / "home2"
        other_home.mkdir()
        link = other_home / "doc.md"
        link.symlink_to(outside_target)
        assert skills_drift._linked_checkout_version(link) is None


class TestPackagedCommandsDir:
    def test_present_docs_returned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        docs = tmp_path / "data" / "commands"
        docs.mkdir(parents=True)
        (docs / "prep-pr.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(importlib.resources, "files", lambda _pkg: tmp_path)

        assert skills_drift._packaged_commands_dir() == docs

    def test_absent_dir_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(importlib.resources, "files", lambda _pkg: tmp_path)

        assert skills_drift._packaged_commands_dir() is None

    def test_empty_dir_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "data" / "commands").mkdir(parents=True)
        monkeypatch.setattr(importlib.resources, "files", lambda _pkg: tmp_path)

        assert skills_drift._packaged_commands_dir() is None
