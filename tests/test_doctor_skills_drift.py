"""Tests for cw.doctor.skills_drift — repo-tracked skills/commands drift check.

Direct calls to ``_check_skills_commands_drift()``, monkeypatching module-level
seams (``_CLAUDE_HOME``, ``_resolve_cw_source_path``, ``_sp.run``) rather than
touching the real filesystem/environment (#1514).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cw.doctor.skills_drift import _CHECK_NAME, _check_skills_commands_drift
from tests.conftest import _patch_cw_dist_not_found


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
        "cw.doctor.skills_drift._sp.run", lambda *_a, **_kw: _mk_proc(tracked)
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
        "cw.doctor.skills_drift._sp.run", lambda *_a, **_kw: _mk_proc(tracked)
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
        "cw.doctor.skills_drift._sp.run", lambda *_a, **_kw: _mk_proc(tracked)
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
        "cw.doctor.skills_drift._sp.run", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert "symlink" in result.detail
    assert ".claude/skills/foo/SKILL.md" in result.detail


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
        "cw.doctor.skills_drift._sp.run", lambda *_a, **_kw: _mk_proc(tracked)
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
    assert result.name == "skills-commands-drift"
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

    monkeypatch.setattr("cw.doctor.skills_drift._sp.run", _raise)

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
        "cw.doctor.skills_drift._sp.run",
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
        "cw.doctor.skills_drift._sp.run", lambda *_a, **_kw: _mk_proc(tracked)
    )

    result = _check_skills_commands_drift()
    assert result.ok is True
    assert result.warn is True
    assert "6 missing" in result.detail
    present = sum(1 for i in range(6) if f"missing-{i}.md" in result.detail)
    assert present < 6
