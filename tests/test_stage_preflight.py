"""Tests for cw.stage_preflight: the generic stage pre-flight (RFC 0019 H2, #3709).

1:1 with the ``cw.stage_preflight`` package, and the home of the assertions
ported from the four deleted guard test modules (``test_executor_clean_review``,
``test_executor_engine_guard``, ``test_executor_finalize_guard`` and
``test_executor_repair_entry``).

The parity table replays every row of
``tests/fixtures/stage_preflight/old_guard_parks.json`` (captured from the old
guard code before it was deleted) through the real executors on the new code:

- a row whose reason did not change matches byte for byte;
- a row under a renamed stage reason now parks ``stage_unsupported`` with the
  same ``retry_eligible`` and ``stage_reached``, its details naming harness and
  stage;
- the local PLAN, REVIEW, FINALIZE and HARDEN rows are the intended #3219
  change: they used to run (or park on clean review) and now park
  ``stage_unsupported``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw import models as cw_models
from cw.auto_dev_result import (
    KNOWN_BLOCKER_REASONS,
    REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON,
    is_known_blocker_reason,
)
from cw.dev_queue.dispositions import (
    REVIEW_ENGINE_UNSUPPORTED_DISPOSITION,
    _hold_aware_disposition,
)
from cw.executor import resolve_executor
from cw.executor._capabilities import (
    CLAUDE_NATIVE_CAPABILITIES,
    CODEX_CAPABILITIES,
    LOCAL_CAPABILITIES,
    OPENCODE_CAPABILITIES,
    declared_capabilities,
)
from cw.executor._preflight import park_if_unmet, spawn_facts
from cw.executor.codex import CodexExecutor
from cw.executor.core import FakeFireAndForgetRunner
from cw.executor.local import LocalExecutor
from cw.executor.opencode import OpencodeExecutor
from cw.local_runner import make_blocked
from cw.models import (
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    ClientConfig,
    SandboxMode,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    TicketTask,
)
from cw.models.bindings import Harness, Role
from cw.models.capabilities import RepairEntry
from cw.models.repair_entry import is_adopted_repair_entry
from cw.opencode_runner import stage_entry_marker
from cw.review_engine.constants import engine_refusal_details
from cw.stage_preflight import (
    LEGACY_REASON_ALIASES,
    STAGE_NEEDS,
    PreflightReason,
    SpawnFacts,
    StageNeeds,
    Unmet,
    canonical_reason,
    first_unmet,
    needs_for,
)
from cw.stage_preflight.details import (
    clean_review_details,
    remediation,
    repair_entry_details,
    stage_unsupported_details,
)
from tests._opencode_helpers import blocked_result
from tests._shared.stage_preflight_builders import (
    client_for,
    engine_client,
    repair_row,
    review_client,
    synthetic_capabilities,
    task_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models.capabilities import HarnessCapabilities

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests" / "fixtures" / "stage_preflight" / "old_guard_parks.json"
_OLD_PARKS: dict[str, dict[str, object]] = json.loads(
    _FIXTURE.read_text(encoding="utf-8")
)
_STAGE_REASON = PreflightReason.STAGE_UNSUPPORTED.value
_ALL_CAPS = (
    CLAUDE_NATIVE_CAPABILITIES,
    CODEX_CAPABILITIES,
    OPENCODE_CAPABILITIES,
    LOCAL_CAPABILITIES,
)
_ENGINE_TEXT = engine_refusal_details(
    "opencode", "backend_unsupported", "skill=repo-review"
)
# Every public backend-name constant in ``cw.models`` (the ``DEFAULT_`` liveness
# probe is not a dispatch backend); a new ``*_BACKEND`` lands here automatically.
_BACKENDS = tuple(
    sorted(
        value
        for name, value in vars(cw_models).items()
        if name.endswith("_BACKEND")
        and not name.startswith("DEFAULT_")
        and isinstance(value, str)
    )
)


def _facts(
    stage: Stage,
    *,
    clean: bool = False,
    engine: str | None = None,
    feedback: bool = False,
    adopted: bool = False,
) -> SpawnFacts:
    return SpawnFacts(
        stage=stage,
        clean_review=clean,
        engine_refusal=engine,
        feedback=feedback,
        adopted=adopted,
    )


# ---------------------------------------------------------------------------
# Parity table: old guard parks replayed through the new executors
# ---------------------------------------------------------------------------


def _spawn(harness: str, stage: Stage, config: str, worktree: Path) -> None:
    """Spawn one row the way the capture did: every availability probe fails."""
    client = client_for(config, worktree)
    task = task_for(config, stage)
    runner = FakeFireAndForgetRunner()
    if harness == "opencode":
        opencode = OpencodeExecutor(
            config=StageExecutorConfig(backend=OPENCODE_BACKEND, model="m"),
            runner=runner,
        )
        with patch("cw.executor.opencode.opencode_available", return_value=False):
            opencode.spawn(stage=stage, task=task, worktree=worktree, client=client)
    elif harness == "local":
        local = LocalExecutor(
            config=StageExecutorConfig(backend=LOCAL_BACKEND, model="m"),
            runner=runner,
        )
        local.spawn(stage=stage, task=task, worktree=worktree, client=client)
    else:
        codex = CodexExecutor(
            config=StageExecutorConfig(backend=CODEX_BACKEND), runner=runner
        )
        with patch("cw.executor.codex.shutil.which", return_value=None):
            codex.spawn(stage=stage, task=task, worktree=worktree, client=client)
    assert runner.calls == []


def _intended_change(harness: str, stage: Stage) -> bool:
    """The #3219 rows: local now parks every stage it does not declare."""
    return harness == "local" and stage is not Stage.IMPL


@pytest.mark.parametrize("key", sorted(_OLD_PARKS))
def test_parity_with_the_old_guard_parks(
    tmp_config_dir: Path, make_git_repo: Callable[[str], Path], key: str
) -> None:
    harness, stage_value, config = key.split("/")
    stage = Stage(stage_value)
    old = _OLD_PARKS[key]
    worktree = make_git_repo(f"wt-parity-{harness}-{stage_value}-{config}")

    _spawn(harness, stage, config, worktree)

    result = blocked_result()
    blocker = result.blocker
    assert blocker is not None
    if _intended_change(harness, stage):
        marker = stage_entry_marker(stage.value)
        assert blocker.reason == _STAGE_REASON
        assert blocker.retry_eligible is (None if stage is Stage.HARDEN else False)
        assert result.stage_reached == marker
        assert blocker.stage == marker
        assert f"harness={harness}" in blocker.details
        assert stage.value in blocker.details
    elif old["reason"] in LEGACY_REASON_ALIASES:
        assert blocker.reason == _STAGE_REASON
        assert blocker.retry_eligible is old["retry_eligible"]
        assert result.stage_reached == old["stage_reached"]
        assert blocker.stage == old["blocker_stage"]
        assert f"harness={harness}" in blocker.details
        assert stage.value in blocker.details
    else:
        assert blocker.reason == old["reason"]
        assert blocker.details == old["details"]
        assert blocker.retry_eligible is old["retry_eligible"]
        assert result.stage_reached == old["stage_reached"]
        assert blocker.stage == old["blocker_stage"]


def test_the_capture_covers_three_harnesses_five_stages_five_configs() -> None:
    assert len(_OLD_PARKS) == 75
    renamed = {k for k, v in _OLD_PARKS.items() if v["reason"] in LEGACY_REASON_ALIASES}
    assert {k.rsplit("/", 1)[0] for k in renamed} == {
        "opencode/harden",
        "opencode/finalize",
        "codex/harden",
    }


# ---------------------------------------------------------------------------
# first_unmet: the pure decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caps", _ALL_CAPS, ids=lambda c: c.harness.value)
@pytest.mark.parametrize("stage", list(Stage), ids=lambda s: s.value)
def test_an_undeclared_stage_is_unsupported_and_a_declared_one_passes(
    caps: HarnessCapabilities, stage: Stage
) -> None:
    unmet = first_unmet(caps, _facts(stage))
    if stage in caps.stages:
        assert unmet is None
        return
    assert unmet is not None
    assert unmet.reason is PreflightReason.STAGE_UNSUPPORTED
    assert unmet.retry_eligible is (None if stage is Stage.HARDEN else False)
    assert f"harness={caps.harness.value}" in unmet.details
    assert f"{stage.value.upper()} spawn" in unmet.details
    assert f"executors.{stage.value}" in unmet.details


def test_claude_native_harden_is_unsupported_at_the_function_level() -> None:
    """R6: the stage check applies to every harness (no executor wiring)."""
    unmet = first_unmet(CLAUDE_NATIVE_CAPABILITIES, _facts(Stage.HARDEN))
    assert unmet is not None
    assert unmet.reason is PreflightReason.STAGE_UNSUPPORTED
    assert unmet.retry_eligible is None


def test_a_clean_review_on_a_harness_without_clean_mode_is_refused() -> None:
    caps = synthetic_capabilities(clean_mode_review=False)
    unmet = first_unmet(caps, _facts(Stage.REVIEW, clean=True))
    assert unmet == Unmet(
        reason=PreflightReason.CLEAN_REVIEW_UNSUPPORTED_BACKEND,
        details=clean_review_details("codex"),
        retry_eligible=False,
    )
    assert first_unmet(caps, _facts(Stage.REVIEW)) is None
    assert first_unmet(caps, _facts(Stage.IMPL, clean=True)) is None


def test_an_engine_review_on_a_harness_without_engines_is_refused() -> None:
    caps = synthetic_capabilities(review_engine=False)
    unmet = first_unmet(caps, _facts(Stage.REVIEW, engine=_ENGINE_TEXT))
    assert unmet == Unmet(
        reason=PreflightReason.REVIEW_ENGINE_UNSUPPORTED_BACKEND,
        details=_ENGINE_TEXT,
        retry_eligible=False,
    )
    assert first_unmet(caps, _facts(Stage.REVIEW)) is None
    assert first_unmet(caps, _facts(Stage.PLAN, engine=_ENGINE_TEXT)) is None


def test_clean_wins_over_engine_when_both_are_unmet() -> None:
    caps = synthetic_capabilities(clean_mode_review=False, review_engine=False)
    unmet = first_unmet(caps, _facts(Stage.REVIEW, clean=True, engine=_ENGINE_TEXT))
    assert unmet is not None
    assert unmet.reason is PreflightReason.CLEAN_REVIEW_UNSUPPORTED_BACKEND


def test_stage_wins_over_clean_engine_and_repair() -> None:
    facts = _facts(Stage.REVIEW, clean=True, engine=_ENGINE_TEXT)
    unmet = first_unmet(LOCAL_CAPABILITIES, facts)
    assert unmet is not None
    assert unmet.reason is PreflightReason.STAGE_UNSUPPORTED
    caps = synthetic_capabilities(
        stages=frozenset({Stage.PLAN}),
        repair_entry=RepairEntry.BLOCKS_ANY_FEEDBACK,
    )
    unmet = first_unmet(caps, _facts(Stage.IMPL, feedback=True, adopted=True))
    assert unmet is not None
    assert unmet.reason is PreflightReason.STAGE_UNSUPPORTED


@pytest.mark.parametrize(
    ("repair_entry", "adopted", "expected_retry"),
    [
        (RepairEntry.BLOCKS_ANY_FEEDBACK, False, None),
        (RepairEntry.BLOCKS_ANY_FEEDBACK, True, None),
        (RepairEntry.BLOCKS_ADOPTED, True, False),
    ],
)
def test_a_feedback_impl_row_is_refused_where_the_harness_blocks_it(
    repair_entry: RepairEntry, adopted: bool, expected_retry: bool | None
) -> None:
    caps = synthetic_capabilities(repair_entry=repair_entry)
    unmet = first_unmet(caps, _facts(Stage.IMPL, feedback=True, adopted=adopted))
    assert unmet == Unmet(
        reason=PreflightReason.REPAIR_ENTRY_UNSUPPORTED_BACKEND,
        details=repair_entry_details("codex"),
        retry_eligible=expected_retry,
    )


@pytest.mark.parametrize(
    "facts",
    [
        _facts(Stage.IMPL, feedback=True, adopted=False),
        _facts(Stage.IMPL),
        _facts(Stage.REVIEW, feedback=True, adopted=True),
        _facts(Stage.PLAN, feedback=True, adopted=True),
    ],
    ids=["feedback-no-branch", "no-feedback", "review", "plan"],
)
def test_blocks_adopted_lets_every_other_row_through(facts: SpawnFacts) -> None:
    caps = synthetic_capabilities(repair_entry=RepairEntry.BLOCKS_ADOPTED)
    assert first_unmet(caps, facts) is None


def test_a_supported_repair_entry_never_parks() -> None:
    facts = _facts(Stage.IMPL, feedback=True, adopted=True)
    assert first_unmet(CODEX_CAPABILITIES, facts) is None
    assert first_unmet(CLAUDE_NATIVE_CAPABILITIES, facts) is None


def test_an_empty_declaration_names_none() -> None:
    details = stage_unsupported_details("x", Stage.PLAN, frozenset())
    assert "(declared: none)" in details
    local = first_unmet(LOCAL_CAPABILITIES, _facts(Stage.PLAN))
    assert local is not None
    assert "(declared: impl)" in local.details


# ---------------------------------------------------------------------------
# StageNeeds: static data, pinned to the declarations
# ---------------------------------------------------------------------------

#: R4: the stages whose prompt reads the tickets binding, per harness.
_TRACKER_MATRIX: dict[Harness, set[Stage]] = {
    Harness.CLAUDE_NATIVE: {Stage.PLAN, Stage.IMPL},
    Harness.CODEX: {Stage.PLAN, Stage.FINALIZE},
    Harness.OPENCODE: {Stage.PLAN, Stage.IMPL},
    Harness.LOCAL: set(),
}


def test_tickets_needs_reproduce_the_tracker_matrix() -> None:
    matrix: dict[Harness, set[Stage]] = {harness: set() for harness in Harness}
    for (harness, stage), needs in STAGE_NEEDS.items():
        if Role.TICKETS in needs.bindings:
            matrix[harness].add(stage)
    assert matrix == _TRACKER_MATRIX


@pytest.mark.parametrize("caps", _ALL_CAPS, ids=lambda c: c.harness.value)
def test_needs_cover_exactly_the_declared_stages(caps: HarnessCapabilities) -> None:
    keyed = {stage for (harness, stage) in STAGE_NEEDS if harness is caps.harness}
    assert keyed == set(caps.stages)


@pytest.mark.parametrize("key", sorted(STAGE_NEEDS), ids=lambda k: f"{k[0]}-{k[1]}")
def test_skill_and_sandbox_needs_can_always_be_met(key: tuple[Harness, Stage]) -> None:
    """R4: no park for skills or write_sandbox, because none can be unmet."""
    caps = declared_capabilities(key[0].value)
    assert caps is not None
    needs = STAGE_NEEDS[key]
    if needs.skills:
        assert caps.skills
    if needs.write_sandbox:
        assert key[1] is Stage.IMPL
        assert not caps.sandbox_modes or SandboxMode.WORKSPACE_WRITE in (
            caps.sandbox_modes
        )


def test_needs_for_an_undeclared_pair_is_empty() -> None:
    assert needs_for(Harness.LOCAL, Stage.PLAN) == StageNeeds(
        bindings=frozenset(), skills=False, write_sandbox=False
    )
    assert (
        needs_for(Harness.CODEX, Stage.PLAN) is STAGE_NEEDS[(Harness.CODEX, Stage.PLAN)]
    )


# ---------------------------------------------------------------------------
# Reasons: the closed enum and the legacy aliases
# ---------------------------------------------------------------------------

_LEGACY = (
    "finalize_unsupported_backend",
    "codex_review_only",
    *(f"opencode_{stage.value}_not_implemented" for stage in Stage),
)


@pytest.mark.parametrize("old", _LEGACY)
def test_every_old_stage_reason_canonicalizes_to_stage_unsupported(old: str) -> None:
    assert canonical_reason(old) == _STAGE_REASON
    assert is_known_blocker_reason(old)


#: RFC 0019 H2b (#3744): the tracker-MCP gate's disposition and skip reason.
_BINDING_LEGACY = {
    "tracker_mcp_gate": PreflightReason.BINDING_UNREACHABLE.value,
    "tracker_mcp_gate_blocked": "binding_blocked",
}


def test_the_alias_table_is_the_old_stage_and_binding_reasons() -> None:
    assert sorted(LEGACY_REASON_ALIASES) == sorted([*_LEGACY, *_BINDING_LEGACY])
    assert isinstance(LEGACY_REASON_ALIASES, MappingProxyType)


@pytest.mark.parametrize(("old", "new"), sorted(_BINDING_LEGACY.items()))
def test_every_old_binding_string_canonicalizes_to_its_new_name(
    old: str, new: str
) -> None:
    assert canonical_reason(old) == new
    assert is_known_blocker_reason(old)


@pytest.mark.parametrize(
    "reason",
    [*(r.value for r in PreflightReason), "plan_missing", "not_a_reason", ""],
)
def test_canonical_reason_is_identity_on_new_and_unknown_strings(reason: str) -> None:
    assert canonical_reason(reason) == reason
    assert canonical_reason(canonical_reason(reason)) == canonical_reason(reason)


@pytest.mark.parametrize("reason", list(PreflightReason), ids=lambda r: r.value)
def test_every_preflight_reason_is_a_known_blocker_reason(
    reason: PreflightReason,
) -> None:
    assert reason.value in KNOWN_BLOCKER_REASONS


def test_the_old_finalize_string_left_the_registry() -> None:
    """Writers emit new names only; the alias keeps old rows known."""
    assert "finalize_unsupported_backend" not in KNOWN_BLOCKER_REASONS
    assert not is_known_blocker_reason("opencode_bogus_not_implemented")


def test_the_engine_reason_constant_is_in_lockstep() -> None:
    assert (
        PreflightReason.REVIEW_ENGINE_UNSUPPORTED_BACKEND.value
        == REVIEW_ENGINE_UNSUPPORTED_BACKEND_BLOCKER_REASON
    )


# ---------------------------------------------------------------------------
# Disposition pins: no new disposition (A2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [_STAGE_REASON, *_LEGACY])
def test_a_stage_park_keeps_the_blocked_disposition(reason: str) -> None:
    assert _hold_aware_disposition("blocked", reason) == "blocked"


def test_the_engine_park_keeps_its_dedicated_disposition() -> None:
    assert (
        _hold_aware_disposition(
            "blocked", PreflightReason.REVIEW_ENGINE_UNSUPPORTED_BACKEND.value
        )
        == REVIEW_ENGINE_UNSUPPORTED_DISPOSITION
    )


# ---------------------------------------------------------------------------
# Details wording (ported from the deleted guard tests)
# ---------------------------------------------------------------------------


def test_clean_review_details_name_the_backend_and_the_ways_out() -> None:
    details = clean_review_details(LOCAL_BACKEND)
    assert f"backend={LOCAL_BACKEND}" in details
    assert "review.convergence: clean" in details
    assert "writes no convergence ledger" in details
    assert "review.convergence: blocking" in details
    assert "claude-native" in details
    assert "codex" in details
    assert "cw dev-queue requeue" in details
    assert "will not clear" in details
    assert "#2806" not in details
    assert "wait" not in details


def test_remediation_takes_only_the_client_reference() -> None:
    assert list(inspect.signature(remediation).parameters) == ["client_ref"]
    text = remediation("client acme")
    assert "client acme" in text
    assert "#2806" not in text
    assert "wait" not in text


@pytest.mark.parametrize("backend", [LOCAL_BACKEND, OPENCODE_BACKEND])
def test_repair_details_name_the_backend_and_the_decision(backend: str) -> None:
    details = repair_entry_details(backend)
    assert f"backend={backend}" in details
    assert "#2983" in details


def test_engine_refusal_is_worded_by_the_review_engine_constants(
    tmp_path: Path,
) -> None:
    facts = spawn_facts(
        OPENCODE_CAPABILITIES,
        task=task_for("blocking", Stage.REVIEW),
        client=engine_client(tmp_path),
        stage=Stage.REVIEW,
    )
    assert facts.engine_refusal == _ENGINE_TEXT
    assert "review engine refused on opencode lane" in _ENGINE_TEXT
    assert "cause=backend_unsupported" in _ENGINE_TEXT
    assert "skill=repo-review" in _ENGINE_TEXT


def test_spawn_facts_without_an_engine_carry_no_refusal(tmp_path: Path) -> None:
    facts = spawn_facts(
        LOCAL_CAPABILITIES,
        task=repair_row(Stage.IMPL),
        client=review_client(tmp_path, "clean"),
        stage=Stage.IMPL,
    )
    assert facts == SpawnFacts(
        stage=Stage.IMPL,
        clean_review=True,
        engine_refusal=None,
        feedback=True,
        adopted=True,
    )


# ---------------------------------------------------------------------------
# is_adopted_repair_entry (moved to cw.models.repair_entry)
# ---------------------------------------------------------------------------


def test_an_adopted_repair_row_needs_both_feedback_and_a_branch() -> None:
    assert is_adopted_repair_entry(repair_row(Stage.IMPL)) is True
    assert is_adopted_repair_entry(repair_row(Stage.IMPL, branch=None)) is False
    assert is_adopted_repair_entry(repair_row(Stage.IMPL, feedback=False)) is False


# ---------------------------------------------------------------------------
# Executor wiring: the pre-flight runs before any binary or endpoint probe
# ---------------------------------------------------------------------------


def test_park_if_unmet_lets_a_supported_spawn_through(tmp_path: Path) -> None:
    assert (
        park_if_unmet(
            LOCAL_CAPABILITIES,
            task=task_for("blocking", Stage.IMPL),
            client=review_client(tmp_path, "blocking"),
            stage=Stage.IMPL,
            worktree=tmp_path,
            blocked=make_blocked,
        )
        is None
    )


@pytest.mark.parametrize("binary_available", [True, False])
def test_opencode_finalize_parks_before_the_binary_probe(
    binary_available: bool,
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
) -> None:
    worktree = make_git_repo("wt-opencode-finalize-preflight")
    runner = FakeFireAndForgetRunner()
    executor = OpencodeExecutor(
        config=StageExecutorConfig(backend=OPENCODE_BACKEND, model="m"),
        runner=runner,
    )
    with patch(
        "cw.executor.opencode.opencode_available", return_value=binary_available
    ):
        executor.spawn(
            stage=Stage.FINALIZE,
            task=task_for("blocking", Stage.FINALIZE),
            worktree=worktree,
            client=ClientConfig(name="test", workspace_path=worktree),
        )
    result = blocked_result()
    assert runner.calls == []
    assert result.blocker is not None
    assert result.blocker.reason == _STAGE_REASON
    assert result.blocker.retry_eligible is False
    assert result.stage_reached == "stage4a_merge_gate"
    assert "harness=opencode" in result.blocker.details


def test_opencode_feedback_row_parks_without_probing_the_binary(
    tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
) -> None:
    """``opencode_available`` is not patched: the pre-flight decides first."""
    worktree = make_git_repo("wt-opencode-feedback-preflight")
    runner = FakeFireAndForgetRunner()
    executor = OpencodeExecutor(
        config=StageExecutorConfig(backend=OPENCODE_BACKEND, model="m"),
        runner=runner,
    )
    executor.spawn(
        stage=Stage.IMPL,
        task=repair_row(Stage.IMPL, branch=None),
        worktree=worktree,
        client=ClientConfig(name="test", workspace_path=worktree),
    )
    result = blocked_result()
    assert runner.calls == []
    assert result.blocker is not None
    assert result.blocker.reason == PreflightReason.REPAIR_ENTRY_UNSUPPORTED_BACKEND
    assert result.blocker.retry_eligible is None
    assert result.stage_reached == "stage2_impl"


@pytest.mark.parametrize(
    ("stage", "config", "reason"),
    [
        (Stage.PLAN, "blocking", _STAGE_REASON),
        (Stage.REVIEW, "clean", _STAGE_REASON),
        (Stage.IMPL, "adopted", "repair_entry_unsupported_backend"),
    ],
)
def test_local_parks_before_the_endpoint_aider_and_plan_checks(
    tmp_config_dir: Path,
    make_git_repo: Callable[[str], Path],
    stage: Stage,
    config: str,
    reason: str,
) -> None:
    """No endpoint, no aider, no plan: the pre-flight still decides first."""
    worktree = make_git_repo(f"wt-local-preflight-{stage.value}")
    runner = FakeFireAndForgetRunner()
    executor = LocalExecutor(
        config=StageExecutorConfig(backend=LOCAL_BACKEND, model="m"), runner=runner
    )
    with patch("cw.executor.local.aider_available", return_value=False):
        executor.spawn(
            stage=stage,
            task=task_for(config, stage),
            worktree=worktree,
            client=client_for(config, worktree),
        )
    result = blocked_result()
    assert runner.calls == []
    assert result.blocker is not None
    assert result.blocker.reason == reason
    assert result.blocker.retry_eligible is False
    assert result.stage_reached == stage_entry_marker(stage.value)


def test_codex_harden_parks_before_the_binary_probe(
    tmp_config_dir: Path, make_git_repo: Callable[[str], Path]
) -> None:
    worktree = make_git_repo("wt-codex-harden-preflight")
    runner = FakeFireAndForgetRunner()
    executor = CodexExecutor(
        config=StageExecutorConfig(backend=CODEX_BACKEND), runner=runner
    )
    with patch("cw.executor.codex.shutil.which", return_value="/usr/bin/codex"):
        executor.spawn(
            stage=Stage.HARDEN,
            task=task_for("blocking", Stage.HARDEN),
            worktree=worktree,
            client=ClientConfig(name="test", workspace_path=worktree),
        )
    result = blocked_result()
    assert runner.calls == []
    assert result.blocker is not None
    assert result.blocker.reason == _STAGE_REASON
    assert result.blocker.retry_eligible is None
    assert result.stage_reached == "stage4a_merge_gate"
    assert "harness=codex" in result.blocker.details


# ---------------------------------------------------------------------------
# Every dispatchable backend has a declaration the pre-flight can read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", _BACKENDS)
def test_every_dispatchable_backend_is_declared(tmp_path: Path, backend: str) -> None:
    """A new ``*_BACKEND`` constant fails here until it declares capabilities."""
    assert declared_capabilities(backend) is not None
    client = ClientConfig(
        name="c",
        workspace_path=tmp_path,
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(backend=backend)}
        ),
    )
    resolve_executor(
        TicketTask(ticket_id="T-1", client="c", stage=Stage.REVIEW), client
    )


# ---------------------------------------------------------------------------
# Docs conformance
# ---------------------------------------------------------------------------


def _unreleased_changelog() -> str:
    text = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def test_the_headless_contract_documents_the_new_reason_and_the_aliases() -> None:
    contract = (_ROOT / "docs" / "headless-contract.md").read_text(encoding="utf-8")
    assert "| `stage_unsupported` |" in contract
    assert "| `repair_entry_unsupported_backend` |" in contract
    assert "| `clean_review_unsupported_backend` |" in contract
    assert "| `finalize_unsupported_backend` | **" not in contract
    start = contract.index("Old to new reason aliases")
    table = contract[start : contract.index("\n\n", contract.index("|", start))]
    for old in _LEGACY:
        assert f"`{old}`" in table


def test_the_changelog_names_every_renamed_reason() -> None:
    unreleased = _unreleased_changelog()
    assert "`stage_unsupported`" in unreleased
    for old in _LEGACY:
        assert f"`{old}`" in unreleased
