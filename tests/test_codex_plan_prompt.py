"""Tests for cw.codex_plan._prompt: docs root, prefetch, prompt (#3060 R3, R4)."""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

import pytest

import cw.codex_plan._prompt as prompt_module
from cw.codex_plan._prompt import (
    APPROVAL_MATCHES,
    APPROVAL_NONE,
    RESUME_SECTION,
    TICKET_SECTION,
    TICKET_TEXT_MAX_CHARS,
    command_docs_root,
    prefetch_ticket,
    render_plan_prompt,
    render_ticket_text,
)
from cw.codex_plan._result import ResumeDraft
from cw.contract_manifest import CLAUDE_ONLY_DENYLIST, RenderError, render_stage_prompt
from cw.contract_manifest._render import _PLAN_CODEX_ADDENDUM, _STAGE_DOCS
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import CODEX_BACKEND
from tests._tool_fakes import FakeGhCli
from tests.conftest import _COMMANDS_ROOT, _REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

_TICKET = "4242"


def _render(
    *,
    ticket: prompt_module.TicketText | None = None,
    resume: ResumeDraft | None = None,
    approval: bool = False,
) -> str:
    return render_plan_prompt(
        ticket_id=_TICKET,
        docs_root=_COMMANDS_ROOT,
        ticket=ticket,
        resume=resume,
        approval_matches=approval,
    )


def _resume(body: str = "## Summary\n\nDRAFT-LINE-XYZ\n") -> ResumeDraft:
    return ResumeDraft(text=body, leading="", body=body, fingerprint="a" * 64)


class TestCommandDocsRoot:
    def test_checkout_resolves_to_the_repo_commands(self) -> None:
        assert command_docs_root() == _COMMANDS_ROOT

    def test_packaged_dir_wins_over_the_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        packaged = tmp_path / "packaged"
        monkeypatch.setattr(prompt_module, "_packaged_commands_dir", lambda: packaged)
        monkeypatch.setattr(
            prompt_module, "_checkout_commands_dir", lambda: tmp_path / "checkout"
        )
        assert command_docs_root() == packaged

    def test_checkout_is_the_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(prompt_module, "_packaged_commands_dir", lambda: None)
        monkeypatch.setattr(prompt_module, "_checkout_commands_dir", lambda: tmp_path)
        assert command_docs_root() == tmp_path

    def test_both_absent_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prompt_module, "_packaged_commands_dir", lambda: None)
        monkeypatch.setattr(prompt_module, "_checkout_commands_dir", lambda: None)
        assert command_docs_root() is None

    def test_a_package_dir_without_the_docs_is_skipped(self) -> None:
        # A source checkout ships no cw/data/commands.
        assert prompt_module._packaged_commands_dir() is None

    def test_a_non_checkout_install_has_no_checkout_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_pkg = tmp_path / "site-packages" / "cw"
        fake_pkg.mkdir(parents=True)
        monkeypatch.setattr(prompt_module.cw, "__file__", str(fake_pkg / "x.py"))
        assert prompt_module._checkout_commands_dir() is None


def test_wheel_force_includes_every_plan_doc() -> None:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for name in _STAGE_DOCS["plan"]:
        assert force[f".claude/commands/{name}"] == f"cw/data/commands/{name}"


class TestRender:
    def test_contract_is_the_renderer_output_for_the_real_ticket(self) -> None:
        text = _render()
        contract = render_stage_prompt(
            "plan", CODEX_BACKEND, docs_root=_COMMANDS_ROOT, ticket_id=_TICKET
        )
        assert text == contract
        preamble = text.splitlines()[0]
        assert f"ticket {_TICKET}" in preamble
        assert "<ticket-id>" not in preamble

    def test_addendum_is_last_and_names_the_plan_template(self) -> None:
        text = _render()
        assert text.rstrip("\n").endswith(_PLAN_CODEX_ADDENDUM.rstrip("\n"))
        assert "stage1_plan" in _PLAN_CODEX_ADDENDUM
        assert "lines_actual null" in _PLAN_CODEX_ADDENDUM

    def test_injected_sections_follow_the_addendum(self) -> None:
        ticket = render_ticket_text("Body with Agent tool phrase", [])
        text = _render(ticket=ticket, resume=_resume())
        addendum_at = text.index(_PLAN_CODEX_ADDENDUM.splitlines()[0])
        ticket_at = text.index(f"=== BEGIN CW-INJECTED: {TICKET_SECTION} ===")
        resume_at = text.index(f"=== BEGIN CW-INJECTED: {RESUME_SECTION} ===")
        assert addendum_at < ticket_at < resume_at
        assert f"=== END CW-INJECTED: {TICKET_SECTION} ===" in text
        # The denylist guards the contract, not operator-written ticket data.
        assert "Agent tool" in text

    def test_addendum_says_cw_runs_the_stations(self) -> None:
        assert "no sub-session" in _PLAN_CODEX_ADDENDUM
        assert "which cw" in _PLAN_CODEX_ADDENDUM
        assert "runs itself after this turn" in _PLAN_CODEX_ADDENDUM
        assert "do not perform or simulate them" in _PLAN_CODEX_ADDENDUM

    def test_addendum_leaves_state_writes_to_cw(self) -> None:
        for phrase in (
            "event recording",
            "result emission",
            "plan-approval revocation",
        ):
            assert phrase in _PLAN_CODEX_ADDENDUM
        assert "leave them alone" in _PLAN_CODEX_ADDENDUM

    def test_addendum_falls_back_to_cat(self) -> None:
        assert ".claude/cw-context.json" in _PLAN_CODEX_ADDENDUM
        assert "cat the file if jq is absent" in _PLAN_CODEX_ADDENDUM

    def test_resume_section_carries_the_draft_and_approval_line(self) -> None:
        matched = _render(resume=_resume(), approval=True)
        assert "DRAFT-LINE-XYZ" in matched
        assert APPROVAL_MATCHES in matched
        unmatched = _render(resume=_resume(), approval=False)
        assert APPROVAL_NONE in unmatched

    def test_no_resume_section_without_a_draft(self) -> None:
        assert RESUME_SECTION not in _render().split(_PLAN_CODEX_ADDENDUM)[-1]
        assert "CW-INJECTED" not in _render()

    def test_contract_passes_the_denylist(self) -> None:
        text = _render()
        assert not [token for token in CLAUDE_ONLY_DENYLIST if token in text]

    def test_unavailable_docs_are_named_as_deferred(self) -> None:
        for path in (
            ".claude/commands/auto-dev-plan-appendix.md",
            ".claude/commands/auto-dev.md",
        ):
            assert path in _PLAN_CODEX_ADDENDUM
        assert "not available in this turn" in _PLAN_CODEX_ADDENDUM
        assert "is deferred" in _PLAN_CODEX_ADDENDUM

    def test_an_empty_docs_root_raises_render_error(self, tmp_path: Path) -> None:
        with pytest.raises(RenderError):
            render_plan_prompt(
                ticket_id=_TICKET,
                docs_root=tmp_path,
                ticket=None,
                resume=None,
                approval_matches=False,
            )


class TestTicketText:
    def test_agent_authored_comments_are_kept_verbatim(self) -> None:
        body = f"Our analysis\n\n{AGENT_COMMENT_MARKER}"
        rendered = render_ticket_text(
            "The body", [{"author": {"login": "bot"}, "body": body, "createdAt": "t"}]
        )
        assert AGENT_COMMENT_MARKER in rendered.text
        assert "### Comment by bot at t" in rendered.text

    def test_oldest_comments_are_dropped_first(self) -> None:
        comments = [
            {"author": {"login": "op"}, "body": f"COMMENT-{i} " + "x" * 400}
            for i in range(5)
        ]
        rendered = render_ticket_text("body", comments, cap=1200)
        assert rendered.comments_dropped > 0
        assert "COMMENT-0" not in rendered.text
        assert "COMMENT-4" in rendered.text
        assert "oldest comment(s) omitted by cw" in rendered.text

    def test_an_oversized_body_is_truncated(self) -> None:
        rendered = render_ticket_text("y" * 500, [], cap=100)
        assert "[ticket body truncated by cw]" in rendered.text

    def test_the_cap_constant_is_the_default(self) -> None:
        comments = [{"author": "op", "body": "z" * (TICKET_TEXT_MAX_CHARS // 2)}] * 3
        rendered = render_ticket_text("body", comments)
        assert rendered.comments_dropped >= 1
        assert len(rendered.text) <= TICKET_TEXT_MAX_CHARS + 200


class TestPrefetch:
    def test_fetches_body_and_comments_through_gh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGhCli().install(monkeypatch)
        fake.reply(
            "issue", "view", _TICKET, "--json", "body", stdout='{"body": "THE BODY"}'
        )
        comments = [{"author": {"login": "op"}, "body": "OPERATOR SAYS"}]
        fake.reply(
            "issue",
            "view",
            _TICKET,
            "--json",
            "comments",
            stdout=json.dumps({"comments": comments}),
        )
        ticket = prefetch_ticket(_TICKET, cwd=tmp_path)
        assert ticket is not None
        assert "THE BODY" in ticket.text
        assert "OPERATOR SAYS" in ticket.text
        assert all(record.cwd == tmp_path for record in fake.records)

    def test_body_failure_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        FakeGhCli().install(monkeypatch)
        assert prefetch_ticket(_TICKET, cwd=tmp_path) is None

    def test_comments_failure_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGhCli().install(monkeypatch)
        fake.reply("issue", "view", _TICKET, "--json", "body", stdout='{"body": "B"}')
        assert prefetch_ticket(_TICKET, cwd=tmp_path) is None
