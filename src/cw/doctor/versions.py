"""Version, dependency, disclaimer, and daemon-reachability checks for cw doctor.

Split out of ``cw.doctor.core`` (#1314, part 2). Holds the bypass-permissions
disclaimer check, the claude-binary version check, the codex-capability probe
mapping, the installed-vs-source cw version + dependency drift checks, and the
native-daemon roster reachability check. Leaf module — no cross-``doctor``
dependencies.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess as _sp
import tomllib
import urllib.parse
from pathlib import Path

from cw.doctor._shared import CheckResult
from cw.executor import (
    CODEX_NOT_FOUND,
    CODEX_VERSION_UNKNOWN,
    codex_capability_diagnosis,
)
from cw.native_daemon import _ROSTER_PATH

# Path to Claude Code user settings — read for the disclaimer-acceptance flag.
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Minimum supported Claude Code version for native-daemon dispatch.
_MIN_CLAUDE_VERSION = (2, 1, 139)

# Number of components (major.minor.patch) required in a version string.
_VERSION_PARTS = 3

# Check name for the installed-vs-source cw version drift detector.
_CW_VERSION_CHECK_NAME = "cw-version"

# Check name for the declared-vs-installed dependency drift detector.
_CW_DEPS_CHECK_NAME = "cw-deps"

# Reinstall command surfaced in warnings when the installed cw is stale.
_CW_REINSTALL_CMD = "uv tool install --reinstall claude-workspace"

# Package name used for importlib.metadata lookups.
_CW_PACKAGE_NAME = "claude-workspace"

# Separator characters that terminate a PEP 508 dependency name (version
# specifiers, environment markers, whitespace) — mirrors _parse_version's
# lightweight, no-`packaging`-dependency parsing style.
_DEP_NAME_SEPARATORS = "<>=!~; "


def _check_bypass_disclaimer() -> CheckResult:
    """Check whether the user has accepted the bypass-permissions disclaimer."""
    try:
        raw = _CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            "bypass-disclaimer",
            ok=True,
            warn=True,
            detail=f"settings.json not found at {_CLAUDE_SETTINGS_PATH}",
        )
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "bypass-disclaimer",
            ok=True,
            warn=True,
            detail=f"could not parse settings.json: {exc}",
        )
    if data.get("skipDangerousModePermissionPrompt"):
        return CheckResult("bypass-disclaimer", ok=True, warn=False, detail="accepted")
    return CheckResult(
        "bypass-disclaimer",
        ok=True,
        warn=True,
        detail=(
            "skipDangerousModePermissionPrompt not set"
            " — run `claude --dangerously-skip-permissions` once interactively"
        ),
    )


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a 'X.Y.Z' version string into a comparable int tuple.

    Returns an empty tuple when the string is absent, too short, or
    non-numeric — callers treat an empty return as "unparseable".
    """
    parts = v.split(".")
    if len(parts) < _VERSION_PARTS:
        return ()
    try:
        return tuple(int(p) for p in parts[:_VERSION_PARTS])
    except ValueError:
        return ()


def _dep_distribution_name(entry: str) -> str:
    """Extract the leading distribution name from a PEP 508 dependency entry.

    Scans for the first separator character (version specifier, environment
    marker, or whitespace) and returns the prefix, stripped. E.g.
    ``"psutil>=6.0"`` → ``"psutil"``, ``"foo; sys_platform=='win32'"`` → ``"foo"``.
    """
    for i, ch in enumerate(entry):
        if ch in _DEP_NAME_SEPARATORS:
            return entry[:i].strip()
    return entry.strip()


def _check_claude_version() -> CheckResult:
    """Check that the claude binary is reachable and return its version.

    Returns ok=True, warn=True when the binary ran but exited non-zero, or when
    the version string cannot be parsed, or when the version is below the floor
    required for native-daemon dispatch.
    """
    try:
        proc = _sp.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        return CheckResult("claude-version", ok=False, detail="claude binary not found")
    except _sp.TimeoutExpired:
        return CheckResult(
            "claude-version", ok=False, detail="claude --version timed out (10s)"
        )

    output = proc.stdout or proc.stderr or ""
    version_line = output.splitlines()[0] if output else ""

    if proc.returncode != 0:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"claude --version exited {proc.returncode}: {version_line}",
        )

    # Parse the leading X.Y.Z token from the version line.
    first_token = version_line.split()[0] if version_line else ""
    parsed = _parse_version(first_token)
    if not parsed:
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=f"could not parse version: {version_line}",
        )

    if parsed < _MIN_CLAUDE_VERSION:
        min_str = ".".join(str(x) for x in _MIN_CLAUDE_VERSION)
        return CheckResult(
            "claude-version",
            ok=True,
            warn=True,
            detail=(
                f"{version_line} — upgrade to >= {min_str} for native-daemon dispatch"
            ),
        )

    return CheckResult("claude-version", ok=True, detail=version_line)


def _check_codex_capability() -> CheckResult:
    """Report codex CLI capability via the shared probe (#1238).

    Thin mapping over ``cw.executor.codex_capability_diagnosis`` — no subprocess
    logic here. Binary absent → FAIL with an install hint; present but
    ``--version`` unconfirmed → WARN with a remediation hint (this diagnosis
    also drives dispatch's pre-spawn capability gate to park codex-backed
    tasks, so the WARN needs an actionable next step, not just the raw
    failure detail); capable → OK with the version line as the diagnostics
    record (the ``detail`` field itself is the persisted diagnostic).
    """
    probe = codex_capability_diagnosis()
    if probe.diagnosis == CODEX_NOT_FOUND:
        return CheckResult(
            "codex-capability",
            ok=False,
            detail=f"{probe.detail} — install via npm install -g @openai/codex",
        )
    if probe.diagnosis == CODEX_VERSION_UNKNOWN:
        return CheckResult(
            "codex-capability",
            ok=True,
            warn=True,
            detail=f"{probe.detail} — re-run `codex --version` manually to diagnose"
            " (PATH, permissions, network)",
        )
    return CheckResult("codex-capability", ok=True, warn=False, detail=probe.detail)


def _resolve_cw_source_path() -> Path | CheckResult:
    """Resolve the local source dir for the installed cw, or a skip CheckResult.

    Returns the source :class:`Path` for an editable/local install. For a
    registry/PyPI install (no package metadata, no/foreign ``direct_url.json``)
    returns an ``ok=True, warn=False`` skip :class:`CheckResult` that the
    caller propagates unchanged.
    """
    try:
        dist = importlib.metadata.distribution(_CW_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is None:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    try:
        direct_url: dict[str, object] = json.loads(direct_url_text)
    except json.JSONDecodeError:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="malformed direct_url.json; skipping source check",
        )

    url = direct_url.get("url", "")
    if not isinstance(url, str) or not url.startswith("file://"):
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=False,
            detail="installed from registry; skipping source check",
        )

    return Path(urllib.parse.urlparse(url).path)


def _check_cw_version() -> CheckResult:
    """Check whether the installed cw matches the source repo's pyproject.toml version.

    Silent-skips (ok=True, warn=False) for registry/PyPI installs and when
    package metadata is absent — source-version comparison only makes sense
    for local installs. Warns (ok=True, warn=True) when installed is behind
    source or when the source path is stale/unreadable.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return source_path

    if not source_path.exists():
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"source path {source_path} no longer exists"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    pyproject_path = source_path / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
        source_version_str: str = pyproject["project"]["version"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"could not read source version from {pyproject_path}",
        )

    installed_version_str = importlib.metadata.version(_CW_PACKAGE_NAME)

    installed_ver = _parse_version(installed_version_str)
    source_ver = _parse_version(source_version_str)

    if not installed_ver or not source_ver:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"could not compare versions:"
                f" installed={installed_version_str} source={source_version_str}"
            ),
        )

    if installed_ver < source_ver:
        return CheckResult(
            _CW_VERSION_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"installed {installed_version_str} < source {source_version_str}"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    return CheckResult(
        _CW_VERSION_CHECK_NAME,
        ok=True,
        warn=False,
        detail=f"installed {installed_version_str} matches source",
    )


def _check_cw_deps() -> CheckResult:
    """Check whether every dependency declared in source pyproject.toml is installed.

    Detects the class of drift that crash-looped `cw dev-queue serve` on
    2026-07-09 after #1075 added `psutil` to pyproject.toml but the running
    tool venv was never re-synced. Silent-skips (ok=True, warn=False) for
    registry/PyPI installs and when package metadata is absent — this check
    only makes sense for local editable installs. Warns (ok=True, warn=True)
    when the source path is stale, the dependencies list is unreadable or
    malformed, or one or more declared dependencies are not installed.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=source_path.ok,
            warn=source_path.warn,
            detail=source_path.detail,
        )

    if not source_path.exists():
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(
                f"source path {source_path} no longer exists"
                f" — run `{_CW_REINSTALL_CMD}`"
            ),
        )

    pyproject_path = source_path / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
        dependencies = pyproject["project"]["dependencies"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"could not read dependencies from {pyproject_path}",
        )

    if not isinstance(dependencies, list):
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=f"dependencies in {pyproject_path} is not a list",
        )

    missing: list[str] = []
    for entry in dependencies:
        if not isinstance(entry, str):
            continue
        name = _dep_distribution_name(entry)
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)

    if missing:
        return CheckResult(
            _CW_DEPS_CHECK_NAME,
            ok=True,
            warn=True,
            detail=(f"not installed: {', '.join(missing)} — run `{_CW_REINSTALL_CMD}`"),
        )

    return CheckResult(
        _CW_DEPS_CHECK_NAME,
        ok=True,
        warn=False,
        detail=f"{len(dependencies)} declared dependencies all installed",
    )


def _check_daemon_reachable() -> CheckResult:
    """Check whether the Claude native daemon's roster reports a running supervisor."""
    try:
        raw = _ROSTER_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            "daemon-reachable",
            ok=True,
            warn=True,
            detail=f"roster.json not found at {_ROSTER_PATH} — daemon not started?",
        )
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "daemon-reachable",
            ok=True,
            warn=True,
            detail=f"could not parse roster.json: {exc}",
        )
    pid = data.get("supervisorPid", 0)
    if isinstance(pid, int) and pid > 0:
        return CheckResult(
            "daemon-reachable", ok=True, warn=False, detail=f"supervisorPid={pid}"
        )
    return CheckResult(
        "daemon-reachable",
        ok=True,
        warn=True,
        detail="supervisorPid absent or zero — daemon may not be running",
    )
