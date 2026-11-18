"""Tests for the harness capability declaration model (RFC 0019 H1, #3708)."""

from __future__ import annotations

import ast
import json
import typing
from enum import StrEnum
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

import cw.models.capabilities as capabilities_mod
from cw.cli import main
from cw.models.bindings import Harness
from cw.models.capabilities import (
    BindingProbeKind,
    HarnessCapabilities,
    RepairEntry,
    ToolNarrowing,
    UsageSource,
    in_enum_order,
)
from cw.models.enums import SandboxMode, Stage
from cw.schema import format_tldr

_FIELDS = {
    "schema_version",
    "harness",
    "stages",
    "skills",
    "subagents",
    "binding_probe",
    "sandbox_modes",
    "clean_mode_review",
    "review_engine",
    "repair_entry",
    "tool_narrowing",
    "usage_source",
}


def _valid(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "harness": Harness.LOCAL,
        "stages": frozenset({Stage.IMPL}),
        "skills": False,
        "subagents": False,
        "binding_probe": BindingProbeKind.NONE,
        "sandbox_modes": frozenset(),
        "clean_mode_review": False,
        "review_engine": False,
        "repair_entry": RepairEntry.BLOCKS_ADOPTED,
        "tool_narrowing": ToolNarrowing.NONE,
        "usage_source": UsageSource.UNAVAILABLE,
    }
    data.update(overrides)
    return data


def test_model_fields_are_exactly_the_declared_set() -> None:
    assert set(HarnessCapabilities.model_fields) == _FIELDS


def test_model_config_is_frozen_and_forbids_extra() -> None:
    assert HarnessCapabilities.model_config.get("frozen") is True
    assert HarnessCapabilities.model_config.get("extra") == "forbid"


def test_schema_version_defaults_to_one() -> None:
    assert HarnessCapabilities.model_validate(_valid()).schema_version == 1


def test_no_dict_or_callable_annotations() -> None:
    hints = typing.get_type_hints(HarnessCapabilities)
    for name, hint in hints.items():
        text = repr(hint)
        assert "dict" not in text.lower(), name
        assert "Callable" not in text, name


def test_omitting_any_required_field_raises() -> None:
    for field in _FIELDS - {"schema_version"}:
        data = _valid()
        data.pop(field, None)
        with pytest.raises(ValidationError):
            HarnessCapabilities.model_validate(data)


def test_unknown_extra_key_raises() -> None:
    with pytest.raises(ValidationError):
        HarnessCapabilities.model_validate(_valid(surprise=True))


def test_assignment_raises_because_frozen() -> None:
    caps = HarnessCapabilities.model_validate(_valid())
    field = "skills"
    with pytest.raises(ValidationError):
        setattr(caps, field, True)


def test_hashable() -> None:
    caps = HarnessCapabilities.model_validate(_valid())
    assert hash(caps) == hash(HarnessCapabilities.model_validate(_valid()))


def test_empty_declaration_builds() -> None:
    caps = HarnessCapabilities.model_validate(
        _valid(
            stages=frozenset(),
            binding_probe=BindingProbeKind.NONE,
            repair_entry=RepairEntry.SUPPORTED,
            tool_narrowing=ToolNarrowing.NONE,
            usage_source=UsageSource.UNAVAILABLE,
        )
    )
    assert caps.stages == frozenset()
    assert caps.sandbox_modes == frozenset()


@pytest.mark.parametrize(
    ("enum_cls", "values"),
    [
        (RepairEntry, {"supported", "blocks_adopted", "blocks_any_feedback"}),
        (ToolNarrowing, {"per_tool", "per_server", "none"}),
        (UsageSource, {"signal_stop", "codex_json_events", "unavailable"}),
        (BindingProbeKind, {"enabled_plugins_branch", "backend_mcp_config", "none"}),
    ],
)
def test_closed_enums_have_exactly_these_members(
    enum_cls: type[StrEnum], values: set[str]
) -> None:
    assert {m.value for m in enum_cls} == values


def test_no_probe_state_name_in_module() -> None:
    assert not hasattr(capabilities_mod, "ProbeState")


def test_round_trip_through_json() -> None:
    caps = HarnessCapabilities.model_validate(
        _valid(
            harness=Harness.CODEX,
            stages=frozenset({Stage.PLAN, Stage.IMPL}),
            sandbox_modes=frozenset(
                {SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE}
            ),
        )
    )
    dumped = json.loads(caps.model_dump_json())
    assert set(dumped["stages"]) == {"plan", "impl"}
    again = HarnessCapabilities.model_validate(dumped)
    assert again == caps


def test_in_enum_order_follows_declaration_not_iteration() -> None:
    ordered = in_enum_order(frozenset({Stage.FINALIZE, Stage.PLAN, Stage.IMPL}))
    assert ordered == (Stage.PLAN, Stage.IMPL, Stage.FINALIZE)


def test_in_enum_order_empty_is_empty_tuple() -> None:
    empty: frozenset[Stage] = frozenset()
    assert in_enum_order(empty) == ()


def test_format_tldr_names_every_field_and_no_bare_object() -> None:
    text = format_tldr(HarnessCapabilities)
    for field in _FIELDS:
        assert field in text
    assert "object" not in text
    assert '"plan"' in text
    assert '"impl"' in text


@pytest.mark.parametrize("fmt", ["tldr", "json"])
def test_schema_show_harness_capabilities(fmt: str) -> None:
    result = CliRunner().invoke(
        main, ["schema", "show", "harness-capabilities", "--format", fmt]
    )
    assert result.exit_code == 0, result.output
    assert "stages" in result.output
    if fmt == "json":
        assert "properties" in json.loads(result.output)


def test_module_imports_only_leaf_dependencies() -> None:
    source = Path(capabilities_mod.__file__).read_text(encoding="utf-8")
    allowed = {
        "__future__",
        "collections.abc",
        "enum",
        "typing",
        "pydantic",
        "cw.models.bindings",
        "cw.models.enums",
    }
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module in allowed, node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed, alias.name


def test_not_re_exported_from_cw_models() -> None:
    import cw.models

    assert "HarnessCapabilities" not in cw.models.__all__
