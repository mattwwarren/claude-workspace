from __future__ import annotations

import ast
import functools
import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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
from cw.codex_review._profile import (
    PROFILE_FILENAME,
    _persist_review_profile,
    build_review_profile,
    instruction_sources,
)
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
_SPEC_PATH = ".claude/agents/code-reviewer.md"
_MARKERS = ["codex_project_docs=disabled", "codex_user_config=ignored"]
# fmt: off
_EXCLUDED = set("role diff changed_files capable delta_mode backend branch_context pending_operator_comment".split())  # noqa: SIM905, E501
_SOURCES: dict[str, tuple[str, str | None, object]] = {
    "agent_spec_text": ("agent_spec", None, "SPEC"),
    "supplement": ("output_supplement", None, _CODEX_TONE_GUIDE_SUPPLEMENT),
    "ticket_text": ("ticket", "## Ticket Context", "T"),
    "operator_comments_text": ("operator_comments", "## Ticket Comments", "C"),
    "plan_text": ("plan", "## Approved Plan", "P"),
    "project_rubrics": ("project_rubrics", "## Project Rubrics", "R"),
    "repo_policy_section": ("repo_policy", f"## Repo Policy for {_ROLE}", "RP"),
    "lint_grounding": ("lint_grounding", "## Repo Lint Configuration", "L"),
    "sensitive_hits": ("sensitive_files", "Touched sensitive files:", [_SensitiveHit("pyproject.toml", "build", "packaging")]),  # noqa: E501
    "adjudicated_findings": ("adjudicated_findings", "## Previously Adjudicated Findings", _disposition_ledger()),  # noqa: E501
    "prior_open_findings": ("prior_open_findings", "## Unresolved Prior Findings", [_make_finding(summary="STILL OPEN")]),  # noqa: E501
    "operator_constraints_text": ("operator_constraints", _CONSTRAINTS_HEADER, "OC"),
    "decided_scope_text": ("decided_scope", _DECIDED_SCOPE_HEADER, "DS"),
}
def _inputs(*, off: str | None = None) -> dict[str, object]:
    return {name: type(value)() if name == off else value for name, (_, _, value) in _SOURCES.items()}  # noqa: E501


def _prompt(inputs: dict[str, object]) -> str:
    kwargs = {k: v for k, v in inputs.items() if k != "supplement"}
    return _build_reviewer_prompt(_ROLE, diff=_make_diff("x = 1"), changed_files=["x.py"], **kwargs)  # noqa: E501


class TestInstructionSources:
    @pytest.mark.parametrize("name", list(_SOURCES))
    def test_toggling_one_input_drops_exactly_its_label(self, name: str) -> None:
        label, heading, _ = _SOURCES[name]
        before = instruction_sources(**_inputs())
        after = instruction_sources(**_inputs(off=name))
        assert before[-2:] == after[-2:] == _MARKERS
        assert [s for s in before if s not in after] == [label]
        if heading:
            assert heading in _prompt(_inputs())
            assert heading not in _prompt(_inputs(off=name))

    def test_every_prompt_input_is_mapped_or_excluded(self) -> None:
        prompt_params = set(inspect.signature(_build_reviewer_prompt).parameters)
        helper_params = set(inspect.signature(instruction_sources).parameters)
        assert helper_params - {"supplement"} == prompt_params - _EXCLUDED
        assert set(_SOURCES) == helper_params


def _prepared(make_git_repo: Callable[..., Path], name: str) -> _ReviewPassInputs:
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    commit_tracked_file(repo, _SPEC_PATH, "AGENT-SPEC-BODY\n")
    commit_tracked_file(repo, "mod.py", "x = 1\n")
    return _prepare_review_pass(_task(), repo, "main", config_branch="main", runner=FakeCodexRunner(), session_id=f"s-{name}")  # noqa: E501

# fmt: on


class TestBuildReviewProfile:
    def test_record_fields_and_the_partial_pin(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        prepared = _prepared(make_git_repo, "prof-record")
        assert isinstance(prepared.render_prompts, functools.partial)
        assert {"agent_specs", "repo_policy", "plan_text", "lint_grounding"} <= set(
            prepared.render_prompts.keywords
        )
        record = build_review_profile(
            prepared, model="gpt-x", reasoning_effort=ReasoningEffort.HIGH
        )
        assert record.profile_version == profile_module.PROFILE_VERSION
        assert record.cli_version == prepared.capability.fingerprint.cli_version
        assert (record.configured_model, record.reasoning_effort) == ("gpt-x", "high")
        assert record.disabled_features == tuple(_LEAN_FEATURES)
        assert record.mcp == "disabled"
        assert record.not_recorded == ["effective_model", "enabled_tool_classes"]
        assert set(record.instruction_sources) == set(prepared.roles)
        assert "agent_spec" in record.instruction_sources[_ROLE]
        for sources in record.instruction_sources.values():
            assert sources[-2:] == _MARKERS
        stub = prepared._replace(render_prompts=lambda _diff, _files: {})
        fallback = build_review_profile(stub, model=None, reasoning_effort=None)
        assert fallback.instruction_sources == dict.fromkeys(prepared.roles, _MARKERS)


class TestPersistReviewProfile:
    def test_writes_json_and_swallow_oserror(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # fmt: off
        record = profile_module.ReviewProfile(profile_module.PROFILE_VERSION, "0.167.0", "codex-default", None, {_ROLE: ["agent_spec", *_MARKERS]}, tuple(_LEAN_FEATURES), "disabled", ["effective_model", "enabled_tool_classes"])  # noqa: E501
        # fmt: on
        _persist_review_profile("s-persist", record)
        path = diagnostics_dir("s-persist") / PROFILE_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["disabled_features"] == _LEAN_FEATURES
        assert payload["instruction_sources"] == {_ROLE: ["agent_spec", *_MARKERS]}
        assert payload["reasoning_effort"] is None
        monkeypatch.setattr(
            profile_module,
            "atomic_write_text",
            lambda _path, _text: (_ for _ in ()).throw(OSError("full")),
        )
        with caplog.at_level("WARNING", logger="cw.codex_review._profile"):
            _persist_review_profile("s-persist-oserror", record)
        assert any("s-persist-oserror" in r.getMessage() for r in caplog.records)


class TestPersistedOncePerPass:
    def test_a_sharded_pass_writes_once_and_run_codex_roles_never(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-prof-shard")
        setup = _sharded_setup(repo, "s-prof-shard")
        roles, shards = setup.prepared.roles, len(setup.plans)
        calls: list[str] = []
        real = profile_module._persist_review_profile

        def _spy(session_id: str, record: object) -> None:
            calls.append(session_id)
            real(session_id, record)

        monkeypatch.setattr("cw.codex_review.core._persist_review_profile", _spy)
        runner = _SequencedRunner(_all_ok(roles, shards))
        # fmt: off
        run_prepared_roles(setup.prepared, runner=runner, worktree=repo, model=None, reasoning_effort=None, wall_clock_budget_seconds=None, session_id="s-prof-shard", max_prompt_chars=setup.limit)  # noqa: E501
        # fmt: on
        assert len(runner.calls) == len(roles) * shards > 1
        assert calls == ["s-prof-shard"]
        assert (diagnostics_dir("s-prof-shard") / PROFILE_FILENAME).is_file()
        # fmt: off
        run_codex_roles(runner=_SequencedRunner([_ok_result()]), worktree=repo, roles=[_ROLE], prompts_by_role={_ROLE: "p"}, model=None, reasoning_effort=None, wall_clock_budget_seconds=None, session_id="s-prof-direct")  # noqa: E501
        # fmt: on
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
        commit_tracked_file(repo, _SPEC_PATH, "AGENT-SPEC-BODY\n")
        commit_tracked_file(repo, "mod.py", "x = 1\n")
        # fmt: off
        prepared = _prepare_review_pass(_task(scope_hint="large"), repo, "main", config_branch="main", runner=FakeCodexRunner(), session_id="s-prof-contract")  # noqa: E501
        # fmt: on
        prompt = prepared.prompts_by_role[_ROLE]
        assert isinstance(prepared.render_prompts, functools.partial)
        agent_spec = prepared.render_prompts.keywords["agent_specs"][_ROLE]
        assert agent_spec
        assert agent_spec in prompt
        assert "## Project Rubrics\nEXTRAS-SURVIVES" in prompt
        assert f"## Repo Policy for {_ROLE}\nPOLICY-SURVIVES" in prompt

        src = Path(cw.__file__).parent
        assert not [
            node.value
            for path in sorted(src.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "AGENTS.md" in node.value
        ]
