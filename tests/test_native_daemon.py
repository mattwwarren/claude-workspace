"""Tests for cw.native_daemon."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.native_daemon import (
    SKIP_PERMISSIONS_MODE,
    FakeNativeDaemonClient,
    RealNativeDaemonClient,
    _is_native_surface_ref,
    get_native_daemon_client,
    model_supports_auto,
    read_supervisor_resume_session_id,
    resolve_permission_mode,
    wait_for_roster_presence,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Fixed-offset aware clock the ``_local_now`` seam is pinned to (#1409).
# Deliberately NOT ``freeze_time``: under freezegun
# ``datetime.now(UTC).astimezone()`` still resolves the HOST zone, so a
# freeze-only assertion would name a different UTC instant on a dev box than
# on a UTC CI runner (the #671/#1727 green-locally/red-in-CI class).
# 2026-09-20 is a Sunday, so ``resets Mon …`` lands on 2026-09-21.
_PINNED_LOCAL_NOW = datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=-4)))


class TestIsNativeSurfaceRef:
    def test_valid_8_char_hex(self) -> None:
        assert _is_native_surface_ref("abcd1234") is True
        assert _is_native_surface_ref("00000001") is True
        assert _is_native_surface_ref("deadbeef") is True

    def test_invalid_too_short(self) -> None:
        assert _is_native_surface_ref("abc1234") is False

    def test_invalid_too_long(self) -> None:
        assert _is_native_surface_ref("abcd12345") is False

    def test_invalid_non_hex_chars(self) -> None:
        assert _is_native_surface_ref("abcg1234") is False
        assert _is_native_surface_ref("impl-pane") is False

    def test_invalid_uppercase(self) -> None:
        assert _is_native_surface_ref("ABCD1234") is False


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess used by patched subprocess.run."""

    def __init__(
        self, *, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestRealNativeDaemonClientSpawnGitEnv:
    """spawn_bg must not leak GIT_* env vars into the worker subprocess."""

    def test_git_vars_stripped_from_subprocess_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GIT_DIR and GIT_INDEX_FILE must not appear in env passed to subprocess.run.

        Without the fix, spawn_bg passes no env= argument and the worker inherits
        the orchestrator's GIT_DIR / GIT_INDEX_FILE, misdirecting all git ops to
        the orchestrator's checkout (GitHub issue #766).
        """
        captured: dict[str, object] = {}

        def fake_run(args: object, **kwargs: object) -> _FakeCompleted:
            captured.update(kwargs)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setenv("GIT_DIR", "/some/other/repo/.git")
        monkeypatch.setenv("GIT_INDEX_FILE", "/some/other/repo/.git/index")
        monkeypatch.setattr(subprocess, "run", fake_run)

        client = RealNativeDaemonClient()
        client.spawn_bg(cwd=tmp_path, prompt="x")

        env = captured.get("env")
        assert isinstance(env, dict), "spawn_bg must pass env= to subprocess.run"
        git_keys = [
            k for k in env if k.startswith("GIT_") and k != "GIT_TERMINAL_PROMPT"
        ]
        assert not git_keys, (
            f"unexpected GIT_* vars must be stripped; found: {git_keys}"
        )
        assert "PATH" in env, "non-GIT env vars must be preserved (PATH missing)"

    def test_gh_and_git_prompt_env_vars_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn_bg must unconditionally set the four prompt-suppression vars.

        Without these, a dispatched worker's `gh`/`git` Bash calls can block on an
        interactive prompt (auth refresh, pager, update notifier) with no human to
        answer it — the worker hangs indefinitely inside the tool call (#979).
        """
        captured: dict[str, object] = {}

        def fake_run(args: object, **kwargs: object) -> _FakeCompleted:
            captured.update(kwargs)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setattr(subprocess, "run", fake_run)

        client = RealNativeDaemonClient()
        client.spawn_bg(cwd=tmp_path, prompt="x")

        env = captured.get("env")
        assert isinstance(env, dict)
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert env["GH_PAGER"] == "cat"
        assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_gh_prompt_env_vars_override_inherited_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The four vars must be unconditional overrides, not setdefault.

        A headless daemon worker must never inherit a value that could
        re-enable an interactive prompt.
        """
        captured: dict[str, object] = {}

        def fake_run(args: object, **kwargs: object) -> _FakeCompleted:
            captured.update(kwargs)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setenv("GH_PROMPT_DISABLED", "0")
        monkeypatch.setenv("GH_PAGER", "less")
        monkeypatch.setenv("GH_NO_UPDATE_NOTIFIER", "0")
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
        monkeypatch.setattr(subprocess, "run", fake_run)

        client = RealNativeDaemonClient()
        client.spawn_bg(cwd=tmp_path, prompt="x")

        env = captured.get("env")
        assert isinstance(env, dict)
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert env["GH_PAGER"] == "cat"
        assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_ci_env_var_not_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CI must NOT be set by spawn_bg — it can reshape other tools' behavior."""
        captured: dict[str, object] = {}

        def fake_run(args: object, **kwargs: object) -> _FakeCompleted:
            captured.update(kwargs)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setattr(subprocess, "run", fake_run)

        client = RealNativeDaemonClient()
        client.spawn_bg(cwd=tmp_path, prompt="x")

        env = captured.get("env")
        assert isinstance(env, dict)
        assert "CI" not in env, "CI must not be injected by spawn_bg"

    def test_pwd_overridden_with_worktree_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env["PWD"] must equal str(cwd) passed to spawn_bg.

        subprocess.run(cwd=...) changes the child OS CWD but does NOT update
        $PWD.  Without the fix, $PWD still points to the orchestrator's main
        checkout; the Claude daemon uses $PWD as the project root, causing
        git ops to leak into the main checkout instead of the worker worktree
        (#766).
        """
        captured: dict[str, object] = {}

        def fake_run(args: object, **kwargs: object) -> _FakeCompleted:
            captured.update(kwargs)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        worktree = tmp_path / "wt"
        worktree.mkdir()
        monkeypatch.setenv("PWD", "/some/other/checkout")
        monkeypatch.setattr(subprocess, "run", fake_run)

        client = RealNativeDaemonClient()
        client.spawn_bg(cwd=worktree, prompt="x")

        env = captured.get("env")
        assert isinstance(env, dict)
        assert env["PWD"] == str(worktree), (
            f"PWD must be overridden to str(cwd); got {env.get('PWD')!r}"
        )


@pytest.fixture
def pinned_local_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin ``cw.native_daemon._local_now`` to a fixed-offset aware instant.

    ``_usage_limit_error`` must call ``_local_now()`` as a module global for
    this patch to reach it. Single-file use, so it lives here rather than in
    ``conftest.py``.
    """
    monkeypatch.setattr("cw.native_daemon._local_now", lambda: _PINNED_LOCAL_NOW)
    return _PINNED_LOCAL_NOW


class TestRealNativeDaemonClientSpawn:
    """spawn_bg shells out to claude --bg and parses the short id."""

    def test_parses_short_id_from_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(args: Sequence[str], **kwargs: object) -> _FakeCompleted:
            captured["args"] = list(args)
            captured["cwd"] = kwargs.get("cwd")
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        worktree = tmp_path / "wt"
        worktree.mkdir()

        short_id = client.spawn_bg(cwd=worktree, prompt="do it")

        assert short_id == "a1b2c3d4"
        args = captured["args"]
        assert isinstance(args, list)
        assert args[:5] == [
            "claude",
            "--bg",
            "--permission-mode",
            "auto",
            "do it",
        ]
        assert captured["cwd"] == worktree

    def test_parses_short_id_from_ansi_coded_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression #203: Claude Code 2.1.150 wraps the short id in CSI SGR.

        Real captured output from claude --bg on 2026-05-23::

            'backgrounded \xc2\xb7 \\x1b[36m7719118f\\x1b[39m\\n'
            '\\x1b[2m  claude agents    list sessions\\x1b[22m\\n'
            ...

        The parser must strip ANSI escapes before searching, otherwise the
        ``\\x1b[36m`` between ``\xc2\xb7`` and the hex id breaks the match.
        """
        ansi_stdout = (
            "backgrounded · \x1b[36m7719118f\x1b[39m\n"
            "\x1b[2m  claude agents             list sessions\x1b[22m\n"
            "\x1b[2m  claude attach 7719118f    open in this terminal\x1b[22m\n"
            "\x1b[2m  claude logs 7719118f      show recent output\x1b[22m\n"
            "\x1b[2m  claude stop 7719118f      stop this session\x1b[22m\n"
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_, **__: _FakeCompleted(stdout=ansi_stdout),
        )
        client = RealNativeDaemonClient()

        assert client.spawn_bg(cwd=tmp_path, prompt="x") == "7719118f"

    def test_missing_short_id_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_, **__: _FakeCompleted(stdout="unexpected output"),
        )
        client = RealNativeDaemonClient()
        with pytest.raises(CwError, match="recognizable session id"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_missing_binary_raises_cwerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            msg = "no claude"
            raise FileNotFoundError(msg)

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(CwError, match="claude binary not on PATH"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_nonzero_exit_raises_cwerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                returncode=2,
                cmd=["claude"],
                output="",
                stderr="boom",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(CwError, match="claude --bg exited 2"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_usage_limit_calledprocesserror_raises_usage_limit_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CalledProcessError with usage-limit stderr raises UsageLimitError."""
        from cw.exceptions import UsageLimitError

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1,
                ["claude"],
                output="",
                stderr="You've hit your session limit · resets 3:45pm",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(UsageLimitError, match="usage limit"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_usage_limit_in_stdout_raises_usage_limit_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exits 0 but stdout contains usage-limit text instead of session id."""
        from cw.exceptions import UsageLimitError

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_, **__: _FakeCompleted(
                stdout="You've hit your weekly limit · resets Mon 12:00am"
            ),
        )
        client = RealNativeDaemonClient()
        with pytest.raises(UsageLimitError, match="usage limit"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_disclaimer_not_accepted_raises_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uses FULL verified stderr from claude binary 2.1.150."""
        from cw.exceptions import DisclaimerNotAcceptedError

        full_stderr = (
            "--bg with bypassPermissions requires accepting the disclaimer first. "
            "Run `claude --dangerously-skip-permissions` once interactively."
        )

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1, ["claude"], output="", stderr=full_stderr
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        with pytest.raises(DisclaimerNotAcceptedError, match="disclaimer"):
            client.spawn_bg(cwd=tmp_path, prompt="x")

    def test_disclaimer_error_message_contains_verbatim_ac_substring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: error message must contain verbatim AC2 lowercase-r substring."""
        from cw.exceptions import DisclaimerNotAcceptedError

        full_stderr = (
            "--bg with bypassPermissions requires accepting the disclaimer first. "
            "Run `claude --dangerously-skip-permissions` once interactively."
        )

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1, ["claude"], output="", stderr=full_stderr
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()
        exc_info: pytest.ExceptionInfo[DisclaimerNotAcceptedError]
        with pytest.raises(DisclaimerNotAcceptedError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")
        # Verbatim AC2 substring (lowercase 'r') must appear in the message.
        assert "run `claude --dangerously-skip-permissions` once" in str(exc_info.value)

    def test_spawn_bg_permission_mode_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """permission_mode override replaces _DEFAULT_PERMISSION_MODE in cmd."""
        captured: dict[str, object] = {}

        def fake_run(args: Sequence[str], **kwargs: object) -> _FakeCompleted:
            captured["args"] = list(args)
            return _FakeCompleted(stdout="backgrounded · a1b2c3d4\n")

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        client.spawn_bg(
            cwd=tmp_path, prompt="do it", permission_mode="bypassPermissions"
        )

        args = captured["args"]
        assert isinstance(args, list)
        assert args[:5] == [
            "claude",
            "--bg",
            "--permission-mode",
            "bypassPermissions",
            "do it",
        ]

    # --- #1409: the raised UsageLimitError carries the parsed reset instant ---
    #
    # The usage-limit strings below derive from INTERACTIVE Claude transcript
    # wording, not from a captured ``claude --bg`` spawn-time message (plan
    # decision P1). The ANSI-wrapped variants are synthetic robustness cases,
    # labelled as such — not observations.

    def test_stderr_usage_limit_carries_parsed_reset_at(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned_local_now: datetime,
    ) -> None:
        from cw.exceptions import UsageLimitError

        raw = "You've hit your session limit · resets 3:45pm"

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(1, ["claude"], output="", stderr=raw)

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert exc_info.value.reset_at == pinned_local_now.replace(hour=15, minute=45)
        assert "usage limit" in str(exc_info.value)
        assert raw in str(exc_info.value)

    def test_stdout_usage_limit_carries_parsed_reset_at(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned_local_now: datetime,
    ) -> None:
        from cw.exceptions import UsageLimitError

        raw = "You've hit your weekly limit · resets Mon 12:00am"
        monkeypatch.setattr(
            subprocess, "run", lambda *_a, **_kw: _FakeCompleted(stdout=raw)
        )
        client = RealNativeDaemonClient()

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert exc_info.value.reset_at == (
            pinned_local_now.replace(hour=0, minute=0) + timedelta(days=1)
        )
        # The `{proc.stdout!r}` message embedding is unchanged.
        assert repr(raw) in str(exc_info.value)

    def test_ansi_wrapped_stderr_still_parses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned_local_now: datetime,
    ) -> None:
        """Synthetic robustness case: the stderr branch does not pre-strip ANSI."""
        from cw.exceptions import UsageLimitError

        raw = "\x1b[31mYou've hit your session limit\x1b[39m · resets \x1b[36m3:45pm"

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(1, ["claude"], output="", stderr=raw)

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert exc_info.value.reset_at == pinned_local_now.replace(hour=15, minute=45)

    def test_ansi_wrapped_stdout_still_parses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned_local_now: datetime,
    ) -> None:
        """Synthetic robustness case: same strip applied on the stdout branch."""
        from cw.exceptions import UsageLimitError

        raw = "\x1b[31mYou've hit your weekly limit\x1b[39m · resets \x1b[36m11pm"
        monkeypatch.setattr(
            subprocess, "run", lambda *_a, **_kw: _FakeCompleted(stdout=raw)
        )
        client = RealNativeDaemonClient()

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert exc_info.value.reset_at == pinned_local_now.replace(hour=23, minute=0)

    def test_unparseable_usage_limit_text_leaves_reset_at_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned_local_now: datetime,
    ) -> None:
        from cw.exceptions import UsageLimitError

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(
                1, ["claude"], output="", stderr="You've hit your 5-hour limit"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert exc_info.value.reset_at is None

    @pytest.mark.parametrize(
        ("branch", "raw"),
        [
            pytest.param(
                "stderr",
                "You've hit your session limit · resets 3:45pm",
                id="stderr-parsed",
            ),
            pytest.param(
                "stderr", "You've hit your 5-hour limit", id="stderr-unparsed"
            ),
            pytest.param(
                "stdout",
                "\x1b[36mYou've hit your weekly limit · resets Mon 12:00am",
                id="stdout-parsed-with-ansi",
            ),
            pytest.param(
                "stdout", "You've hit your 5-hour limit", id="stdout-unparsed"
            ),
        ],
    )
    def test_raw_spawn_message_logged_exactly_once_at_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        pinned_local_now: datetime,
        branch: str,
        raw: str,
    ) -> None:
        """P1 binding: one WARNING record per raise, raw text verbatim as ``%r``.

        The record carries the PRE-ANSI-strip subprocess text, so an escape
        sequence sitting between ``resets`` and the time is visible in the
        sample. ``%r`` renders ESC as the four characters ``\\x1b``, hence the
        ``repr(raw) in ...`` assertion rather than a ``"\\x1b" in caplog.text``.
        """
        from cw.exceptions import UsageLimitError

        if branch == "stderr":

            def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
                raise subprocess.CalledProcessError(
                    1, ["claude"], output="", stderr=raw
                )

            monkeypatch.setattr(subprocess, "run", fake_run)
        else:
            monkeypatch.setattr(
                subprocess, "run", lambda *_a, **_kw: _FakeCompleted(stdout=raw)
            )

        client = RealNativeDaemonClient()
        with (
            caplog.at_level(logging.WARNING, logger="cw.native_daemon"),
            pytest.raises(UsageLimitError),
        ):
            client.spawn_bg(cwd=tmp_path, prompt="x")

        records = [
            r
            for r in caplog.records
            if r.name == "cw.native_daemon" and r.levelno == logging.WARNING
        ]
        assert len(records) == 1
        assert repr(raw) in records[0].getMessage()

    def test_local_now_is_timezone_aware(self) -> None:
        """R1 contract: the host-local clock seam never yields a naive instant."""
        from cw.native_daemon import _local_now

        assert _local_now().utcoffset() is not None

    def test_raw_spawn_message_log_is_bounded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        pinned_local_now: datetime,
    ) -> None:
        """Review round 1: `exc.stderr` has no size bound; the log line does.

        Reuses ``cw._text._bounded`` (tail-kept, 4000 chars), the same
        convention ``codex_runner`` logs excerpts under.
        """
        from cw.exceptions import UsageLimitError
        from cw.executor_diagnostics import _EXCERPT_LIMIT

        raw = "x" * (_EXCERPT_LIMIT * 3) + "You've hit your session limit"

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(1, ["claude"], output="", stderr=raw)

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        with (
            caplog.at_level(logging.WARNING, logger="cw.native_daemon"),
            pytest.raises(UsageLimitError),
        ):
            client.spawn_bg(cwd=tmp_path, prompt="x")

        message = caplog.records[0].getMessage()
        assert len(message) < len(raw)
        assert "chars omitted" in message
        # The tail is what carries the limit phrasing this log exists to sample.
        assert "You've hit your session limit" in message

    def test_raw_spawn_message_log_is_redacted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        pinned_local_now: datetime,
    ) -> None:
        """Captured subprocess output may carry a token; the log must not."""
        from cw.exceptions import UsageLimitError

        secret = "ghp_" + "a" * 36
        raw = f"Authorization: Bearer {secret}\nYou've hit your session limit"

        def fake_run(*_a: object, **_kw: object) -> _FakeCompleted:
            raise subprocess.CalledProcessError(1, ["claude"], output="", stderr=raw)

        monkeypatch.setattr(subprocess, "run", fake_run)
        client = RealNativeDaemonClient()

        with (
            caplog.at_level(logging.WARNING, logger="cw.native_daemon"),
            pytest.raises(UsageLimitError),
        ):
            client.spawn_bg(cwd=tmp_path, prompt="x")

        message = caplog.records[0].getMessage()
        assert secret not in message
        assert "<redacted>" in message
        assert "You've hit your session limit" in message


class TestHostTimezoneDst:
    """#1409 review round 1: resolve the offset at the RESET's date, not now's.

    ``datetime.now(UTC).astimezone()`` flattens the host zone to whatever fixed
    offset is in effect at this instant. Applying that frozen offset to a wall
    clock on the far side of a DST transition lands an hour off — and for a
    spring-forward it lands an hour LATE, which lengthens the back-off window
    (the unsafe direction: dispatch stays parked past the real reset).

    Every case here sets ``TZ`` explicitly and reads it back through
    :func:`cw.native_daemon._host_timezone`, which consults the environment
    variable itself rather than libc. No ``tzset`` and no host-zone dependency:
    the assertions hold identically on a UTC CI runner and on a dev box in any
    zone. US DST transitions used: 2026-03-08 (spring forward) and 2026-11-01
    (fall back); 2026-03-07 and 2026-10-31 are both Saturdays.
    """

    def test_host_timezone_keeps_dst_rules(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.native_daemon import _host_timezone

        monkeypatch.setenv("TZ", "America/New_York")
        tz = _host_timezone()

        assert datetime(2026, 1, 15, 12, tzinfo=tz).utcoffset() == timedelta(hours=-5)
        assert datetime(2026, 7, 15, 12, tzinfo=tz).utcoffset() == timedelta(hours=-4)

    def test_host_timezone_falls_back_for_a_non_iana_tz_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A POSIX-style or bogus ``TZ`` degrades, it never raises."""
        from cw.native_daemon import _host_timezone

        monkeypatch.setenv("TZ", "Not/AZone")

        resolved = datetime(2026, 7, 15, 12, tzinfo=_host_timezone())

        assert resolved.utcoffset() is not None

    def test_host_timezone_falls_back_to_the_flat_offset_as_a_last_resort(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No usable TZ and no readable tz database still yields a zone.

        The last resort is the pre-#1409 flattened offset — worse for a
        DST-crossing reset, but never a crash and never a naive instant.
        """
        import cw.native_daemon

        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(
            cw.native_daemon, "_LOCALTIME_PATH", tmp_path / "absent-localtime"
        )

        resolved = datetime(2026, 7, 15, 12, tzinfo=cw.native_daemon._host_timezone())

        assert resolved.utcoffset() is not None

    def test_local_now_resolves_through_the_host_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from freezegun import freeze_time

        from cw.native_daemon import _local_now

        monkeypatch.setenv("TZ", "America/New_York")
        with freeze_time("2026-03-07 15:00:00"):
            now = _local_now()

        assert now.utcoffset() == timedelta(hours=-5)
        assert (now.hour, now.minute) == (10, 0)

    def test_spring_forward_reset_resolves_at_the_target_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Sunday reset read on Saturday uses EDT (-04:00), not Saturday's EST.

        The flattened-offset version of this produced 15:00Z — one hour later
        than the real reset, so dispatch would stay parked an extra hour.
        """
        from freezegun import freeze_time

        from cw.native_daemon import _usage_limit_error

        monkeypatch.setenv("TZ", "America/New_York")
        raw = "You've hit your weekly limit · resets Sun 10:00am"

        with freeze_time("2026-03-07 15:00:00"):  # 10:00 Saturday, EST
            err = _usage_limit_error("usage limit", raw)

        assert err.reset_at == datetime(2026, 3, 8, 14, 0, tzinfo=UTC)

    def test_fall_back_ambiguous_hour_takes_the_later_instant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """01:30 happens twice on 2026-11-01; the second one is the safe pick.

        Picking the first (EDT, 05:30Z) would reopen the spawn gate an hour
        before the limit actually lifts, and the re-hit costs a real attempt.
        """
        from freezegun import freeze_time

        from cw.native_daemon import _usage_limit_error

        monkeypatch.setenv("TZ", "America/New_York")
        raw = "You've hit your weekly limit · resets Sun 1:30am"

        with freeze_time("2026-10-31 14:00:00"):  # 10:00 Saturday, EDT
            err = _usage_limit_error("usage limit", raw)

        assert err.reset_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


class TestRealNativeDaemonClientRoster:
    """list_live_session_short_ids reads roster.json."""

    def test_returns_worker_keys(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(
            json.dumps(
                {
                    "workers": {
                        "aaaa1111": {"pid": 1},
                        "bbbb2222": {"pid": 2},
                    }
                }
            )
        )
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == {"aaaa1111", "bbbb2222"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        client = RealNativeDaemonClient(roster_path=tmp_path / "nope.json")
        assert client.list_live_session_short_ids() == set()

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text("{not json")
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == set()

    def test_workers_not_a_dict_returns_empty(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"workers": ["a", "b"]}))
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == set()


class TestRealNativeDaemonClientWorkerCwds:
    """list_live_worker_cwds: which worktrees live daemon workers are homed on.

    Unlike ``list_live_session_short_ids`` (fail-open: unreadable -> empty), this
    is consumed by a mutation guard, so it fails closed: ``None`` means "could
    not tell", and only an absent roster (no daemon ever ran) is an empty set.
    """

    def test_returns_worker_cwds(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(
            json.dumps(
                {
                    "workers": {
                        "aaaa1111": {"pid": 1, "cwd": "/wt/one"},
                        "bbbb2222": {"pid": 2, "cwd": "/wt/two"},
                        "cccc3333": {"pid": 3, "cwd": "/wt/one"},
                    }
                }
            )
        )
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_worker_cwds() == frozenset(
            {Path("/wt/one"), Path("/wt/two")}
        )

    def test_empty_workers_returns_empty_set(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"workers": {}}))
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_worker_cwds() == frozenset()

    def test_absent_roster_returns_empty_set(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = RealNativeDaemonClient(roster_path=tmp_path / "nope.json")
        with caplog.at_level("WARNING", logger="cw.native_daemon"):
            assert client.list_live_worker_cwds() == frozenset()
        assert caplog.records == []

    def test_invalid_json_returns_none_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text("{not json")
        client = RealNativeDaemonClient(roster_path=roster)
        with caplog.at_level("WARNING", logger="cw.native_daemon"):
            assert client.list_live_worker_cwds() is None
        assert any("not valid JSON" in r.getMessage() for r in caplog.records)

    def test_invalid_utf8_returns_none_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # ``read_text(encoding="utf-8")`` raises UnicodeDecodeError -- a
        # ValueError, NOT an OSError or JSONDecodeError -- on invalid bytes. It
        # must read as "cannot determine" (None), never escape the reader (#2213).
        roster = tmp_path / "roster.json"
        roster.write_bytes(b'{"workers": {"aaaa1111": {"cwd": "/wt/\xff\xfe"}}}')
        client = RealNativeDaemonClient(roster_path=roster)
        with caplog.at_level("WARNING", logger="cw.native_daemon"):
            assert client.list_live_worker_cwds() is None
        assert any("unreadable" in r.getMessage() for r in caplog.records)

    def test_short_ids_fail_open_on_invalid_utf8(self, tmp_path: Path) -> None:
        """The same shared parser: the liveness view stays fail-open."""
        roster = tmp_path / "roster.json"
        roster.write_bytes(b"\xff\xfe\x00 not utf-8")
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == set()

    def test_non_enoent_oserror_returns_none_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A directory at the roster path: read_text raises IsADirectoryError, an
        # OSError that is NOT FileNotFoundError.
        roster = tmp_path / "roster.json"
        roster.mkdir()
        client = RealNativeDaemonClient(roster_path=roster)
        with caplog.at_level("WARNING", logger="cw.native_daemon"):
            assert client.list_live_worker_cwds() is None
        assert any("unreadable" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"workers": ["a", "b"]}, id="workers-list"),
            pytest.param({"proto": 1}, id="workers-missing"),
            pytest.param(["not", "a", "dict"], id="top-level-list"),
            pytest.param({"workers": {"aaaa1111": "not-a-dict"}}, id="entry-not-dict"),
            pytest.param({"workers": {"aaaa1111": {"pid": 1}}}, id="entry-no-cwd"),
            pytest.param({"workers": {"aaaa1111": {"cwd": ""}}}, id="entry-empty-cwd"),
            pytest.param({"workers": {"aaaa1111": {"cwd": 7}}}, id="entry-int-cwd"),
            pytest.param(
                {"workers": {"aaaa1111": {"cwd": "/wt/ok"}, "bbbb2222": {"pid": 2}}},
                id="one-good-one-bad",
            ),
        ],
    )
    def test_malformed_shape_returns_none(
        self, tmp_path: Path, payload: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps(payload))
        client = RealNativeDaemonClient(roster_path=roster)
        with caplog.at_level("WARNING", logger="cw.native_daemon"):
            assert client.list_live_worker_cwds() is None
        assert caplog.records  # the failure is not silent

    def test_short_ids_and_cwds_share_one_parser(self, tmp_path: Path) -> None:
        """Both views read the same file through the same loader, so they can
        never disagree about what the roster says."""
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"workers": {"aaaa1111": {"cwd": "/wt/one"}}}))
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == {"aaaa1111"}
        assert client.list_live_worker_cwds() == frozenset({Path("/wt/one")})

    def test_short_ids_still_fail_open_on_unreadable_roster(
        self, tmp_path: Path
    ) -> None:
        roster = tmp_path / "roster.json"
        roster.mkdir()
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == set()

    def test_short_ids_tolerate_entries_without_cwd(self, tmp_path: Path) -> None:
        """The liveness view only needs the keys: a cwd-less entry is a live
        worker for reconcile even though it makes the cwd view fail closed."""
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"workers": {"aaaa1111": {"pid": 1}}}))
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids() == {"aaaa1111"}
        assert client.list_live_worker_cwds() is None


class TestRealNativeDaemonClientShortIdsFailClosed:
    """list_live_session_short_ids_fail_closed: the requeue guard's view (#2275).

    Same split as ``list_live_worker_cwds`` (#2213): an absent roster is an
    empty set (no daemon, so no live sessions); an unreadable or malformed one
    is ``None`` ("cannot rule out a live session").
    """

    def test_returns_worker_keys(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.write_text(json.dumps({"workers": {"aaaa1111": {"pid": 1}}}))
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids_fail_closed() == {"aaaa1111"}

    def test_absent_roster_returns_empty_set(self, tmp_path: Path) -> None:
        client = RealNativeDaemonClient(roster_path=tmp_path / "nope.json")
        assert client.list_live_session_short_ids_fail_closed() == set()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"{not json", id="invalid-json"),
            pytest.param(b"\xff\xfe\x00 not utf-8", id="invalid-utf8"),
            pytest.param(json.dumps({"workers": ["a"]}).encode(), id="workers-list"),
            pytest.param(json.dumps(["x"]).encode(), id="top-level-list"),
        ],
    )
    def test_malformed_roster_returns_none(self, tmp_path: Path, raw: bytes) -> None:
        roster = tmp_path / "roster.json"
        roster.write_bytes(raw)
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids_fail_closed() is None
        # The fail-open view over the same file is unchanged.
        assert client.list_live_session_short_ids() == set()

    def test_unreadable_roster_returns_none(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        roster.mkdir()
        client = RealNativeDaemonClient(roster_path=roster)
        assert client.list_live_session_short_ids_fail_closed() is None

    def test_roster_path_is_exposed(self, tmp_path: Path) -> None:
        roster = tmp_path / "roster.json"
        assert RealNativeDaemonClient(roster_path=roster).roster_path == roster


class TestRealNativeDaemonClientStop:
    """stop is best-effort and swallows expected failure modes."""

    def test_invokes_claude_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(args: Sequence[str], **kwargs: object) -> _FakeCompleted:
            captured["args"] = list(args)
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", fake_run)
        RealNativeDaemonClient().stop("deadbeef")
        assert captured["args"] == ["claude", "stop", "deadbeef"]

    def test_missing_binary_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Must not raise — best-effort cleanup.
        RealNativeDaemonClient().stop("deadbeef")

    def test_timeout_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=10)

        monkeypatch.setattr(subprocess, "run", fake_run)
        RealNativeDaemonClient().stop("deadbeef")


class TestFakeNativeDaemonClient:
    """FakeNativeDaemonClient records calls and maintains a live set."""

    def test_spawn_records_and_returns_short_id(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        first = client.spawn_bg(cwd=tmp_path, prompt="a")
        second = client.spawn_bg(cwd=tmp_path, prompt="b")

        assert first != second
        assert len(first) == 8
        assert client.spawn_calls == [(tmp_path, "a"), (tmp_path, "b")]
        assert client.list_live_session_short_ids() == {first, second}

    def test_stop_drops_from_live_set(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        short_id = client.spawn_bg(cwd=tmp_path, prompt="x")
        client.stop(short_id)
        assert client.stop_calls == [short_id]
        assert client.list_live_session_short_ids() == set()

    def test_worker_cwds_track_live_spawns(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        first = client.spawn_bg(cwd=tmp_path / "one", prompt="a")
        client.spawn_bg(cwd=tmp_path / "two", prompt="b")
        assert client.list_live_worker_cwds() == frozenset(
            {tmp_path / "one", tmp_path / "two"}
        )
        client.stop(first)
        assert client.list_live_worker_cwds() == frozenset({tmp_path / "two"})

    def test_worker_cwds_exclude_unregistered_spawn(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        client.raise_unregistered = True
        client.spawn_bg(cwd=tmp_path, prompt="x")
        assert client.list_live_worker_cwds() == frozenset()

    def test_worker_cwds_none_when_roster_unreadable(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        client.spawn_bg(cwd=tmp_path, prompt="x")
        client.roster_unreadable = True
        assert client.list_live_worker_cwds() is None

    def test_short_ids_fail_closed_none_when_roster_unreadable(
        self, tmp_path: Path
    ) -> None:
        """#2275: one flag drives every fail-closed roster view."""
        client = FakeNativeDaemonClient()
        short_id = client.spawn_bg(cwd=tmp_path, prompt="x")
        assert client.list_live_session_short_ids_fail_closed() == {short_id}
        client.roster_unreadable = True
        assert client.list_live_session_short_ids_fail_closed() is None
        assert isinstance(client.roster_path, Path)

    def test_raise_usage_limit_raises_before_counter(self, tmp_path: Path) -> None:
        """raise_usage_limit=True raises UsageLimitError before incrementing counter."""
        from cw.exceptions import UsageLimitError

        client = FakeNativeDaemonClient()
        client.raise_usage_limit = True
        with pytest.raises(UsageLimitError):
            client.spawn_bg(cwd=tmp_path, prompt="x")
        # Counter should not have been incremented — no slot consumed.
        assert client.spawn_calls == []
        assert client.list_live_session_short_ids() == set()

    def test_raise_usage_limit_false_by_default(self, tmp_path: Path) -> None:
        """raise_usage_limit defaults to False — normal spawn behavior."""
        client = FakeNativeDaemonClient()
        assert client.raise_usage_limit is False
        short_id = client.spawn_bg(cwd=tmp_path, prompt="x")
        assert len(short_id) == 8

    def test_usage_limit_reset_at_defaults_to_none(self, tmp_path: Path) -> None:
        """#1409: the injected reset instant is opt-in."""
        from cw.exceptions import UsageLimitError

        client = FakeNativeDaemonClient()
        client.raise_usage_limit = True

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert client.usage_limit_reset_at is None
        assert exc_info.value.reset_at is None
        assert str(exc_info.value) == "fake: usage limit"

    def test_usage_limit_reset_at_propagates_to_exception(self, tmp_path: Path) -> None:
        """#1409: tests inject reset_at directly, no message crafting needed."""
        from cw.exceptions import UsageLimitError

        reset_at = datetime(2026, 9, 20, 19, 45, tzinfo=timezone(timedelta(0)))
        client = FakeNativeDaemonClient()
        client.raise_usage_limit = True
        client.usage_limit_reset_at = reset_at

        exc_info: pytest.ExceptionInfo[UsageLimitError]
        with pytest.raises(UsageLimitError) as exc_info:
            client.spawn_bg(cwd=tmp_path, prompt="x")

        assert exc_info.value.reset_at == reset_at
        assert str(exc_info.value) == "fake: usage limit"


def test_get_native_daemon_client_returns_real_instance() -> None:
    assert isinstance(get_native_daemon_client(), RealNativeDaemonClient)


class TestWaitForRosterPresence:
    """The one bounded roster poller: spawn registration and stop confirmation."""

    def test_present_true_once_the_worker_is_live(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        short_id = client.seed_live_worker(tmp_path)

        assert wait_for_roster_presence(
            client, short_id, present=True, timeout=0.0, interval=0.0
        )

    def test_present_false_when_never_registered(self) -> None:
        client = FakeNativeDaemonClient()

        assert not wait_for_roster_presence(
            client, "deadbeef", present=True, timeout=0.0, interval=0.0
        )

    def test_absent_true_once_the_worker_is_stopped(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        short_id = client.seed_live_worker(tmp_path)
        client.stop(short_id)

        assert wait_for_roster_presence(
            client, short_id, present=False, timeout=0.0, interval=0.0
        )

    def test_absent_false_while_the_worker_is_still_live(self, tmp_path: Path) -> None:
        client = FakeNativeDaemonClient()
        short_id = client.seed_live_worker(tmp_path)

        assert not wait_for_roster_presence(
            client, short_id, present=False, timeout=0.0, interval=0.0
        )

    def test_absent_fails_closed_on_an_unreadable_roster(self) -> None:
        """An unreadable roster never confirms a worker is gone."""
        client = FakeNativeDaemonClient()
        client.roster_unreadable = True

        assert not wait_for_roster_presence(
            client, "deadbeef", present=False, timeout=0.0, interval=0.0
        )

    def test_polls_until_the_roster_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It sleeps between reads and returns once the roster catches up."""
        client = FakeNativeDaemonClient()
        short_id = client.seed_live_worker(tmp_path)
        sleeps: list[float] = []

        def _sleep_then_stop(seconds: float) -> None:
            sleeps.append(seconds)
            client.stop(short_id)

        monkeypatch.setattr("cw.native_daemon.time.sleep", _sleep_then_stop)

        assert wait_for_roster_presence(
            client, short_id, present=False, timeout=60.0, interval=0.25
        )
        assert sleeps == [0.25]


class TestModelSupportsAuto:
    """model_supports_auto gates ``--permission-mode auto`` by worker_model (#1111)."""

    def test_none_is_auto_capable(self) -> None:
        """No pin (None) keeps today's behavior — treated as auto-capable."""
        assert model_supports_auto(None) is True

    def test_empty_string_is_auto_capable(self) -> None:
        """Falsy value matches the ``if client.worker_model:`` truthiness convention."""
        assert model_supports_auto("") is True

    def test_bare_sonnet_alias_is_auto_capable(self) -> None:
        assert model_supports_auto("claude-sonnet-5") is True

    def test_dated_sonnet_id_is_auto_capable(self) -> None:
        """Prefix match, not exact-set — dated/suffixed ids still resolve."""
        assert model_supports_auto("claude-sonnet-4-6-20251015") is True

    def test_opus_is_auto_capable(self) -> None:
        assert model_supports_auto("claude-opus-4-8") is True

    def test_known_haiku_is_not_auto_capable(self) -> None:
        assert model_supports_auto("claude-haiku-4-5-20251001") is False

    def test_unknown_ids_are_not_auto_capable(self) -> None:
        """An unrecognized non-None id is conservatively NOT auto-capable."""
        assert model_supports_auto("claude-fable-5") is False
        assert model_supports_auto("claude-mythos-5") is False

    def test_case_and_whitespace_insensitive(self) -> None:
        assert model_supports_auto("  CLAUDE-SONNET-5-x  ") is True

    def test_skip_permissions_mode_literal(self) -> None:
        """Lock the fallback mode literal."""
        assert SKIP_PERMISSIONS_MODE == "bypassPermissions"


class TestResolvePermissionMode:
    """resolve_permission_mode: shared derivation for both spawn chokepoints (#1111)."""

    def test_explicit_wins_over_non_auto_model(self) -> None:
        """A caller-supplied permission_mode always wins over the derivation."""
        assert (
            resolve_permission_mode("claude-haiku-4-5-20251001", explicit="acceptEdits")
            == "acceptEdits"
        )

    def test_explicit_wins_over_auto_capable_model(self) -> None:
        assert (
            resolve_permission_mode("claude-sonnet-5", explicit="acceptEdits")
            == "acceptEdits"
        )

    def test_auto_capable_model_returns_none(self) -> None:
        assert resolve_permission_mode("claude-sonnet-4-6-20251015") is None

    def test_no_pin_returns_none(self) -> None:
        assert resolve_permission_mode(None) is None

    def test_non_auto_model_returns_skip_mode(self) -> None:
        assert (
            resolve_permission_mode("claude-haiku-4-5-20251001")
            == SKIP_PERMISSIONS_MODE
        )

    def test_non_auto_model_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The bypassPermissions fallback is audit-logged, not silent (#1111)."""
        import logging

        with caplog.at_level(logging.WARNING):
            resolve_permission_mode("claude-haiku-4-5-20251001")

        assert any(
            "claude-haiku-4-5-20251001" in rec.message
            and "bypassPermissions" in rec.message
            for rec in caplog.records
        )

    def test_auto_capable_model_does_not_log_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            resolve_permission_mode("claude-sonnet-5")

        assert caplog.records == []


class TestReadSupervisorResumeSessionId:
    """read_supervisor_resume_session_id reads ~/.claude/jobs/<id>/state.json."""

    def test_returns_resume_session_id(self, tmp_path: Path) -> None:
        short_id = "a1b2c3d4"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        full_uuid = "a1b2c3d4-0000-0000-0000-000000000001"
        (state_dir / "state.json").write_text(
            json.dumps({"resumeSessionId": full_uuid, "sessionId": full_uuid}),
            encoding="utf-8",
        )
        assert (
            read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) == full_uuid
        )

    def test_missing_directory_returns_none(self, tmp_path: Path) -> None:
        assert read_supervisor_resume_session_id("deadbeef", jobs_path=tmp_path) is None

    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        (tmp_path / short_id).mkdir()
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text("{not json", encoding="utf-8")
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text(
            json.dumps({"sessionId": "abc"}), encoding="utf-8"
        )
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_non_string_value_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text(
            json.dumps({"resumeSessionId": 42}), encoding="utf-8"
        )
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None

    def test_non_dict_json_returns_none(self, tmp_path: Path) -> None:
        short_id = "deadbeef"
        state_dir = tmp_path / short_id
        state_dir.mkdir()
        (state_dir / "state.json").write_text(
            json.dumps(["not", "a", "dict"]), encoding="utf-8"
        )
        assert read_supervisor_resume_session_id(short_id, jobs_path=tmp_path) is None
