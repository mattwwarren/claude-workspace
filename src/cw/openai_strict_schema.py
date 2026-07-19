"""Transform a pydantic JSON Schema for OpenAI structured-output strict mode (#1364).

``codex exec --output-schema`` runs under OpenAI's structured-output strict
mode, which imposes two constraints a raw ``BaseModel.model_json_schema()``
dump does not satisfy: every object node (including nested ``$defs`` entries)
must set ``additionalProperties: false``, and ``required`` must list every key
in ``properties`` — a field that was previously optional/defaulted has to be
made nullable instead (``anyOf: [<original schema>, {"type": "null"}]``) rather
than omitted from ``required``. Without this transform, every real ``codex
exec`` invocation against ``ReviewerFindingsDocument``'s schema 400s
(``invalid_json_schema``) before any model turn runs.
"""

from __future__ import annotations

import copy
from typing import Any


def to_openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenAI strict-mode-compatible copy of *schema*.

    Does not mutate *schema*. ``_process(result)``'s own recursion (via
    :func:`_walk`) already reaches every ``$defs`` entry, since ``$defs`` is
    itself a value of the top-level node.
    """
    result = copy.deepcopy(schema)
    _process(result)
    return result


def _is_nullable(prop_schema: dict[str, Any]) -> bool:
    """True iff *prop_schema* already accepts ``null`` via an ``anyOf`` branch."""
    branches = prop_schema.get("anyOf")
    if not isinstance(branches, list):
        return False
    return any(
        isinstance(branch, dict) and branch.get("type") == "null" for branch in branches
    )


def _process(node: dict[str, Any]) -> None:
    """Strip ``default``, and on an object node, enforce the strict-mode shape.

    Recurses into every value reachable from *node* via :func:`_walk`, so
    nested schemas (property subschemas, ``anyOf`` branches, etc.) get the same
    treatment regardless of nesting depth.
    """
    node.pop("default", None)
    if node.get("type") == "object" and isinstance(node.get("properties"), dict):
        properties: dict[str, Any] = node["properties"]
        original_required = set(node.get("required") or [])
        for name, sub in list(properties.items()):
            if (
                name not in original_required
                and isinstance(sub, dict)
                and not _is_nullable(sub)
            ):
                properties[name] = {"anyOf": [sub, {"type": "null"}]}
        node["additionalProperties"] = False
        node["required"] = list(properties.keys())
    for value in node.values():
        _walk(value)


def _walk(value: object) -> None:
    """Recurse into *value* if it is a dict or list; no-op otherwise."""
    if isinstance(value, dict):
        _process(value)
    elif isinstance(value, list):
        for item in value:
            _walk(item)
