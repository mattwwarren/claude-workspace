"""Tests for .claude/scripts/check_changelog_frozen.py (#2304).

The gate asserts three invariants over ``CHANGELOG.md`` against the release
tags reachable from HEAD:

* Rule 1 — every tagged ``## [X.Y.Z]`` section at/after ``since_tag`` exists
  exactly once and is byte-identical to the tag's own copy.
* Rule 2 — at most one untagged version heading, first after ``[Unreleased]``,
  matching ``pyproject.toml``'s ``[project].version``.
* Rule 3 — no duplicate headings of any kind, regardless of ``since_tag``.

Every fixture is a real throwaway git repo (``make_git_repo``) with real
``git tag`` / ``git show`` — the script's view of history is never mocked.
Loaded via importlib, mirroring ``tests/test_check_imports.py`` — the script
lives outside the ``src/`` tree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from cw.cli import main
from tests._changelog_cut_helpers import (
    BRANCH,
    CL_BASE,
    CL_BRANCH,
    CL_EXPECTED,
    CL_MAIN,
    GAMMA,
    PREAMBLE,
    RELEASED_1_1_0,
    ReleaseMovedRepo,
    commit_files,
    cut_straddle_repo,
    pyproject,
    release_cut_pr_repo,
    squash_onto_origin_main,
)
from tests._shared.git_repos import git_in, list_tags, write_pyproject_override
from tests._tool_fakes import AbsentToolCli

pytestmark = pytest.mark.release_cut

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_changelog_frozen.py"

Section = tuple[str, list[str]]
Result = tuple[int, dict[str, object], str]


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_changelog_frozen", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_changelog_frozen", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod() -> types.ModuleType:
    return _load_module()


@pytest.fixture(autouse=True)
def _strip_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest may run inside a git hook whose GIT_DIR would redirect the
    # script's own git calls away from the fixture repo.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)


def _changelog(*sections: Section) -> str:
    lines = ["# Changelog", ""]
    for heading, bullets in sections:
        lines += [f"## [{heading}]", "", *bullets, ""]
    return "\n".join(lines)


def _pyproject(version: str, since_tag: str | None) -> str:
    body = f'[project]\nname = "fixture"\nversion = "{version}"\n'
    if since_tag is not None:
        body += f'\n[tool.cw.changelog_freeze]\nsince_tag = "{since_tag}"\n'
    return body


def _commit(
    repo: Path,
    *sections: Section,
    version: str = "0.0.0",
    since_tag: str | None = None,
    tag: str | None = None,
) -> None:
    (repo / "CHANGELOG.md").write_text(_changelog(*sections), encoding="utf-8")
    write_pyproject_override(repo, _pyproject(version, since_tag))
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", f"commit {tag or 'untagged'}")
    if tag is not None:
        git_in(repo, "tag", tag)


def _run(
    mod: types.ModuleType,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> Result:
    code = mod.main(["--changelog", str(repo / "CHANGELOG.md"), "--json", *extra])
    captured = capsys.readouterr()
    payload: dict[str, object] = json.loads(captured.out) if captured.out else {}
    return code, payload, captured.err


def _kinds(payload: dict[str, object]) -> list[str]:
    violations = payload["violations"]
    assert isinstance(violations, list)
    return [str(v["kind"]) for v in violations]


def _violation(payload: dict[str, object], kind: str) -> dict[str, str]:
    violations = payload["violations"]
    assert isinstance(violations, list)
    matches = [v for v in violations if v["kind"] == kind]
    assert matches, f"no {kind!r} violation in {violations}"
    return dict(matches[0])


UNRELEASED: Section = ("Unreleased", [])
V100: Section = ("1.0.0", ["- original bullet"])


@pytest.fixture
def released_repo(make_git_repo: Callable[..., Path]) -> Path:
    """A repo whose HEAD is tag ``v1.0.0`` with a single ``[1.0.0]`` section."""
    repo = make_git_repo("repo")
    _commit(repo, UNRELEASED, V100, version="1.0.0", since_tag="v1.0.0", tag="v1.0.0")
    return repo


# ---------------------------------------------------------------------------
# Rule 1 — tagged sections frozen
# ---------------------------------------------------------------------------


def test_clean_release_passes(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, _ = _run(mod, released_repo, capsys)
    assert code == 0
    assert payload == {"ok": True, "since_tag": "v1.0.0", "violations": []}


def test_missing_release_heading_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        ("Unreleased", ["- original bullet"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    violation = _violation(payload, "missing_heading")
    assert violation["tag"] == "v1.0.0"
    assert "1.0.0" in violation["detail"]


def test_duplicate_tagged_heading_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(released_repo, UNRELEASED, V100, V100, version="1.0.0", since_tag="v1.0.0")

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    assert _kinds(payload) == ["duplicate_heading"]
    assert _violation(payload, "duplicate_heading")["tag"] == "v1.0.0"


def test_entry_added_to_released_section_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        ("1.0.0", ["- original bullet", "- misfiled bullet"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    violation = _violation(payload, "section_changed")
    assert violation["tag"] == "v1.0.0"


def test_entry_moved_into_unreleased_passes(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        ("Unreleased", ["- new bullet"]),
        V100,
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)
    assert code == 0, payload


def test_tag_missing_its_own_section_fails(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("repo")
    _commit(repo, UNRELEASED, version="1.0.0", since_tag="v1.0.0", tag="v1.0.0")
    _commit(repo, UNRELEASED, V100, version="1.0.0", since_tag="v1.0.0")

    code, payload, _ = _run(mod, repo, capsys)

    assert code == 1
    assert "has no" in _violation(payload, "section_changed")["detail"]


def test_entry_filed_under_released_heading_immediately_after_tag_creation_fails(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Incident #3 (#2275): the commit right after the tag files under it."""
    repo = make_git_repo("repo")
    released = ("1.51.0", ["- shipped in 1.51.0"])
    _commit(
        repo, UNRELEASED, released, version="1.51.0", since_tag="v1.51.0", tag="v1.51.0"
    )
    _commit(
        repo,
        UNRELEASED,
        ("1.51.0", ["- shipped in 1.51.0", "- #2275 entry"]),
        version="1.51.0",
        since_tag="v1.51.0",
    )

    code, payload, _ = _run(mod, repo, capsys)

    assert code == 1
    violation = _violation(payload, "section_changed")
    assert violation["tag"] == "v1.51.0"


def test_entry_filed_under_older_released_heading_fails(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Incident #4: a main-merge files an entry under an older tagged heading.

    No ``[tool.cw.changelog_freeze]`` table at all: an absent ``since_tag``
    means beginning-of-history, never fail-open.
    """
    repo = make_git_repo("repo")
    v149: Section = ("1.49.0", ["- in 1.49.0"])
    v150: Section = ("1.50.0", ["- in 1.50.0"])
    _commit(repo, UNRELEASED, v149, version="1.49.0", tag="v1.49.0")
    _commit(repo, UNRELEASED, v150, v149, version="1.50.0", tag="v1.50.0")
    _commit(
        repo,
        UNRELEASED,
        v150,
        ("1.49.0", ["- in 1.49.0", "- merged in late"]),
        version="1.50.0",
    )

    code, payload, _ = _run(mod, repo, capsys)

    assert code == 1
    assert payload["since_tag"] == ""
    violation = _violation(payload, "section_changed")
    assert violation["tag"] == "v1.49.0"


# ---------------------------------------------------------------------------
# Rule 2 — the one untagged heading is the release in progress
# ---------------------------------------------------------------------------


def test_release_pr_shape_passes(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        ("1.1.0", ["- next release"]),
        V100,
        version="1.1.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)
    assert code == 0, payload


def test_untagged_heading_not_first_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        V100,
        ("1.1.0", ["- misplaced"]),
        version="1.1.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    assert _kinds(payload) == ["entry_outside_unreleased"]
    violation = _violation(payload, "entry_outside_unreleased")
    assert violation["tag"] == "v1.1.0"
    assert "first heading after [Unreleased]" in violation["detail"]


def test_untagged_heading_version_mismatch_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        ("1.1.0", ["- next release"]),
        V100,
        version="1.2.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    violation = _violation(payload, "entry_outside_unreleased")
    assert "1.2.0" in violation["detail"]


def test_two_untagged_headings_fail(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        ("1.2.0", ["- a"]),
        ("1.1.0", ["- b"]),
        V100,
        version="1.2.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    assert set(_kinds(payload)) == {"entry_outside_unreleased"}
    assert (
        "more than one untagged"
        in _violation(payload, "entry_outside_unreleased")["detail"]
    )


def test_non_semver_untagged_heading_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        V100,
        ("bogus", ["- stray"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    assert _violation(payload, "entry_outside_unreleased")["tag"] == "bogus"


# ---------------------------------------------------------------------------
# since_tag cutoff (grandfathering) and Rule 3
# ---------------------------------------------------------------------------


def _grandfathered_repo(make_git_repo: Callable[..., Path]) -> Path:
    repo = make_git_repo("repo")
    v080: Section = ("0.8.0", ["- in 0.8.0"])
    v090: Section = ("0.9.0", ["- in 0.9.0"])
    _commit(repo, UNRELEASED, v080, version="0.8.0", tag="v0.8.0")
    _commit(repo, UNRELEASED, v090, v080, version="0.9.0", tag="v0.9.0")
    _commit(repo, UNRELEASED, V100, v090, v080, version="1.0.0", tag="v1.0.0")
    return repo


def test_grandfathered_tag_below_cutoff_is_skipped(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _grandfathered_repo(make_git_repo)
    # v0.9.0's body is edited, v0.8.0's heading is gone, and an untagged
    # pre-cutoff heading appears out of place — all accepted history.
    _commit(
        repo,
        UNRELEASED,
        V100,
        ("0.9.0", ["- in 0.9.0", "- edited later"]),
        ("0.7.5", ["- never tagged"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, repo, capsys)
    assert code == 0, payload
    assert payload["since_tag"] == "v1.0.0"


def test_duplicate_heading_ignores_cutoff_fails(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _grandfathered_repo(make_git_repo)
    v090: Section = ("0.9.0", ["- in 0.9.0"])
    _commit(
        repo,
        UNRELEASED,
        V100,
        v090,
        v090,
        ("0.8.0", ["- in 0.8.0"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, repo, capsys)

    assert code == 1
    assert _kinds(payload) == ["duplicate_heading"]
    assert _violation(payload, "duplicate_heading")["tag"] == "v0.9.0"


def test_conflict_free_duplicate_heading_fails(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Incident #5: a union-driver merge leaves two ``[1.51.0]`` headings.

    No conflict markers anywhere — the corruption did not come from a
    hand-resolved conflict, so the check must not depend on one.
    """
    repo = make_git_repo("repo")
    released: Section = ("1.51.0", ["- shipped"])
    _commit(
        repo, UNRELEASED, released, version="1.51.0", since_tag="v1.51.0", tag="v1.51.0"
    )
    _commit(
        repo,
        UNRELEASED,
        ("1.51.0", ["- stranded entry"]),
        released,
        version="1.51.0",
        since_tag="v1.51.0",
    )

    code, payload, _ = _run(mod, repo, capsys)

    assert code == 1
    assert "duplicate_heading" in _kinds(payload)
    assert "<<<<<<<" not in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def test_missing_unreleased_heading_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(released_repo, V100, version="1.0.0", since_tag="v1.0.0")

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    violation = _violation(payload, "missing_heading")
    assert violation["tag"] == "Unreleased"


def test_unreleased_not_first_heading_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(released_repo, V100, UNRELEASED, version="1.0.0", since_tag="v1.0.0")

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    violation = _violation(payload, "missing_heading")
    assert violation["tag"] == "Unreleased"
    assert "must be the first heading" in str(violation["detail"])


def test_duplicate_unreleased_heading_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo, UNRELEASED, UNRELEASED, V100, version="1.0.0", since_tag="v1.0.0"
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    assert _violation(payload, "duplicate_heading")["tag"] == "Unreleased"


# ---------------------------------------------------------------------------
# --require-tags: unresolvable since_tag
# ---------------------------------------------------------------------------


@pytest.fixture
def untagged_repo(make_git_repo: Callable[..., Path]) -> Path:
    repo = make_git_repo("repo")
    _commit(repo, UNRELEASED, V100, version="1.0.0", since_tag="v1.0.0")
    assert list_tags(repo) == []
    return repo


def test_no_tags_and_require_tags_fails(
    mod: types.ModuleType, untagged_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, err = _run(mod, untagged_repo, capsys, "--require-tags")

    assert code == 1
    assert payload == {}
    assert "v1.0.0" in err
    assert "git fetch --tags" in err


def test_no_tags_without_require_tags_warns_and_passes(
    mod: types.ModuleType, untagged_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, err = _run(mod, untagged_repo, capsys)

    assert code == 0
    assert payload == {}
    assert "WARNING" in err
    assert "v1.0.0" in err
    assert "git fetch --tags" in err


def test_tags_present_enforces_in_both_modes(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        ("1.0.0", ["- original bullet", "- misfiled bullet"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    strict_code, strict_payload, _ = _run(mod, released_repo, capsys, "--require-tags")
    lax_code, lax_payload, lax_err = _run(mod, released_repo, capsys)

    assert strict_code == lax_code == 1
    assert strict_payload == lax_payload
    assert _kinds(lax_payload) == ["section_changed"]
    assert "WARNING" not in lax_err


# ---------------------------------------------------------------------------
# Configuration and IO errors (fail closed)
# ---------------------------------------------------------------------------


def test_malformed_since_tag_fails(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("repo")
    _commit(
        repo, UNRELEASED, V100, version="1.0.0", since_tag="release-1", tag="release-1"
    )

    code, _, err = _run(mod, repo, capsys)

    assert code == 1
    assert "vX.Y.Z" in err


def test_malformed_pyproject_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_pyproject_override(released_repo, "[tool.cw.changelog_freeze\nsince_tag =")

    code, _, err = _run(mod, released_repo, capsys)

    assert code == 1
    assert "pyproject.toml" in err


def test_missing_pyproject_enforces_full_history(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (released_repo / "pyproject.toml").unlink()

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 0, payload
    assert payload["since_tag"] == ""


def test_missing_changelog_fails(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (released_repo / "CHANGELOG.md").unlink()

    code, _, err = _run(mod, released_repo, capsys)

    assert code == 1
    assert "CHANGELOG.md" in err


def test_changelog_outside_git_repo_fails(
    mod: types.ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "CHANGELOG.md").write_text(_changelog(UNRELEASED), encoding="utf-8")

    code, _, err = _run(mod, tmp_path, capsys)

    assert code == 1
    assert "git" in err


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_json_output_shape(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(
        released_repo,
        UNRELEASED,
        ("1.0.0", ["- original bullet", "- misfiled bullet"]),
        version="1.0.0",
        since_tag="v1.0.0",
    )

    code, payload, _ = _run(mod, released_repo, capsys)

    assert code == 1
    assert set(payload) == {"ok", "since_tag", "violations"}
    assert payload["ok"] is False
    assert payload["since_tag"] == "v1.0.0"
    violations = payload["violations"]
    assert isinstance(violations, list)
    assert len(violations) == 1
    assert set(violations[0]) == {"tag", "kind", "detail"}
    assert violations[0]["kind"] == "section_changed"


def test_human_summary_names_violations(
    mod: types.ModuleType, released_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = str(released_repo / "CHANGELOG.md")
    assert mod.main(["--changelog", changelog]) == 0
    assert "OK" in capsys.readouterr().out

    _commit(released_repo, UNRELEASED, V100, V100, version="1.0.0", since_tag="v1.0.0")
    assert mod.main(["--changelog", changelog]) == 1
    out = capsys.readouterr().out
    assert "duplicate_heading" in out
    assert "v1.0.0" in out


def test_declares_no_cw_script_version_marker() -> None:
    head = _SCRIPT.read_text(encoding="utf-8").splitlines()[:5]
    assert not any("cw-script-version" in line for line in head)


# ---------------------------------------------------------------------------
# Rule 4 — merged-result mode, --merge-into (#3190)
# ---------------------------------------------------------------------------

_REMEDY = (
    "check_changelog_frozen: fix: run `cw worktree changelog-sync --base main`"
    " from the PR's worktree, push, and let CI re-run; never edit a released"
    " section by hand"
)
_MERGE_KEYS = {"ok", "mode", "since_tag", "violations", "merge_into", "head"}


def _merge_run(
    mod: types.ModuleType,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
    ref: str = "origin/main",
) -> Result:
    argv = ["--changelog", str(repo / "CHANGELOG.md"), "--merge-into", ref]
    code = mod.main([*argv, "--json", *extra])
    captured = capsys.readouterr()
    payload: dict[str, object] = json.loads(captured.out) if captured.out else {}
    return code, payload, captured.err


def _pairs(payload: dict[str, object]) -> set[tuple[str, str, str]]:
    violations = payload["violations"]
    assert isinstance(violations, list)
    return {(str(v["tag"]), str(v["kind"]), str(v["detail"])) for v in violations}


def _late_branch(scenario: ReleaseMovedRepo, changelog: str) -> None:
    """A branch cut from the post-cut ``origin/main`` with *changelog* committed."""
    git_in(scenario.clone, "checkout", "-q", "-b", "late", "origin/main")
    commit_files(scenario.clone, {"CHANGELOG.md": changelog}, "late change")


def _drop_local_tags(repo: Path) -> None:
    git_in(repo, "tag", "-d", "v1.0.0", "v1.1.0")


@pytest.fixture
def straddle(tmp_path: Path, make_git_repo: Callable[..., Path]) -> ReleaseMovedRepo:
    return cut_straddle_repo(tmp_path, make_git_repo)


def test_merge_into_flags_hunk_landing_in_released_section(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload, _ = _merge_run(mod, straddle.clone, capsys)

    assert code == 1
    assert set(payload) == _MERGE_KEYS
    assert payload["mode"] == "merge_into"
    assert payload["ok"] is False
    assert payload["since_tag"] == ""
    assert payload["merge_into"] == "origin/main"
    assert payload["head"] == "HEAD"
    assert _pairs(payload) == {("v1.1.0", "merge_changes_released_section", "changed")}


def test_default_mode_payload_has_no_mode_key(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload, _ = _run(mod, straddle.clone, capsys)

    assert code == 0
    assert set(payload) == {"ok", "since_tag", "violations"}


def test_merge_into_passes_when_hunk_in_unreleased(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _late_branch(straddle, CL_EXPECTED)

    code, payload, _ = _merge_run(mod, straddle.clone, capsys)

    assert code == 0
    assert payload["ok"] is True
    assert payload["violations"] == []


def test_merge_into_passes_for_release_cut_pr(
    mod: types.ModuleType,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = release_cut_pr_repo(tmp_path, make_git_repo)

    code, payload, _ = _merge_run(mod, scenario.clone, capsys)

    assert code == 0
    assert payload["violations"] == []


def test_merge_into_reports_removed_and_duplicated_sections(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _late_branch(
        straddle, PREAMBLE + "## [Unreleased]\n\n" + RELEASED_1_1_0 + RELEASED_1_1_0
    )

    code, payload, _ = _merge_run(mod, straddle.clone, capsys)

    assert code == 1
    assert _pairs(payload) == {
        ("v1.1.0", "merge_changes_released_section", "duplicated"),
        ("v1.0.0", "merge_changes_released_section", "removed"),
    }


def test_merge_into_conflict_is_noted_and_passes(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit_files(straddle.clone, {"pyproject.toml": pyproject("1.0.1")}, "bump")

    code, payload, err = _merge_run(mod, straddle.clone, capsys)

    assert code == 0
    assert payload == {}
    assert "conflict" in err


def test_merge_into_missing_changelog_in_ref_is_noted_and_passes(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("repo")
    commit_files(repo, {"src/app.py": "A = 1\n"}, "base without a changelog")
    git_in(repo, "update-ref", "refs/remotes/origin/main", "main")
    git_in(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"CHANGELOG.md": CL_BASE}, "add a changelog")

    code, payload, err = _merge_run(mod, repo, capsys)

    assert code == 0
    assert payload == {}
    assert "origin/main has no CHANGELOG.md" in err


def test_merge_into_unknown_ref_fails_without_flag(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload, err = _merge_run(mod, straddle.clone, capsys, ref="origin/nope")

    assert code == 1
    assert payload == {}
    assert "origin/nope" in err
    assert "WARNING" not in err


def test_warn_on_error_unknown_ref_passes_with_warning(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    code, payload, err = _merge_run(
        mod, straddle.clone, capsys, "--warn-on-error", ref="origin/nope"
    )

    assert code == 0
    assert payload == {}
    assert err.startswith("check_changelog_frozen: WARNING: ")
    assert "origin/nope" in err
    assert "Skipping the check" in err


def test_warn_on_error_merge_tree_unavailable(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = mod._git

    def _old_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if "merge-tree" in args:
            return subprocess.CompletedProcess(
                list(args), 129, "", "error: unknown option `write-tree'\n"
            )
        result: subprocess.CompletedProcess[str] = real(cwd, *args)
        return result

    monkeypatch.setattr(mod, "_git", _old_git)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    code, payload, err = _merge_run(mod, straddle.clone, capsys, "--warn-on-error")

    assert code == 0
    assert payload == {}
    assert "merge-tree" in err
    assert "WARNING" in err

    assert _merge_run(mod, straddle.clone, capsys)[0] == 1


def test_warn_on_error_uses_actions_annotation(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    code, _, err = _merge_run(
        mod, straddle.clone, capsys, "--warn-on-error", ref="origin/nope"
    )

    assert code == 0
    assert err.startswith("::warning title=CHANGELOG merge-time freeze not evaluated::")


def test_warn_on_error_does_not_swallow_a_violation(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload, _ = _merge_run(mod, straddle.clone, capsys, "--warn-on-error")

    assert code == 1
    assert payload["ok"] is False


def test_head_without_merge_into_is_a_usage_error(
    mod: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--head", "HEAD"])

    assert excinfo.value.code == 2
    assert "--merge-into" in capsys.readouterr().err


def test_warn_on_error_without_merge_into_is_a_usage_error(
    mod: types.ModuleType,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--warn-on-error"])

    assert excinfo.value.code == 2


def test_require_tags_ignored_in_merge_mode(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _late_branch(straddle, CL_EXPECTED)
    _drop_local_tags(straddle.clone)

    code, payload, err = _merge_run(mod, straddle.clone, capsys, "--require-tags")

    assert code == 0
    assert payload["ok"] is True
    assert "since_tag" not in err


def test_merge_into_independent_of_tags(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _drop_local_tags(straddle.clone)
    assert list_tags(straddle.clone) == []

    code, payload, _ = _merge_run(mod, straddle.clone, capsys)

    assert code == 1
    assert _pairs(payload) == {("v1.1.0", "merge_changes_released_section", "changed")}


_TAGGED_1_1_0: Section = ("1.1.0", ["- tagged bullet"])
_STRAY_1_1_0: Section = ("1.1.0", ["- tagged bullet", "- stray bullet"])
_MOVED: Section = ("Unreleased", ["- moved bullet"])
_RELEASED_CHANGED = ("v1.1.0", "merge_changes_released_section", "changed")


def _repair_commit(repo: Path, *sections: Section) -> None:
    _commit(repo, *sections, version="1.1.0", since_tag="v1.1.0")


def _drift_repo(
    make_git_repo: Callable[..., Path], *, drifted: bool
) -> tuple[Path, str]:
    """A repo whose main has tag ``v1.1.0``; ``drifted`` adds a stray bullet to it.

    Returns the repo, checked out on a ``repair`` branch cut from main, and
    main's sha, which the tests pass as ``--merge-into``.
    """
    repo = make_git_repo("drift")
    _commit(
        repo,
        UNRELEASED,
        _TAGGED_1_1_0,
        version="1.1.0",
        since_tag="v1.1.0",
        tag="v1.1.0",
    )
    if drifted:
        _repair_commit(repo, UNRELEASED, _STRAY_1_1_0)
    main_sha = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", "-q", "-b", "repair")
    return repo, main_sha


def test_merge_into_accepts_a_restoration_to_the_tag_text(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, _TAGGED_1_1_0)

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 0
    assert payload["ok"] is True
    assert payload["violations"] == []


def test_merge_into_does_not_flag_a_drifted_section_the_merge_leaves_alone(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, _STRAY_1_1_0)

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    # Rule 4 compares main with the merge; the drift against the tag is Rule 1's.
    assert code == 0
    assert payload["violations"] == []


@pytest.mark.parametrize(
    ("label", "section"),
    [
        (
            "further away from the tag",
            ("1.1.0", ["- tagged bullet", "- stray bullet", "- another stray"]),
        ),
        ("a third text unrelated to the tag", ("1.1.0", ["- different bullet"])),
    ],
)
def test_merge_into_still_flags_a_change_that_does_not_end_at_the_tag_text(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
    label: str,
    section: Section,
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, section)

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1, label
    assert _pairs(payload) == {_RELEASED_CHANGED}


def test_merge_into_still_flags_a_change_to_a_section_that_matches_its_tag(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=False)
    _repair_commit(repo, UNRELEASED, _STRAY_1_1_0)

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {_RELEASED_CHANGED}


def test_merge_into_never_forgives_a_removed_released_section(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, ("Unreleased", ["- everything moved"]))

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {("v1.1.0", "merge_changes_released_section", "removed")}


def test_merge_into_fails_closed_when_the_release_tag_is_not_readable(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, _TAGGED_1_1_0)
    git_in(repo, "tag", "-d", "v1.1.0")

    code, payload, err = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {_RELEASED_CHANGED}
    assert "git fetch --tags" in err


def _repoint_tag_at(repo: Path, *sections: Section) -> None:
    """Re-point ``v1.1.0`` at an orphan commit whose CHANGELOG holds *sections*."""
    git_in(repo, "checkout", "-q", "--orphan", "bare")
    _repair_commit(repo, *sections)
    git_in(repo, "tag", "-f", "v1.1.0")
    git_in(repo, "checkout", "-q", "repair")


def test_merge_into_fails_closed_when_the_tag_copy_lacks_the_section(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, _TAGGED_1_1_0)
    _repoint_tag_at(repo, UNRELEASED)

    code, payload, err = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {_RELEASED_CHANGED}
    assert "git fetch --tags" in err


def test_merge_into_fails_closed_when_the_tag_copy_repeats_the_section(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, _TAGGED_1_1_0)
    # The first copy equals the merged text; an ambiguous tag copy must not pass.
    _repoint_tag_at(repo, UNRELEASED, _TAGGED_1_1_0, _TAGGED_1_1_0)

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {_RELEASED_CHANGED}


def test_merge_into_never_forgives_a_duplicated_released_section(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, main_sha = _drift_repo(make_git_repo, drifted=True)
    _repair_commit(repo, _MOVED, _TAGGED_1_1_0, _TAGGED_1_1_0)

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {
        ("v1.1.0", "merge_changes_released_section", "duplicated")
    }


def test_merge_into_forgives_each_section_against_its_own_tag(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("two-sections")
    older: Section = ("1.1.0", ["- older tagged"])
    newer: Section = ("1.2.0", ["- newer tagged"])
    _commit(repo, UNRELEASED, older, version="1.1.0", since_tag="v1.1.0", tag="v1.1.0")
    _commit(
        repo,
        UNRELEASED,
        newer,
        older,
        version="1.2.0",
        since_tag="v1.1.0",
        tag="v1.2.0",
    )
    _commit(
        repo,
        UNRELEASED,
        ("1.2.0", [*newer[1], "- newer stray"]),
        ("1.1.0", [*older[1], "- older stray"]),
        version="1.2.0",
        since_tag="v1.1.0",
    )
    main_sha = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", "-q", "-b", "repair")
    # Restore 1.1.0 to its tag and push 1.2.0 further from its own.
    _commit(
        repo,
        UNRELEASED,
        ("1.2.0", [*newer[1], "- newer stray", "- newer more"]),
        older,
        version="1.2.0",
        since_tag="v1.1.0",
    )

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {("v1.2.0", "merge_changes_released_section", "changed")}


def test_merge_into_does_not_read_a_ref_named_like_a_non_release_heading(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("legacy")
    _commit(repo, UNRELEASED, ("legacy", ["- old"]), version="1.1.0")
    main_sha = git_in(repo, "rev-parse", "HEAD")
    git_in(repo, "checkout", "-q", "-b", "repair")
    _commit(repo, UNRELEASED, ("legacy", ["- new"]), version="1.1.0")
    # A branch named like the heading holds the merged text; it is not a release tag.
    git_in(repo, "branch", "legacy")

    code, payload, _ = _merge_run(mod, repo, capsys, ref=main_sha)

    assert code == 1
    assert _pairs(payload) == {("legacy", "merge_changes_released_section", "changed")}


def test_changelog_flag_names_the_path(
    mod: types.ModuleType,
    make_git_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("repo")
    commit_files(repo, {"docs/CHANGELOG.md": CL_BASE}, "base")
    git_in(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {"docs/CHANGELOG.md": CL_BRANCH}, "gamma")
    git_in(repo, "checkout", "-q", "main")
    commit_files(repo, {"docs/CHANGELOG.md": CL_MAIN}, "cut 1.1.0")
    git_in(repo, "update-ref", "refs/remotes/origin/main", "main")
    git_in(repo, "checkout", "-q", "feature")

    code = mod.main(
        [
            "--changelog",
            str(repo / "docs" / "CHANGELOG.md"),
            "--merge-into",
            "origin/main",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert _pairs(payload) == {("v1.1.0", "merge_changes_released_section", "changed")}


def test_head_defaults_to_head_ref(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_in(straddle.clone, "checkout", "-q", "--detach", "origin/main")

    code, payload, _ = _merge_run(mod, straddle.clone, capsys)
    assert (code, payload["head"]) == (0, "HEAD")

    code, payload, _ = _merge_run(
        mod, straddle.clone, capsys, "--head", straddle.branch_sha
    )
    assert (code, payload["head"]) == (1, straddle.branch_sha)


def _merge_summary(
    mod: types.ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[str]]:
    changelog = str(repo / "CHANGELOG.md")
    code = mod.main(["--changelog", changelog, "--merge-into", "origin/main"])
    return code, capsys.readouterr().out.splitlines()


def test_violation_summary_ends_with_the_remedy(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _merge_summary(mod, straddle.clone, capsys)

    assert code == 1
    assert out[-1] == _REMEDY
    row = "[merge_changes_released_section] v1.1.0: changed"
    assert any(row in line for line in out)


def test_merge_into_clean_summary_says_ok(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _late_branch(straddle, CL_EXPECTED)

    code, out = _merge_summary(mod, straddle.clone, capsys)

    assert code == 0
    assert "OK" in out[-1]


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def test_simulated_cut_squash_turns_main_red_without_sync_green_with_sync(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = straddle.clone
    released = git_in(clone, "show", "v1.1.0:CHANGELOG.md") + "\n"

    squash_onto_origin_main(straddle)
    code, payload, _ = _run(mod, clone, capsys, "--require-tags")
    assert code == 1
    assert _violation(payload, "section_changed")["tag"] == "v1.1.0"
    squashed = (clone / "CHANGELOG.md").read_text(encoding="utf-8")
    assert GAMMA in _section(squashed, "## [1.1.0]", "## [1.0.0]")

    git_in(clone, "checkout", "-q", BRANCH)
    synced = CliRunner().invoke(
        main, ["worktree", "changelog-sync", "--worktree", str(clone), "--base", "main"]
    )
    assert synced.exit_code == 0, synced.output
    assert json.loads(synced.output)["state"] == "moved"

    squash_onto_origin_main(straddle)
    code, payload, _ = _run(mod, clone, capsys, "--require-tags")
    assert code == 0, payload
    squashed = (clone / "CHANGELOG.md").read_text(encoding="utf-8")
    assert squashed == CL_EXPECTED
    assert GAMMA in _section(squashed, "## [Unreleased]", "## [1.1.0]")
    assert _section(squashed, "## [1.1.0]", "## [1.0.0]") == _section(
        released, "## [1.1.0]", "## [1.0.0]"
    )


# ---------------------------------------------------------------------------
# #3322: a git that cannot be launched is a FreezeCheckError, not a traceback
# ---------------------------------------------------------------------------

_GIT_LAUNCH_MSG = "could not run git"


def test_git_launch_oserror_becomes_freeze_check_error(
    mod: types.ModuleType,
    released_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    AbsentToolCli({"git"}).install(monkeypatch)

    with pytest.raises(mod.FreezeCheckError, match=_GIT_LAUNCH_MSG) as exc:
        mod._git(released_repo, "status")

    assert isinstance(exc.value.__cause__, FileNotFoundError)


def test_warn_on_error_git_missing_passes_with_warning(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    AbsentToolCli({"git"}).install(monkeypatch)

    code, payload, err = _merge_run(mod, straddle.clone, capsys, "--warn-on-error")

    assert code == 0
    assert payload == {}
    assert "WARNING" in err
    assert _GIT_LAUNCH_MSG in err
    assert "Traceback" not in err


def test_git_missing_without_warn_on_error_fails_cleanly(
    mod: types.ModuleType,
    straddle: ReleaseMovedRepo,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    AbsentToolCli({"git"}).install(monkeypatch)

    code, payload, err = _merge_run(mod, straddle.clone, capsys)

    assert code == 1
    assert payload == {}
    assert _GIT_LAUNCH_MSG in err
    assert "Traceback" not in err


def test_git_missing_in_the_tag_mode_fails_cleanly(
    mod: types.ModuleType,
    released_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    AbsentToolCli({"git"}).install(monkeypatch)

    code = mod.main(["--changelog", str(released_repo / "CHANGELOG.md")])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert _GIT_LAUNCH_MSG in captured.err
    assert "Traceback" not in captured.err
