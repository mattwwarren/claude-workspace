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
from cw.spawn import STOP_HOOK_COMMAND

_BARE_COMMAND = "cw signal-stop"


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
            settings_json=json.dumps(_stop_hook_settings(STOP_HOOK_COMMAND)),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert f'"command": {json.dumps(STOP_HOOK_COMMAND)}' in result.detail

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
    """Negative, missing and malformed inputs — never warn, never raise."""

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
            "not json at all {{{",
            json.dumps(["a", "list"]),
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
        """Malformed or wrong-shape settings degrade to a silent skip."""
        _seed(monkeypatch, tmp_path, settings_json=raw)

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is False

    def test_unparseable_file_is_noted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Unparseable JSON appends a skip note to the clean detail."""
        home = _seed(monkeypatch, tmp_path, settings_json="{{{ nope")

        result = _check_user_level_stop_hook()

        assert result.warn is False
        assert f"(skipped unparseable {home / 'settings.json'})" in result.detail

    def test_unreadable_file_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """settings.json as a directory → IsADirectoryError, skipped not raised."""
        claude_home = tmp_path / ".claude"
        (claude_home / "settings.json").mkdir(parents=True)
        monkeypatch.setattr(
            "cw.doctor.user_level_hooks._CLAUDE_HOME", claude_home, raising=True
        )

        result = _check_user_level_stop_hook()

        assert result.ok is True
        assert result.warn is False
        assert "skipped unparseable" in result.detail

    def test_malformed_one_file_hit_in_other_still_warns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A malformed settings.json does not mask a hit in settings.local.json."""
        home = _seed(
            monkeypatch,
            tmp_path,
            settings_json="{{{ nope",
            settings_local_json=json.dumps(_stop_hook_settings(_BARE_COMMAND)),
        )

        result = _check_user_level_stop_hook()

        assert result.warn is True
        assert str(home / "settings.local.json") in result.detail
        assert f"(skipped unparseable {home / 'settings.json'})" in result.detail


class TestStopHookPatternCrossPin:
    """The detector's pattern must track the command cw actually injects."""

    def test_pattern_matches_injected_and_legacy_commands(self) -> None:
        """Both the guarded #2226 form and the legacy bare form match."""
        assert _STOP_HOOK_PATTERN.search(STOP_HOOK_COMMAND) is not None
        assert _STOP_HOOK_PATTERN.search(_BARE_COMMAND) is not None

    def test_pattern_does_not_match_other_cw_subcommands(self) -> None:
        """Sibling cw hooks are not Stop-hook installs."""
        assert _STOP_HOOK_PATTERN.search("cw guard-cwd") is None
        assert _STOP_HOOK_PATTERN.search("cw orchestrate status --json") is None
