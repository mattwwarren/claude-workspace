"""Schema inspection utilities for cw Pydantic models.

Registry and formatters for exposing AutoDevResult, TicketTask, and Session
schemas to downstream consumers (skill bodies, CI validators, operators).

Canonical read surface for the auto-dev sentinel contract and related models.
See: GitHub issue #313.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import AutoDevResult
from cw.models import Session, TicketTask

if TYPE_CHECKING:
    from pydantic import BaseModel

REGISTRY: dict[str, type[BaseModel]] = {
    "auto-dev-result": AutoDevResult,
    "ticket-task": TicketTask,
    "session": Session,
}

# Fields absent from model_json_schema() `required` arrays but enforced at
# runtime by AutoDevResult._check_invariants. Source of truth is the validator
# logic (auto_dev_result.py:444): `exited_pre_impl = stage_reached in
# ("stage1_plan", "stage1_pre_flight")`, enforced as non-null when not pre-impl.
_CONDITIONAL_FIELDS: dict[str, str] = {
    "AutoDevResult.scope.tier": (
        "required when stage_reached not in {stage1_plan, stage1_pre_flight}"
    ),
    "AutoDevResult.scope.lines_actual": (
        "required when stage_reached not in {stage1_plan, stage1_pre_flight}"
    ),
    "AutoDevResult.health.lowest_agent_confidence": (
        "required when stage_reached not in {stage1_plan, stage1_pre_flight}"
    ),
}

# Cross-field invariants from AutoDevResult._check_invariants.
# Not derivable from model_json_schema() — validators are invisible to Pydantic
# schema generation. This prose is the canonical operator/skill reference.
_AUTODEVRESULT_CONSTRAINTS = (
    "Constraints (from _check_invariants cross-field validators):\n"
    '  - pr: non-null iff status == "shipped"\n'
    '  - blocker: non-null iff status == "blocked"\n'
    '  - next_actions contains "wait_for_ci" iff status == "shipped"\n'
    "  - scope.tier: required when stage_reached not in"
    " {stage1_plan, stage1_pre_flight}\n"
    "  - scope.lines_actual: required when stage_reached not in"
    " {stage1_plan, stage1_pre_flight}\n"
    "  - health.lowest_agent_confidence: required when stage_reached not in"
    " {stage1_plan, stage1_pre_flight}\n"
    "  - branch: null when status in pre-branch statuses"
    " (plan_pending_approval, etc.)\n"
    '  - health.downgrade_applied: True requires status == "review_pending_approval"\n'
    '  - schema_version: v2-introduced statuses (e.g., "no_op")'
    " require schema_version >= 2"
)


def format_json(model_cls: type[BaseModel]) -> str:
    """Return raw model_json_schema() as indented JSON. No envelope added."""
    return json.dumps(model_cls.model_json_schema(), indent=2)


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    """Resolve a $ref like '#/$defs/Status' against schema $defs."""
    key = ref.rsplit("/", maxsplit=1)[-1]
    result: dict[str, Any] = defs.get(key, {})
    return result


def _primitive_type_str(field_schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """Render the plain ``type`` keyword (array/null/scalar) of a field schema."""
    t = field_schema.get("type", "")
    if t == "array":
        items = field_schema.get("items", {})
        return f"list[{_type_str(items, defs)}]"
    if t == "null":
        return "null"
    return t or "object"


def _type_str(field_schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """Extract a concise, human-readable type string from a field schema."""
    if "$ref" in field_schema:
        resolved = _resolve_ref(field_schema["$ref"], defs)
        return _type_str(resolved, defs)
    if "anyOf" in field_schema:
        parts = [_type_str(s, defs) for s in field_schema["anyOf"]]
        return " | ".join(p for p in parts if p)
    if "enum" in field_schema:
        return " | ".join(
            f'"{v}"' if isinstance(v, str) else str(v) for v in field_schema["enum"]
        )
    if "const" in field_schema:
        return repr(field_schema["const"])
    return _primitive_type_str(field_schema, defs)


def _render_submodel(
    model_name: str,
    field_name: str,
    ref_def: dict[str, Any],
    defs: dict[str, Any],
    indent: str = "    ",
) -> list[str]:
    """Render depth-1 sub-model fields as indented lines."""
    if ref_def.get("type") != "object" or "properties" not in ref_def:
        return []
    sub_req = set(ref_def.get("required", []))
    lines = []
    for sf, ss in ref_def["properties"].items():
        cond_key = f"{model_name}.{field_name}.{sf}"
        cond = _CONDITIONAL_FIELDS.get(cond_key, "")
        cond_suffix = f"  ({cond})" if cond else ""
        opt_marker = "" if sf in sub_req else "[opt] "
        type_s = _type_str(ss, defs)
        lines.append(f"{indent}{opt_marker}{sf}: {type_s}{cond_suffix}")
    return lines


def format_tldr(model_cls: type[BaseModel]) -> str:
    """Concise field summary for human/skill consumption.

    - Depth-1 recursion into direct sub-model fields.
    - Conditionally-required fields annotated with condition text.
    - Source of truth: model_json_schema() (Pydantic v2). NOT __fields__.
    - Includes Constraints section for AutoDevResult cross-field invariants.
    """
    schema = model_cls.model_json_schema()
    defs = schema.get("$defs", {})
    required_set = set(schema.get("required", []))
    props = schema.get("properties", {})
    model_name = model_cls.__name__

    required_lines: list[str] = []
    optional_lines: list[str] = []

    for fname, fschema in props.items():
        type_s = _type_str(fschema, defs)
        is_req = fname in required_set

        # Attempt depth-1 sub-model resolution
        ref_def: dict[str, Any] | None = None
        if "$ref" in fschema:
            ref_def = _resolve_ref(fschema["$ref"], defs)
        elif "anyOf" in fschema:
            for part in fschema["anyOf"]:
                if "$ref" in part:
                    ref_def = _resolve_ref(part["$ref"], defs)
                    break

        sub = (
            _render_submodel(model_name, fname, ref_def, defs)
            if ref_def is not None
            else []
        )

        entry = "\n".join([f"  {fname}: {type_s}", *sub])

        if is_req:
            required_lines.append(entry)
        else:
            optional_lines.append(entry)

    out: list[str] = [model_name, "", "Required:"]
    out.extend(required_lines or ["  (none)"])
    out.extend(["", "Optional:"])
    out.extend(optional_lines or ["  (none)"])

    if model_name == "AutoDevResult":
        out.extend(["", _AUTODEVRESULT_CONSTRAINTS])

    return "\n".join(out)
