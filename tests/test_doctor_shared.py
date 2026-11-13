"""Tests for cw.doctor._shared: the settings reader and ``ReapScope``.

Every doctor check that reads a user-level settings file goes through this one
helper (#2226, review round 2). A check whose purpose is to diagnose a broken
install must survive that broken install, so a file it cannot read is a
*value* the caller turns into a WARN, never an exception out of ``run_doctor``.

``ReapScope`` (#2716) is the frozen ``(client, ticket)`` value a scoped
``cw doctor --reap --ticket <T> -c <C>`` carries into the reap.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

from cw.doctor import ReapScope as ReexportedReapScope
from cw.doctor._shared import (
    ReapScope,
    SettingsReadFailure,
    _read_settings,
    orchestrator_config_or_default,
)
from cw.exceptions import ConfigValidationError, CwError
from cw.models import OrchestratorConfig

if TYPE_CHECKING:
    from pathlib import Path

_CANARY = "sekrit-token"


def test_object_json_is_returned_as_dict(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"skipDangerousModePermissionPrompt": true}', encoding="utf-8")

    result = _read_settings(path)

    assert result == {"skipDangerousModePermissionPrompt": True}


def test_missing_file_is_flagged_missing(tmp_path: Path) -> None:
    result = _read_settings(tmp_path / "absent.json")

    assert isinstance(result, SettingsReadFailure)
    assert result.missing is True


def test_invalid_utf8_is_a_failure_not_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"k": "\xff\xfe\x80"}')

    result = _read_settings(path)

    assert isinstance(result, SettingsReadFailure)
    assert result.missing is False
    assert result.reason == "invalid UTF-8"


def test_malformed_json_is_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{{{ nope", encoding="utf-8")

    result = _read_settings(path)

    assert isinstance(result, SettingsReadFailure)
    assert result.missing is False
    assert result.reason == "malformed JSON"


@pytest.mark.parametrize("raw", ["[]", '"a string"', "3", "null", "true"])
def test_valid_json_that_is_not_an_object_is_a_failure(
    tmp_path: Path, raw: str
) -> None:
    """``json.loads('[]').get`` used to raise AttributeError out of run_doctor."""
    path = tmp_path / "settings.json"
    path.write_text(raw, encoding="utf-8")

    result = _read_settings(path)

    assert isinstance(result, SettingsReadFailure)
    assert result.missing is False
    assert result.reason == "not a JSON object"


def test_directory_in_place_of_file_is_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.mkdir()

    result = _read_settings(path)

    assert isinstance(result, SettingsReadFailure)
    assert result.missing is False
    assert result.reason == "unreadable: IsADirectoryError"


def test_reason_never_echoes_file_contents(tmp_path: Path) -> None:
    """A failure label is a class name; exception text can quote the file."""
    for name, payload in (
        ("utf8.json", f"{_CANARY}-".encode() + b"\xff"),
        ("malformed.json", f"{_CANARY} {{".encode()),
        ("list.json", f'["{_CANARY}"]'.encode()),
    ):
        path = tmp_path / name
        path.write_bytes(payload)

        result = _read_settings(path)

        assert isinstance(result, SettingsReadFailure)
        assert _CANARY not in result.reason


class TestReapScope:
    """The frozen ``(client, ticket)`` value of a scoped reap (#2716)."""

    def test_is_frozen(self) -> None:
        scope = ReapScope("acme", "ACM-1")
        field_name = "client"

        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(scope, field_name, "other")

    def test_key_is_client_then_ticket(self) -> None:
        assert ReapScope("acme", "ACM-1").key == ("acme", "ACM-1")

    def test_covers_only_the_exact_pair(self) -> None:
        scope = ReapScope("acme", "ACM-1")

        assert scope.covers("acme", "ACM-1") is True
        assert scope.covers("other", "ACM-1") is False
        assert scope.covers("acme", "ACM-2") is False
        assert scope.covers("acme", None) is False

    def test_is_reexported_from_the_package(self) -> None:
        assert ReexportedReapScope is ReapScope


class TestOrchestratorConfigOrDefault:
    """The one degrade-to-defaults loader every doctor check shares (#3162)."""

    def test_returns_the_loaded_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = OrchestratorConfig(inbox_line_count_warn=7)
        monkeypatch.setattr("cw.doctor._shared.load_orchestrator_config", lambda: cfg)

        assert orchestrator_config_or_default() is cfg

    @pytest.mark.parametrize(
        "error",
        [
            OSError("unreadable"),
            yaml.YAMLError("bad yaml"),
            CwError("cw failure"),
            ConfigValidationError("bad config"),
            ValidationError.from_exception_data("OrchestratorConfig", []),
        ],
        ids=["oserror", "yaml", "cwerror", "config-validation", "pydantic"],
    )
    def test_degrades_to_defaults_on_load_failure(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        def _raise() -> OrchestratorConfig:
            raise error

        monkeypatch.setattr("cw.doctor._shared.load_orchestrator_config", _raise)

        assert orchestrator_config_or_default() == OrchestratorConfig()
