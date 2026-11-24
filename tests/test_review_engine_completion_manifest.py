"""Manifest, loader, CLI and doctor surface of ``completion_check`` (#3885)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from cw.cli.review.engine import _validate_manifest
from cw.models.review_engine_manifest import (
    DEFAULT_COMPLETION_TIMEOUT_SECONDS,
    MAX_COMPLETION_TIMEOUT_SECONDS,
    MIN_COMPLETION_TIMEOUT_SECONDS,
    EngineCompletionCheck,
    EngineManifest,
)
from cw.review_engine_manifest_io import (
    EngineManifestError,
    EngineManifestRejection,
    load_engine_manifest,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "review_engine_checker"
_BASE: dict[str, object] = {
    "manifest_version": 1,
    "backends": ["codex"],
    "roles": [
        {"id": "security", "agent_spec": "agents/security.md"},
        {"id": "quality", "agent_spec": "agents/quality.md"},
    ],
    "aggregation_prompt": "prompts/aggregate.md",
}


def _engine_dir(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    shutil.copytree(_FIXTURE, engine)
    return engine


def _write_manifest(engine: Path, completion: object) -> Path:
    payload = dict(_BASE)
    if completion is not None:
        payload["completion_check"] = completion
    manifest = engine / "cw-engine.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _rejection(path: Path) -> tuple[EngineManifestRejection, str | None]:
    with pytest.raises(EngineManifestError) as info:
        load_engine_manifest(path)
    return info.value.reason, info.value.field_path


# ---- model ---------------------------------------------------------------


def test_completion_check_defaults_to_none() -> None:
    assert EngineManifest.model_validate(_BASE).completion_check is None


def test_completion_check_accepts_path_and_default_timeout() -> None:
    manifest = EngineManifest.model_validate(
        {**_BASE, "completion_check": {"path": "bin/check-completion"}}
    )
    assert manifest.completion_check == EngineCompletionCheck(
        path="bin/check-completion",
        timeout_seconds=DEFAULT_COMPLETION_TIMEOUT_SECONDS,
    )


def test_timeout_constants_are_pinned() -> None:
    assert DEFAULT_COMPLETION_TIMEOUT_SECONDS == 300
    assert MIN_COMPLETION_TIMEOUT_SECONDS == 1
    assert MAX_COMPLETION_TIMEOUT_SECONDS == 900


@pytest.mark.parametrize(
    "timeout", [0, 901, True, "5"], ids=["zero", "over", "bool", "str"]
)
def test_timeout_is_strict_and_bounded(timeout: object) -> None:
    with pytest.raises(ValidationError):
        EngineCompletionCheck.model_validate(
            {"path": "bin/x", "timeout_seconds": timeout}
        )


def test_completion_check_forbids_extra_keys_and_empty_path() -> None:
    with pytest.raises(ValidationError):
        EngineCompletionCheck.model_validate({"path": "bin/x", "shell": "sh"})
    with pytest.raises(ValidationError):
        EngineCompletionCheck.model_validate({"path": ""})


# ---- loader --------------------------------------------------------------


def test_valid_checker_path_loads(tmp_path: Path) -> None:
    engine = _engine_dir(tmp_path)
    loaded = load_engine_manifest(
        _write_manifest(engine, {"path": "bin/check-completion"})
    )
    assert loaded.manifest.completion_check is not None
    assert loaded.manifest.completion_check.path == "bin/check-completion"


def test_checker_file_is_never_read_as_text(tmp_path: Path) -> None:
    engine = _engine_dir(tmp_path)
    (engine / "bin" / "binary").write_bytes(b"\xff\xfe\x00 not utf8")
    loaded = load_engine_manifest(_write_manifest(engine, {"path": "bin/binary"}))
    assert loaded.manifest.completion_check is not None


@pytest.mark.parametrize(
    "path",
    ["/etc/hostname", "../outside", "bin/missing", "bin"],
    ids=["absolute", "dotdot", "missing", "directory"],
)
def test_bad_checker_paths_are_path_invalid(tmp_path: Path, path: str) -> None:
    engine = _engine_dir(tmp_path)
    reason, field = _rejection(_write_manifest(engine, {"path": path}))
    assert reason is EngineManifestRejection.PATH_INVALID
    assert field == "completion_check.path"


def test_checker_symlink_escape_is_path_invalid(tmp_path: Path) -> None:
    engine = _engine_dir(tmp_path)
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (engine / "bin" / "escape").symlink_to(outside)
    reason, field = _rejection(_write_manifest(engine, {"path": "bin/escape"}))
    assert reason is EngineManifestRejection.PATH_INVALID
    assert field == "completion_check.path"


def test_checker_symlink_inside_the_skill_dir_is_path_invalid(tmp_path: Path) -> None:
    # The target is a real executable file inside the skill directory, so the
    # path resolves; the runner refuses a final-component symlink, so the loader
    # must too.
    engine = _engine_dir(tmp_path)
    (engine / "bin" / "check-link").symlink_to(engine / "bin" / "check-completion")
    path = _write_manifest(engine, {"path": "bin/check-link"})
    with pytest.raises(EngineManifestError) as info:
        load_engine_manifest(path)
    assert info.value.reason is EngineManifestRejection.PATH_INVALID
    assert info.value.field_path == "completion_check.path"
    assert "symlink" in str(info.value)


# ---- CLI line ------------------------------------------------------------


def test_cli_line_is_unchanged_without_key(tmp_path: Path) -> None:
    engine = _engine_dir(tmp_path)
    line = _validate_manifest(_write_manifest(engine, None))
    assert (
        line
        == "review engine manifest valid: manifest_version=1 backends=codex roles=2"
    )


def test_cli_line_names_the_checker_when_set(tmp_path: Path) -> None:
    engine = _engine_dir(tmp_path)
    line = _validate_manifest(_write_manifest(engine, {"path": "bin/check-completion"}))
    assert line.endswith(" completion_check=bin/check-completion")
    assert line.startswith("review engine manifest valid: manifest_version=1")
