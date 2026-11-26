"""Tests for cw.stage_verification.finalize_review_artifacts (#3543).

``gh`` is a scripted :class:`FakeGhCli` and the ``cw review ...`` CLIs are a
scripted :class:`FakeCwCli`; git runs for real in a sandboxed repo. Every argv
the module sends is also parsed under the real click command, so option drift
fails here and not in the field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import click
import pytest

from cw.cli.review._group import review
from cw.review_adjudication import (
    REVIEW_ADJUDICATION_HEADING,
    Adjudication,
    parse_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.stage_verification.finalize_pr import OVERRIDE_HEADING
from cw.stage_verification.finalize_review_artifacts import (
    ArtifactsOutcome,
    PrTarget,
    adjudication_section,
    link_debt_tickets,
    render_signoff,
    run_debt_refile,
    run_review_artifacts,
    write_pr_body,
)
from tests._review_debt_helpers import write_deferred_findings
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._tool_fakes import (
    FakeCwCli,
    FakeGhCli,
    GhReply,
    signoff_render_echo,
    stub_cw_cli,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PR = 7
_URL = f"https://github.com/o/r/pull/{_PR}"
_TICKET = "42"
_CLIENT = "acme"
_HEAD = "a" * 40
_BODY = "## Summary\n\nAdds the widget.\n"
_UI = "frontends/app/X.tsx"
_HEAD_FIELDS = "headRefName,headRefOid,baseRefName,state,autoMergeRequest"
_COMPLETE = GhReply(0, json.dumps({"status": "complete", "unticketed": 0}))
_NO_PR_VIEW = GhReply(1, "", "gh: not found")
_RENDER = "review signoff render"
_DEBT_FILE = "review debt file"
_LINK = "review debt link-pr"
_START = "<!-- cw-review-signoff:start -->"
_END = "<!-- cw-review-signoff:end -->"
_SIGNOFF_BLOCK = f"{_START}\n## Review sign-off\n{_END}"


def _defer(
    file: str = "src/cw/real.py", summary: str = "Helper swallows it", ticket: str = ""
) -> Adjudication:
    return Adjudication(
        severity="SHOULD_FIX",
        file=file,
        summary=summary,
        outcome="defer",
        rationale="Out of scope",
        ticket=ticket,
    )


def _render_adjudication(*entries: Adjudication) -> str:
    text = render_deferred_findings_md(list(entries))
    return text[text.index(REVIEW_ADJUDICATION_HEADING) :]


@dataclass
class Rig:
    """One Step 4d target with its scripted ``gh`` and ``cw``."""

    target: PrTarget
    gh: FakeGhCli
    monkeypatch: pytest.MonkeyPatch
    cw_replies: dict[str, Any] = field(default_factory=dict)
    cw: FakeCwCli = field(default_factory=lambda: FakeCwCli({}))

    def script_cw(self, prefix: str, reply: Any) -> Rig:
        self.cw_replies[prefix] = reply
        self.cw = stub_cw_cli(self.monkeypatch, self.cw_replies)
        return self

    def view(self, body: str | None, head: str = _HEAD) -> Rig:
        """Script the one ``body,headRefOid`` read; ``None`` fails it."""
        if body is None:
            self.gh.reply("pr", "view", _URL, "--json", "body,headRefOid", returncode=1)
        else:
            self.gh.reply(
                "pr",
                "view",
                _URL,
                "--json",
                "body,headRefOid",
                stdout=json.dumps({"body": body, "headRefOid": head}),
            )
        return self

    def armed(self, armed: bool | None) -> Rig:
        """Script the PR head read: armed, unarmed, or failing (``None``)."""
        if armed is None:
            self.gh.reply("pr", "view", _URL, "--json", _HEAD_FIELDS, returncode=1)
            return self
        self.gh.reply(
            "pr",
            "view",
            _URL,
            "--json",
            _HEAD_FIELDS,
            stdout=json.dumps(
                {
                    "headRefName": "dev/42",
                    "headRefOid": _HEAD,
                    "baseRefName": "main",
                    "state": "OPEN",
                    "autoMergeRequest": {"mergeMethod": "SQUASH"} if armed else None,
                }
            ),
        )
        return self

    def artifact(self, *entries: Adjudication) -> Path:
        return write_deferred_findings(self.target.worktree, *entries)

    def cw_names(self) -> list[str]:
        return [" ".join(call[:3]) for call in self.cw.calls]

    def edits(self) -> list[str]:
        return [
            r.body_file or "" for r in self.gh.records if r.argv[1:3] == ["pr", "edit"]
        ]

    def with_target(self, **changes: Any) -> Rig:
        self.target = replace(self.target, **changes)
        return self

    def run(self) -> ArtifactsOutcome:
        return run_review_artifacts(self.target)


@pytest.fixture
def rig(
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Rig:
    worktree = make_git_repo("artifacts")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    fork = git_in(worktree, "rev-parse", "HEAD")
    target = PrTarget(
        worktree=worktree,
        scratch=scratch,
        ticket_id=_TICKET,
        client_name=_CLIENT,
        pr_number=_PR,
        pr_url=_URL,
        base="main",
        fork_point=fork,
        head_sha=fork,
        override_section=None,
    )
    gh = FakeGhCli().install(monkeypatch)
    gh.reply("pr", "edit", stdout="")
    made = Rig(target, gh, monkeypatch)
    made.view(_BODY).armed(True)
    made.script_cw(_DEBT_FILE, _COMPLETE)
    made.script_cw(_RENDER, signoff_render_echo)
    made.script_cw(_LINK, GhReply(0, "{}"))
    return made


def _ui_commit(made: Rig) -> None:
    commit_tracked_file(made.target.worktree, _UI, "export const X = 1\n")
    made.with_target(head_sha=git_in(made.target.worktree, "rev-parse", "HEAD"))


def _parse(argv: list[str]) -> None:
    """Parse *argv* (``review ...``) under the real click command, no invoke."""
    assert argv[0] == "review", argv
    command: click.Command = review
    rest = argv[1:]
    while rest and isinstance(command, click.Group) and rest[0] in command.commands:
        command = command.commands[rest.pop(0)]
    # A group accepts any trailing words, so stopping on one would check nothing.
    assert not isinstance(command, click.Group), argv
    command.make_context(command.name or "", rest, resilient_parsing=False)


class TestArgv:
    def test_the_three_commands_send_the_documented_argv(self, rig: Rig) -> None:
        rig.artifact(_defer())
        rig.view(_BODY + "\n![shot](x.png)\n")
        rig.run()
        artifact = str(rig.target.worktree / ".cw" / "deferred-findings.md")
        debt, render, link = rig.cw.calls
        assert debt == [
            "review",
            "debt",
            "file",
            "--ticket",
            _TICKET,
            "-c",
            _CLIENT,
            "--deferred-findings",
            artifact,
        ]
        assert render[:3] == ["review", "signoff", "render"]
        assert render[3] == "--body-file"
        assert render[5] == "--current-body-file"
        assert render[7:] == ["--pr-head", _HEAD, "--ticket", _TICKET, "-c", _CLIENT]
        assert link == [
            "review",
            "debt",
            "link-pr",
            "--pr",
            str(_PR),
            "--ticket",
            _TICKET,
            "-c",
            _CLIENT,
        ]

    def test_every_argv_parses_under_the_real_click_command(self, rig: Rig) -> None:
        rig.artifact(_defer())
        rig.run()
        assert len(rig.cw.calls) == 3
        for argv in rig.cw.calls:
            _parse(argv)

    @pytest.mark.parametrize(
        "command",
        [
            ["debt", "file"],
            ["debt", "link-pr"],
            ["debt", "emit"],
            ["debt", "record"],
            ["signoff", "render"],
        ],
    )
    def test_the_argv_check_rejects_an_unknown_option(self, command: list[str]) -> None:
        with pytest.raises(click.NoSuchOption):
            _parse(["review", *command, "--no-such-option", "x"])

    def test_the_mcp_hint_commands_parse_under_the_real_click_command(
        self, rig: Rig
    ) -> None:
        rig.script_cw(
            _DEBT_FILE,
            GhReply(1, json.dumps({"status": "mcp_pending", "unticketed": 2})),
        )
        block = rig.run().block
        assert block is not None
        details = block.details
        emit = details.split("`")[1].removeprefix("cw ").split()
        record = details.split("`")[3].removeprefix("cw ").split()
        requeue = details.split("`")[5].removeprefix("cw ").split()
        _parse(emit)
        _parse([*record[:4], "ids.json", *record[5:]])
        assert requeue[:3] == ["dev-queue", "requeue", _TICKET]


class TestOrder:
    def test_debt_file_then_signoff_then_link(self, rig: Rig) -> None:
        rig.artifact(_defer())
        rig.run()
        assert rig.cw_names() == [
            "review debt file",
            "review signoff render",
            "review debt link-pr",
        ]


class TestDebtRefile:
    def test_exit_zero_does_not_block(self, rig: Rig) -> None:
        out = rig.run()
        assert out.block is None

    @pytest.mark.parametrize(
        ("reply", "needle"),
        [
            (
                GhReply(1, json.dumps({"status": "incomplete", "unticketed": 3})),
                "status incomplete): 3 deferral(s) still unticketed",
            ),
            (
                GhReply(1, json.dumps({"status": "mcp_pending", "unticketed": 1})),
                "status mcp_pending): 1 deferral(s) still unticketed",
            ),
            (GhReply(2, "", "Error: bad option"), "exited 2: Error: bad option"),
            (None, "did not finish"),
        ],
    )
    def test_a_failed_refile_blocks_review_debt_unfiled(
        self, rig: Rig, reply: GhReply | None, needle: str
    ) -> None:
        rig.script_cw(_DEBT_FILE, reply)
        out = rig.run()
        assert out.block is not None
        assert out.block.reason == "review_debt_unfiled"
        assert out.block.retry_eligible is True
        assert out.block.blocker_stage == "stage4_review_debt"
        assert needle in out.block.details
        assert "\n" not in out.block.details
        assert rig.cw_names() == ["review debt file"]

    def test_stderr_friction_and_failure_kinds_are_carried(self, rig: Rig) -> None:
        stderr = (
            "debt filing failed [rate_limited] gh said no\n"
            "friction: debt_filing_failed: 2\n"
        )
        rig.script_cw(
            _DEBT_FILE,
            GhReply(1, json.dumps({"status": "incomplete", "unticketed": 2}), stderr),
        )
        out = rig.run()
        assert out.friction == ("friction: debt_filing_failed: 2",)
        assert out.block is not None
        assert "failure kinds: rate_limited" in out.block.details

    def test_the_mcp_hint_never_says_to_rerun_the_file(self, rig: Rig) -> None:
        rig.script_cw(
            _DEBT_FILE,
            GhReply(1, json.dumps({"status": "mcp_pending", "unticketed": 2})),
        )
        block = rig.run().block
        assert block is not None
        assert "cw review debt emit" in block.details
        assert "cw review debt record" in block.details
        assert "rerun cw review debt file" not in block.details
        assert f"cw dev-queue requeue {_TICKET} -c {_CLIENT} --stage finalize" in (
            block.details
        )

    def test_the_other_failures_say_to_rerun_the_file(self, rig: Rig) -> None:
        rig.script_cw(
            _DEBT_FILE,
            GhReply(1, json.dumps({"status": "incomplete", "unticketed": 1})),
        )
        block = rig.run().block
        assert block is not None
        assert "rerun cw review debt file, then requeue finalize" in block.details

    @pytest.mark.parametrize(("armed", "expected"), [(True, True), (False, False)])
    def test_pr_info_reports_the_armed_state_read_back(
        self, rig: Rig, armed: bool, expected: bool
    ) -> None:
        rig.armed(armed).script_cw(_DEBT_FILE, GhReply(1, "{}"))
        block = rig.run().block
        assert block is not None
        assert block.pr_info == {
            "number": _PR,
            "url": _URL,
            "base": "main",
            "auto_merge": expected,
        }

    def test_pr_info_omits_auto_merge_when_the_read_fails(self, rig: Rig) -> None:
        rig.armed(None).script_cw(_DEBT_FILE, GhReply(1, "{}"))
        block = rig.run().block
        assert block is not None
        assert block.pr_info == {"number": _PR, "url": _URL, "base": "main"}
        assert not any(r.argv[1:3] == ["pr", "merge"] for r in rig.gh.records)

    def test_a_refused_fixture_entry_is_removed_before_the_copy(self, rig: Rig) -> None:
        fixture = _defer(file="src/cw/foo.py", summary="fixture")
        real = _defer(summary="keep me")
        path = rig.artifact(fixture, real)
        rig.script_cw(
            _DEBT_FILE,
            GhReply(0, "{}", "friction: debt_filing: refused_fixture_entry\n"),
        )
        out = run_debt_refile(rig.target)
        assert out.friction == ("friction: debt_filing: refused_fixture_entry",)
        kept = parse_deferred_findings_md(path.read_text(encoding="utf-8"))
        assert [a.summary for a in kept] == ["keep me"]

    def test_the_artifact_is_deleted_when_only_fixtures_remain(self, rig: Rig) -> None:
        path = rig.artifact(_defer(file="src/cw/foo.py"))
        rig.script_cw(
            _DEBT_FILE,
            GhReply(0, "{}", "friction: debt_filing: refused_fixture_entry\n"),
        )
        run_debt_refile(rig.target)
        assert not path.exists()

    def test_an_unreadable_artifact_does_not_stop_the_run_after_a_refusal(
        self, rig: Rig
    ) -> None:
        path = rig.target.worktree / ".cw" / "deferred-findings.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe not utf8")
        rig.script_cw(
            _DEBT_FILE,
            GhReply(0, "{}", "friction: debt_filing: refused_fixture_entry\n"),
        )
        assert run_debt_refile(rig.target).block is None
        assert path.exists()


class TestAdjudicationSection:
    def test_an_absent_or_blank_file_gives_empty(self, tmp_path: Path) -> None:
        assert adjudication_section(tmp_path / "none.md") == ""
        blank = tmp_path / "blank.md"
        blank.write_text(" \n", encoding="utf-8")
        assert adjudication_section(blank) == ""

    def test_a_normal_artifact_is_sliced_verbatim_from_the_heading(
        self, rig: Rig
    ) -> None:
        path = rig.artifact(_defer(), _defer(summary="second", ticket="#9"))
        text = path.read_text(encoding="utf-8")
        section = adjudication_section(path)
        assert section == text[text.index(REVIEW_ADJUDICATION_HEADING) :]
        assert section.startswith(REVIEW_ADJUDICATION_HEADING)
        assert "# Deferred Review Findings" not in section
        assert "written by Stage 3" not in section
        assert "DEFERRED-REVIEW-FINDINGS" in section
        assert "ticket: #9" in section

    def test_a_fixture_entry_is_left_out(self, rig: Rig) -> None:
        path = rig.artifact(
            _defer(file="src/cw/foo.py", summary="fixture"), _defer(summary="real")
        )
        section = adjudication_section(path)
        assert "real" in section
        assert "fixture" not in section

    def test_a_fixture_only_artifact_gives_empty(self, rig: Rig) -> None:
        path = rig.artifact(_defer(file="src/cw/foo.py"))
        assert adjudication_section(path) == ""

    def test_an_unparsable_artifact_is_sliced_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.md"
        text = f"# Title\n{REVIEW_ADJUDICATION_HEADING}\nnot a real entry line\n"
        path.write_text(text, encoding="utf-8")
        assert adjudication_section(path) == text[text.index("## ") :]

    def test_a_file_without_the_heading_gives_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.md"
        path.write_text("just words\n", encoding="utf-8")
        assert adjudication_section(path) == ""

    def test_a_non_utf8_file_and_a_directory_give_empty(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.md"
        bad.write_bytes(b"\xff\xfe")
        directory = tmp_path / "dir.md"
        directory.mkdir()
        assert adjudication_section(bad) == ""
        assert adjudication_section(directory) == ""


class TestWritePrBody:
    def test_the_adjudication_replaces_the_create_time_copy_in_place(
        self, rig: Rig
    ) -> None:
        section = _render_adjudication(_defer(summary="fresh"))
        old = f"{_BODY}\n## Review adjudication\n\nold copy\n"
        rig.view(old)
        out = write_pr_body(rig.target, current=old, pr_head=_HEAD, section=section)
        assert out.block is None
        (edit,) = rig.edits()
        assert "old copy" not in edit
        assert edit.count(REVIEW_ADJUDICATION_HEADING) == 1
        assert "fresh" in edit

    def test_an_empty_section_keeps_the_existing_one(self, rig: Rig) -> None:
        body = f"{_BODY}\n## Review adjudication\n\nkeep\n"
        out = write_pr_body(rig.target, current=body, pr_head=_HEAD, section="")
        assert out.block is None
        assert rig.edits() == []

    def test_the_override_is_written_after_the_adjudication(self, rig: Rig) -> None:
        override = f"{OVERRIDE_HEADING}\n\nwaived by the operator\n"
        rig.with_target(override_section=override)
        section = _render_adjudication(_defer())
        write_pr_body(rig.target, current=_BODY, pr_head=_HEAD, section=section)
        (edit,) = rig.edits()
        assert edit.index(REVIEW_ADJUDICATION_HEADING) < edit.index(OVERRIDE_HEADING)

    def test_an_unchanged_body_makes_no_edit(self, rig: Rig) -> None:
        section = _render_adjudication(_defer())
        body = f"{_BODY.rstrip()}\n\n{section.strip()}\n"
        out = write_pr_body(rig.target, current=body, pr_head=_HEAD, section=section)
        assert out.block is None
        assert rig.edits() == []

    def test_the_working_files_live_in_the_scratch_dir(self, rig: Rig) -> None:
        write_pr_body(
            rig.target,
            current=_BODY,
            pr_head=_HEAD,
            section=_render_adjudication(_defer()),
        )
        assert (rig.target.scratch / "pr-body.current.md").exists()
        assert (rig.target.scratch / "pr-body.composed.md").exists()
        assert not (rig.target.worktree / "pr-body.current.md").exists()

    def test_a_failed_edit_with_changed_sections_blocks(self, rig: Rig) -> None:
        rig.gh.reply("pr", "edit", returncode=1, stderr="boom")
        out = write_pr_body(
            rig.target,
            current=_BODY,
            pr_head=_HEAD,
            section=_render_adjudication(_defer()),
        )
        assert out.block is not None
        assert out.block.reason == "operator_unavailable"
        assert out.block.retry_eligible is True
        assert "could not write the PR body review sections" in out.block.details

    def test_a_failed_signoff_only_edit_is_friction(self, rig: Rig) -> None:
        rig.script_cw(
            _RENDER, lambda _args: GhReply(0, _BODY + "\n" + _SIGNOFF_BLOCK + "\n")
        )
        rig.gh.reply("pr", "edit", returncode=1, stderr="boom")
        out = write_pr_body(rig.target, current=_BODY, pr_head=_HEAD, section="")
        assert out.block is None
        assert out.friction == ("review_signoff_write_failed",)

    def test_a_none_edit_result_is_the_same_failure(self, rig: Rig) -> None:
        from unittest.mock import patch

        with patch(
            "cw.stage_verification.finalize_review_artifacts.edit_pr_body",
            return_value=None,
        ):
            out = write_pr_body(
                rig.target,
                current=_BODY,
                pr_head=_HEAD,
                section=_render_adjudication(_defer()),
            )
        assert out.block is not None

    @pytest.mark.parametrize(
        "reply", [GhReply(1, "", "boom"), None, GhReply(0, "  \n", "")]
    )
    def test_a_failed_render_still_writes_the_composed_sections(
        self, rig: Rig, reply: GhReply | None
    ) -> None:
        rig.script_cw(_RENDER, reply)
        section = _render_adjudication(_defer(summary="fresh"))
        out = write_pr_body(rig.target, current=_BODY, pr_head=_HEAD, section=section)
        assert out.block is None
        assert out.friction == ("friction: review_signoff: skipped_cli_failed",)
        (edit,) = rig.edits()
        assert edit == f"{_BODY.rstrip()}\n\n{section.strip()}\n"

    @pytest.mark.parametrize(
        "reply", [GhReply(1, "", "boom"), None, GhReply(0, "  \n", "")]
    )
    def test_a_failed_render_with_no_section_makes_no_edit(
        self, rig: Rig, reply: GhReply | None
    ) -> None:
        rig.script_cw(_RENDER, reply)
        out = write_pr_body(rig.target, current=_BODY, pr_head=_HEAD, section="")
        assert out.block is None
        assert out.friction == ("friction: review_signoff: skipped_cli_failed",)
        assert rig.edits() == []


class TestRenderSignoff:
    def _files(self, rig: Rig, composed: str, current: str) -> tuple[Path, Path]:
        a = rig.target.scratch / "c.md"
        b = rig.target.scratch / "b.md"
        a.write_text(composed, encoding="utf-8")
        b.write_text(current, encoding="utf-8")
        return a, b

    def test_exit_zero_returns_stdout(self, rig: Rig) -> None:
        rig.script_cw(
            _RENDER, GhReply(0, "NEW BODY\n", "friction: review_signoff: truncated\n")
        )
        composed, current = self._files(rig, "c", "b")
        text, friction = render_signoff(
            rig.target, composed=composed, current=current, pr_head=_HEAD
        )
        assert text == "NEW BODY\n"
        assert friction == ("friction: review_signoff: truncated",)

    def test_exit_three_means_no_edit(self, rig: Rig) -> None:
        rig.script_cw(_RENDER, GhReply(3, "", ""))
        composed, current = self._files(rig, "c", "c")
        text, _ = render_signoff(
            rig.target, composed=composed, current=current, pr_head=_HEAD
        )
        assert text is None

    @pytest.mark.parametrize(
        "reply", [GhReply(1, "", "boom"), None, GhReply(0, "  \n", "")]
    )
    def test_a_failure_returns_the_composed_text_with_a_token(
        self, rig: Rig, reply: GhReply | None
    ) -> None:
        rig.script_cw(_RENDER, reply)
        composed, current = self._files(rig, "composed text", "b")
        text, friction = render_signoff(
            rig.target, composed=composed, current=current, pr_head=_HEAD
        )
        assert text == "composed text"
        assert friction == ("friction: review_signoff: skipped_cli_failed",)

    def test_an_empty_head_skips_the_cli(self, rig: Rig) -> None:
        composed, current = self._files(rig, "composed text", "b")
        text, friction = render_signoff(
            rig.target, composed=composed, current=current, pr_head=""
        )
        assert text == "composed text"
        assert friction == ("friction: review_signoff: skipped_pr_head_unreadable",)
        assert rig.cw.calls == []


class TestSignoffThroughTheRun:
    def test_rc_zero_writes_the_rendered_body(self, rig: Rig) -> None:
        rig.script_cw(
            _RENDER, lambda _args: GhReply(0, _BODY + "\n" + _SIGNOFF_BLOCK + "\n")
        )
        rig.run()
        (edit,) = rig.edits()
        assert "cw-review-signoff:start" in edit

    def test_rc_three_makes_no_edit(self, rig: Rig) -> None:
        rig.run()
        assert rig.edits() == []


class TestLink:
    def test_a_clean_link_is_silent(self, rig: Rig) -> None:
        assert link_debt_tickets(rig.target) == ()

    @pytest.mark.parametrize("reply", [GhReply(1, "", "boom"), None])
    def test_a_failed_link_is_friction_only(
        self, rig: Rig, reply: GhReply | None
    ) -> None:
        rig.script_cw(_LINK, reply)
        out = rig.run()
        assert out.block is None
        assert out.friction[-1] == "review_debt_link: cli_failed"

    def test_stderr_friction_lines_are_copied(self, rig: Rig) -> None:
        rig.script_cw(_LINK, GhReply(0, "{}", "friction: debt_pr_link_failed: 1\n"))
        assert link_debt_tickets(rig.target) == ("friction: debt_pr_link_failed: 1",)


class TestMediaCheckInput:
    """cw's own sections are never evidence; real media in the description is."""

    _FINDING = "add alt text to `<img>`"

    def _artifact(self, rig: Rig) -> str:
        path = rig.artifact(_defer(summary=self._FINDING))
        return adjudication_section(path)

    def test_finding_text_in_the_adjudication_region_holds(self, rig: Rig) -> None:
        _ui_commit(rig)
        section = self._artifact(rig)
        rig.view(f"{_BODY}\n{section}")
        out = rig.run()
        assert out.hold_automerge is True
        assert "ui_evidence_missing" in out.friction
        assert all(self._FINDING in edit for edit in rig.edits())

    def test_the_edited_body_still_carries_the_finding_text(self, rig: Rig) -> None:
        _ui_commit(rig)
        self._artifact(rig)
        out = rig.run()
        assert out.hold_automerge is True
        (edit,) = rig.edits()
        assert self._FINDING in edit

    def test_media_text_in_the_override_region_holds(self, rig: Rig) -> None:
        _ui_commit(rig)
        override = f"{OVERRIDE_HEADING}\n\n- reason: see <img src=x>\n"
        rig.with_target(override_section=override)
        rig.view(f"{_BODY}\n{override}")
        assert rig.run().hold_automerge is True

    def test_media_text_in_the_signoff_block_holds(self, rig: Rig) -> None:
        _ui_commit(rig)
        rig.view(f"{_BODY}\n{_START}\n<img src=x>\n{_END}\n")
        out = rig.run()
        assert out.hold_automerge is True
        assert out.friction.count("ui_evidence_missing") == 1

    def test_real_media_in_the_description_does_not_hold(self, rig: Rig) -> None:
        _ui_commit(rig)
        section = self._artifact(rig)
        rig.view(f"{_BODY}\n![shot](https://x/y.png)\n\n{section}")
        out = rig.run()
        assert out.hold_automerge is False
        assert not any(f.startswith("ui_evidence") for f in out.friction)


class TestFailedBodyRead:
    """Only what depends on the body blocks; the rest ships with friction."""

    _UNREADABLE = "friction: review_signoff: skipped_pr_head_unreadable"

    @pytest.mark.parametrize("view", ["fail", "non-string"])
    def test_nothing_depends_on_it(self, rig: Rig, view: str) -> None:
        if view == "fail":
            rig.view(None)
        else:
            rig.gh.reply(
                "pr",
                "view",
                _URL,
                "--json",
                "body,headRefOid",
                stdout=json.dumps({"body": None, "headRefOid": _HEAD}),
            )
        out = rig.run()
        assert out.block is None
        assert out.hold_automerge is False
        assert self._UNREADABLE in out.friction
        assert rig.edits() == []
        assert _RENDER not in [" ".join(c[:3]) for c in rig.cw.calls]
        assert rig.cw_names() == ["review debt file", "review debt link-pr"]

    def test_ui_files_hold_the_arm(self, rig: Rig) -> None:
        _ui_commit(rig)
        rig.view(None)
        out = rig.run()
        assert out.block is None
        assert out.hold_automerge is True
        assert "ui_evidence_unreadable" in out.friction
        assert self._UNREADABLE in out.friction
        assert rig.edits() == []

    def test_an_adjudication_artifact_blocks(self, rig: Rig) -> None:
        rig.artifact(_defer())
        rig.view(None)
        out = rig.run()
        assert out.block is not None
        assert out.block.reason == "operator_unavailable"
        assert out.block.retry_eligible is True
        assert "review adjudication" in out.block.details
        assert rig.edits() == []

    def test_an_override_section_blocks(self, rig: Rig) -> None:
        rig.with_target(override_section=f"{OVERRIDE_HEADING}\n\nwaived\n")
        rig.view(None)
        out = rig.run()
        assert out.block is not None
        assert out.block.reason == "operator_unavailable"
        assert "operator override" in out.block.details

    def test_both_sections_are_named_by_one_block(self, rig: Rig) -> None:
        rig.artifact(_defer())
        rig.with_target(override_section=f"{OVERRIDE_HEADING}\n\nwaived\n")
        rig.view(None)
        block = rig.run().block
        assert block is not None
        assert "review adjudication and operator override" in block.details

    def test_ui_files_and_an_artifact_block_with_the_ui_token(self, rig: Rig) -> None:
        _ui_commit(rig)
        rig.artifact(_defer())
        rig.view(None)
        out = rig.run()
        assert out.block is not None
        assert "ui_evidence_unreadable" in out.friction
        assert rig.edits() == []

    def test_a_body_without_a_head_skips_the_signoff(self, rig: Rig) -> None:
        rig.gh.reply(
            "pr",
            "view",
            _URL,
            "--json",
            "body,headRefOid",
            stdout=json.dumps({"body": _BODY, "headRefOid": None}),
        )
        out = rig.run()
        assert self._UNREADABLE in out.friction
        assert out.block is None


class TestUiFiles:
    def test_an_unreadable_diff_holds_whatever_the_body_says(self, rig: Rig) -> None:
        rig.with_target(fork_point="")
        rig.view(_BODY + "\n![x](y)\n")
        out = rig.run()
        assert out.hold_automerge is True
        assert "ui_evidence_unreadable" in out.friction

    def test_no_ui_files_means_no_hold_and_no_ui_friction(self, rig: Rig) -> None:
        out = rig.run()
        assert out.hold_automerge is False
        assert not any(f.startswith("ui_evidence") for f in out.friction)

    def test_friction_comes_in_call_order(self, rig: Rig) -> None:
        _ui_commit(rig)
        rig.script_cw(
            _DEBT_FILE, GhReply(0, "{}", "friction: debt_filing: skipped_no_tracker\n")
        )
        rig.script_cw(
            _RENDER,
            lambda _args: GhReply(
                0, _BODY + "x\n", "friction: review_signoff: truncated\n"
            ),
        )
        rig.script_cw(_LINK, GhReply(0, "{}", "friction: debt_pr_link_failed: 1\n"))
        out = rig.run()
        assert out.friction == (
            "friction: debt_filing: skipped_no_tracker",
            "ui_evidence_missing",
            "friction: review_signoff: truncated",
            "friction: debt_pr_link_failed: 1",
        )
