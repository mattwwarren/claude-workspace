"""Tests for the ``cw-deps-drift`` doctor check (#2124).

Detects an installed optional-extra package (``mcp``, ``starlette``,
``uvicorn``) whose version differs from the ``uv.lock`` pin of the editable
source tree -- the stale-extra class behind the ``mcp 1.27.1`` vs ``2.1.1``
incident. Direct calls to ``_check_cw_deps_drift()``, monkeypatching the
``_resolve_cw_source_path`` seam and ``importlib.metadata.version`` rather than
touching the real environment (precedent: ``test_doctor_skills_drift.py``).

Verbatim ``detail`` templates, one per outcome (``{lock}`` = ``<repo>/uv.lock``,
``{pyproject}`` = ``<repo>/pyproject.toml``, ``{cmd}`` = the remediation
command ``uv tool install --reinstall -e '<repo>[<extras>]'``). Only drift sets
``warn=True``; every skip and OK is ``ok=True, warn=False``. Skip reasons are
short class labels (``unreadable: <ExcName>``), never raw exception text:

- drift WARN:            ``<pkg> <inst> installed != <locked> locked (extra <x>)``
                         entries joined by ``"; "``, then notes, then
                         `` — run `{cmd}` ``
- all match / OK:        ``<n> optional-extra package(s) match uv.lock``
- not installed note:    ``; not installed: <a>, <b>`` (sorted)
- not in lock note:      ``; not in uv.lock: <a>, <b>`` (sorted)
- mixed outcome:         drift entries + ``; not installed: ...`` +
                         ``; not in uv.lock: ...`` + `` — run `{cmd}` ``
- no optional extras:    ``no optional extras declared in {pyproject}``
- pyproject unreadable:  ``could not read optional extras from {pyproject}
                         ({reason}); skipping extras drift check``
- lock unreadable:       ``could not read {lock} ({reason}); skipping extras
                         drift check`` where reason is ``not found`` |
                         ``unreadable: <ExcName>`` | ``malformed:
                         TOMLDecodeError`` | ``malformed: no [[package]] array``
- source path gone:      ``source path <p> no longer exists; skipping extras
                         drift check``
"""

from __future__ import annotations

import importlib.metadata
import json
from typing import TYPE_CHECKING

import pytest

from cw.doctor._shared import CheckResult
from cw.doctor.core import run_doctor
from cw.doctor.versions import (
    _CW_DEPS_DRIFT_CHECK_NAME,
    _check_cw_deps_drift,
    _dep_distribution_name,
)
from tests.conftest import _patch_cw_dist_not_found

if TYPE_CHECKING:
    from pathlib import Path

_SKIP_SUFFIX = "skipping extras drift check"
_MCP_EXTRA = {"mcp": ["mcp[cli]>=2.1.1,<3"]}


def _write_repo(
    tmp_path: Path,
    *,
    extras: dict[str, list[str]],
    lock_packages: dict[str, list[str]] | None,
) -> Path:
    """Write ``<tmp>/repo/pyproject.toml`` and (optionally) ``uv.lock``.

    *extras* maps extra name to PEP 508 entry strings (empty dict -> no
    ``[project.optional-dependencies]`` table). *lock_packages* maps lock
    package name to the list of versions to emit as ``[[package]]`` tables;
    ``None`` writes no lockfile.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = ["[project]", 'name = "demo"', 'version = "0.0.1"']
    if extras:
        lines.append("[project.optional-dependencies]")
        lines.extend(
            f"{extra} = {json.dumps(entries)}" for extra, entries in extras.items()
        )
    (repo / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if lock_packages is not None:
        lock_lines = ["version = 1", "revision = 3"]
        for name, versions in lock_packages.items():
            for version in versions:
                lock_lines.extend(
                    ["", "[[package]]", f'name = "{name}"', f'version = "{version}"']
                )
        (repo / "uv.lock").write_text("\n".join(lock_lines) + "\n", encoding="utf-8")
    return repo


def _patch_source(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr("cw.doctor.versions._resolve_cw_source_path", lambda: repo)


def _patch_installed(monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]) -> None:
    """Make ``importlib.metadata.version`` serve *versions*; others not installed."""

    def _fake(name: str) -> str:
        if name not in versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr(importlib.metadata, "version", _fake)


def _assert_quiet(result: CheckResult) -> None:
    assert result.name == _CW_DEPS_DRIFT_CHECK_NAME
    assert result.ok is True
    assert result.warn is False


def _cmd(repo: Path, extras: str) -> str:
    return f"uv tool install --reinstall -e '{repo}[{extras}]'"


def test_check_name_constant() -> None:
    assert _CW_DEPS_DRIFT_CHECK_NAME == "cw-deps-drift"


def test_extra_not_installed_skips_not_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1"]})
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert (
        result.detail == "0 optional-extra package(s) match uv.lock; not installed: mcp"
    )
    assert "2.1.1" not in result.detail


def test_version_inside_pin_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1"]})
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "2.1.1"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "1 optional-extra package(s) match uv.lock"


def test_version_outside_pin_warns_names_package_versions_and_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1"]})
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "1.27.1"})

    result = _check_cw_deps_drift()

    assert result.name == _CW_DEPS_DRIFT_CHECK_NAME
    assert result.ok is True
    assert result.warn is True
    assert result.detail == (
        f"mcp 1.27.1 installed != 2.1.1 locked (extra mcp) — run `{_cmd(repo, 'mcp')}`"
    )


def test_installed_matches_one_of_multiple_lock_versions_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(
        tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.0.0", "2.1.1"]}
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "2.0.0"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "1 optional-extra package(s) match uv.lock"


def test_installed_outside_all_of_multiple_lock_versions_lists_each_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(
        tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1", "2.0.0"]}
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "1.27.1"})

    result = _check_cw_deps_drift()

    assert result.warn is True
    assert "mcp 1.27.1 installed != 2.0.0/2.1.1 locked (extra mcp)" in result.detail


def test_package_installed_but_absent_from_lock_skips_with_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"other": ["1.0"]})
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "2.1.1"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert (
        result.detail
        == "0 optional-extra package(s) match uv.lock; not in uv.lock: mcp"
    )


def test_lockfile_missing_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages=None)
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "2.1.1"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read {repo / 'uv.lock'} (not found); {_SKIP_SUFFIX}"
    )


def test_lockfile_malformed_toml_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages=None)
    (repo / "uv.lock").write_text("this is = = not toml [[", encoding="utf-8")
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read {repo / 'uv.lock'} (malformed: TOMLDecodeError);"
        f" {_SKIP_SUFFIX}"
    )


def test_lockfile_unreadable_oserror_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages=None)
    (repo / "uv.lock").mkdir()  # open() on a directory -> IsADirectoryError
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read {repo / 'uv.lock'} (unreadable: IsADirectoryError);"
        f" {_SKIP_SUFFIX}"
    )


def test_lockfile_non_utf8_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tomllib raises UnicodeDecodeError (a ValueError) on non-UTF-8 bytes (#2226)."""
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages=None)
    (repo / "uv.lock").write_bytes(b"\xff\xfe\x00 not utf-8")
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read {repo / 'uv.lock'} (unreadable: UnicodeDecodeError);"
        f" {_SKIP_SUFFIX}"
    )


@pytest.mark.parametrize("lock_body", ['package = "x"\n', "version = 1\n"])
def test_lockfile_without_package_array_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lock_body: str
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages=None)
    (repo / "uv.lock").write_text(lock_body, encoding="utf-8")
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read {repo / 'uv.lock'} (malformed: no [[package]] array);"
        f" {_SKIP_SUFFIX}"
    )


def test_lock_entries_without_name_or_version_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages=None)
    (repo / "uv.lock").write_text(
        'package = ["bare-string", {name = "mcp"}, {version = "9"},'
        ' {name = "mcp", version = "2.1.1"}]\n',
        encoding="utf-8",
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "2.1.1"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "1 optional-extra package(s) match uv.lock"


def test_no_optional_extras_declared_is_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras={}, lock_packages=None)
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == f"no optional extras declared in {repo / 'pyproject.toml'}"


def test_pyproject_extras_not_a_table_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras={}, lock_packages=None)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\noptional-dependencies = "nope"\n', encoding="utf-8"
    )
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read optional extras from {repo / 'pyproject.toml'}"
        f" (malformed: optional-dependencies is not a table); {_SKIP_SUFFIX}"
    )


def test_pyproject_without_project_table_is_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras={}, lock_packages=None)
    (repo / "pyproject.toml").write_text('[tool.x]\nk = "v"\n', encoding="utf-8")
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail.startswith("no optional extras declared in ")


def test_pyproject_missing_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1"]})
    (repo / "pyproject.toml").unlink()
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read optional extras from {repo / 'pyproject.toml'}"
        f" (unreadable: FileNotFoundError); {_SKIP_SUFFIX}"
    )


def test_pyproject_malformed_toml_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1"]})
    (repo / "pyproject.toml").write_text("[project\nbroken = = =", encoding="utf-8")
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read optional extras from {repo / 'pyproject.toml'}"
        f" (malformed: TOMLDecodeError); {_SKIP_SUFFIX}"
    )


def test_pyproject_non_utf8_skips_with_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras=_MCP_EXTRA, lock_packages={"mcp": ["2.1.1"]})
    (repo / "pyproject.toml").write_bytes(b"\xff\xfe\x00 not utf-8")
    _patch_source(monkeypatch, repo)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == (
        f"could not read optional extras from {repo / 'pyproject.toml'}"
        f" (unreadable: UnicodeDecodeError); {_SKIP_SUFFIX}"
    )


def test_name_normalization_pep503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pyproject ``Ruamel.Yaml`` matches lock ``ruamel-yaml``."""
    repo = _write_repo(
        tmp_path,
        extras={"yaml": ["Ruamel.Yaml>=0.18"]},
        lock_packages={"ruamel-yaml": ["0.18.6"]},
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"Ruamel.Yaml": "0.18.6"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "1 optional-extra package(s) match uv.lock"


def test_lock_names_are_normalized_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(
        tmp_path,
        extras={"yaml": ["ruamel-yaml>=0.18"]},
        lock_packages={"Ruamel_Yaml": ["0.18.6"]},
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"ruamel-yaml": "0.17.0"})

    result = _check_cw_deps_drift()

    assert result.warn is True
    assert "ruamel-yaml 0.17.0 installed != 0.18.6 locked (extra yaml)" in result.detail


def test_extra_entry_with_subextra_and_specifier_parses_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(
        tmp_path,
        extras={"mcp": ["mcp[cli]>=2.1.1,<3"]},
        lock_packages={"mcp": ["2.1.1"]},
    )
    _patch_source(monkeypatch, repo)
    # The fake serves only the bare name: "mcp[cli]" would count as not installed.
    _patch_installed(monkeypatch, {"mcp": "2.1.1"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "1 optional-extra package(s) match uv.lock"


def test_dep_distribution_name_strips_subextras() -> None:
    assert _dep_distribution_name("mcp[cli]>=2.1.1,<3") == "mcp"
    assert _dep_distribution_name("foo[a,b]; sys_platform=='win32'") == "foo"
    assert _dep_distribution_name("psutil>=6.0") == "psutil"
    assert _dep_distribution_name("plain") == "plain"


def test_non_string_extra_entries_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(tmp_path, extras={}, lock_packages={"mcp": ["2.1.1"]})
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n[project.optional-dependencies]\n'
        'mcp = ["mcp>=2", 42, {table = "x"}]\nbad = "not-a-list"\n',
        encoding="utf-8",
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"mcp": "2.1.1"})

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "1 optional-extra package(s) match uv.lock"


def test_drift_across_two_extras_lists_both_in_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(
        tmp_path,
        extras={"b": ["beta>=1"], "a": ["alpha>=1"]},
        lock_packages={"alpha": ["2.0"], "beta": ["3.0"]},
    )
    _patch_source(monkeypatch, repo)
    _patch_installed(monkeypatch, {"alpha": "1.0", "beta": "1.0"})

    result = _check_cw_deps_drift()

    assert result.warn is True
    assert result.detail == (
        "alpha 1.0 installed != 2.0 locked (extra a);"
        " beta 1.0 installed != 3.0 locked (extra b)"
        f" — run `{_cmd(repo, 'a,b')}`"
    )


def test_remediation_lists_every_extra_with_an_installed_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``uv tool install --reinstall -e`` replaces the tool env, so the command
    must carry every extra still in use, not just the drifted one."""
    repo = _write_repo(
        tmp_path,
        extras={"a": ["alpha>=1"], "b": ["beta>=1"], "c": ["gamma>=1"]},
        lock_packages={"alpha": ["2.0"], "beta": ["3.0"], "gamma": ["4.0"]},
    )
    _patch_source(monkeypatch, repo)
    # alpha drifted, beta matches (extra b is in use), gamma not installed.
    _patch_installed(monkeypatch, {"alpha": "1.0", "beta": "3.0"})

    result = _check_cw_deps_drift()

    assert result.warn is True
    assert result.detail == (
        "alpha 1.0 installed != 2.0 locked (extra a); not installed: gamma"
        f" — run `{_cmd(repo, 'a,b')}`"
    )


def test_mixed_outcome_drift_with_not_installed_and_not_locked_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _write_repo(
        tmp_path,
        extras={"mcp": ["mcp>=2", "starlette>=1", "uvicorn>=0.30"]},
        lock_packages={"mcp": ["2.1.1"], "uvicorn": ["0.48.0"]},
    )
    _patch_source(monkeypatch, repo)
    # mcp drifts, starlette installed but not locked, uvicorn not installed.
    _patch_installed(monkeypatch, {"mcp": "1.27.1", "starlette": "1.1.0"})

    result = _check_cw_deps_drift()

    assert result.warn is True
    assert result.detail == (
        "mcp 1.27.1 installed != 2.1.1 locked (extra mcp);"
        " not installed: uvicorn; not in uv.lock: starlette"
        f" — run `{_cmd(repo, 'mcp')}`"
    )


def test_registry_install_skips_under_drift_check_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cw_dist_not_found(monkeypatch)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == "installed from registry; skipping source check"


def test_source_path_missing_skips_without_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gone = tmp_path / "gone"
    _patch_source(monkeypatch, gone)

    result = _check_cw_deps_drift()

    _assert_quiet(result)
    assert result.detail == f"source path {gone} no longer exists; {_SKIP_SUFFIX}"


def test_check_included_in_run_doctor_after_cw_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Membership/order only: this runs the real check against the dev venv."""
    ok = CheckResult("stub", ok=True, warn=False, detail="stub")
    for name in (
        "_check_bypass_disclaimer",
        "_check_claude_version",
        "_check_codex_capability",
        "_check_daemon_reachable",
        "_check_ssh_key_loaded",
    ):
        monkeypatch.setattr(f"cw.doctor.core.{name}", lambda: ok)

    names = [c.name for c in run_doctor().checks]

    assert names.count(_CW_DEPS_DRIFT_CHECK_NAME) == 1
    assert names.index(_CW_DEPS_DRIFT_CHECK_NAME) == names.index("cw-deps") + 1
