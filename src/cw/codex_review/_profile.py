"""The lean reviewer profile's diagnostics record (#1711).

``run_prepared_roles`` writes one ``codex-review-profile.json`` per pass, beside
``codex-capability.json``. It never gates a verdict and its write never raises.
``instruction_sources`` is a pure helper over the inputs ``_build_reviewer_prompt``
receives, never a parse of the rendered prompt (diff text could forge a heading).
Pass-level inputs come from the keywords of ``prepared.render_prompts``, a
``functools.partial``; any other renderer degrades to the two markers.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.atomic import atomic_write_text
from cw.codex_review._const import _LEAN_DISABLED_FEATURES
from cw.codex_review._context._prompt_text import _codex_output_format_supplement
from cw.config import diagnostics_dir

if TYPE_CHECKING:
    from cw.codex_review._context import _ReviewPassInputs

_log = logging.getLogger(__name__)

PROFILE_VERSION: Final = 1
PROFILE_FILENAME: Final = "codex-review-profile.json"

_MARKERS: Final = ("codex_project_docs=disabled", "codex_user_config=ignored")
# Source labels, in the order of `instruction_sources`' keyword parameters.
_SOURCE_LABELS: Final = (
    "agent_spec",
    "output_supplement",
    "ticket",
    "operator_comments",
    "plan",
    "project_rubrics",
    "repo_policy",
    "lint_grounding",
    "sensitive_files",
    "adjudicated_findings",
    "prior_open_findings",
    "operator_constraints",
    "decided_scope",
)
# Prompt inputs shared by every role, read from the renderer's keywords.
_PASS_INPUTS: Final = (
    "ticket_text",
    "operator_comments_text",
    "plan_text",
    "project_rubrics",
    "lint_grounding",
    "sensitive_hits",
    "adjudicated_findings",
    "prior_open_findings",
    "operator_constraints_text",
    "decided_scope_text",
)


class ReviewProfile(NamedTuple):
    """What one review pass ran under (#1711)."""

    profile_version: int
    cli_version: str | None
    configured_model: str
    reasoning_effort: str | None
    instruction_sources: dict[str, list[str]]
    disabled_features: tuple[str, ...]
    mcp: str
    not_recorded: list[str]


def instruction_sources(
    *,
    agent_spec_text: object,
    supplement: object,
    ticket_text: object,
    operator_comments_text: object,
    plan_text: object,
    project_rubrics: object,
    repo_policy_section: object,
    lint_grounding: object,
    sensitive_hits: object,
    adjudicated_findings: object,
    prior_open_findings: object,
    operator_constraints_text: object,
    decided_scope_text: object,
) -> list[str]:
    """Return the labels of the non-empty prompt inputs, then the two markers.

    Only presence is read, hence ``object``. The drift test pins this signature
    against ``_build_reviewer_prompt``'s, so a new prompt input fails CI here.
    """
    given = (
        agent_spec_text,
        supplement,
        ticket_text,
        operator_comments_text,
        plan_text,
        project_rubrics,
        repo_policy_section,
        lint_grounding,
        sensitive_hits,
        adjudicated_findings,
        prior_open_findings,
        operator_constraints_text,
        decided_scope_text,
    )
    present = [
        label for label, value in zip(_SOURCE_LABELS, given, strict=True) if value
    ]
    return [*present, *_MARKERS]


def _role_sources(prepared: _ReviewPassInputs, role: str) -> list[str]:
    """One role's sources, or just the markers when the renderer is opaque."""
    renderer = prepared.render_prompts
    if not isinstance(renderer, functools.partial):
        return list(_MARKERS)
    keywords = renderer.keywords
    return instruction_sources(
        agent_spec_text=keywords.get("agent_specs", {}).get(role),
        supplement=_codex_output_format_supplement(role),
        repo_policy_section=keywords.get("repo_policy", {}).get(role),
        **{name: keywords.get(name) for name in _PASS_INPUTS},
    )


def build_review_profile(
    prepared: _ReviewPassInputs, *, model: str | None, reasoning_effort: str | None
) -> ReviewProfile:
    """Assemble the record for *prepared*; reads no file and spawns nothing."""
    return ReviewProfile(
        profile_version=PROFILE_VERSION,
        cli_version=prepared.capability.fingerprint.cli_version,
        configured_model=model or "codex-default",
        reasoning_effort=None if reasoning_effort is None else str(reasoning_effort),
        instruction_sources={
            role: _role_sources(prepared, role) for role in prepared.roles
        },
        disabled_features=_LEAN_DISABLED_FEATURES,
        mcp="disabled",
        not_recorded=["effective_model", "enabled_tool_classes"],
    )


def _persist_review_profile(session_id: str, profile: ReviewProfile) -> None:
    """Write *profile* under *session_id*'s diagnostics dir; never raises."""
    try:
        target = diagnostics_dir(session_id)
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            target / PROFILE_FILENAME, json.dumps(profile._asdict(), indent=2)
        )
    except OSError:
        _log.warning("codex review profile write failed for session %s", session_id)
