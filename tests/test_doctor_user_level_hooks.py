"""Tests for cw.doctor.user_level_hooks — user-level Stop-hook scope check.

Direct calls to ``_check_user_level_stop_hook()``, monkeypatching the
module-level ``_CLAUDE_HOME`` seam rather than touching the real home
directory (same shape as ``tests/test_doctor_skills_drift.py``, #1514).
The ``run_doctor`` wiring tests live in ``tests/test_doctor.py``, where the
module-private ``_stub_claude_version_ok`` helper is reachable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cw.doctor.user_level_hooks import (
    _CHECK_NAME,
    _STOP_HOOK_PATTERN,
    _check_user_level_stop_hook,
)
from cw.spawn import _stop_hook_command

_BARE_COMMAND = "cw signal-stop"

# What cw actually injects today: the guard with a baked-in absolute path.
_INJECTED_COMMAND = _stop_hook_command(Path("/wt/.claude/cw-context.json"))


def _stop_hook_settings(*commands: str) -> dict[str, object]:
    """One Stop entry whose hooks list carries *commands* in order."""
    return {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": command} for command in commands
                    ],
                }
            ]
        }
    }


def _seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **files: str) -> Path:
    """Point _CLAUDE_HOME at a tmp dir and write the named settings files."""
    claude_home = tmp_path / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (claude_home / name.replace("_", ".")).write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        "cw.doctor.user_level_hooks._CLAUDE_HOME", claude_home, raising=True
    )
    return claude_home


class TestUserLevelStopHookDetection:
    """Positive detections — a cw Stop hook installed user-level."""

    def test_bare_command_in_settings_json_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A bare ``cw signal-stop`` Stop hook warns and names the file/line."""
        home = _seed(
            monkeypatch,
            tmp_path,
            settings_json=json.dumps(_stop_hook_settings(_BARE_COMMAND)),
        )

        result = _check_user_level_stop_hook()

        assert result.name == _CHECK_NAME
        assert result.ok is True
        assert result.warn is True
        assert str(home / "settings.json") in result.detail
        assert "hooks.Stop[0].hooks[0]" in result.detail
        assert f'"command": {json.dumps(_BARE_COMMAND)}' in result.detail

    def test_guarded_command_in_settings_json_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The #2226 guarded form is detected too, not just the bare form."""
        _seed(
            monkeypatch,
            tmp_path,
            settings_json=json.dumps(_stop_hook_settings(_INJECTED_COMMAND)),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert f'"command": {json.dumps(_INJECTED_COMMAND)}' in result.detail

    def test_settings_local_json_only_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """settings.local.json is scanned too (a cwd=$HOME spawn writes it)."""
        home = _seed(
            monkeypatch,
            tmp_path,
            settings_local_json=json.dumps(_stop_hook_settings(_BARE_COMMAND)),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert str(home / "settings.local.json") in result.detail

    def test_both_files_produce_two_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A hit in each file yields two findings joined with '; '."""
        home = _seed(
            monkeypatch,
            tmp_path,
            settings_json=json.dumps(_stop_hook_settings(_BARE_COMMAND)),
            settings_local_json=json.dumps(_stop_hook_settings(_BARE_COMMAND)),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert result.detail.count("; ") >= 1
        assert str(home / "settings.json") in result.detail
        assert str(home / "settings.local.json") in result.detail

    def test_nested_indices_are_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A hit at Stop[1].hooks[2] reports those indices, not 0/0."""
        data = {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{"command": "true"}]},
                    {
                        "matcher": "",
                        "hooks": [
                            {"command": "echo a"},
                            {"command": "echo b"},
                            {"type": "command", "command": _BARE_COMMAND},
                        ],
                    },
                ]
            }
        }
        _seed(monkeypatch, tmp_path, settings_json=json.dumps(data))

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert "hooks.Stop[1].hooks[2]" in result.detail

    def test_absolute_path_invocation_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``/opt/bin/cw signal-stop`` is the same hook, differently spelled."""
        _seed(
            monkeypatch,
            tmp_path,
            settings_json=json.dumps(_stop_hook_settings("/opt/bin/cw signal-stop")),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True


class TestUserLevelStopHookCleanAndMalformed:
    """Negative, missing and wrong-shape inputs — never warn, never raise."""

    def test_no_files_is_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Neither settings file exists → clean, no crash."""
        _seed(monkeypatch, tmp_path)

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is False
        assert result.detail == "no cw Stop hook in user-level settings"

    def test_unrelated_stop_hook_is_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A Stop hook that is not cw's does not warn."""
        _seed(
            monkeypatch,
            tmp_path,
            settings_json=json.dumps(_stop_hook_settings("notify-send done")),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is False

    def test_cw_hooks_under_other_events_are_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """PreToolUse/SessionStart cw hooks are legitimate user-level config."""
        data = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"command": "cw orchestrate status --json || true"}]}
                ],
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"command": "cw guard-cwd"}]}
                ],
            }
        }
        _seed(monkeypatch, tmp_path, settings_json=json.dumps(data))

        result = _check_user_level_stop_hook()

        assert result.warn is False

    @pytest.mark.parametrize(
        "raw",
        [
            json.dumps({"hooks": "not-a-dict"}),
            json.dumps({"hooks": {"Stop": "not-a-list"}}),
            json.dumps({"hooks": {"Stop": ["not-a-dict"]}}),
            json.dumps({"hooks": {"Stop": [{"hooks": "not-a-list"}]}}),
            json.dumps({"hooks": {"Stop": [{"hooks": ["not-a-dict"]}]}}),
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": 42}]}]}}),
            json.dumps({"permissions": {"allow": ["Bash(cw:*)"]}}),
        ],
    )
    def test_wrong_shape_json_never_warns_or_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
    ) -> None:
        """Valid JSON of the wrong shape has no Stop hook: clean, not a failure."""
        _seed(monkeypatch, tmp_path, settings_json=raw)

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is False


class TestUserLevelStopHookUnreadableFiles:
    """A settings file we cannot read/parse is a WARN finding, never a crash."""

    def test_missing_file_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A missing settings file is the ordinary case: no warn, no note."""
        _seed(monkeypatch, tmp_path)

        result = _check_user_level_stop_hook()

        assert result.warn is False
        assert "could not read/parse" not in result.detail

    def test_malformed_json_warns_naming_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Unparseable-but-valid-UTF-8 JSON → WARN naming the file and class."""
        home = _seed(monkeypatch, tmp_path, settings_json="{{{ nope")

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is True
        assert (
            f"{home / 'settings.json'}: could not read/parse (malformed JSON)"
            in result.detail
        )

    def test_top_level_non_object_warns_naming_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A settings file that is valid JSON but not an object is not settings.

        It used to read as "no Stop hook, silent"; via the shared reader it is
        a WARN, the same as the bypass-disclaimer check reports it.
        """
        home = _seed(monkeypatch, tmp_path, settings_json=json.dumps(["a", "list"]))

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is True
        assert (
            f"{home / 'settings.json'}: could not read/parse (not a JSON object)"
            in result.detail
        )

    def test_invalid_utf8_warns_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Invalid UTF-8 was an uncaught UnicodeDecodeError out of run_doctor."""
        home = _seed(monkeypatch, tmp_path)
        (home / "settings.json").write_bytes(b'{"hooks": "\xff\xfe\x80"}')

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is True
        assert (
            f"{home / 'settings.json'}: could not read/parse (invalid UTF-8)"
            in result.detail
        )

    def test_unreadable_file_warns_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """settings.json as a directory → IsADirectoryError, WARN not raised."""
        claude_home = tmp_path / ".claude"
        (claude_home / "settings.json").mkdir(parents=True)
        monkeypatch.setattr(
            "cw.doctor.user_level_hooks._CLAUDE_HOME", claude_home, raising=True
        )

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is True
        assert "could not read/parse (unreadable: IsADirectoryError)" in result.detail

    def test_failure_detail_does_not_echo_file_contents(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The detail names a failure class, never the raw exception text."""
        home = _seed(monkeypatch, tmp_path)
        (home / "settings.json").write_bytes(b"sekrit-token-\xff")

        result = _check_user_level_stop_hook()

        assert "sekrit-token" not in result.detail

    def test_unreadable_file_does_not_mask_hit_in_other_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unreadable settings.json does not hide a hit in settings.local.json."""
        home = _seed(
            monkeypatch,
            tmp_path,
            settings_local_json=json.dumps(_stop_hook_settings(_BARE_COMMAND)),
        )
        (home / "settings.json").write_bytes(b"\xff\xfe")

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert str(home / "settings.local.json") in result.detail
        assert f"{home / 'settings.json'}: could not read/parse" in result.detail

    def test_both_files_unreadable_warn_twice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Each unreadable file gets its own finding; the check still returns."""
        home = _seed(
            monkeypatch,
            tmp_path,
            settings_json="{{{",
            settings_local_json="[[[",
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert result.detail.count("could not read/parse") == 2
        assert str(home / "settings.json") in result.detail
        assert str(home / "settings.local.json") in result.detail


class TestStopHookPatternCrossPin:
    """The detector's pattern must track the command cw actually injects."""

    def test_pattern_matches_injected_and_legacy_commands(self) -> None:
        """Both the guarded #2226 form and the legacy bare form match."""
        assert _STOP_HOOK_PATTERN.search(_INJECTED_COMMAND) is not None
        assert _STOP_HOOK_PATTERN.search(_BARE_COMMAND) is not None

    def test_pattern_does_not_match_other_cw_subcommands(self) -> None:
        """Sibling cw hooks are not Stop-hook installs."""
        assert _STOP_HOOK_PATTERN.search("cw guard-cwd") is None
        assert _STOP_HOOK_PATTERN.search("cw orchestrate status --json") is None
