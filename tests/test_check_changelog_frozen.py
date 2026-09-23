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
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import git_in, list_tags, write_pyproject_override

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
    assert "more than one untagged" in _violation(payload, "entry_outside_unreleased")[
        "detail"
    ]


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
    _commit(repo, UNRELEASED, V100, version="1.0.0", since_tag="release-1", tag="release-1")

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
