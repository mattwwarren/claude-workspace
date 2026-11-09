"""``cw review signoff render`` end to end (#3043, RFC 0017 II2).

Hermetic ``tmp_path`` repos through the hoisted attested setup. The CLI never
calls ``gh``; the launch guard fails the test if it tries. Maps to
``src/cw/cli/review/signoff.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.cli import main
from cw.cli.review import signoff as cli_mod
from cw.models.review_attestation import AttestationOverride
from cw.review_adjudication import Adjudication
from cw.review_approval import INVALID_HEAD_UNREADABLE, AttestationVerdict
from cw.review_signoff import END_MARKER, MAX_VALUE_CHARS, START_MARKER, friction_line
from tests._review_approval_helpers import head
from tests._review_attestation_helpers import (
    AttestedSetup,
    build_attested_setup,
    write_attested,
)
from tests._review_debt_helpers import write_deferred_findings
from tests._review_signoff_helpers import render_signoff, write_body

if TYPE_CHECKING:
    from collections.abc import Callable

    from click.testing import Result


OTHER_HEAD = "b" * 40
OPT_IN: dict[str, object] = {"attestation": {"pr_section": True}}
HELP_FIRST_PARA = "Write the head-pinned review sign-off into a PR body (RFC 0017)."


@pytest.fixture
def setup(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> AttestedSetup:
    return build_attested_setup(tmp_path, make_git_repo_with_origin, "wt-signoff-cli")


def _opt_in(setup: AttestedSetup) -> None:
    setup.live_client(OPT_IN)


def _write_section_body(setup: AttestedSetup, name: str = "b.md") -> Path:
    """A body that already carries a valid sign-off section (opted in)."""
    _opt_in(setup)
    body = write_body(setup, name, "# Summary\n\ntext\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 0, result.output
    return write_body(setup, "signed.md", result.stdout)


def _friction(result: Result) -> list[str]:
    return [line for line in result.stderr.splitlines() if line.startswith("friction")]


# -- opt-in gate -------------------------------------------------------------


def test_opted_out_client_returns_body_untouched(setup: AttestedSetup) -> None:
    setup.live_client(None)
    body = write_body(setup, "b.md", "# T\n\nbody\n")
    current = write_body(setup, "cur.md", "# old\n")
    result = render_signoff(
        setup, body, head(setup.worktree), current_body_file=current
    )
    assert result.exit_code == 0
    assert result.stdout == "# T\n\nbody\n"
    assert result.stderr == ""


def test_opted_out_default_current_exits_unchanged_with_nothing_printed(
    setup: AttestedSetup,
) -> None:
    setup.live_client({"attestation": {"pr_section": False}})
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == ""


def test_opted_out_leaves_existing_section_in_place(setup: AttestedSetup) -> None:
    signed = _write_section_body(setup)
    setup.live_client(None)
    result = render_signoff(setup, signed, head(setup.worktree))
    assert result.exit_code == 3
    assert result.stdout == ""


def test_unknown_client_is_a_silent_noop(setup: AttestedSetup) -> None:
    setup.live_client(None)
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(
        setup,
        body,
        head(setup.worktree),
        extra=("--client", "no-such-client"),
    )
    assert result.exit_code == 3
    assert result.stderr == ""


# -- valid, opted in ---------------------------------------------------------


def test_valid_opted_in_writes_section_with_attested_head(setup: AttestedSetup) -> None:
    _opt_in(setup)
    body = write_body(setup, "b.md", "# Summary\n\ntext\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 0, result.output
    assert START_MARKER in result.stdout
    assert END_MARKER in result.stdout
    assert f"- attested head: {setup.attested}" in result.stdout
    assert "verified at PR head" not in result.stdout
    assert _friction(result) == []


def test_render_twice_is_byte_identical_and_second_run_exits_3(
    setup: AttestedSetup,
) -> None:
    _opt_in(setup)
    body = write_body(setup, "b.md", "# Summary\n\ntext\n")
    first = render_signoff(setup, body, head(setup.worktree))
    once = write_body(setup, "once.md", first.stdout)
    second = render_signoff(setup, once, head(setup.worktree))
    assert second.exit_code == 3
    assert second.stdout == ""
    assert first.stdout.count(START_MARKER) == 1


def test_fallback_body_equal_to_current_appends_section(setup: AttestedSetup) -> None:
    _opt_in(setup)
    body = write_body(setup, "current.md", "# Summary\n\nno adjudication\n")
    result = render_signoff(setup, body, head(setup.worktree), current_body_file=body)
    assert result.exit_code == 0
    assert result.stdout.startswith("# Summary")
    assert START_MARKER in result.stdout


def test_tolerated_changelog_commit_shows_both_heads(setup: AttestedSetup) -> None:
    _opt_in(setup)
    setup.commit("CHANGELOG.md", "# Changelog\n\nnew entry\n")
    pr_head = head(setup.worktree)
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, pr_head)
    assert result.exit_code == 0, result.output
    assert f"- attested head: {setup.attested}" in result.stdout
    assert f"- verified at PR head: {pr_head}" in result.stdout


# -- head and verifier ------------------------------------------------------


def test_head_mismatch_skips_and_reports_friction(setup: AttestedSetup) -> None:
    _opt_in(setup)
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, OTHER_HEAD)
    assert result.exit_code == 3
    assert result.stdout == ""
    assert _friction(result) == [friction_line("skipped_head_mismatch")]


def test_head_mismatch_strips_stale_section(setup: AttestedSetup) -> None:
    signed = _write_section_body(setup)
    result = render_signoff(setup, signed, OTHER_HEAD)
    assert result.exit_code == 0
    assert START_MARKER not in result.stdout
    assert _friction(result) == [friction_line("stripped_head_mismatch")]


def test_missing_context_is_invalid_unreadable(setup: AttestedSetup) -> None:
    _opt_in(setup)
    (setup.worktree / ".claude" / "cw-context.json").unlink()
    body = write_body(setup, "b.md", "# T\n")
    # Without the context the client is unknown, so only -c reaches the verifier.
    result = render_signoff(
        setup, body, head(setup.worktree), extra=("--client", "test-client")
    )
    assert result.exit_code == 3
    assert _friction(result) == [friction_line("skipped_invalid:unreadable")]


def test_no_context_and_no_client_is_silent_noop(setup: AttestedSetup) -> None:
    _opt_in(setup)
    (setup.worktree / ".claude" / "cw-context.json").unlink()
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 3
    assert result.stderr == ""


def test_wrong_ticket_is_invalid(setup: AttestedSetup) -> None:
    _opt_in(setup)
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(
        setup, body, head(setup.worktree), extra=("--ticket", "T-2")
    )
    assert _friction(result) == [friction_line("skipped_invalid:wrong_ticket")]


def test_untolerated_code_commit_after_attested_head(setup: AttestedSetup) -> None:
    _opt_in(setup)
    setup.commit("src/app.py", "x = 1\n")
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert _friction(result) == [friction_line("skipped_invalid:untolerated_commit")]


@pytest.mark.parametrize("token", [INVALID_HEAD_UNREADABLE, "invalid:span_too_large"])
def test_seam_verdicts_skip_before_head_comparison(
    setup: AttestedSetup, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    _opt_in(setup)

    def fake_verdict(*_args: object, **_kwargs: object) -> AttestationVerdict:
        return AttestationVerdict(token, "forced")

    monkeypatch.setattr(cli_mod, "attestation_verdict", fake_verdict)
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 3
    assert _friction(result) == [friction_line(f"skipped_{token}")]


def test_unparseable_attestation_refused_by_verifier(setup: AttestedSetup) -> None:
    _opt_in(setup)
    ctx_path = setup.worktree / ".claude" / "cw-context.json"
    context = json.loads(ctx_path.read_text(encoding="utf-8"))
    context["queue_metadata"]["review_attestation"] = {"bogus": 1}
    ctx_path.write_text(json.dumps(context), encoding="utf-8")
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 3
    assert _friction(result) == [friction_line("skipped_invalid:schema_version")]


# -- artifact and debt -------------------------------------------------------


def _entry(file: str, ticket: str) -> Adjudication:
    return Adjudication.model_validate(
        {
            "severity": "SHOULD_FIX",
            "file": file,
            "summary": "s",
            "outcome": "defer",
            "rationale": "r",
            "ticket": ticket,
        }
    )


def test_debt_links_come_from_artifact_with_pending_fallback(
    setup: AttestedSetup,
) -> None:
    _opt_in(setup)
    write_deferred_findings(
        setup.worktree,
        _entry("src/a.py", "https://github.com/o/r/issues/12"),
        _entry("src/b.py", ""),
    )
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert "https://github.com/o/r/issues/12" in result.stdout
    assert "ticket pending" in result.stdout
    assert _friction(result) == []


def test_count_mismatch_lists_artifact_and_flags(setup: AttestedSetup) -> None:
    _opt_in(setup)
    write_attested(
        setup,
        attestation_overrides={
            "deferrals": [
                {"fingerprint": "fp-1", "severity": "SHOULD_FIX"},
                {"fingerprint": "fp-2", "severity": "SHOULD_FIX"},
            ]
        },
    )
    write_deferred_findings(setup.worktree, _entry("src/a.py", ""))
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert "src/a.py" in result.stdout
    assert _friction(result) == [friction_line("debt_count_mismatch")]


def test_unreadable_artifact_reports_not_reported(setup: AttestedSetup) -> None:
    _opt_in(setup)
    directory = setup.worktree / "not-a-file"
    directory.mkdir()
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(
        setup,
        body,
        head(setup.worktree),
        extra=("--deferred-findings", str(directory)),
    )
    assert result.exit_code == 0
    assert "- accepted debt: not reported" in result.stdout
    assert _friction(result) == [friction_line("artifact_unreadable")]


# -- redaction ---------------------------------------------------------------


def test_worktree_and_home_paths_are_redacted(setup: AttestedSetup) -> None:
    # #3140: values are capped at MAX_VALUE_CHARS before redaction (section 13.7).
    # One combined reason exceeded the cap under a gate-length TMPDIR, cutting the
    # home path mid-way. Each value now stays under the cap whatever tmp_path is.
    worktree_reason = f"see {setup.worktree}/x.py"
    home_reason = f"see {Path.home()}/notes"
    assert len(worktree_reason) < MAX_VALUE_CHARS
    assert len(home_reason) < MAX_VALUE_CHARS
    _opt_in(setup)
    write_attested(
        setup,
        attestation_overrides={
            "overrides": [
                AttestationOverride(
                    kind="must_fix_override",
                    actor="ops",
                    reason=worktree_reason,
                    recorded_at="2026-11-08T12:00:00Z",
                ),
                AttestationOverride(
                    kind="must_fix_override",
                    actor="ops",
                    reason=home_reason,
                    recorded_at="2026-11-08T12:00:00Z",
                ),
            ]
        },
    )
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert str(setup.worktree) not in result.stdout
    assert str(Path.home()) not in result.stdout
    assert "<worktree>" in result.stdout
    assert "<home>" in result.stdout


# -- usage and contract ------------------------------------------------------


def test_missing_pr_head_is_usage_error(setup: AttestedSetup) -> None:
    body = write_body(setup, "b.md", "# T\n")
    result = setup.invoke(("review", "signoff", "render"), "--body-file", str(body))
    assert result.exit_code == 2


def test_missing_body_file_is_usage_error(setup: AttestedSetup) -> None:
    result = setup.invoke(
        ("review", "signoff", "render"),
        "--body-file",
        str(setup.worktree / "absent.md"),
        "--pr-head",
        head(setup.worktree),
    )
    assert result.exit_code == 2


def test_runs_from_outside_repo_with_worktree_anchor(
    setup: AttestedSetup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _opt_in(setup)
    monkeypatch.chdir(tmp_path)
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 0, result.output
    assert START_MARKER in result.stdout


def test_context_override_is_honored(setup: AttestedSetup) -> None:
    _opt_in(setup)
    ctx = setup.worktree / ".claude" / "cw-context.json"
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(
        setup, body, head(setup.worktree), extra=("--context", str(ctx))
    )
    assert result.exit_code == 0
    assert START_MARKER in result.stdout


def test_help_text_is_pinned() -> None:
    from click.testing import CliRunner

    runner = CliRunner()
    group = runner.invoke(main, ["review", "signoff", "--help"])
    assert group.exit_code == 0
    assert HELP_FIRST_PARA in group.output
    render_help = runner.invoke(main, ["review", "signoff", "render", "--help"])
    assert render_help.exit_code == 0
    assert "Exits 3 and prints nothing" in render_help.output
    assert "friction: review_signoff: <token>" in render_help.output
    assert "Required; must exist." in render_help.output


def test_registered_under_review_group() -> None:
    from click.testing import CliRunner

    result = CliRunner().invoke(main, ["review", "signoff", "--help"])
    assert result.exit_code == 0
    assert "render" in result.output


def test_defensive_model_error_after_valid_verdict_is_malformed(
    setup: AttestedSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _opt_in(setup)
    monkeypatch.setattr(cli_mod, "raw_attestation", lambda _context: {"bogus": 1})
    body = write_body(setup, "b.md", "# T\n")
    result = render_signoff(setup, body, head(setup.worktree))
    assert result.exit_code == 3
    assert _friction(result) == [friction_line("skipped_invalid:malformed")]
