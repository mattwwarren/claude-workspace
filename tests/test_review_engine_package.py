"""Pins for the ``cw.review_engine`` package surface and import discipline."""

from __future__ import annotations

import subprocess
import sys

import cw.review_engine as package
from cw.review_engine.constants import (
    ENGINE_ARGS_PREFIX,
    ENGINE_PREAMBLE,
    ENGINE_ROLE,
    FRICTION,
    ROUND_ID_RE,
    UNRESOLVABLE_CAUSES,
    EngineRoundFailure,
    EngineRouteRefusalCode,
)

EXPECTED_ALL = {
    "ENGINE_ARGS_PREFIX",
    "ENGINE_PREAMBLE",
    "ENGINE_ROLE",
    "EngineRoundFailure",
    "EngineRouteError",
    "EngineRouteRefusal",
    "EngineRouteRefusalCode",
    "RoutedOutcome",
    "engine_document",
    "engine_finding_to_finding",
    "engine_route",
}
HEAVY = ("context", "skill", "preflight", "materialize", "record", "allow")
LIGHT = ("constants", "mapping", "route")


def test_all_is_pinned_to_exhaustive_set() -> None:
    assert set(package.__all__) == EXPECTED_ALL
    assert len(package.__all__) == len(EXPECTED_ALL)
    for name in package.__all__:
        assert hasattr(package, name)


def test_init_docstring_lists_light_and_heavy_modules() -> None:
    doc = package.__doc__ or ""

    for module in LIGHT:
        assert f"``{module}``" in doc
    for module in HEAVY:
        assert f"``{module}``" in doc


def test_importing_package_does_not_pull_heavy_modules() -> None:
    code = (
        "import sys; import cw.review_engine; "
        "heavy = ['cw.review_engine.' + m for m in "
        f"{HEAVY!r}]; "
        "loaded = [m for m in heavy if m in sys.modules]; "
        "workspace = [m for m in ('cw.review_workspace', "
        "'cw.review_convergence', 'cw.dev_queue') if m in sys.modules]; "
        "print(loaded + workspace); sys.exit(1 if loaded or workspace else 0)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_friction_table_has_exactly_one_entry_per_failure() -> None:
    assert set(FRICTION) == set(EngineRoundFailure)
    assert len(FRICTION) == len(list(EngineRoundFailure))
    assert all(isinstance(text, str) and text for text in FRICTION.values())


def test_failure_and_refusal_vocabularies_are_pinned() -> None:
    assert {f.value for f in EngineRoundFailure} == {
        "engine_summary_invalid",
        "engine_coverage_incomplete",
        "engine_head_mismatch",
        "engine_no_summary",
        "engine_worktree_mutated",
        "engine_unresolvable",
    }
    assert {r.value for r in EngineRouteRefusalCode} == {
        "engine_reject_forbidden",
        "engine_non_deferrable",
        "engine_must_fix_defer_requires_operator",
    }
    assert {
        "skill_not_found",
        "skill_unreadable",
        "materialize_failed",
        "default_branch_unresolved",
    } == UNRESOLVABLE_CAUSES


def test_constants_are_pinned() -> None:
    assert ENGINE_ROLE == "Repo Review Engine"
    assert ENGINE_ARGS_PREFIX == "cw-engine:"
    assert ROUND_ID_RE == r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    assert ENGINE_PREAMBLE == (
        "This is a gate step. The worktree is read-only: do not edit, stage, "
        "commit, push or post anything. Write only the summary file named in the "
        "args line. Do not ask questions; report what you could not verify as "
        "coverage PARTIAL or FAILED."
    )
