"""The roster seam of ``_prepare_review_pass`` (#3216, R3).

``cw.codex_review._context._roster.resolve_roster`` picks a pass's reviewer
roles and agent-spec texts: the built-in selection and repo-local-first spec
resolution, or, with an ``engine_roster``, a repo review engine's manifest roles
verbatim. These tests drive the resolver directly and through the real
``_prepare_review_pass`` over a real git repo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_review import _prepare_review_pass
from cw.codex_review._context import EngineRoster
from cw.codex_review._context._file_selection import _categorize_changed_files
from cw.codex_review._context._roster import resolve_roster
from tests._codex_review_helpers import _populate_global_agents_dir, _task, _write
from tests._tool_fakes import ScriptedCodexRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ROSTER = EngineRoster(
    (
        ("security", "SECURITY SPEC: look for injection.\n"),
        ("quality", "QUALITY SPEC: look for dead code.\n"),
    )
)
_POISON = "POISONED-SPEC-MUST-NOT-APPEAR"
_BUILTIN_ROLE = "Code Quality Reviewer"


def _poison_every_builtin_source(
    worktree: Path, global_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make both ``.claude/agents`` sources carry text and every resolver raise."""
    _write(worktree / ".claude" / "agents" / "code-reviewer.md", _POISON)
    _populate_global_agents_dir(global_dir, code_reviewer=_POISON)
    monkeypatch.setattr(
        "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_dir
    )

    def _unreachable(*_args: object, **_kwargs: object) -> None:
        msg = "an engine roster must not resolve built-in agent specs"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "cw.codex_review._context._roster._resolve_agent_spec", _unreachable
    )
    monkeypatch.setattr(
        "cw.codex_review._context._roster._load_agent_spec_fallback_gate",
        _unreachable,
    )
    monkeypatch.setattr(
        "cw.codex_review._context._roster._select_reviewer_roles", _unreachable
    )


class TestResolveRoster:
    def test_an_engine_roster_passes_ids_and_texts_through_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _poison_every_builtin_source(tmp_path, tmp_path / "global", monkeypatch)

        roles, specs, statuses = resolve_roster(
            tmp_path,
            _ROSTER,
            scope_tier="small",
            categories=_categorize_changed_files(["a.py"]),
            mutates_persisted_state=False,
            has_ticket_context=False,
        )

        assert roles == ["security", "quality"]
        assert specs == {
            "security": "SECURITY SPEC: look for injection.\n",
            "quality": "QUALITY SPEC: look for dead code.\n",
        }
        assert [(s.role, s.source, s.empty, s.empty_repo_file) for s in statuses] == [
            ("security", "repo", False, False),
            ("quality", "repo", False, False),
        ]

    def test_without_a_roster_the_built_in_selection_and_resolution_run(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / ".claude" / "agents" / "code-reviewer.md", "REPO SPEC\n")

        roles, specs, statuses = resolve_roster(
            tmp_path,
            None,
            scope_tier="small",
            categories=_categorize_changed_files(["a.py"]),
            mutates_persisted_state=False,
            has_ticket_context=False,
        )

        assert _BUILTIN_ROLE in roles
        assert specs[_BUILTIN_ROLE] == "REPO SPEC\n"
        assert [s.role for s in statuses] == roles
        by_role = {s.role: s for s in statuses}
        assert by_role[_BUILTIN_ROLE].source == "repo"
        assert set(specs) == set(roles)


class TestPrepareReviewPassWithARoster:
    def test_roles_prompts_and_statuses_come_from_the_roster(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree, _origin = make_git_repo_with_origin("wt-roster")
        _poison_every_builtin_source(worktree, tmp_path / "global", monkeypatch)

        prepared = _prepare_review_pass(
            _task(),
            worktree,
            "main",
            config_branch="main",
            runner=ScriptedCodexRunner(),
            session_id="s-roster",
            engine_roster=_ROSTER,
        )

        assert prepared.roles == ["security", "quality"]
        assert set(prepared.prompts_by_role) == {"security", "quality"}
        for role, spec in _ROSTER.roles:
            assert f"## Agent Specification\n{spec}" in prepared.prompts_by_role[role]
        assert [(s.role, s.source) for s in prepared.agent_spec_status] == [
            ("security", "repo"),
            ("quality", "repo"),
        ]
        for prompt in prepared.prompts_by_role.values():
            assert _POISON not in prompt
        assert prepared.delta_diff is None
        assert prepared.delta_changed_files is None
        assert "feature.py" in prepared.changed_files

    def test_a_poisoned_global_agents_dir_is_never_read(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the roster's texts reach a prompt, even with spec files on disk."""
        worktree, _origin = make_git_repo_with_origin("wt-roster-poison")
        global_dir = tmp_path / "global"
        monkeypatch.setattr(
            "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_dir
        )
        _populate_global_agents_dir(global_dir, code_reviewer=_POISON)
        _write(worktree / ".claude" / "agents" / "code-reviewer.md", _POISON)

        prepared = _prepare_review_pass(
            _task(),
            worktree,
            "main",
            config_branch="main",
            runner=ScriptedCodexRunner(),
            session_id="s-roster-poison",
            engine_roster=_ROSTER,
        )

        assert all(_POISON not in p for p in prepared.prompts_by_role.values())

    def test_a_roster_pass_is_a_full_diff_pass(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        worktree, _origin = make_git_repo_with_origin("wt-roster-full")

        prepared = _prepare_review_pass(
            _task(),
            worktree,
            "main",
            config_branch="main",
            runner=ScriptedCodexRunner(),
            session_id="s-roster-full",
            delta_from_sha=None,
            engine_roster=_ROSTER,
        )

        assert prepared.delta_diff is None
        assert "feature.py" in prepared.diff.text

    def test_no_roster_leaves_the_pass_unchanged(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    ) -> None:
        worktree, _origin = make_git_repo_with_origin("wt-roster-none")
        runner = ScriptedCodexRunner()

        default = _prepare_review_pass(
            _task(),
            worktree,
            "main",
            config_branch="main",
            runner=runner,
            session_id="s-none",
        )
        explicit = _prepare_review_pass(
            _task(),
            worktree,
            "main",
            config_branch="main",
            runner=runner,
            session_id="s-none",
            engine_roster=None,
        )

        assert default.roles == explicit.roles
        assert default.prompts_by_role == explicit.prompts_by_role
        assert default.agent_spec_status == explicit.agent_spec_status
        assert _BUILTIN_ROLE in default.roles
