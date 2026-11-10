"""Tests for the ``EngineManifest`` model (#3216, RFC 0018 A4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cw.models import CODEX_BACKEND
from cw.models import review_engine_manifest as model_mod
from cw.models.review_engine_manifest import (
    ENGINE_MANIFEST_VERSION,
    ENGINE_ROLE_ID_RE,
    MAX_ENGINE_MANIFEST_BYTES,
    MAX_ENGINE_ROLES,
    MAX_ENGINE_SPEC_BYTES,
    EngineManifest,
    EngineRole,
)
from cw.schema import REGISTRY


def manifest_payload(**overrides: object) -> dict[str, object]:
    """A valid manifest object; *overrides* replace top-level keys."""
    payload: dict[str, object] = {
        "manifest_version": 1,
        "backends": ["codex"],
        "roles": [
            {"id": "security", "agent_spec": "agents/security.md"},
            {"id": "quality", "agent_spec": "agents/quality.md"},
        ],
        "aggregation_prompt": "prompts/aggregate.md",
    }
    payload.update(overrides)
    return payload


def _error_locs(payload: dict[str, object]) -> list[tuple[str | int, ...]]:
    with pytest.raises(ValidationError) as excinfo:
        EngineManifest.model_validate(payload)
    return [error["loc"] for error in excinfo.value.errors()]


def test_valid_manifest_round_trips() -> None:
    manifest = EngineManifest.model_validate(manifest_payload())

    assert manifest.manifest_version == 1
    assert manifest.backends == ["codex"]
    assert [role.id for role in manifest.roles] == ["security", "quality"]
    assert manifest.aggregation_prompt == "prompts/aggregate.md"


def test_constants_are_pinned() -> None:
    assert ENGINE_MANIFEST_VERSION == 1
    assert MAX_ENGINE_MANIFEST_BYTES == 262_144
    assert MAX_ENGINE_ROLES == 8
    assert MAX_ENGINE_SPEC_BYTES == 65_536
    assert ENGINE_ROLE_ID_RE == r"^[a-z0-9][a-z0-9-]*$"


def test_codex_literal_matches_the_backend_constant() -> None:
    assert model_mod._CODEX == CODEX_BACKEND


def test_model_module_imports_no_cw_module() -> None:
    source = Path(model_mod.__file__).read_text(encoding="utf-8")
    assert "from cw" not in source
    assert "import cw" not in source


@pytest.mark.parametrize("value", [True, "1", 1.0, 2, 0, None])
def test_manifest_version_must_be_exactly_the_int_one(value: object) -> None:
    locs = _error_locs(manifest_payload(manifest_version=value))

    assert ("manifest_version",) in locs


def test_manifest_version_missing() -> None:
    payload = manifest_payload()
    del payload["manifest_version"]

    assert ("manifest_version",) in _error_locs(payload)


def test_schema_version_is_an_unknown_key() -> None:
    locs = _error_locs(manifest_payload(schema_version=1))

    assert ("schema_version",) in locs


def test_extra_key_forbidden_at_role_level() -> None:
    roles = [{"id": "security", "agent_spec": "a.md", "model": "x"}]

    assert ("roles", 0, "model") in _error_locs(manifest_payload(roles=roles))


@pytest.mark.parametrize("missing", ["roles", "backends", "aggregation_prompt"])
def test_required_top_level_keys(missing: str) -> None:
    payload = manifest_payload()
    del payload[missing]

    assert (missing,) in _error_locs(payload)


def test_roles_empty() -> None:
    assert ("roles",) in _error_locs(manifest_payload(roles=[]))


def test_roles_over_cap() -> None:
    roles = [{"id": f"r{i}", "agent_spec": "a.md"} for i in range(MAX_ENGINE_ROLES + 1)]

    assert ("roles",) in _error_locs(manifest_payload(roles=roles))


def test_roles_at_cap_is_valid() -> None:
    roles = [{"id": f"r{i}", "agent_spec": "a.md"} for i in range(MAX_ENGINE_ROLES)]

    assert len(EngineManifest.model_validate(manifest_payload(roles=roles)).roles) == 8


@pytest.mark.parametrize("role_id", ["Security", "-security", "sec_urity", "", "a b"])
def test_bad_role_id(role_id: str) -> None:
    roles = [{"id": role_id, "agent_spec": "a.md"}]

    assert ("roles", 0, "id") in _error_locs(manifest_payload(roles=roles))


def test_role_missing_agent_spec() -> None:
    assert ("roles", 0, "agent_spec") in _error_locs(
        manifest_payload(roles=[{"id": "security"}])
    )


def test_role_agent_spec_must_be_a_non_empty_string() -> None:
    assert ("roles", 0, "agent_spec") in _error_locs(
        manifest_payload(roles=[{"id": "security", "agent_spec": ""}])
    )
    assert ("roles", 0, "agent_spec") in _error_locs(
        manifest_payload(roles=[{"id": "security", "agent_spec": 3}])
    )


def test_aggregation_prompt_must_be_a_non_empty_string() -> None:
    assert ("aggregation_prompt",) in _error_locs(
        manifest_payload(aggregation_prompt="")
    )


@pytest.mark.parametrize(
    ("backends", "loc"),
    [
        ([], ("backends",)),
        (["opencode"], ("backends", 0)),
        (["claude-native"], ("backends", 0)),
        (["codex", "opencode"], ("backends", 1)),
        ([1], ("backends", 0)),
        ("codex", ("backends",)),
    ],
)
def test_backends_closed_to_codex(backends: object, loc: tuple[str | int, ...]) -> None:
    assert loc in _error_locs(manifest_payload(backends=backends))


def test_backend_rejection_does_not_echo_the_value() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EngineManifest.model_validate(manifest_payload(backends=["SENTINEL-BACKEND"]))

    assert "SENTINEL-BACKEND" not in excinfo.value.errors(include_input=False)[0]["msg"]


def test_role_model_is_strict() -> None:
    with pytest.raises(ValidationError):
        EngineRole.model_validate({"id": 1, "agent_spec": "a.md"})


def test_json_schema_pins_the_codex_enum() -> None:
    schema = EngineManifest.model_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["backends"]["items"]["enum"] == ["codex"]
    assert schema["properties"]["backends"]["minItems"] == 1
    assert schema["properties"]["roles"]["maxItems"] == MAX_ENGINE_ROLES
    assert "EngineRole" in schema["$defs"]


def test_registered_as_review_engine_manifest() -> None:
    assert REGISTRY["review-engine-manifest"] is EngineManifest


def test_stub_fixture_manifest_is_valid() -> None:
    stub = Path(__file__).parent / "fixtures" / "review_engine_stub" / "cw-engine.json"

    manifest = EngineManifest.model_validate(json.loads(stub.read_text("utf-8")))

    assert [role.id for role in manifest.roles] == ["security", "quality"]
