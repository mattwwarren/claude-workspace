"""Codex filesystem-capability probe and its fingerprint-keyed cache (#1709).

Answers one question the rest of this package used to hardcode a wrong answer
to: **can a ``codex exec --sandbox read-only`` invocation on this runtime
actually read the repository?** The answer varies by install method, not by OS —
a snap-confined codex cannot reach the host's ``bwrap`` (its own PATH/mount
namespace hides it) and fails closed, while the same machine's non-snap install
reads files fine. Only running codex reveals which one you have: a host-side
``shutil.which("bwrap")`` check answers CAPABLE for a snap install and is wrong.

Deliberately a **sibling of, not an extension to**,
``cw.executor.codex_capability_diagnosis`` and the in-process TTL cache
``cw.dispatch.claim`` wraps around it. That subsystem answers "is a codex binary
present and does ``--version`` parse?" — a static install-level fact where a
60-second in-process TTL is the right freshness/cost tradeoff for a hot dispatch
tick. This module answers an environment-dependent question that needs a live
``codex exec`` round-trip, so its verdict is persisted **to disk** (dispatch is a
long-running loop spread across many separate ``cw`` processes) and keyed by a
runtime fingerprint with **no TTL** — only an actual environment change
invalidates it, never elapsed time.

``codex --version`` is fetched here rather than imported from
``cw.executor``: ``executor.py`` imports this package at module scope, so the
reverse import would be a cycle.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.atomic import atomic_write_text
from cw.codex_review._const import _CODEX_VERSION_RE, _is_spawn_error
from cw.config import diagnostics_dir, state_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.codex_runner import CodexRunner, CodexRunResult

_log = logging.getLogger(__name__)

# The only sandbox mode this package ever hands a reviewer role
# (``_roles._build_generic_codex_argv``). Recorded on the fingerprint so a
# future second mode cannot silently reuse this one's cached verdict.
_PROBE_SANDBOX_MODE = "read-only"

# The probe invocation: a bare ``codex exec`` with no ``--output-schema``/``-o``
# and none of ``_AUDIT_ARGV_FLAGS``. Kept minimal so the probe exercises the
# sandbox itself rather than any flag an older codex-cli might reject.
#
# ``--skip-git-repo-check`` is load-bearing (#1732), not defensive. The probe
# deliberately runs in ``_probe_scratch_dir()`` rather than the review worktree,
# so by construction it is never inside a git repo — and ``codex exec`` refuses
# to start outside one ("Not inside a trusted directory and
# --skip-git-repo-check was not specified", exit 1) before any sandbox work
# happens. Without this flag the probe answers "incapable" on every host.
_PROBE_ARGV = (
    "codex",
    "exec",
    "--sandbox",
    _PROBE_SANDBOX_MODE,
    "--skip-git-repo-check",
)

_VERSION_PROBE_TIMEOUT_SECONDS = 10

# The live probe is a real model round-trip on a one-line prompt. Generous
# enough that a cold model start is not mistaken for an incapable sandbox, but
# bounded — a probe that never returns degrades rather than hanging the review.
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 120

_PROBE_SENTINEL_FILENAME = "sentinel.txt"

# Written into the sentinel file and NEVER into the prompt: codex can only
# reproduce it by actually reading the file, which is the whole measurement.
_PROBE_SENTINEL = "CW_CODEX_FS_PROBE_5F3A9C2E7B14"

_PROBE_PROMPT = (
    f"Read the file `{_PROBE_SENTINEL_FILENAME}` in your current working "
    "directory and reply with its exact contents and nothing else. If you "
    "cannot read it for any reason, reply with exactly NO_FILESYSTEM_ACCESS."
)

_CAPABILITY_CACHE_FILENAME = "capability-cache.json"
_CAPABILITY_DIAGNOSTICS_FILENAME = "codex-capability.json"

_INSTALL_SNAP = "snap"
_INSTALL_OTHER = "other"
_INSTALL_UNKNOWN = "unknown"

# Why the probe reported incapable. ``None`` iff capable.
#
# The install-incomplete marker is codex 0.147.0 routing tools through a
# code-mode host binary that isn't there — a broken install that yields the
# same NO_FILESYSTEM_ACCESS answer as a sandbox limitation while having nothing
# to do with sandboxing (#1709 comment 2). Distinguishing the two is the point:
# one is fixed by reinstalling, the other by not using a snap.
_REASON_INSTALL_INCOMPLETE = "install_incomplete"
_REASON_SANDBOX_INCAPABLE = "sandbox_incapable"
_REASON_UNKNOWN = "unknown"
# The probe itself never completed (timeout / spawn failure). Distinct from the
# three above because it says nothing about the runtime — it is never cached.
_REASON_PROBE_ERROR = "probe_error"

_INSTALL_INCOMPLETE_MARKERS = ("Code mode will fail closed",)

# The durable half of the bubblewrap panic. Deliberately NOT matching
# ``linux-sandbox/src/launcher.rs:43:13`` from the same capture — that file and
# line drift across codex point releases, and a classifier keyed on them would
# silently degrade to "unknown" on the next release.
_SANDBOX_INCAPABLE_MARKER = "bubblewrap is unavailable"


class _CodexFingerprint(NamedTuple):
    """The runtime facts a capability verdict is valid for.

    ``install_type`` is the load-bearing dimension: 0.146.0-snap and
    0.146.1-Homebrew differ in capability while differing trivially in version
    (#1709 comment 2), so version alone is not a sufficient key.
    """

    cli_version: str | None
    platform: str
    install_type: str
    sandbox_mode: str


class _CodexFilesystemCapability(NamedTuple):
    """One probe verdict: can codex read the repo, and if not, why not."""

    capable: bool
    reason: str | None
    fingerprint: _CodexFingerprint


def _which_codex() -> str | None:
    """Resolve the codex binary's path.

    A module-level seam (rather than a bare ``shutil.which`` call) so tests can
    patch ``cw.codex_review._capability._which_codex``. See
    :func:`_run_codex_version` for why patching the module-qualified
    ``shutil.which`` instead would be wrong.
    """
    return shutil.which("codex")


def _run_codex_version(timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    """Run ``codex --version``.

    Exists as a module-level function so tests patch
    ``cw.codex_review._capability._run_codex_version`` rather than
    ``cw.codex_review._capability.subprocess.run``. The latter path is not
    module-local: ``subprocess`` here IS the global module object, so setting
    its ``run`` attribute replaces ``subprocess.run`` **process-wide**. That is
    tolerable inside one ``with patch(...)`` block but not for the autouse
    fixture this probe needs, which would otherwise break every git helper in
    the test suite for the whole session.
    """
    return subprocess.run(
        ["codex", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _detect_install_type(resolved: str | None) -> str:
    """Classify the resolved codex path into the fingerprint's install type."""
    if resolved is None:
        return _INSTALL_UNKNOWN
    if "/snap/" in resolved:
        return _INSTALL_SNAP
    return _INSTALL_OTHER


def _codex_cli_version(resolved: str | None) -> str | None:
    """Return the parsed ``codex --version`` number, or ``None``.

    ``None`` covers every way the question can fail to get an answer — binary
    absent, spawn failure, timeout, non-zero exit, unparseable banner. All of
    them are equally valid fingerprint components: an unanswerable version is a
    stable fact about this runtime, and the cache key only needs to *change*
    when the runtime does.
    """
    if resolved is None:
        return None
    try:
        proc = _run_codex_version(_VERSION_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    output = proc.stdout or proc.stderr or ""
    version_line = output.splitlines()[0] if output else ""
    match = _CODEX_VERSION_RE.search(version_line)
    return match.group(0) if match else None


def _compute_fingerprint() -> _CodexFingerprint:
    """Snapshot the runtime facts this probe's verdict is keyed by."""
    resolved = _which_codex()
    return _CodexFingerprint(
        cli_version=_codex_cli_version(resolved),
        platform=platform.system(),
        install_type=_detect_install_type(resolved),
        sandbox_mode=_PROBE_SANDBOX_MODE,
    )


def _classify_capability_failure(result: CodexRunResult) -> str:
    """Name *why* a completed probe failed to read the sentinel.

    Three live classes. ``unknown`` is the catch-all for a failure signature
    nobody has captured yet — deliberately a real bucket, not a placeholder: a
    probe that failed for an unrecognized reason is still a determinate "this
    runtime cannot read the repo" answer.

    The bubblewrap check is deliberately first. The two markers are expected to
    be disjoint in practice (they were captured on different installs), but if
    both appear the panic wins: it is a hard, verbatim-observed sandbox failure
    with a specific remedy (do not use a snap-confined codex), while the
    code-mode marker is a broader warning about a missing host binary.
    """
    if _SANDBOX_INCAPABLE_MARKER in result.stderr:
        return _REASON_SANDBOX_INCAPABLE
    if any(marker in result.stderr for marker in _INSTALL_INCOMPLETE_MARKERS):
        return _REASON_INSTALL_INCOMPLETE
    return _REASON_UNKNOWN


def _is_probe_error(result: CodexRunResult) -> bool:
    """True when the probe never produced an answer (vs. answering "no").

    The third arm (#1732) is the one that matters for cache safety. Codex can
    refuse to start *before* any sandbox work happens — an unreadable config,
    an auth failure, a rejected flag — exiting non-zero with nothing on stdout.
    That is not the runtime answering "I cannot read files"; it is the runtime
    never being asked. Treating it as a determinate verdict wrote a permanent
    ``incapable`` into a cache that has no TTL, which is exactly the
    "transient failure becomes silently permanent" outcome R7 forbids.

    Keyed on empty stdout rather than on any particular message, so a new
    refusal reason inherits the safe behavior instead of being misfiled as an
    answer. A genuinely incapable sandbox still *replies* (with the prompt's
    ``NO_FILESYSTEM_ACCESS`` fallback or a panic on stderr), so it produces
    stdout and is correctly classified below rather than caught here.
    """
    produced_no_answer = result.returncode != 0 and not result.stdout.strip()
    return result.timed_out or _is_spawn_error(result) or produced_no_answer


def _capability_cache_path() -> Path:
    """Return the on-disk cache path (``state_dir()``, alongside scratch dirs).

    Reads ``state_dir()`` at call time so the autouse ``tmp_config_dir`` fixture
    reaches it, same as every other path accessor in this codebase.
    """
    return state_dir() / "codex-review" / _CAPABILITY_CACHE_FILENAME


def _probe_scratch_dir() -> Path:
    """Return (creating if needed) the probe's own working directory.

    Never the worktree under review — the probe writes a sentinel file, and
    dirtying the worktree would corrupt the very diff being reviewed. Never
    ``/tmp`` either: a snap-confined codex gets a private tmp namespace and
    cannot read it (same constraint as ``_roles._codex_scratch_dir``).
    """
    scratch = state_dir() / "codex-review" / "capability-probe"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _fingerprint_payload(fingerprint: _CodexFingerprint) -> dict[str, str | None]:
    """Render *fingerprint* as the JSON object the cache is keyed by."""
    return {
        "cli_version": fingerprint.cli_version,
        "platform": fingerprint.platform,
        "install_type": fingerprint.install_type,
        "sandbox_mode": fingerprint.sandbox_mode,
    }


def _read_cached_capability(
    fingerprint: _CodexFingerprint,
) -> _CodexFilesystemCapability | None:
    """Return the cached verdict for *fingerprint*, or ``None`` to re-probe.

    No TTL by design (R7): an unreadable, corrupt, or differently-fingerprinted
    cache misses, but an old one does not. Re-probing on a timer would spend a
    real ``codex exec`` round-trip per dispatch tick to re-learn a fact that
    only changes when the codex install does.
    """
    try:
        raw = _capability_cache_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("fingerprint") != _fingerprint_payload(fingerprint):
        return None
    capable = data.get("capable")
    reason = data.get("reason")
    if not isinstance(capable, bool) or not (reason is None or isinstance(reason, str)):
        return None
    return _CodexFilesystemCapability(
        capable=capable, reason=reason, fingerprint=fingerprint
    )


def _write_cached_capability(capability: _CodexFilesystemCapability) -> None:
    """Persist *capability* for future processes; never raises."""
    path = _capability_cache_path()
    payload = {
        "fingerprint": _fingerprint_payload(capability.fingerprint),
        "capable": capability.capable,
        "reason": capability.reason,
        # Recorded for operators reading the file, never read back as a TTL.
        "probed_at": datetime.now(UTC).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, indent=2))
    except OSError:
        _log.warning("codex capability cache write failed: %s", path)


def _reset_filesystem_capability_cache() -> None:
    """Delete the cached verdict, forcing a fresh probe on the next call.

    The documented manual-reset path (an operator who changed their codex
    install and does not want to wait for the fingerprint to notice) and the
    test seam. Named distinctly from ``cw.dispatch.claim``'s
    ``_reset_codex_capability_cache`` — that one clears an in-process TTL cache
    of a different question.
    """
    _capability_cache_path().unlink(missing_ok=True)


def _persist_capability_diagnostics(
    session_id: str, capability: _CodexFilesystemCapability
) -> None:
    """Record *capability* under *session_id*'s diagnostics dir; never raises.

    Written on cache hits too: "which mode did THIS session run in" must be
    answerable per session, and the cache is shared across sessions. Mirrors
    ``persist_diagnostics_bundle``'s never-raise contract — a failed
    diagnostics write must not take the review down with it.
    """
    _log.info(
        "codex_filesystem_capability: session=%s capable=%s reason=%s fingerprint=%s",
        session_id,
        capability.capable,
        capability.reason,
        _fingerprint_payload(capability.fingerprint),
    )
    try:
        target = diagnostics_dir(session_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / _CAPABILITY_DIAGNOSTICS_FILENAME).write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "capable": capability.capable,
                    "reason": capability.reason,
                    "fingerprint": _fingerprint_payload(capability.fingerprint),
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        _log.warning(
            "codex capability diagnostics write failed for session %s", session_id
        )


def _run_capability_probe(
    *, runner: CodexRunner, fingerprint: _CodexFingerprint
) -> _CodexFilesystemCapability:
    """Drive one live ``codex exec --sandbox read-only`` sentinel read.

    Success is keyed on the sentinel appearing in **stdout** — never on stderr
    being clean. A capable run still emits shell-snapshot and xset noise on
    stderr (R6 capture), so treating stderr as a failure signal would report
    every healthy runtime as degraded.
    """
    scratch = _probe_scratch_dir()
    (scratch / _PROBE_SENTINEL_FILENAME).write_text(
        f"{_PROBE_SENTINEL}\n", encoding="utf-8"
    )
    result = runner.run(
        scratch,
        list(_PROBE_ARGV),
        _CAPABILITY_PROBE_TIMEOUT_SECONDS,
        stdin=_PROBE_PROMPT,
    )
    if _is_probe_error(result):
        _log.warning(
            "codex filesystem-capability probe did not complete "
            "(timed_out=%s returncode=%s); degrading for this run only",
            result.timed_out,
            result.returncode,
        )
        return _CodexFilesystemCapability(
            capable=False, reason=_REASON_PROBE_ERROR, fingerprint=fingerprint
        )
    if _PROBE_SENTINEL in result.stdout:
        return _CodexFilesystemCapability(
            capable=True, reason=None, fingerprint=fingerprint
        )
    return _CodexFilesystemCapability(
        capable=False,
        reason=_classify_capability_failure(result),
        fingerprint=fingerprint,
    )


def _probe_filesystem_capability(
    *, runner: CodexRunner, session_id: str
) -> _CodexFilesystemCapability:
    """Return this runtime's filesystem capability, probing only on a miss.

    A probe-execution error is logged, degrades this one run, and is
    **not** cached: R7's requirement is that a transient failure never becomes
    silently permanent. Only a determinate outcome — capable, or incapable with
    a classified reason — is written to disk.
    """
    fingerprint = _compute_fingerprint()
    cached = _read_cached_capability(fingerprint)
    if cached is not None:
        _persist_capability_diagnostics(session_id, cached)
        return cached
    capability = _run_capability_probe(runner=runner, fingerprint=fingerprint)
    if capability.reason != _REASON_PROBE_ERROR:
        _write_cached_capability(capability)
    _persist_capability_diagnostics(session_id, capability)
    return capability
