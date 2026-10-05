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
import re
import shlex
import subprocess as _sp
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from cw.doctor._shared import CheckResult, SettingsReadFailure, _read_settings
from cw.executor import (
    CODEX_NOT_FOUND,
    CODEX_VERSION_UNKNOWN,
    codex_capability_diagnosis,
)
from cw.native_daemon import _ROSTER_PATH
from cw.ssh import check_ssh_key_available

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

# Check name for the optional-extra installed-vs-uv.lock drift detector (#2124).
_CW_DEPS_DRIFT_CHECK_NAME = "cw-deps-drift"

# Suffix on every drift-check skip detail (lock/pyproject unreadable, source gone).
_DRIFT_SKIP_SUFFIX = "skipping extras drift check"

# Reinstall command surfaced in warnings when the installed cw is stale.
_CW_REINSTALL_CMD = "uv tool install --reinstall claude-workspace"

# Package name used for importlib.metadata lookups.
_CW_PACKAGE_NAME = "claude-workspace"

# Separator characters that terminate a PEP 508 dependency name (extras
# bracket, version specifiers, environment markers, whitespace) — mirrors
# _parse_version's lightweight, no-`packaging`-dependency parsing style.
_DEP_NAME_SEPARATORS = "[<>=!~; "


def _check_bypass_disclaimer() -> CheckResult:
    """Check whether the user has accepted the bypass-permissions disclaimer.

    Reads through :func:`cw.doctor._shared._read_settings`, so an unreadable
    or unparseable settings file is a WARN naming the file and the failure
    class rather than an exception out of ``run_doctor`` (#2226).
    """
    data = _read_settings(_CLAUDE_SETTINGS_PATH)
    if isinstance(data, SettingsReadFailure):
        if data.missing:
            detail = f"settings.json not found at {_CLAUDE_SETTINGS_PATH}"
        else:
            detail = f"could not read/parse {_CLAUDE_SETTINGS_PATH} ({data.reason})"
        return CheckResult("bypass-disclaimer", ok=True, warn=True, detail=detail)
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

    Scans for the first separator character (extras bracket, version
    specifier, environment marker, or whitespace) and returns the prefix,
    stripped. E.g. ``"psutil>=6.0"`` → ``"psutil"``,
    ``"mcp[cli]>=2.1.1,<3"`` → ``"mcp"``,
    ``"foo; sys_platform=='win32'"`` → ``"foo"``.
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


def _normalize_dist_name(name: str) -> str:
    """PEP 503 normalization: runs of ``-``/``_``/``.`` collapse to ``-``, lowercased.

    ``uv.lock`` records ``ruamel-yaml`` where ``pyproject.toml`` says
    ``ruamel.yaml``; ``packaging`` is deliberately not a dependency.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True, order=True)
class _ExtraDrift:
    """One optional-extra package whose installed version is outside the lock.

    *locked* is every locked version joined by ``/`` (a lock can hold several
    ``[[package]]`` entries per name when resolution forks).
    """

    package: str
    extra: str
    installed: str
    locked: str


@dataclass(frozen=True)
class _ExtrasScan:
    """Outcome of comparing installed optional-extra packages with the lock.

    *installed_extras* is every declared extra with at least one installed
    package — the set a reinstall must keep (``uv tool install --reinstall -e``
    replaces the whole tool environment).
    """

    matched: int
    not_installed: tuple[str, ...]
    not_locked: tuple[str, ...]
    drifted: tuple[_ExtraDrift, ...]
    installed_extras: tuple[str, ...]


def _load_toml(path: Path) -> dict[str, object] | str:
    """Parse *path* as TOML, or return a short failure-reason label.

    Reasons are class labels, never raw exception text. ``UnicodeDecodeError``
    (a ``ValueError``) is caught explicitly: ``tomllib.load`` raises it on
    non-UTF-8 bytes (precedent: ``_shared._read_settings``, #2226).
    """
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return "not found"
    except tomllib.TOMLDecodeError:
        return "malformed: TOMLDecodeError"
    except (OSError, UnicodeDecodeError) as exc:
        return f"unreadable: {type(exc).__name__}"


def _read_lock_versions(lock_path: Path) -> dict[str, frozenset[str]] | str:
    """Map each normalized package name in ``uv.lock`` to its locked versions.

    Returns a failure-reason string (see :func:`_load_toml`) when the lock is
    missing, unreadable, malformed, or has no ``[[package]]`` array.
    Individual entries lacking a string ``name``/``version`` are ignored.
    """
    lock = _load_toml(lock_path)
    if isinstance(lock, str):
        return lock
    packages = lock.get("package")
    if not isinstance(packages, list):
        return "malformed: no [[package]] array"
    versions: dict[str, set[str]] = {}
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        name, version = entry.get("name"), entry.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions.setdefault(_normalize_dist_name(name), set()).add(version)
    return {name: frozenset(found) for name, found in versions.items()}


def _read_optional_extras(pyproject_path: Path) -> dict[str, list[str]] | str:
    """Read ``[project.optional-dependencies]`` as ``{extra: [entry, ...]}``.

    An absent table is an empty mapping (no extras declared). Returns a
    failure-reason string when the file cannot be read or parsed, or when the
    table is not a table. Non-list extras and non-string entries are dropped.
    """
    pyproject = _load_toml(pyproject_path)
    if isinstance(pyproject, str):
        return pyproject
    project = pyproject.get("project")
    optional = (
        project.get("optional-dependencies") if isinstance(project, dict) else None
    )
    if optional is None:
        return {}
    if not isinstance(optional, dict):
        return "malformed: optional-dependencies is not a table"
    return {
        extra: [entry for entry in entries if isinstance(entry, str)]
        for extra, entries in optional.items()
        if isinstance(entries, list)
    }


def _scan_extras(
    extras: dict[str, list[str]], lock: dict[str, frozenset[str]]
) -> _ExtrasScan:
    """Compare each declared optional-extra package's installed version to *lock*.

    Not installed and installed-but-not-locked are quiet skips, never drift.
    Only direct declarations are checked, not their transitive dependencies.
    """
    matched = 0
    not_installed: set[str] = set()
    not_locked: set[str] = set()
    drifted: list[_ExtraDrift] = []
    installed_extras: set[str] = set()
    for extra, entries in extras.items():
        for entry in entries:
            package = _dep_distribution_name(entry)
            if not package:
                continue
            try:
                installed = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                not_installed.add(package)
                continue
            installed_extras.add(extra)
            locked = lock.get(_normalize_dist_name(package))
            if locked is None:
                not_locked.add(package)
            elif installed in locked:
                matched += 1
            else:
                drifted.append(
                    _ExtraDrift(package, extra, installed, "/".join(sorted(locked)))
                )
    return _ExtrasScan(
        matched=matched,
        not_installed=tuple(sorted(not_installed)),
        not_locked=tuple(sorted(not_locked)),
        drifted=tuple(sorted(drifted)),
        installed_extras=tuple(sorted(installed_extras)),
    )


def _drift_skip(detail: str) -> CheckResult:
    """A quiet (ok, no warn) ``cw-deps-drift`` result."""
    return CheckResult(_CW_DEPS_DRIFT_CHECK_NAME, ok=True, warn=False, detail=detail)


def _drift_result(source_path: Path, scan: _ExtrasScan) -> CheckResult:
    """Build the ``cw-deps-drift`` result from a completed scan.

    Only drift warns. Not-installed / not-in-lock packages are appended as
    ``; not installed: ...`` / ``; not in uv.lock: ...`` notes either way. The
    remediation lists every extra that has an installed package, because
    ``uv tool install --reinstall -e`` replaces the tool environment and would
    otherwise drop the extras that are still in use. It deliberately does not
    reuse ``_CW_REINSTALL_CMD``, which installs from the registry sans extras.
    """
    notes: list[str] = []
    if scan.not_installed:
        notes.append(f"not installed: {', '.join(scan.not_installed)}")
    if scan.not_locked:
        notes.append(f"not in uv.lock: {', '.join(scan.not_locked)}")
    if not scan.drifted:
        summary = f"{scan.matched} optional-extra package(s) match uv.lock"
        return _drift_skip("; ".join([summary, *notes]))
    entries = "; ".join(
        f"{d.package} {d.installed} installed != {d.locked} locked (extra {d.extra})"
        for d in scan.drifted
    )
    target = shlex.quote(f"{source_path}[{','.join(scan.installed_extras)}]")
    return CheckResult(
        _CW_DEPS_DRIFT_CHECK_NAME,
        ok=True,
        warn=True,
        detail=(
            "; ".join([entries, *notes])
            + f" — run `uv tool install --reinstall -e {target}`"
        ),
    )


def _check_cw_deps_drift() -> CheckResult:
    """Check installed optional-extra versions against the source ``uv.lock`` (#2124).

    Catches the stale-extra class behind the ``mcp 1.27.1`` installed vs
    ``2.1.1`` locked incident: ``cw-deps`` only proves declared dependencies
    are *present*, not that they match the lock. Sibling of
    :func:`_check_cw_deps` (separate outcome, separate input file). Skips
    quietly (ok=True, warn=False) for registry installs, a vanished source
    path (``cw-version``/``cw-deps`` already warn on it), and any unreadable
    pyproject/lock. Only confirmed drift warns.
    """
    source_path = _resolve_cw_source_path()
    if isinstance(source_path, CheckResult):
        return CheckResult(
            _CW_DEPS_DRIFT_CHECK_NAME,
            ok=source_path.ok,
            warn=source_path.warn,
            detail=source_path.detail,
        )
    if not source_path.exists():
        return _drift_skip(
            f"source path {source_path} no longer exists; {_DRIFT_SKIP_SUFFIX}"
        )
    pyproject_path = source_path / "pyproject.toml"
    extras = _read_optional_extras(pyproject_path)
    if isinstance(extras, str):
        return _drift_skip(
            f"could not read optional extras from {pyproject_path}"
            f" ({extras}); {_DRIFT_SKIP_SUFFIX}"
        )
    if not extras:
        return _drift_skip(f"no optional extras declared in {pyproject_path}")
    lock_path = source_path / "uv.lock"
    lock = _read_lock_versions(lock_path)
    if isinstance(lock, str):
        return _drift_skip(f"could not read {lock_path} ({lock}); {_DRIFT_SKIP_SUFFIX}")
    return _drift_result(source_path, _scan_extras(extras, lock))


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


def _check_ssh_key_loaded() -> CheckResult:
    """Check whether an ED25519/RSA key is loaded in the ssh-agent (#1400).

    Diagnostic surfacing of the same condition #927's dev-queue dispatch
    SSH-key preflight gate (``check_ssh_key_available``, ``cw.ssh``) blocks
    on before spawning workers. Soft/non-blocking here: ``cw doctor`` only
    reports, it never gates.
    """
    if check_ssh_key_available():
        return CheckResult(
            "ssh-key-loaded",
            ok=True,
            warn=False,
            detail="ED25519/RSA key loaded in ssh-agent",
        )
    return CheckResult(
        "ssh-key-loaded",
        ok=True,
        warn=True,
        detail=(
            "no ED25519/RSA key loaded in ssh-agent — run `ssh-add` to unlock"
            " (same condition the dev-queue dispatch SSH-key preflight gates on)"
        ),
    )
