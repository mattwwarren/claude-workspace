"""Tests for cw.codex_review._capability — the codex filesystem-capability
probe, its runtime fingerprint, and its on-disk no-TTL cache (#1709).

This module patches ``_capability``'s own two module-level seam functions
(``_which_codex`` / ``_run_codex_version``) rather than
``cw.codex_review._capability.subprocess.run``: the latter path resolves to the
*global* ``subprocess`` module object, so patching it would leak process-wide.
The autouse ``_mock_codex_capability_probe`` fixture in ``conftest.py`` patches
the same two names to a stable default; each test here re-patches them and
pytest's patch stacking lets the test-level patch win.
"""

from __future__ import annotations

import json
import platform
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.codex_review._capability import (
    _CAPABILITY_PROBE_TIMEOUT_SECONDS,
    _PROBE_ARGV,
    _PROBE_SENTINEL,
    _REASON_INSTALL_INCOMPLETE,
    _REASON_PROBE_ERROR,
    _REASON_SANDBOX_INCAPABLE,
    _REASON_UNKNOWN,
    _capability_cache_path,
    _classify_capability_failure,
    _CodexFilesystemCapability,
    _compute_fingerprint,
    _detect_install_type,
    _persist_capability_diagnostics,
    _probe_filesystem_capability,
    _reset_filesystem_capability_cache,
    _run_codex_version,
    _which_codex,
)
from cw.codex_runner import CodexRunResult
from cw.config import diagnostics_dir
from tests._codex_review_helpers import _mk_codex_proc

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Verbatim captures from issue #1709's own probe comments — do NOT tidy.
# ---------------------------------------------------------------------------

# Snap-confined Linux install, codex-cli 0.146.0 (#1709 comment 1).
_BWRAP_PANIC_STDERR = (
    "thread 'main' panicked at linux-sandbox/src/launcher.rs:43:13:\n"
    "bubblewrap is unavailable: no system bwrap was found on PATH and no bundled\n"
    "codex-resources/bwrap binary was found next to the Codex executable\n"
)

# Benign noise a capable run still emits on stderr (R6 capture).
_BENIGN_NOISE_STDERR = (
    "ERROR codex_core::shell_snapshot: Shell snapshot validation failed\n"
    "xset: unable to open display\n"
)

# codex-cli 0.147.0 routing tools through a missing code-mode host binary
# (#1709 comment 2) — a broken install, not a sandbox limitation.
_CODE_MODE_STDERR = "Code mode will fail closed\n"


class _ProbeRunner:
    """CodexRunner double for the capability probe; records every call."""

    def __init__(self, result: CodexRunResult | None = None) -> None:
        self._result = result or CodexRunResult(returncode=0, stdout="", stderr="")
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "timeout": timeout_seconds,
                "stdin": stdin,
            }
        )
        return self._result


def _sentinel_result(*, stderr: str = "") -> CodexRunResult:
    """A probe result whose stdout carries the sentinel (i.e. codex read it)."""
    return CodexRunResult(
        returncode=0, stdout=f"codex\n{_PROBE_SENTINEL}\n", stderr=stderr
    )


# ---------------------------------------------------------------------------
# _compute_fingerprint
# ---------------------------------------------------------------------------


class TestComputeFingerprint:
    def test_binary_absent_yields_unknown_install_and_no_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.codex_review._capability._which_codex", lambda: None)
        fingerprint = _compute_fingerprint()
        assert fingerprint.cli_version is None
        assert fingerprint.install_type == "unknown"

    def test_version_timeout_yields_no_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_timeout_seconds: int) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=10)

        monkeypatch.setattr("cw.codex_review._capability._run_codex_version", _boom)
        assert _compute_fingerprint().cli_version is None

    def test_version_filenotfound_yields_no_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone = "gone"

        def _boom(_timeout_seconds: int) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(gone)

        monkeypatch.setattr("cw.codex_review._capability._run_codex_version", _boom)
        assert _compute_fingerprint().cli_version is None

    def test_version_nonzero_exit_yields_no_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._capability._run_codex_version",
            lambda _t: _mk_codex_proc(stdout="codex-cli 0.147.0", returncode=2),
        )
        assert _compute_fingerprint().cli_version is None

    def test_version_unparseable_yields_no_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._capability._run_codex_version",
            lambda _t: _mk_codex_proc(stdout="codex-cli (dev build)"),
        )
        assert _compute_fingerprint().cli_version is None

    def test_version_parsed_from_banner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cw.codex_review._capability._run_codex_version",
            lambda _t: _mk_codex_proc(stdout="codex-cli 0.147.0\n"),
        )
        assert _compute_fingerprint().cli_version == "0.147.0"

    def test_platform_and_sandbox_mode_recorded(self) -> None:
        fingerprint = _compute_fingerprint()
        assert fingerprint.platform == platform.system()
        # The only sandbox mode this package ever uses for a reviewer role.
        assert fingerprint.sandbox_mode == "read-only"

    @pytest.mark.parametrize(
        ("resolved", "expected"),
        [
            ("/snap/bin/codex", "snap"),
            ("/usr/local/bin/codex", "other"),
            (None, "unknown"),
        ],
    )
    def test_install_type_detection(self, resolved: str | None, expected: str) -> None:
        assert _detect_install_type(resolved) == expected


# ---------------------------------------------------------------------------
# _classify_capability_failure — the 3-way reason taxonomy
# ---------------------------------------------------------------------------


class TestProbeSeams:
    """The two seams the autouse fixture replaces everywhere else.

    Imported by name here, so these bindings are the real functions rather than
    the fixture's stand-ins. Patched inside a ``with`` block — a scoped patch of
    the global ``subprocess``/``shutil`` modules is the established idiom in
    ``test_codex_executor.py``; only an *autouse* one would leak process-wide.
    """

    def test_which_codex_delegates_to_shutil(self) -> None:
        with patch(
            "cw.codex_review._capability.shutil.which", return_value="/usr/bin/codex"
        ) as which:
            assert _which_codex() == "/usr/bin/codex"
        which.assert_called_once_with("codex")

    def test_run_codex_version_invokes_the_version_flag(self) -> None:
        proc = _mk_codex_proc(stdout="codex-cli 0.147.0\n")
        with patch(
            "cw.codex_review._capability.subprocess.run", return_value=proc
        ) as run:
            assert _run_codex_version(7) is proc
        assert run.call_args.args[0] == ["codex", "--version"]
        assert run.call_args.kwargs["timeout"] == 7
        assert run.call_args.kwargs["check"] is False


class TestClassifyCapabilityFailure:
    def test_bubblewrap_panic_is_sandbox_incapable(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="NO_FILESYSTEM_ACCESS", stderr=_BWRAP_PANIC_STDERR
        )
        assert _classify_capability_failure(result) == _REASON_SANDBOX_INCAPABLE

    def test_sandbox_incapable_keys_only_on_the_durable_substring(self) -> None:
        """The classifier must not match the panic's file:line, which drifts
        across codex point releases — only the durable message."""
        drifted = _BWRAP_PANIC_STDERR.replace(
            "linux-sandbox/src/launcher.rs:43:13", "linux-sandbox/src/launcher.rs:91:7"
        )
        result = CodexRunResult(returncode=0, stdout="", stderr=drifted)
        assert _classify_capability_failure(result) == _REASON_SANDBOX_INCAPABLE

    def test_code_mode_marker_is_install_incomplete(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="NO_FILESYSTEM_ACCESS", stderr=_CODE_MODE_STDERR
        )
        assert _classify_capability_failure(result) == _REASON_INSTALL_INCOMPLETE

    def test_unrecognized_stderr_is_unknown(self) -> None:
        result = CodexRunResult(returncode=0, stdout="", stderr="something else\n")
        assert _classify_capability_failure(result) == _REASON_UNKNOWN


# ---------------------------------------------------------------------------
# _probe_filesystem_capability — live probe classification
# ---------------------------------------------------------------------------


class TestProbeFilesystemCapability:
    def test_sentinel_on_stdout_is_capable(self) -> None:
        runner = _ProbeRunner(_sentinel_result())
        capability = _probe_filesystem_capability(runner=runner, session_id="s-cap")
        assert capability.capable is True
        assert capability.reason is None

    def test_probe_argv_is_bare_read_only_exec(self) -> None:
        runner = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=runner, session_id="s-argv")
        assert runner.calls[0]["argv"] == ["codex", "exec", "--sandbox", "read-only"]
        assert list(_PROBE_ARGV) == runner.calls[0]["argv"]
        assert runner.calls[0]["timeout"] == _CAPABILITY_PROBE_TIMEOUT_SECONDS
        # The sentinel VALUE must never appear in the prompt — otherwise codex
        # could echo it back without ever reading the file.
        stdin = runner.calls[0]["stdin"]
        assert isinstance(stdin, str)
        assert _PROBE_SENTINEL not in stdin

    def test_probe_runs_in_its_own_scratch_dir_under_state_dir(self) -> None:
        """The probe writes a sentinel file, so it must run in its own scratch
        dir under ``state_dir()`` — never in the worktree under review (that
        would dirty the very diff being reviewed) and never in /tmp (a
        snap-confined codex cannot read its private tmp namespace)."""
        runner = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=runner, session_id="s-cwd")
        cwd = runner.calls[0]["cwd"]
        assert str(cwd).startswith(str(_capability_cache_path().parent))

    def test_sentinel_only_in_output_file_is_not_capable(self) -> None:
        """Success is keyed on stdout, not the ``-o`` document (R6): the probe
        argv carries no ``-o`` at all, so a sentinel appearing there is noise."""
        runner = _ProbeRunner(
            CodexRunResult(
                returncode=0,
                stdout="NO_FILESYSTEM_ACCESS",
                stderr="",
                output_file_content=_PROBE_SENTINEL,
            )
        )
        capability = _probe_filesystem_capability(runner=runner, session_id="s-outf")
        assert capability.capable is False

    def test_benign_stderr_noise_still_classifies_capable(self) -> None:
        """The most important non-regression in this file (R6): a capable run
        emits shell-snapshot/xset noise on stderr. Classification must key on
        the stdout sentinel, never on stderr being clean."""
        runner = _ProbeRunner(_sentinel_result(stderr=_BENIGN_NOISE_STDERR))
        capability = _probe_filesystem_capability(runner=runner, session_id="s-noise")
        assert capability.capable is True
        assert capability.reason is None

    def test_bubblewrap_panic_probes_sandbox_incapable(self) -> None:
        runner = _ProbeRunner(
            CodexRunResult(
                returncode=0,
                stdout="codex\nNO_FILESYSTEM_ACCESS\n",
                stderr=_BWRAP_PANIC_STDERR,
            )
        )
        capability = _probe_filesystem_capability(runner=runner, session_id="s-bwrap")
        assert capability.capable is False
        assert capability.reason == _REASON_SANDBOX_INCAPABLE

    def test_timeout_degrades_and_is_not_cached(self) -> None:
        """R7: a probe that never completed must not become silently permanent."""
        runner = _ProbeRunner(
            CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        )
        capability = _probe_filesystem_capability(runner=runner, session_id="s-to")
        assert capability.capable is False
        assert capability.reason == _REASON_PROBE_ERROR
        assert not _capability_cache_path().exists()

        # ...and the next call re-probes rather than reusing the degrade.
        second = _ProbeRunner(_sentinel_result())
        again = _probe_filesystem_capability(runner=second, session_id="s-to")
        assert len(second.calls) == 1
        assert again.capable is True

    def test_spawn_error_degrades_and_is_not_cached(self) -> None:
        runner = _ProbeRunner(
            CodexRunResult(returncode=127, stdout="", stderr="codex: command not found")
        )
        capability = _probe_filesystem_capability(runner=runner, session_id="s-spawn")
        assert capability.reason == _REASON_PROBE_ERROR
        assert not _capability_cache_path().exists()


# ---------------------------------------------------------------------------
# On-disk, fingerprint-keyed, no-TTL cache
# ---------------------------------------------------------------------------


class TestCapabilityCache:
    def test_second_call_hits_cache_without_reprobing(self) -> None:
        first = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=first, session_id="s-cache")
        assert len(first.calls) == 1

        second = _ProbeRunner(_sentinel_result())
        capability = _probe_filesystem_capability(runner=second, session_id="s-cache")
        assert second.calls == []
        assert capability.capable is True

    def test_degraded_verdict_is_cached_too(self) -> None:
        first = _ProbeRunner(
            CodexRunResult(returncode=0, stdout="", stderr=_BWRAP_PANIC_STDERR)
        )
        _probe_filesystem_capability(runner=first, session_id="s-cache-deg")
        second = _ProbeRunner(_sentinel_result())
        capability = _probe_filesystem_capability(
            runner=second, session_id="s-cache-deg"
        )
        assert second.calls == []
        assert capability.capable is False
        assert capability.reason == _REASON_SANDBOX_INCAPABLE

    @pytest.mark.parametrize(
        ("which_value", "version_stdout"),
        [
            ("/snap/bin/codex", "codex-cli 0.144.5\n"),  # install_type changed
            ("/usr/bin/codex", "codex-cli 0.147.0\n"),  # cli_version changed
        ],
    )
    def test_fingerprint_change_invalidates_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
        which_value: str,
        version_stdout: str,
    ) -> None:
        first = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=first, session_id="s-fp")
        assert len(first.calls) == 1

        monkeypatch.setattr(
            "cw.codex_review._capability._which_codex", lambda: which_value
        )
        monkeypatch.setattr(
            "cw.codex_review._capability._run_codex_version",
            lambda _t: _mk_codex_proc(stdout=version_stdout),
        )
        second = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=second, session_id="s-fp")
        assert len(second.calls) == 1

    def test_cache_has_no_ttl(self) -> None:
        """An ancient ``probed_at`` with a matching fingerprint still hits."""
        runner = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=runner, session_id="s-ttl")
        path = _capability_cache_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["probed_at"] = "2001-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")

        second = _ProbeRunner(_sentinel_result())
        capability = _probe_filesystem_capability(runner=second, session_id="s-ttl")
        assert second.calls == []
        assert capability.capable is True

    @pytest.mark.parametrize(
        "body",
        [
            "{not json",  # unparseable
            "[]",  # parseable, but not the object shape
            '{"fingerprint": {}, "capable": "yes", "reason": null}',  # wrong types
        ],
        ids=["unparseable", "not-an-object", "wrong-types"],
    )
    def test_unusable_cache_reprobes(self, body: str) -> None:
        """A cache we cannot trust is a miss, never a silent capability answer."""
        path = _capability_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        runner = _ProbeRunner(_sentinel_result())
        capability = _probe_filesystem_capability(runner=runner, session_id="s-corrupt")
        assert len(runner.calls) == 1
        assert capability.capable is True

    def test_cache_with_matching_fingerprint_but_bad_reason_type_reprobes(self) -> None:
        runner = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=runner, session_id="s-badreason")
        path = _capability_cache_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reason"] = 42
        path.write_text(json.dumps(payload), encoding="utf-8")

        second = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=second, session_id="s-badreason")
        assert len(second.calls) == 1

    def test_cache_write_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unwritable cache costs a re-probe next run, never the review."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "cw.codex_review._capability._capability_cache_path",
            lambda: blocker / "capability-cache.json",
        )
        runner = _ProbeRunner(_sentinel_result())
        capability = _probe_filesystem_capability(runner=runner, session_id="s-nowrite")
        assert capability.capable is True

    def test_reset_clears_cache_and_forces_reprobe(self) -> None:
        first = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=first, session_id="s-reset")
        assert _capability_cache_path().exists()

        _reset_filesystem_capability_cache()
        assert not _capability_cache_path().exists()

        second = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=second, session_id="s-reset")
        assert len(second.calls) == 1

    def test_reset_on_absent_cache_is_a_noop(self) -> None:
        _reset_filesystem_capability_cache()
        _reset_filesystem_capability_cache()
        assert not _capability_cache_path().exists()


# ---------------------------------------------------------------------------
# Per-session diagnostics artifact
# ---------------------------------------------------------------------------


class TestCapabilityDiagnostics:
    def test_probe_writes_per_session_artifact(self) -> None:
        runner = _ProbeRunner(_sentinel_result())
        _probe_filesystem_capability(runner=runner, session_id="s-diag")
        artifact = diagnostics_dir("s-diag") / "codex-capability.json"
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["capable"] is True
        assert payload["reason"] is None
        assert payload["fingerprint"]["sandbox_mode"] == "read-only"

    def test_cache_hit_still_records_the_session_artifact(self) -> None:
        _probe_filesystem_capability(
            runner=_ProbeRunner(_sentinel_result()), session_id="s-diag-a"
        )
        _probe_filesystem_capability(
            runner=_ProbeRunner(_sentinel_result()), session_id="s-diag-b"
        )
        # The second session hit the cache but still has its own artifact:
        # "which mode did THIS session run in" must be answerable per session.
        assert (diagnostics_dir("s-diag-b") / "codex-capability.json").exists()

    def test_diagnostics_write_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors ``persist_diagnostics_bundle``'s never-raise contract: a
        review must not die because a diagnostics write failed."""

        message = "read-only filesystem"

        def _boom(_session_id: str) -> Path:
            raise OSError(message)

        monkeypatch.setattr("cw.codex_review._capability.diagnostics_dir", _boom)
        _persist_capability_diagnostics(
            "s-diag-fail",
            _CodexFilesystemCapability(
                capable=False,
                reason=_REASON_UNKNOWN,
                fingerprint=_compute_fingerprint(),
            ),
        )
