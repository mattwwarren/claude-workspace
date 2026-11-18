"""Hermetic per-harness proxy for the bindings block (#3703, dogfood plan (g)).

The real-harness run is operator-run and post-merge; it is NOT automated here.
This proxy drives each harness's production render entry point with a client
shaped like this repo's (``tickets`` on GitHub through ``gh``, ``code_host``
on ``gh``) and asserts the observable the operator checks by hand: one block,
naming ``gh`` for both roles, and the codex/opencode prompts carrying the same
bytes as the context key.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.bindings_block import BINDINGS_BEGIN, bindings_block_for
from cw.local_runner import TASK_CONTEXT_RELATIVE_PATH, build_task_message
from cw.models import SessionOrigin
from cw.models.bindings import Harness
from cw.opencode_runner import build_stage_prompt
from cw.spawn import _write_hook_context
from tests._doctor_helpers import make_client
from tests._shared.bindings_builders import bindings_github_cli
from tests._shared.workflow_text import _REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.client import ClientConfig

_TICKET = "4242"
_COMMANDS_ROOT = _REPO_ROOT / ".claude" / "commands"


def _client(tmp_path: Path) -> ClientConfig:
    return make_client(workspace_path=tmp_path, bindings=bindings_github_cli())


def _assert_one_gh_block(block: str) -> None:
    assert block.count(BINDINGS_BEGIN) == 1
    assert f"gh issue view {_TICKET} --json" in block
    assert "code_host (github): cli gh." in block


def test_claude_native_stamps_the_block_into_the_context(tmp_path: Path) -> None:
    block = bindings_block_for(_client(tmp_path), Harness.CLAUDE_NATIVE, _TICKET)
    worktree = tmp_path / "wt"
    worktree.mkdir()

    _write_hook_context(
        worktree,
        session_id="dogfood-cn",
        session_name="dogfood",
        client="client-a",
        purpose="impl",
        ticket_id=_TICKET,
        origin=SessionOrigin.DAEMON,
        write_stop_hook=False,
        bindings_block=block,
    )

    context = json.loads((worktree / ".claude" / "cw-context.json").read_text())
    assert context["bindings_block"] == block
    _assert_one_gh_block(context["bindings_block"])


def test_codex_plan_prompt_carries_the_context_bytes(tmp_path: Path) -> None:
    from cw.codex_plan._prompt import render_plan_prompt

    block = bindings_block_for(_client(tmp_path), Harness.CODEX, _TICKET)
    prompt = render_plan_prompt(
        ticket_id=_TICKET,
        docs_root=_COMMANDS_ROOT,
        ticket=None,
        resume=None,
        approval_matches=False,
        bindings_block=block,
    )

    assert prompt.endswith(block)
    _assert_one_gh_block(prompt)


def test_codex_impl_and_finalize_prompts_carry_the_block(tmp_path: Path) -> None:
    from cw.codex_finalize._prompt import render_finalize_prompt
    from cw.codex_impl._prompt import render_impl_prompt

    block = bindings_block_for(_client(tmp_path), Harness.CODEX, _TICKET)
    impl = render_impl_prompt(
        ticket_id=_TICKET,
        docs_root=_COMMANDS_ROOT,
        ticket=None,
        plan_text="PLAN",
        feedback=None,
        allowed_files=None,
        merge_note=None,
        bindings_block=block,
    )
    finalize = render_finalize_prompt(
        ticket_id=_TICKET,
        docs_root=_COMMANDS_ROOT,
        worktree=tmp_path,
        ticket=None,
        branch_facts="facts",
        override_json=None,
        bindings_block=block,
    )

    for prompt in (impl, finalize):
        assert prompt.endswith(block)
        _assert_one_gh_block(prompt)


def test_opencode_plan_and_impl_prompts_carry_the_context_bytes(tmp_path: Path) -> None:
    block = bindings_block_for(_client(tmp_path), Harness.OPENCODE, _TICKET)
    for stage in ("plan", "impl"):
        prompt = build_stage_prompt(stage, _TICKET, tmp_path, bindings_block=block)
        assert block in prompt
        _assert_one_gh_block(prompt)


def test_local_task_context_starts_with_the_block(tmp_path: Path) -> None:
    block = bindings_block_for(_client(tmp_path), Harness.LOCAL, _TICKET)
    plan = tmp_path / ".cw" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("PLAN\n", encoding="utf-8")

    assert build_task_message(tmp_path, ticket_id=_TICKET, bindings_block=block)

    content = (tmp_path / TASK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    assert content.startswith(block)
    _assert_one_gh_block(content)
