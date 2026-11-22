"""``ClientConfig.epic`` (#3783): the enqueue-only size ceiling's config model."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from cw.models import ClientConfig

if TYPE_CHECKING:
    from pathlib import Path


def _client(**extra: object) -> ClientConfig:
    return ClientConfig.model_validate(
        {"name": "acme", "workspace_path": "/unused", **extra}
    )


def test_default_is_unset() -> None:
    assert _client().epic.max_diff_lines is None


def test_zero_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _client(epic={"max_diff_lines": 0})


def test_unknown_epic_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _client(epic={"max_diff_line": 10})


def test_top_level_typo_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        _client(epics={"max_diff_lines": 10})


def test_epic_yaml_round_trips_through_load_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.config import load_clients

    clients_file = tmp_path / "clients.yaml"
    clients_file.write_text(
        "clients:\n"
        "  acme:\n"
        f"    workspace_path: {tmp_path}\n"
        "    epic:\n"
        "      max_diff_lines: 400\n"
    )
    monkeypatch.setattr("cw.config.clients_file", lambda: clients_file)
    loaded = load_clients()
    assert loaded["acme"].epic.max_diff_lines == 400
