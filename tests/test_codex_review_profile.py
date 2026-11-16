"""Tests for cw.codex_review._profile, the lean reviewer profile record (#1711).

Diagnostics only: the record never gates a verdict, so every failure is soft.
``instruction_sources`` is a pure helper over the inputs ``_build_reviewer_prompt``
receives (never a parse of the rendered prompt, whose diff text could forge
headings), pinned by a drift test per input.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cw.codex_review._profile import (
    PROFILE_FILENAME,
    ReviewProfile,
    _persist_review_profile,
    build_review_profile,
    instruction_sources,
)

import cw
from cw.codex_review import _prepare_review_pass, run_codex_roles
from cw.codex_review import _profile as profile_module
from cw.codex_review._context import _ReviewPassInputs
from cw.codex_review._context._prompt_render import _build_reviewer_prompt
from cw.codex_review._context._prompt_text import (
    _CODEX_TONE_GUIDE_SUPPLEMENT,
    _CONSTRAINTS_HEADER,
    _DECIDED_SCOPE_HEADER,
)
from cw.codex_review._context._sensitive_files import _SensitiveHit
from cw.codex_review.core import run_prepared_roles
from cw.codex_runner import FakeCodexRunner
from cw.config import diagnostics_dir
from cw.models.enums import ReasoningEffort
from tests._codex_review_helpers import (
    _all_ok,
    _large_diff_repo,
    _ok_result,
    _seed_default_branch_config,
    _SequencedRunner,
    _sharded_setup,
    _task,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.review_builders import _make_diff, _make_finding
from tests.test_codex_review_const import _LEAN_FEATURES
from tests.test_codex_review_context import _disposition_ledger

if TYPE_CHECKING:
    from collections.abc import Callable

_ROLE = "Code Quality Reviewer"
_MARKERS = ["codex_project_docs=disabled", "codex_user_config=ignored"]
# Prompt inputs with no instruction text: everything else must be mapped.
_EXCLUDED = {
    "role",
    "diff",
    "changed_files",
    "capable",
    "delta_mode",
    "backend",
    "branch_context",
    "pending_operator_comment",
}
# helper keyword -> (source label, heading the real prompt carries or None, value)
_SOURCES: dict[str, tuple[str, str | None, object]] = {
    "agent_spec_text": ("agent_spec", None, "SPEC"),
    "supplement": ("output_supplement", None, _CODEX_TONE_GUIDE_SUPPLEMENT),
    "ticket_text": ("ticket", "## Ticket Context", "T"),
    "operator_comments_text": ("operator_comments", "## Ticket Comments", "C"),
    "plan_text": ("plan", "## Approved Plan", "P"),
    "project_rubrics": ("project_rubrics", "## Project Rubrics", "R"),
    "repo_policy_section": ("repo_policy", f"## Repo Policy for {_ROLE}", "RP"),
    "lint_grounding": ("lint_grounding", "## Repo Lint Configuration", "L"),
    "sensitive_hits": (
        "sensitive_files",
        "Touched sensitive files:",
        [_SensitiveHit("pyproject.toml", "build", "packaging")],
    ),
    "adjudicated_findings": (
        "adjudicated_findings",
        "## Previously Adjudicated Findings",
        _disposition_ledger(),
    ),
    "prior_open_findings": (
        "prior_open_findings",
        "## Unresolved Prior Findings",
        [_make_finding(summary="STILL OPEN")],
    ),
    "operator_constraints_text": ("operator_constraints", _CONSTRAINTS_HEADER, "OC"),
    "decided_scope_text": ("decided_scope", _DECIDED_SCOPE_HEADER, "DS"),
}


def _inputs(*, off: str | None = None) -> dict[str, object]:
    """Every input populated, except *off* which is emptied."""
    return {
        name: type(value)() if name == off else value
        for name, (_, _, value) in _SOURCES.items()
    }


def _prompt(inputs: dict[str, object]) -> str:
    kwargs = {k: v for k, v in inputs.items() if k != "supplement"}
    return _build_reviewer_prompt(
        _ROLE, diff=_make_diff("x = 1"), changed_files=["x.py"], **kwargs
    )


class TestInstructionSources:
    def test_all_present_then_all_absent(self) -> None:
        labels = [label for label, _, _ in _SOURCES.values()]
        assert instruction_sources(**_inputs()) == [*labels, *_MARKERS]
        empty = {name: type(v)() for name, (_, _, v) in _SOURCES.items()}
        assert instruction_sources(**empty) == _MARKERS

    @pytest.mark.parametrize("name", list(_SOURCES))
    def test_toggling_one_input_drops_exactly_its_label(self, name: str) -> None:
        label, heading, _ = _SOURCES[name]
        before = instruction_sources(**_inputs())
        after = instruction_sources(**_inputs(off=name))
        assert [s for s in before if s not in after] == [label]
        assert [s for s in after if s not in before] == []
        if heading:  # the real prompt agrees: heading present iff listed
            assert heading in _prompt(_inputs())
            assert heading not in _prompt(_inputs(off=name))

    def test_every_prompt_input_is_mapped_or_excluded(self) -> None:
        prompt_params = set(inspect.signature(_build_reviewer_prompt).parameters)
        helper_params = set(inspect.signature(instruction_sources).parameters)
        # `supplement` is derived from the role inside the prompt builder.
        assert helper_params - {"supplement"} == prompt_params - _EXCLUDED
        assert set(_SOURCES) == helper_params


def _prepared(make_git_repo: Callable[..., Path], name: str) -> _ReviewPassInputs:
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    commit_tracked_file(repo, "mod.py", "x = 1\n")
    return _prepare_review_pass(
        _task(),
        repo,
        "main",
        config_branch="main",
        runner=FakeCodexRunner(),
        session_id=f"s-{name}",
    )


def _record() -> ReviewProfile:
    return ReviewProfile(
        profile_version=1,
        cli_version="0.167.0",
        configured_model="codex-default",
        reasoning_effort=None,
        instruction_sources={_ROLE: ["agent_spec", *_MARKERS]},
        disabled_features=tuple(_LEAN_FEATURES),
        mcp="disabled",
        not_recorded=["effective_model", "enabled_tool_classes"],
    )


class TestBuildReviewProfile:
    def test_record_fields_and_the_partial_pin(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        prepared = _prepared(make_git_repo, "prof-record")
        # The record reads the prompt inputs from the renderer's partial keywords.
        assert isinstance(prepared.render_prompts, functools.partial)
        assert {"agent_specs", "repo_policy", "plan_text", "lint_grounding"} <= set(
            prepared.render_prompts.keywords
        )
        record = build_review_profile(
            prepared, model="gpt-x", reasoning_effort=ReasoningEffort.HIGH
        )
        assert record.profile_version == 1
        assert record.cli_version == prepared.capability.fingerprint.cli_version
        assert (record.configured_model, record.reasoning_effort) == ("gpt-x", "high")
        assert record.disabled_features == tuple(_LEAN_FEATURES)
        assert record.mcp == "disabled"
        assert record.not_recorded == ["effective_model", "enabled_tool_classes"]
        assert set(record.instruction_sources) == set(prepared.roles)
        for sources in record.instruction_sources.values():
            assert "agent_spec" in sources
            assert sources[-2:] == _MARKERS

    def test_unpinned_and_non_partial_renderer_degrade_softly(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        prepared = _prepared(make_git_repo, "prof-stub")
        stub = prepared._replace(render_prompts=lambda _diff, _files: {})
        record = build_review_profile(stub, model=None, reasoning_effort=None)
        assert record.configured_model == "codex-default"
        assert record.reasoning_effort is None
        assert record.instruction_sources == dict.fromkeys(prepared.roles, _MARKERS)


class TestPersistReviewProfile:
    def test_writes_the_json_beside_the_capability_file(self) -> None:
        _persist_review_profile("s-persist", _record())
        path = diagnostics_dir("s-persist") / PROFILE_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert path.name == "codex-review-profile.json"
        assert payload["disabled_features"] == _LEAN_FEATURES
        assert payload["instruction_sources"] == {_ROLE: ["agent_spec", *_MARKERS]}
        assert payload["reasoning_effort"] is None

    def test_an_oserror_is_swallowed_and_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(path: Path, text: str) -> None:
            message = "disk full"
            raise OSError(message)

        monkeypatch.setattr(profile_module, "atomic_write_text", _boom)
        with caplog.at_level("WARNING", logger="cw.codex_review._profile"):
            _persist_review_profile("s-persist-oserror", _record())
        assert any("s-persist-oserror" in r.getMessage() for r in caplog.records)
        assert not any("disk full" in r.getMessage() for r in caplog.records)


class TestPersistedOncePerPass:
    def test_a_sharded_pass_writes_once_and_run_codex_roles_never(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-prof-shard")
        setup = _sharded_setup(repo, "s-prof-shard")
        roles, shards = setup.prepared.roles, len(setup.plans)
        calls: list[str] = []
        real = profile_module._persist_review_profile

        def _spy(session_id: str, record: ReviewProfile) -> None:
            calls.append(session_id)
            real(session_id, record)

        monkeypatch.setattr("cw.codex_review.core._persist_review_profile", _spy)
        runner = _SequencedRunner(_all_ok(roles, shards))
        run_prepared_roles(
            setup.prepared,
            runner=runner,
            worktree=repo,
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="s-prof-shard",
            max_prompt_chars=setup.limit,
        )
        assert len(runner.calls) == len(roles) * shards > 1
        assert calls == ["s-prof-shard"]
        assert (diagnostics_dir("s-prof-shard") / PROFILE_FILENAME).is_file()
        run_codex_roles(
            runner=_SequencedRunner([_ok_result()]),
            worktree=repo,
            roles=[_ROLE],
            prompts_by_role={_ROLE: "p"},
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="s-prof-direct",
        )
        assert calls == ["s-prof-shard"]
        assert not (diagnostics_dir("s-prof-direct") / PROFILE_FILENAME).exists()


class TestRequiredPolicySurvives:
    def test_agent_spec_extras_and_repo_policy_reach_the_prompt(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("wt-prof-contract")
        _seed_default_branch_config(
            repo,
            {
                ".claude/review-extras.md": "EXTRAS-SURVIVES\n",
                ".claude/review-policy.md": f"## {_ROLE}\nPOLICY-SURVIVES\n",
            },
        )
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "mod.py", "x = 1\n")
        prepared = _prepare_review_pass(
            _task(scope_hint="large"),
            repo,
            "main",
            config_branch="main",
            runner=FakeCodexRunner(),
            session_id="s-prof-contract",
        )
        prompt = prepared.prompts_by_role[_ROLE]
        assert isinstance(prepared.render_prompts, functools.partial)
        agent_spec = prepared.render_prompts.keywords["agent_specs"][_ROLE]
        assert agent_spec
        assert agent_spec in prompt
        assert "## Project Rubrics\nEXTRAS-SURVIVES" in prompt
        assert f"## Repo Policy for {_ROLE}\nPOLICY-SURVIVES" in prompt

    def test_no_cw_module_inlines_agents_md(self) -> None:
        # project_doc_max_bytes=0 drops a client repo's AGENTS.md, and nothing
        # in cw reads it back in: the documented limitation (#1711).
        src = Path(cw.__file__).parent
        holders = [
            path.name
            for path in sorted(src.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "AGENTS.md" in node.value
        ]
        assert holders == []
