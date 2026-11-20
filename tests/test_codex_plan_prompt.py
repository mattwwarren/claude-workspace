"""Tests for cw.codex_plan._prompt: docs root, prefetch, prompt (#3060 R3, R4)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import cw.codex_plan._prompt as prompt_module
from cw.bindings_block import BINDINGS_BEGIN, render_bindings_block
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
    ticket_text_from_snapshot,
)
from cw.codex_plan._result import ResumeDraft
from cw.contract_manifest import CLAUDE_ONLY_DENYLIST, RenderError, render_stage_prompt
from cw.contract_manifest._render import _PLAN_CODEX_ADDENDUM, _STAGE_DOCS
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import CODEX_BACKEND
from cw.models.bindings import Harness
from tests._shared.bindings_builders import bindings_github_cli
from tests._shared.ticket_context import make_comment, make_snapshot_from
from tests._tool_fakes import FakeGhCli

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMMANDS_ROOT = _REPO_ROOT / ".claude" / "commands"

_TICKET = "4242"
_BLOCK = render_bindings_block(bindings_github_cli(), Harness.CODEX, ticket_id=_TICKET)


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
        bindings_block=_BLOCK,
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
        monkeypatch.setattr(
            prompt_module, "_packaged_commands_dir", lambda *_a, **_k: packaged
        )
        monkeypatch.setattr(
            prompt_module,
            "_checkout_commands_dir",
            lambda *_a, **_k: tmp_path / "checkout",
        )
        assert command_docs_root() == packaged

    def test_checkout_is_the_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            prompt_module, "_packaged_commands_dir", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            prompt_module, "_checkout_commands_dir", lambda *_a, **_k: tmp_path
        )
        assert command_docs_root() == tmp_path

    def test_both_absent_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            prompt_module, "_packaged_commands_dir", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            prompt_module, "_checkout_commands_dir", lambda *_a, **_k: None
        )
        assert command_docs_root() is None

    def test_docs_param_requires_every_named_doc_present(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("a", encoding="utf-8")
        assert prompt_module._has_plan_docs(tmp_path, ("a.md",))
        assert not prompt_module._has_plan_docs(tmp_path, ("a.md", "b.md"))
        assert not prompt_module._has_plan_docs(tmp_path)

    def test_default_docs_are_the_plan_docs(self) -> None:
        assert command_docs_root() == command_docs_root(prompt_module.PLAN_DOCS)

    def test_docs_are_threaded_positionally_to_both_seams(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[tuple[str, tuple[str, ...]]] = []

        def _record(name: str, result: Path | None) -> object:
            def _seam(docs: tuple[str, ...]) -> Path | None:
                seen.append((name, docs))
                return result

            return _seam

        monkeypatch.setattr(
            prompt_module, "_packaged_commands_dir", _record("packaged", None)
        )
        monkeypatch.setattr(
            prompt_module, "_checkout_commands_dir", _record("checkout", tmp_path)
        )
        assert command_docs_root(("x.md",)) == tmp_path
        assert seen == [("packaged", ("x.md",)), ("checkout", ("x.md",))]

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


def test_wheel_force_includes_every_impl_doc() -> None:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for name in _STAGE_DOCS["impl"]:
        assert force[f".claude/commands/{name}"] == f"cw/data/commands/{name}"


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
        assert text == contract + "\n" + _BLOCK
        preamble = text.splitlines()[0]
        assert f"ticket {_TICKET}" in preamble
        assert "<ticket-id>" not in preamble

    def test_bindings_block_is_last_once_and_the_addendum_precedes_it(self) -> None:
        text = _render()
        assert text.rstrip("\n").endswith(_BLOCK.rstrip("\n"))
        assert text.count(BINDINGS_BEGIN) == 1
        assert _PLAN_CODEX_ADDENDUM.rstrip("\n") in text
        assert text.index(_PLAN_CODEX_ADDENDUM.splitlines()[0]) < text.index(
            BINDINGS_BEGIN
        )
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
        # The Bindings block is always present; nothing else is injected.
        assert "CW-INJECTED" not in _render().replace(_BLOCK, "")

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
                bindings_block=_BLOCK,
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
        assert len(rendered.text) <= 1200

    @pytest.mark.parametrize("cap", [300, 700, 1000, 1500])
    def test_the_cap_holds_for_the_whole_rendered_text(self, cap: int) -> None:
        comments = [
            {"author": {"login": "op"}, "body": f"C{i} " + "x" * 90} for i in range(12)
        ]
        rendered = render_ticket_text("b" * 50, comments, cap=cap)
        assert len(rendered.text) <= cap
        assert rendered.comments_total == 12

    def test_an_oversized_body_is_truncated_within_the_cap(self) -> None:
        rendered = render_ticket_text("y" * 500, [], cap=200)
        assert "[ticket body truncated by cw]" in rendered.text
        assert len(rendered.text) <= 200

    def test_a_body_truncated_after_dropping_every_comment_stays_in_the_cap(
        self,
    ) -> None:
        comments = [{"author": "op", "body": "c" * 100}] * 3
        rendered = render_ticket_text("y" * 500, comments, cap=200)
        assert rendered.comments_dropped == 3
        assert "[3 oldest comment(s) omitted by cw]" in rendered.text
        assert "[ticket body truncated by cw]" in rendered.text
        assert len(rendered.text) <= 200

    def test_a_cap_below_the_framing_yields_the_framing_only(self) -> None:
        rendered = render_ticket_text("y" * 500, [], cap=10)
        assert "[ticket body truncated by cw]" in rendered.text
        assert "yy" not in rendered.text

    def test_the_cap_constant_is_the_default(self) -> None:
        comments = [{"author": "op", "body": "z" * (TICKET_TEXT_MAX_CHARS // 2)}] * 3
        rendered = render_ticket_text("body", comments)
        assert rendered.comments_dropped >= 1
        assert len(rendered.text) <= TICKET_TEXT_MAX_CHARS


class TestTicketTextFromSnapshot:
    """RFC 0019 T2: the snapshot-fed ticket text for IMPL and FINALIZE."""

    def test_renders_the_display_author_never_the_author_id(self) -> None:
        comment = make_comment(
            "c1", body="STEERING", author_id="uid-1234", author="Dana Display"
        )
        snapshot = make_snapshot_from([comment], body="THE BODY")
        text = ticket_text_from_snapshot(snapshot).text
        assert "THE BODY" in text
        assert "### Comment by Dana Display at 2026-11-18T12:00:00Z" in text
        assert "uid-1234" not in text

    def test_keeps_every_comment_agent_authored_ones_included(self) -> None:
        comments = [
            make_comment("c1", body="OPERATOR NOTE", author_id="op"),
            make_comment("c2", body="AGENT NOTE", author_id="bot", agent_authored=True),
            make_comment("c3", body="CONFLICTED", author_id="x", author_conflict=True),
        ]
        rendered = ticket_text_from_snapshot(make_snapshot_from(comments))
        assert rendered.comments_total == 3
        assert rendered.comments_dropped == 0
        for body in ("OPERATOR NOTE", "AGENT NOTE", "CONFLICTED"):
            assert body in rendered.text
        order = [rendered.text.index(b) for b in ("OPERATOR NOTE", "AGENT NOTE")]
        assert order == sorted(order)

    def test_a_missing_display_author_renders_unknown(self) -> None:
        comment = make_comment("c1", body="X", author_id=None).model_copy(
            update={"author": None}
        )
        text = ticket_text_from_snapshot(make_snapshot_from([comment])).text
        assert "### Comment by unknown at" in text

    def test_shares_the_cap_with_render_ticket_text(self) -> None:
        big = "z" * (TICKET_TEXT_MAX_CHARS // 2)
        comments = [make_comment(f"c{n}", body=big) for n in range(3)]
        snapshot = make_snapshot_from(comments, body="body")
        rendered = ticket_text_from_snapshot(snapshot)
        assert rendered.comments_dropped >= 1
        assert rendered.comments_total == 3
        assert len(rendered.text) <= TICKET_TEXT_MAX_CHARS
        live = render_ticket_text(
            "body",
            [
                {"author": c.author, "created_at": c.created_at, "body": c.body}
                for c in comments
            ],
        )
        assert rendered == live


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
