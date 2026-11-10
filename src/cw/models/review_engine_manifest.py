"""``EngineManifest``: the cw-owned contract for a codex-capable review engine.

A repo's own review skill may commit a ``cw-engine.json`` beside its
``SKILL.md`` (RFC 0018 A4, #3216). It names the reviewer roles cw runs as
separate codex turns, each with an agent-spec file, and the prompt for the one
aggregation turn that folds the role documents into the engine summary. cw
reads the manifest from the default branch and validates it strictly; it never
writes one.

Every model is ``extra="forbid"`` and ``strict=True`` (no ``true`` or ``"1"``
coerced to an int). ``cw schema show review-engine-manifest`` prints its JSON
schema. ``manifest_version`` is checked exactly, with no look-ahead.

The ``agent_spec`` and ``aggregation_prompt`` values are paths relative to the
manifest's directory. Their rules (no absolute path, no ``..``, no escape by
symlink, a non-blank UTF-8 regular file under ``MAX_ENGINE_SPEC_BYTES``) and the
role-slug uniqueness rule need the filesystem, so ``cw.review_engine_manifest_io``
enforces them, not this model.

Pure pydantic with no ``cw`` import. Imported by submodule path and not
re-exported from ``cw.models``, as ``cw.models.review_engine_summary`` is not.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from .orchestrator_config.constants import CODEX_BACKEND

ENGINE_MANIFEST_VERSION = 1

#: Raw-byte cap on the manifest file, checked before decoding.
MAX_ENGINE_MANIFEST_BYTES = 262_144
MAX_ENGINE_ROLES = 8
#: Byte cap on each agent-spec file and on the aggregation prompt file.
MAX_ENGINE_SPEC_BYTES = 65_536

ENGINE_ROLE_ID_RE = r"^[a-z0-9][a-z0-9-]*$"

# Kept as a private compatibility alias for the model tests; the backend
# identifier itself is owned by the dependency-free configuration constants.
_CODEX: Final = CODEX_BACKEND


def _check_codex(value: str) -> str:
    """Accept only the codex backend; never echo the rejected value."""
    if value != _CODEX:
        msg = f"backend must be {CODEX_BACKEND!r}"
        raise ValueError(msg)
    return value


_Backend = Annotated[str, AfterValidator(_check_codex)]


class EngineRole(BaseModel):
    """One reviewer role: its id and the agent-spec file that briefs it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(
        pattern=ENGINE_ROLE_ID_RE,
        description=(
            "Lowercase role identifier (letters, digits, hyphens), unique after "
            "slug normalization."
        ),
    )
    agent_spec: str = Field(
        min_length=1,
        description=(
            "Path to the role's agent-spec file, relative to the manifest's "
            "directory; no absolute path, no `..`, no symlink escape."
        ),
    )


class EngineManifest(BaseModel):
    """The ``cw-engine.json`` a codex-capable review engine commits."""

    model_config = ConfigDict(extra="forbid", strict=True)

    manifest_version: int = Field(
        description="Manifest schema version; must equal the version cw supports."
    )
    backends: list[_Backend] = Field(
        min_length=1,
        json_schema_extra={"items": {"type": "string", "enum": [CODEX_BACKEND]}},
        description="Review backends the engine supports; only `codex` today.",
    )
    roles: list[EngineRole] = Field(
        min_length=1,
        max_length=MAX_ENGINE_ROLES,
        description="Reviewer roles cw runs as separate codex turns.",
    )
    aggregation_prompt: str = Field(
        min_length=1,
        description=(
            "Path to the aggregation-turn prompt file, relative to the "
            "manifest's directory."
        ),
    )

    @field_validator("manifest_version")
    @classmethod
    def _check_manifest_version(cls, value: int) -> int:
        if value != ENGINE_MANIFEST_VERSION:
            msg = (
                f"manifest_version {value} is not supported "
                f"(expected {ENGINE_MANIFEST_VERSION})"
            )
            raise ValueError(msg)
        return value
