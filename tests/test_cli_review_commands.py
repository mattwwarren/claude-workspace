"""Tests for the ``cw review`` subcommands in ``cw.cli.review.commands``.

Covers ``register``, ``adjudicate``, ``check-voided`` and ``verify-fixes``
(GitHub #1154, RFC 0011 S2; #1241). Split out of ``tests/test_cli_review.py``
for #2049 so the test modules mirror the ``src/cw/cli/review/`` package seams.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner
from freezegun import freeze_time

import cw.events
from cw.cli import main
from cw.cli.review.dispositions import _age_cell
from cw.dev_queue import add_ticket, load_dev_queue
from cw.events import read_events
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    OrchestratorConfig,
    Stage,
    TicketTask,
)
from cw.models.enums import OrchestratorEventType
from cw.review_adjudication import (
    Adjudication,
    VoidedFinding,
    parse_deferred_findings_md,
    parse_voided_findings_block,
    render_deferred_findings_md,
    render_voided_findings_block,
)
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    parse_finding_disposition_block,
)
from tests._cli_review_helpers import (
    _CONSOLIDATE_DIFF,
    _branch_repo,
    _consolidate_payload,
    _extract_settle_payloads,
    _settle_entry,
    _settle_payload,
)
from tests.conftest import (
    _finding_kwargs,
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    commit_tracked_file,
    git_in,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


_URL = "https://github.com/acme/widgets/pull/42"
#: What a failing ``record_event`` raises in the settle audit-ordering tests.
#: Asserted on in the command's own error output, so it is one literal.
_EVENT_STORE_FAILURE = "event inbox is read-only"
_OPERATOR = "mattwwarren"


def _patch_identity(
    monkeypatch: pytest.MonkeyPatch, login: str | None = _OPERATOR
) -> None:
    monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: login)


def _patch_fetch(
    monkeypatch: pytest.MonkeyPatch, review_requests: list[dict[str, Any]] | None
) -> None:
    payload = None if review_requests is None else {"reviewRequests": review_requests}
    monkeypatch.setattr("cw.gh.fetch_pr_view", lambda *_a, **_kw: payload)


class TestReviewRegisterCommand:
    def test_individual_target_registers_and_prints_confirmation(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code == 0
        assert "Registered" in result.output
        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].source == "cli"
        assert watched[0].requester_login is None

    def test_team_target_prints_reason_and_exits_zero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"slug": "eng-team"}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code == 0
        assert "team_targeted" in result.output
        assert load_dev_queue().watched_prs == []

    def test_identity_unresolved_raises_cw_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch, login=None)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code != 0

    def test_unparseable_pr_argument_raises_cw_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        result = runner.invoke(main, ["review", "register", "not-a-url"])
        assert result.exit_code != 0

    def test_gh_fetch_failure_raises_cw_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, None)
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code != 0

    def test_register_idempotent_prints_already_registered(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        first = runner.invoke(main, ["review", "register", _URL])
        assert first.exit_code == 0
        second = runner.invoke(main, ["review", "register", _URL])
        assert second.exit_code == 0
        assert "already_registered" in second.output
        assert len(load_dev_queue().watched_prs) == 1

    def test_repo_override_wins_over_process_identity(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """RFC 0011 follow-up (#1171): the repo-keyed override wins over the
        process gh identity at this client-less entry point."""
        from cw.models import OrchestratorConfig

        _patch_identity(monkeypatch, login="process-user")
        monkeypatch.setattr(
            "cw.config.load_orchestrator_config",
            lambda: OrchestratorConfig(
                operator_github_login_by_repo={"acme/widgets": "override-user"}
            ),
        )
        _patch_fetch(monkeypatch, [{"login": "override-user"}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code == 0
        assert "Registered" in result.output
        watched = load_dev_queue().watched_prs
        assert len(watched) == 1

    def test_no_override_falls_back_to_process_identity_unchanged(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_config_dir: Path
    ) -> None:
        """Regression guard: default-empty-map behavior is bit-for-bit unchanged."""
        _patch_identity(monkeypatch)
        _patch_fetch(monkeypatch, [{"login": _OPERATOR}])
        result = runner.invoke(main, ["review", "register", _URL])
        assert result.exit_code == 0
        assert "Registered" in result.output
        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].source == "cli"
        assert watched[0].requester_login is None


def _verdict_payload(*accepted: dict[str, Any], **overrides: object) -> dict[str, Any]:
    """A raw ``ReviewVerdict`` dict for the #1805 adjudicate/verify-fixes CLI."""
    must_fix = [
        af["finding"]
        for af in accepted
        if af["finding"]["severity"] == "MUST_FIX"
        and af.get("disposition", "fixed") != "deferred"
    ]
    payload: dict[str, Any] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "abc1234",
        "accepted": list(accepted),
        "review": {
            "must_fix_initial": len(must_fix),
            "should_fix": 0,
            "fix_cycles_used": 0,
            "deferred": 0,
            "agents_run": 1,
        },
    }
    payload.update(overrides)
    return payload


def _accepted_payload(**overrides: object) -> dict[str, Any]:
    """A raw ``AcceptedFinding`` dict wrapping ``_finding_kwargs``."""
    finding_overrides = {
        k: v
        for k, v in overrides.items()
        if k not in {"disposition", "disposition_detail", "reviewers"}
    }
    payload: dict[str, Any] = {
        "finding": _finding_kwargs(**finding_overrides),
        "reviewers": overrides.get("reviewers", ["Code Quality Reviewer"]),
    }
    for key in ("disposition", "disposition_detail"):
        if key in overrides:
            payload[key] = overrides[key]
    return payload


def _defer_entry(**overrides: Any) -> dict[str, Any]:
    """A raw adjudication dict lined up with ``_accepted_payload``'s defaults."""
    entry: dict[str, Any] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "line_start": 2,
        "line_end": 2,
        "evidence": "def broken():",
        "summary": "Bug here",
        "outcome": "defer",
        "rationale": "first round call",
    }
    entry.update(overrides)
    return entry


def _legacy_deferred_file(*, extra: list[Adjudication] | None = None) -> str:
    """A ``.cw/deferred-findings.md`` in the pre-#1840 (unstamped) shape.

    Rendered rather than hand-written so the seeded fixture cannot drift from
    the artifact shape the command itself produces.
    """
    entries = [
        Adjudication(
            severity="SHOULD_FIX",
            file="src/cw/legacy.py",
            summary="Old rejection",
            outcome="reject",
            rationale="settled before round stamping existed",
        ),
        Adjudication(
            severity="SHOULD_FIX",
            file="src/cw/legacy.py",
            summary="Old deferral",
            outcome="defer",
            rationale="recorded before round stamping existed",
        ),
        *(extra or []),
    ]
    return render_deferred_findings_md(entries)


class TestReviewAdjudicateCommand:
    """#1805: ``cw review adjudicate`` stamps real adjudication outcomes."""

    def test_defer_outcome_stamps_disposition_and_recomputes_verdict(
        self, runner: CliRunner
    ) -> None:
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "adjudications": [
                {
                    "severity": "MUST_FIX",
                    "file": "src/cw/foo.py",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence": "def broken():",
                    "summary": "Bug here",
                    "outcome": "defer",
                    "rationale": "out of scope for this ticket",
                }
            ],
        }
        result = runner.invoke(
            main, ["review", "adjudicate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["accepted"][0]["disposition"] == "deferred"
        assert (
            verdict["accepted"][0]["disposition_detail"]
            == "out of scope for this ticket"
        )
        assert verdict["blocking"] is False
        assert verdict["must_fix"] == []
        assert verdict["review"]["deferred"] == 1
        assert verdict["unmatched_adjudication_count"] == 0

    def test_no_diff_anchor_round_trip_reaches_operator_actionable(
        self, runner: CliRunner
    ) -> None:
        """#1817 end-to-end through the real JSON boundary the pipeline uses.

        A non-diff-anchorable MUST_FIX survives ``consolidate`` (never
        mechanically rejected as ``unknown_file``) and ``adjudicate`` stamps it
        ``operator_actionable`` — a recorded decision, so it stops blocking.
        """
        finding = _make_finding(
            severity="MUST_FIX",
            no_diff_anchor=True,
            file="N/A",
            line_start=None,
            line_end=None,
            summary="AC3's follow-up ticket was never filed",
            evidence="AC3: a follow-up ticket must exist before this ships",
        )
        doc = _make_reviewer_doc(
            finding, reviewer_role="Product Manager Reviewer", status="ok"
        )
        consolidated = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(
                _consolidate_payload(documents=[doc.model_dump(mode="json")])
            ),
        )
        assert consolidated.exit_code == 0, consolidated.output
        verdict = json.loads(consolidated.output)
        assert verdict["rejected"] == []
        assert verdict["rejected_must_fix"] == []
        assert verdict["accepted"][0]["finding"]["no_diff_anchor"] is True
        assert verdict["blocking"] is True

        payload = {
            "verdict": verdict,
            "adjudications": [
                {
                    "severity": "MUST_FIX",
                    "file": "N/A",
                    "line_start": None,
                    "line_end": None,
                    "evidence": "AC3: a follow-up ticket must exist before this ships",
                    "summary": "AC3's follow-up ticket was never filed",
                    "outcome": "operator_action",
                    "rationale": (
                        "acceptance criterion 3 requires a follow-up ticket; "
                        "none exists — operator must file it before this ships"
                    ),
                }
            ],
        }
        result = runner.invoke(
            main, ["review", "adjudicate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        adjudicated = json.loads(result.output)
        assert adjudicated["accepted"][0]["disposition"] == "operator_actionable"
        assert adjudicated["blocking"] is False
        assert adjudicated["must_fix"] == []
        assert adjudicated["unmatched_adjudication_count"] == 0

    def test_unmatched_entry_surfaces_count_in_printed_json(
        self, runner: CliRunner
    ) -> None:
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "adjudications": [
                {
                    "severity": "MUST_FIX",
                    "file": "src/cw/foo.py",
                    "line_start": 99,
                    "line_end": 99,
                    "outcome": "reject",
                    "rationale": "stale anchor",
                }
            ],
        }
        result = runner.invoke(
            main, ["review", "adjudicate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["unmatched_adjudication_count"] == 1
        assert verdict["accepted"][0]["disposition"] == "dropped"
        assert verdict["blocking"] is True

    def test_deferred_findings_out_excludes_unmatched_entry(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """An adjudication entry that matched no finding must not appear in
        the rendered artifact as if the verdict recorded that decision --
        the verdict itself stamped this finding "dropped", not "deferred".
        """
        out = tmp_path / "deferred-findings.md"
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "adjudications": [
                {
                    "severity": "MUST_FIX",
                    "file": "src/cw/foo.py",
                    "line_start": 99,
                    "line_end": 99,
                    "summary": "Stale entry",
                    "outcome": "defer",
                    "rationale": "stale anchor, matches nothing",
                }
            ],
        }
        result = runner.invoke(
            main,
            ["review", "adjudicate", "-", "--deferred-findings-out", str(out)],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["unmatched_adjudication_count"] == 1
        assert verdict["accepted"][0]["disposition"] == "dropped"
        # Nothing was actually applied, so the documented "omit the file
        # entirely when every finding was fixed" rule's sibling case -- no
        # *applied* rejection/deferral -- also skips the write.
        assert not out.exists()

    def test_deferred_findings_out_writes_documented_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out = tmp_path / "nested" / "deferred-findings.md"
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "adjudications": [
                {
                    "severity": "MUST_FIX",
                    "file": "src/cw/foo.py",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence": "def broken():",
                    "summary": "Bug here",
                    "outcome": "defer",
                    "rationale": "handle when scale demands",
                }
            ],
        }
        result = runner.invoke(
            main,
            ["review", "adjudicate", "-", "--deferred-findings-out", str(out)],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        written = out.read_text()
        assert written.startswith("# Deferred Review Findings\n")
        assert "<!-- DEFERRED-REVIEW-FINDINGS" in written
        assert '  rationale: "handle when scale demands"' in written

    def test_deferred_findings_out_skips_write_when_all_fixed(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out = tmp_path / "deferred-findings.md"
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "adjudications": [
                {
                    "severity": "MUST_FIX",
                    "file": "src/cw/foo.py",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence": "def broken():",
                    "summary": "Bug here",
                    "outcome": "fix",
                }
            ],
        }
        result = runner.invoke(
            main,
            ["review", "adjudicate", "-", "--deferred-findings-out", str(out)],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        assert not out.exists()

    def _adjudicate(
        self, runner: CliRunner, out: Path, payload: dict[str, Any]
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "adjudicate", "-", "--deferred-findings-out", str(out)],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output

    def test_deferred_findings_out_first_call_with_no_prior_file_still_succeeds(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: an absent prior file is "nothing to merge", not an error."""
        out = tmp_path / "deferred-findings.md"
        assert not out.exists()

        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(
                    _accepted_payload(line_start=2, line_end=2)
                ),
                "adjudications": [_defer_entry()],
            },
        )

        written = out.read_text(encoding="utf-8")
        assert "first round call" in written
        assert "  round: 1\n" in written

    def test_deferred_findings_out_appends_across_calls(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: the second call must not clobber the first call's record."""
        out = tmp_path / "deferred-findings.md"
        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(
                    _accepted_payload(line_start=2, line_end=2)
                ),
                "adjudications": [_defer_entry()],
            },
        )
        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(
                    _accepted_payload(
                        file="src/cw/bar.py",
                        line_start=5,
                        line_end=5,
                        summary="Slow loop",
                        evidence="slow = True",
                    )
                ),
                "adjudications": [
                    _defer_entry(
                        file="src/cw/bar.py",
                        line_start=5,
                        line_end=5,
                        summary="Slow loop",
                        evidence="slow = True",
                        rationale="second round call",
                    )
                ],
            },
        )

        written = out.read_text(encoding="utf-8")
        assert "first round call" in written
        assert "second round call" in written
        assert "  round: 1\n" in written
        assert "  round: 2\n" in written

    def test_deferred_findings_out_second_call_does_not_duplicate_identical_round(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: an identical re-adjudication collapses to one entry."""
        out = tmp_path / "deferred-findings.md"
        payload = {
            "verdict": _verdict_payload(_accepted_payload(line_start=2, line_end=2)),
            "adjudications": [_defer_entry()],
        }
        self._adjudicate(runner, out, payload)
        self._adjudicate(runner, out, payload)

        written = out.read_text(encoding="utf-8")
        assert written.count("first round call") == 1
        assert "  round: 2\n" not in written

    def test_deferred_findings_out_stamps_and_dedupes_a_rejected_entry(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: a rejected bullet gets the round prefix and still dedupes.

        The rendered bullet records no severity, so a rejected entry is the
        one shape whose merge identity has to be normalized on both sides --
        without that, re-running the same call would append a second copy.
        """
        out = tmp_path / "deferred-findings.md"
        payload = {
            "verdict": _verdict_payload(_accepted_payload(line_start=2, line_end=2)),
            "adjudications": [
                _defer_entry(
                    outcome="reject", rationale="deliberate tradeoff, documented"
                )
            ],
        }
        self._adjudicate(runner, out, payload)
        self._adjudicate(runner, out, payload)

        written = out.read_text(encoding="utf-8")
        assert "Rejected (intentional / documented tradeoff):" in written
        assert "- [round 1, " in written
        assert written.count("deliberate tradeoff, documented") == 1
        assert "[round 2, " not in written

    def test_deferred_findings_out_hard_errors_on_malformed_existing_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: content matching neither shape must never be overwritten."""
        out = tmp_path / "deferred-findings.md"
        out.write_text("just some notes I left here\n", encoding="utf-8")

        result = runner.invoke(
            main,
            ["review", "adjudicate", "-", "--deferred-findings-out", str(out)],
            input=json.dumps(
                {
                    "verdict": _verdict_payload(
                        _accepted_payload(line_start=2, line_end=2)
                    ),
                    "adjudications": [_defer_entry()],
                }
            ),
        )

        assert result.exit_code == 1
        assert "Could not parse" in result.output
        assert out.read_text(encoding="utf-8") == "just some notes I left here\n"

    def test_deferred_findings_out_reads_pre_1840_legacy_file_without_erroring(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: a legacy-shaped artifact is merged, not rejected."""
        out = tmp_path / "deferred-findings.md"
        out.write_text(_legacy_deferred_file(), encoding="utf-8")

        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(
                    _accepted_payload(line_start=2, line_end=2)
                ),
                "adjudications": [_defer_entry()],
            },
        )

        written = out.read_text(encoding="utf-8")
        assert "settled before round stamping existed" in written
        assert "recorded before round stamping existed" in written
        # The legacy entry contributes no round signal, so the new entry is
        # round 1 -- not round 2 from treating a legacy entry as round 0.
        assert "  round: 1\n" in written
        assert "  round: 2\n" not in written

    def test_deferred_findings_out_legacy_entry_does_not_affect_round_number(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: next_round comes from the stamped prior entries only."""
        out = tmp_path / "deferred-findings.md"
        stamped = Adjudication(
            severity="SHOULD_FIX",
            file="src/cw/older.py",
            summary="Third-round deferral",
            outcome="defer",
            rationale="deferred on the third round",
            round=3,
            recorded_at="2026-08-16T09:00:00Z",
        )
        out.write_text(_legacy_deferred_file(extra=[stamped]), encoding="utf-8")

        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(
                    _accepted_payload(line_start=2, line_end=2)
                ),
                "adjudications": [_defer_entry()],
            },
        )

        written = out.read_text(encoding="utf-8")
        assert "  round: 3\n" in written
        assert "  round: 4\n" in written
        assert "recorded before round stamping existed" in written

    def test_deferred_findings_out_preserves_distinct_findings_sharing_text(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: two distinct same-round findings must not fingerprint-collide.

        Two accepted findings at different lines, deferred with identical
        severity/summary/rationale text (a realistic shape for templated
        review output) -- the CLI's artifact-shape projection strips line
        anchors before merge, so both must still survive as two entries, not
        collapse into one via the dedup fingerprint.
        """
        out = tmp_path / "deferred-findings.md"

        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(
                    _accepted_payload(line_start=10, line_end=10),
                    _accepted_payload(line_start=20, line_end=20),
                ),
                "adjudications": [
                    _defer_entry(line_start=10, line_end=10),
                    _defer_entry(line_start=20, line_end=20),
                ],
            },
        )

        entries = parse_deferred_findings_md(out.read_text(encoding="utf-8"))
        assert len(entries) == 2

    def test_deferred_findings_out_outcome_flip_across_calls_via_cli(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """#1840: REJECT in round 1, DEFER in round 2 for the same finding
        accumulates as two entries end-to-end through the CLI, not just at
        the ``merge_deferred_adjudications`` unit level.
        """
        out = tmp_path / "deferred-findings.md"
        accepted = _accepted_payload(line_start=2, line_end=2)

        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(accepted),
                "adjudications": [
                    _defer_entry(
                        outcome="reject", rationale="deliberate tradeoff, documented"
                    )
                ],
            },
        )
        self._adjudicate(
            runner,
            out,
            {
                "verdict": _verdict_payload(accepted),
                "adjudications": [
                    _defer_entry(outcome="defer", rationale="handle when scale demands")
                ],
            },
        )

        written = out.read_text(encoding="utf-8")
        assert "Rejected (intentional / documented tradeoff):" in written
        assert "deliberate tradeoff, documented" in written
        assert "handle when scale demands" in written

        entries = parse_deferred_findings_md(written)
        assert [e.outcome for e in entries] == ["reject", "defer"]

    def test_malformed_payload_prints_field_path_errors(
        self, runner: CliRunner
    ) -> None:
        payload = {
            "verdict": _verdict_payload(_accepted_payload()),
            "adjudications": [
                {
                    "severity": "CRITICAL",
                    "file": "src/cw/foo.py",
                    "outcome": "reject",
                    "rationale": "why",
                }
            ],
        }
        result = runner.invoke(
            main, ["review", "adjudicate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 1
        assert "adjudications.0.severity" in result.output

    def test_path_argument_reads_from_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        payload = {
            "verdict": _verdict_payload(_accepted_payload(line_start=2, line_end=2)),
            "adjudications": [],
        }
        payload_file = tmp_path / "req.json"
        payload_file.write_text(json.dumps(payload))
        result = runner.invoke(main, ["review", "adjudicate", str(payload_file)])
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["accepted"][0]["disposition"] == "dropped"


class TestReviewVerifyFixesCommand:
    """#1805: ``cw review verify-fixes`` downgrades unverified 'fixed' claims."""

    def test_untouched_fixed_finding_is_downgraded(self, runner: CliRunner) -> None:
        accepted = _accepted_payload(
            file="src/cw/untouched.py", line_start=2, line_end=2
        )
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": _CONSOLIDATE_DIFF,
            "reviewed_sha": "abc1234",
        }
        result = runner.invoke(
            main,
            ["review", "verify-fixes", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["accepted"][0]["disposition"] == "dropped"
        assert "src/cw/untouched.py" in verdict["accepted"][0]["disposition_detail"]

    def test_touched_fixed_finding_is_retained(self, runner: CliRunner) -> None:
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": _CONSOLIDATE_DIFF,
            "reviewed_sha": "abc1234",
        }
        result = runner.invoke(
            main,
            ["review", "verify-fixes", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["accepted"][0]["disposition"] == "fixed"
        assert verdict["accepted"][0]["disposition_detail"] == ""

    def test_malformed_payload_prints_field_path_errors(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "verify-fixes", "--no-base-check", "-"],
            input=json.dumps({"diff": _CONSOLIDATE_DIFF}),
        )
        assert result.exit_code == 1
        assert "verdict" in result.output


_TICKET = "T-1814"


def _voided_payload(**overrides: object) -> dict[str, Any]:
    """A raw ``VoidedFinding`` dict lined up with ``_accepted_payload``."""
    payload: dict[str, Any] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "summary": "Bug here",
        "evidence": "def broken():",
        "operator_comment_id": "mattwwarren@2026-08-11T02:43:30Z",
        "operator_comment_excerpt": "intentional; do not re-raise",
        "voided_at": "2026-08-11T02:43:30Z",
        "original_rationale": "deliberate design choice",
    }
    payload.update(overrides)
    return payload


class TestReviewCheckVoidedCommand:
    """#1814: ``cw review check-voided`` is the Claude path's suppression hop."""

    def _payload(self, **overrides: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verdict": _verdict_payload(_accepted_payload(line_start=2, line_end=2)),
            "ticket_id": _TICKET,
            "comment_bodies": [],
            "new_voided_entries": [],
        }
        payload.update(overrides)
        return payload

    def test_existing_sentinel_comment_suppresses_a_re_derived_finding(
        self, runner: CliRunner
    ) -> None:
        body = render_voided_findings_block(
            [VoidedFinding.model_validate(_voided_payload())]
        )
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(self._payload(comment_bodies=["prose", body])),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["blocking"] is False
        assert out["verdict"]["must_fix"] == []
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"
        assert [a["outcome"] for a in out["adjudications"]] == ["reject"]
        assert out["adjudications"][0]["rationale"].strip()
        # Identity fields come off the matched FINDING, not the void, so a
        # later `cw review adjudicate` pass over the same array still matches.
        assert out["adjudications"][0]["line_start"] == 2

    def test_new_entries_suppress_without_any_prior_comment(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(self._payload(new_voided_entries=[_voided_payload()])),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["accepted"][0]["disposition"] == "rejected"

    def test_emitted_event_correlates_to_the_payload_ticket_id(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(self._payload(new_voided_entries=[_voided_payload()])),
        )

        assert result.exit_code == 0, result.output
        events = read_events(event_types=[OrchestratorEventType.REVIEW_FINDING_VOIDED])
        assert len(events) == 1
        assert events[0].correlation_id == _TICKET

    def test_no_match_leaves_the_verdict_blocking(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps(
                self._payload(
                    new_voided_entries=[_voided_payload(summary="a different bug")]
                )
            ),
        )

        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["verdict"]["blocking"] is True
        assert out["adjudications"] == []

    def test_malformed_payload_prints_field_path_errors(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "check-voided", "-"],
            input=json.dumps({"ticket_id": "T-1"}),
        )

        assert result.exit_code == 1
        assert "verdict" in result.output

    def test_voided_findings_out_writes_the_merged_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "nested" / "voided-findings-comment.md"
        prior = _voided_payload(summary="an earlier void", evidence="def earlier():")
        body = render_voided_findings_block([VoidedFinding.model_validate(prior)])
        result = runner.invoke(
            main,
            [
                "review",
                "check-voided",
                "--voided-findings-out",
                str(out_path),
                "-",
            ],
            input=json.dumps(
                self._payload(
                    comment_bodies=[body], new_voided_entries=[_voided_payload()]
                )
            ),
        )

        assert result.exit_code == 0, result.output
        merged = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert [entry.summary for entry in merged] == ["an earlier void", "Bug here"]

    def test_voided_findings_out_writes_nothing_when_there_is_nothing_to_record(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "voided-findings-comment.md"
        result = runner.invoke(
            main,
            ["review", "check-voided", "--voided-findings-out", str(out_path), "-"],
            input=json.dumps(self._payload()),
        )

        assert result.exit_code == 0, result.output
        assert not out_path.exists()

    def test_absent_voided_at_is_stamped_by_the_cli(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The Claude session supplies the judgment; the CLI supplies the clock."""
        out_path = tmp_path / "voided-findings-comment.md"
        entry = _voided_payload()
        del entry["voided_at"]
        result = runner.invoke(
            main,
            ["review", "check-voided", "--voided-findings-out", str(out_path), "-"],
            input=json.dumps(self._payload(new_voided_entries=[entry])),
        )

        assert result.exit_code == 0, result.output
        written = parse_voided_findings_block([out_path.read_text(encoding="utf-8")])
        assert written[0].voided_at != ""


_SETTLE_REASON = "operator rejected: intentional tradeoff, see ADR-0012"


def _write_session_context(root: Path, *, headless: bool) -> None:
    """Stamp *root* with the ``.claude/cw-context.json`` ``cw`` would write.

    Shared by every settle case because the refusal now fails CLOSED (#2210
    round 4): a directory with no resolvable dispatch context refuses, so
    "operator's machine" has to be expressed as a context reporting
    ``headless: false`` rather than as the absence of one. Joins
    :data:`~cw.models.HOOK_CONTEXT_RELATIVE_PATH`, the same constant the
    writer and the guard use, so this fixture cannot drift onto another path.
    """
    path = root / HOOK_CONTEXT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 8, "session_id": "abc", "headless": headless}),
        encoding="utf-8",
    )


class TestReviewSettle:
    """#2210: ``cw review settle`` is the ledger's first production writer.

    It is also the ledger's only *durable silencer*, so every test here is as
    much about the audit trail as about the marker: a settle that cannot say
    who ran it, when, and against which reviewed sha is not a record, and a
    settle run from inside a dispatch worker is the pipeline silencing its own
    reviewer.
    """

    @pytest.fixture(autouse=True)
    def _operator_machine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Run every settle test from an interactive cw session worktree.

        The guard fails CLOSED since #2210 round 4: the ONLY state it proceeds
        from is a discovered ``.claude/cw-context.json`` whose ``headless`` is
        the JSON boolean ``false``, which is what ``cw`` stamps for an
        interactive session. The repo checkout this suite runs in carries a
        real context of its own (``headless: true`` under dispatch), so every
        case needs its own; writing one here rather than per test keeps the
        cases about what they are testing.
        """
        _write_session_context(tmp_path, headless=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: _OPERATOR)

    def _invoke(self, runner: CliRunner, payload: dict[str, Any], *args: str) -> Result:
        extra = list(args)
        if "--reason" not in extra:
            extra = ["--reason", _SETTLE_REASON, *extra]
        return runner.invoke(
            main,
            ["review", "settle", *extra, "-"],
            input=json.dumps(payload),
        )

    def _only_entry(self, output: str) -> FindingDisposition:
        return next(iter(parse_finding_disposition_block([output])[0].values()))

    def test_happy_path_renders_the_postable_marker(self, runner: CliRunner) -> None:
        result = self._invoke(runner, _settle_payload())

        assert result.exit_code == 0, result.output
        assert result.output.startswith("## Review Finding Dispositions")
        ledger, refused = parse_finding_disposition_block([result.output])
        assert refused == []
        assert list(ledger) == [_disposition_key("src/cw/foo.py", "Bug here")]
        assert next(iter(ledger.values())).outcome == "REJECTED"

    def test_the_minted_key_binds_the_verbatim_summary_digest(
        self, runner: CliRunner
    ) -> None:
        """#2210 round 3: the settle -> reader round trip keeps the binding.

        The key the command mints ends in the SHA-256 of the exact summary the
        record stores, so the reader's provenance check accepts it (nothing is
        refused) and a finding with any other wording cannot match it.
        """
        result = self._invoke(runner, _settle_payload())

        assert result.exit_code == 0, result.output
        ledger, refused = parse_finding_disposition_block([result.output])
        assert refused == []
        ((key, entry),) = ledger.items()
        assert entry.summary == "Bug here"
        assert key.endswith("::" + hashlib.sha256(b"Bug here").hexdigest())

    def test_out_writes_the_file_and_creates_parents(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "nested" / "settle.md"
        result = self._invoke(runner, _settle_payload(), "--out", str(out_path))

        assert result.exit_code == 0, result.output
        assert parse_finding_disposition_block([out_path.read_text(encoding="utf-8")])[
            0
        ]

    @freeze_time("2026-09-20T12:00:00Z")
    def test_record_carries_actor_timestamp_identity_and_sha(
        self, runner: CliRunner
    ) -> None:
        """ "Who silenced this, when, against what code" — all four, durably."""
        result = self._invoke(runner, _settle_payload())

        assert result.exit_code == 0, result.output
        entry = self._only_entry(result.output)
        assert entry.actor == _OPERATOR
        assert entry.recorded_at == "2026-09-20T12:00:00Z"
        assert entry.reviewed_sha == "abc1234"
        # The key holds only the NORMALISED summary; the verbatim one is what a
        # future per-record rollback targets.
        assert entry.summary == "Bug here"
        assert entry.rationale == _SETTLE_REASON

    def test_missing_reason_is_a_usage_error(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "settle", "-"],
            input=json.dumps(_settle_payload()),
        )
        assert result.exit_code != 0
        assert "--reason" in result.output

    @pytest.mark.parametrize("reason", ["", "   ", "\t\n "])
    def test_blank_reason_is_refused_and_writes_nothing(
        self, runner: CliRunner, tmp_path: Path, reason: str
    ) -> None:
        out_path = tmp_path / "settle.md"
        result = self._invoke(
            runner,
            _settle_payload(),
            "--reason",
            reason,
            "--out",
            str(out_path),
        )

        assert result.exit_code != 0
        assert "REVIEW-FINDING-DISPOSITIONS" not in result.output
        assert not out_path.exists()
        assert read_events() == []

    def test_per_entry_rationale_overrides_the_reason(self, runner: CliRunner) -> None:
        result = self._invoke(
            runner, _settle_payload(_settle_entry(rationale="this one specifically"))
        )

        assert result.exit_code == 0, result.output
        assert self._only_entry(result.output).rationale == "this one specifically"

    def test_operator_supplied_recorded_at_is_refused(self, runner: CliRunner) -> None:
        """``recorded_at`` is audit data, so only the CLI clock may set it."""
        result = self._invoke(
            runner,
            {"entries": [{**_settle_entry(), "recorded_at": "1999-01-01T00:00:00Z"}]},
        )

        assert result.exit_code == 1
        assert "recorded_at" in result.output

    def test_entry_without_a_resolvable_sha_is_refused(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "settle.md"
        result = self._invoke(
            runner,
            _settle_payload(_settle_entry(reviewed_sha="")),
            "--out",
            str(out_path),
        )

        assert result.exit_code != 0
        assert "reviewed_sha" in result.output
        assert not out_path.exists()
        assert read_events() == []

    def test_reviewed_sha_option_supplies_a_hand_written_payload(
        self, runner: CliRunner
    ) -> None:
        result = self._invoke(
            runner,
            _settle_payload(_settle_entry(reviewed_sha="")),
            "--reviewed-sha",
            "deadbee",
        )

        assert result.exit_code == 0, result.output
        assert self._only_entry(result.output).reviewed_sha == "deadbee"

    def test_entry_sha_wins_over_the_option(self, runner: CliRunner) -> None:
        result = self._invoke(runner, _settle_payload(), "--reviewed-sha", "deadbee")

        assert result.exit_code == 0, result.output
        assert self._only_entry(result.output).reviewed_sha == "abc1234"

    def test_unresolvable_actor_is_refused_and_writes_nothing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
        out_path = tmp_path / "settle.md"
        result = self._invoke(runner, _settle_payload(), "--out", str(out_path))

        assert result.exit_code != 0
        assert "identity" in result.output
        assert not out_path.exists()
        assert read_events() == []

    @freeze_time("2026-09-20T12:00:00Z")
    def test_one_audit_event_per_settled_finding(self, runner: CliRunner) -> None:
        result = self._invoke(
            runner,
            _settle_payload(
                _settle_entry(),
                _settle_entry(summary="Second bug", file="src/cw/bar.py"),
            ),
            "--ticket",
            "2210",
        )

        assert result.exit_code == 0, result.output
        events = [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.REVIEW_FINDING_SETTLED
        ]
        assert len(events) == 2
        assert {e.correlation_id for e in events} == {"2210"}
        payload = next(
            e.payload for e in events if e.payload["file"] == "src/cw/foo.py"
        )
        assert payload["actor"] == _OPERATOR
        assert payload["summary"] == "Bug here"
        assert payload["outcome"] == "REJECTED"
        assert payload["reason"] == _SETTLE_REASON
        assert payload["reviewed_sha"] == "abc1234"
        assert payload["recorded_at"] == "2026-09-20T12:00:00Z"
        assert payload["key"] == _disposition_key("src/cw/foo.py", "Bug here")

    def test_the_audit_event_is_recorded_before_the_marker_is_written(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 2: the record comes first, the durable suppression second.

        A marker written before its audit event leaves a durable suppression
        with no audit record behind if the emit then fails — precisely what
        round 1 added the event for.
        """
        out_path = tmp_path / "settle.md"
        real = cw.events.record_event
        marker_existed_at_emit: list[bool] = []

        def _spy(*args: Any, **kwargs: Any) -> object:
            marker_existed_at_emit.append(out_path.exists())
            return real(*args, **kwargs)

        monkeypatch.setattr("cw.events.record_event", _spy)
        result = self._invoke(runner, _settle_payload(), "--out", str(out_path))

        assert result.exit_code == 0, result.output
        assert marker_existed_at_emit == [False]
        assert out_path.exists()

    def test_failed_audit_emit_aborts_with_no_marker(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out_path = tmp_path / "settle.md"

        def _boom(*_args: Any, **_kwargs: Any) -> object:
            raise OSError(_EVENT_STORE_FAILURE)

        monkeypatch.setattr("cw.events.record_event", _boom)
        result = self._invoke(runner, _settle_payload(), "--out", str(out_path))

        assert result.exit_code != 0
        assert not out_path.exists()
        assert "REVIEW-FINDING-DISPOSITIONS" not in result.output
        # The message must name the finding that failed and the failure.
        assert "src/cw/foo.py" in result.output
        assert _EVENT_STORE_FAILURE in result.output

    def test_a_later_entrys_failed_emit_still_writes_no_marker(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All events first, then the marker — atomic in the safe direction.

        An audit record with no effect is noise; a suppression with no audit
        record is invisible. So a failure part-way through leaves the earlier
        events recorded and writes NO marker at all.
        """
        out_path = tmp_path / "settle.md"
        real = cw.events.record_event
        calls: list[int] = []

        def _fail_on_second(*args: Any, **kwargs: Any) -> object:
            calls.append(1)
            if len(calls) == 2:
                raise OSError(_EVENT_STORE_FAILURE)
            return real(*args, **kwargs)

        monkeypatch.setattr("cw.events.record_event", _fail_on_second)
        result = self._invoke(
            runner,
            _settle_payload(
                _settle_entry(),
                _settle_entry(summary="Second bug", file="src/cw/bar.py"),
            ),
            "--out",
            str(out_path),
        )

        assert result.exit_code != 0
        assert not out_path.exists()
        assert "REVIEW-FINDING-DISPOSITIONS" not in result.output
        assert len(read_events()) == 1

    def test_duplicate_entries_settle_once_and_emit_one_event(
        self, runner: CliRunner
    ) -> None:
        result = self._invoke(
            runner,
            _settle_payload(
                _settle_entry(rationale="older"), _settle_entry(rationale="newer")
            ),
        )

        assert result.exit_code == 0, result.output
        ledger, refused = parse_finding_disposition_block([result.output])
        assert refused == []
        assert len(ledger) == 1
        assert next(iter(ledger.values())).rationale == "newer"
        assert len(read_events()) == 1

    def test_refuses_inside_a_dispatch_worker(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A worker settling its own reviewer's findings is self-suppression."""
        _write_session_context(tmp_path, headless=True)
        out_path = tmp_path / "settle.md"
        result = self._invoke(runner, _settle_payload(), "--out", str(out_path))

        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert "REVIEW-FINDING-DISPOSITIONS" not in result.output
        assert not out_path.exists()
        assert read_events() == []

    def test_refusal_finds_the_context_from_a_subdirectory(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_session_context(tmp_path, headless=True)
        nested = tmp_path / "src" / "cw"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        result = self._invoke(runner, _settle_payload())
        assert result.exit_code != 0
        assert "dispatch worker" in result.output

    def test_an_interactive_session_context_proceeds(self, runner: CliRunner) -> None:
        """``headless: false`` — a real JSON boolean — is the ONLY pass state."""
        result = self._invoke(runner, _settle_payload())

        assert result.exit_code == 0, result.output
        assert "REVIEW-FINDING-DISPOSITIONS" in result.output

    def test_a_reversed_outcome_renders_a_marker(self, runner: CliRunner) -> None:
        """#2232: rollback is settle with a third outcome, not a new command."""
        result = self._invoke(
            runner, _settle_payload(_settle_entry(outcome="REVERSED"))
        )

        assert result.exit_code == 0, result.output
        ledger, refused = parse_finding_disposition_block([result.output])
        assert refused == []
        assert list(ledger) == [_disposition_key("src/cw/foo.py", "Bug here")]
        assert next(iter(ledger.values())).outcome == "REVERSED"

    def test_a_reversal_emits_the_reverted_event_not_the_settled_one(
        self, runner: CliRunner
    ) -> None:
        """The event TYPE carries the semantic, not an `outcome` field (#2232).

        An operator asking "what have I withdrawn" must be able to answer it
        with one `cw event tail --type review.finding_disposition_reverted`,
        not by filtering settles on payload content — the convention every
        other event in this region already follows.
        """
        result = self._invoke(
            runner,
            _settle_payload(_settle_entry(outcome="REVERSED")),
            "--ticket",
            "2232",
        )

        assert result.exit_code == 0, result.output
        events = read_events()
        assert [e.type for e in events] == [
            OrchestratorEventType.REVIEW_FINDING_DISPOSITION_REVERTED
        ]
        assert events[0].correlation_id == "2232"
        assert events[0].payload["outcome"] == "REVERSED"
        assert events[0].payload["file"] == "src/cw/foo.py"
        assert events[0].payload["summary"] == "Bug here"

    def test_a_mixed_payload_emits_one_event_of_each_type(
        self, runner: CliRunner
    ) -> None:
        result = self._invoke(
            runner,
            _settle_payload(
                _settle_entry(),
                _settle_entry(
                    summary="Second bug", file="src/cw/bar.py", outcome="REVERSED"
                ),
            ),
        )

        assert result.exit_code == 0, result.output
        by_type = {e.type: e.payload for e in read_events()}
        assert set(by_type) == {
            OrchestratorEventType.REVIEW_FINDING_SETTLED,
            OrchestratorEventType.REVIEW_FINDING_DISPOSITION_REVERTED,
        }
        assert (
            by_type[OrchestratorEventType.REVIEW_FINDING_SETTLED]["file"]
            == "src/cw/foo.py"
        )
        assert (
            by_type[OrchestratorEventType.REVIEW_FINDING_DISPOSITION_REVERTED]["file"]
            == "src/cw/bar.py"
        )

    def test_a_failed_emit_for_a_reversal_names_the_reversal_event(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error must not report the wrong event name (#2232)."""

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError(_EVENT_STORE_FAILURE)

        monkeypatch.setattr(cw.events, "record_event", _boom)
        result = self._invoke(
            runner, _settle_payload(_settle_entry(outcome="REVERSED"))
        )

        assert result.exit_code != 0
        assert "review.finding_disposition_reverted" in result.output
        assert "review.finding_settled" not in result.output

    def test_the_worker_refusal_is_outcome_agnostic(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A REVERSED payload gets no special-case bypass of the guard (#2232).

        A reversal is still a durable ledger write, and the guard exists
        because the pipeline must not be able to edit its own suppressions in
        either direction.
        """
        _write_session_context(tmp_path, headless=True)
        monkeypatch.chdir(tmp_path)
        result = self._invoke(
            runner, _settle_payload(_settle_entry(outcome="REVERSED"))
        )

        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert read_events() == []

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("no context file at all", None),
            ("no headless key", '{"session_id": "abc"}'),
            ("malformed json", "{not json"),
            ("empty file", ""),
            ("non-object payload", '["headless"]'),
            ("headless is a string", '{"headless": "false"}'),
            ("headless is a number", '{"headless": 0}'),
            ("headless is null", '{"headless": null}'),
        ],
    )
    def test_an_indeterminate_context_refuses_and_writes_nothing(
        self, runner: CliRunner, tmp_path: Path, label: str, body: str | None
    ) -> None:
        """Fail CLOSED (#2210 round 4): "cannot tell" is treated as "worker".

        ``find_cw_context`` returns ``None`` both for "no dispatch context
        anywhere above cwd" and for "the context is there but unreadable", and
        a non-bool ``headless`` says nothing either way. None of those is
        evidence that an operator is at the keyboard, and this guard decides
        whether a durable, invisible suppression may be minted — the same
        fail-closed posture as #2213.
        """
        assert label
        context_path = tmp_path / HOOK_CONTEXT_RELATIVE_PATH
        if body is None:
            context_path.unlink()
        else:
            context_path.write_text(body, encoding="utf-8")
        out_path = tmp_path / "settle.md"

        result = self._invoke(runner, _settle_payload(), "--out", str(out_path))

        assert result.exit_code != 0
        assert "could not be resolved" in result.output
        assert "REVIEW-FINDING-DISPOSITIONS" not in result.output
        assert not out_path.exists()
        assert read_events() == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"entries": [_settle_entry(file="N/A")]},
            {"entries": [_settle_entry(file="  ")]},
            {"entries": [_settle_entry(summary="")]},
            {"entries": [_settle_entry(outcome="MAYBE")]},
            {"entries": []},
        ],
    )
    def test_invalid_payloads_exit_one_with_field_path_errors(
        self, runner: CliRunner, payload: dict[str, Any]
    ) -> None:
        result = self._invoke(runner, payload)

        assert result.exit_code == 1
        assert "entries" in result.output

    def test_malformed_json_exits_one(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "settle", "--reason", _SETTLE_REASON, "-"],
            input="{not json",
        )
        assert result.exit_code != 0

    def test_payload_pasted_from_a_blocking_comment_is_sufficient_with_no_editing(
        self, runner: CliRunner
    ) -> None:
        """The comment's payload carries the ledger's whole identity (#2210)."""
        from cw.codex_review import render_verdict_comment
        from cw.review_finding_dispositions import suppress_adjudicated_findings
        from cw.review_findings import consolidate_verdict

        finding = _make_finding(severity="MUST_FIX")
        verdict = consolidate_verdict(
            [_make_reviewer_doc(finding)], _make_diff(), reviewed_sha="sha"
        )
        assert verdict.blocking is True
        comment = render_verdict_comment(verdict, fix_loop_enabled=False)
        payloads = _extract_settle_payloads(comment)
        assert payloads

        result = self._invoke(runner, payloads[0])
        assert result.exit_code == 0, result.output
        ledger, refused = parse_finding_disposition_block([result.output])
        assert refused == []
        assert next(iter(ledger.values())).reviewed_sha == "sha"

        suppressed = suppress_adjudicated_findings(verdict, ledger, ticket_id="T-2210")
        assert suppressed.blocking is False
        assert suppressed.accepted[0].disposition == "rejected"


class TestReviewVerifyFixesBaseFlag:
    """#1988: --base proves verify-fixes' diff is the real fix-cycle diff."""

    def test_neither_base_nor_no_base_check_is_usage_error(
        self, runner: CliRunner
    ) -> None:
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": _CONSOLIDATE_DIFF,
            "reviewed_sha": "abc1234",
        }
        result = runner.invoke(
            main, ["review", "verify-fixes", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 2, result.output
        assert "--base" in result.output
        assert "--no-base-check" in result.output

    def test_base_and_no_base_check_together_is_usage_error(
        self, runner: CliRunner
    ) -> None:
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": _CONSOLIDATE_DIFF,
            "reviewed_sha": "abc1234",
        }
        result = runner.invoke(
            main,
            ["review", "verify-fixes", "--base", "main", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 2, result.output
        assert "mutually exclusive" in result.output

    def test_no_base_check_skips_verification(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accepted = _accepted_payload(line_start=2, line_end=2)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": _CONSOLIDATE_DIFF,
            "reviewed_sha": "abc1234",
        }
        baseline = runner.invoke(
            main,
            ["review", "verify-fixes", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert baseline.exit_code == 0, baseline.output

        calls: list[object] = []

        def _boom(*args: object, **kwargs: object) -> object:
            calls.append(args)
            msg = "run_git must not be called without --base"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.cli.review._diff_integrity.run_git", _boom)
        result = runner.invoke(
            main,
            ["review", "verify-fixes", "--no-base-check", "-"],
            input=json.dumps(payload),
        )

        assert calls == []
        assert result.exit_code == 0, result.output
        assert result.output == baseline.output

    def test_base_matching_diff_passes(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, sha, real_diff = _branch_repo(make_git_repo, "verify-match")
        accepted = _accepted_payload(file="src/thing.py", line_start=1, line_end=1)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": real_diff,
            "reviewed_sha": sha,
        }
        result = runner.invoke(
            main,
            [
                "review",
                "verify-fixes",
                "--worktree",
                str(repo),
                "--base",
                "main",
                "-",
            ],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["accepted"][0]["disposition"] == "fixed"
        assert verdict["accepted"][0]["disposition_detail"] == ""

    def test_base_mismatched_diff_errors(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, sha, real_diff = _branch_repo(make_git_repo, "verify-mismatch")
        mutated = real_diff.replace("+y = 2", "+y = 3")
        assert mutated != real_diff
        accepted = _accepted_payload(file="src/thing.py", line_start=1, line_end=1)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": mutated,
            "reviewed_sha": sha,
        }
        result = runner.invoke(
            main,
            [
                "review",
                "verify-fixes",
                "--worktree",
                str(repo),
                "--base",
                "main",
                "-",
            ],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        assert '"blocking"' not in result.output

    def test_base_unresolvable_ref_errors(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, sha, real_diff = _branch_repo(make_git_repo, "verify-badref")
        accepted = _accepted_payload(file="src/thing.py", line_start=1, line_end=1)
        payload = {
            "verdict": _verdict_payload(accepted),
            "diff": real_diff,
            "reviewed_sha": sha,
        }
        result = runner.invoke(
            main,
            [
                "review",
                "verify-fixes",
                "--worktree",
                str(repo),
                "--base",
                "no-such-ref",
                "-",
            ],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        assert "no-such-ref" in result.output

    def test_base_with_reviewed_sha_matches_verdict_reviewed_sha(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        """``verdict.reviewed_sha`` (the Checkpoint-3a-frozen sha) and the
        payload's own ``reviewed_sha`` (the --base check's fix-cycle tip) are
        independent fields the command never cross-checks — a payload
        carrying two different shas for the two purposes still round-trips
        cleanly.
        """
        repo, sha, real_diff = _branch_repo(make_git_repo, "verify-independent")
        accepted = _accepted_payload(file="src/thing.py", line_start=1, line_end=1)
        payload = {
            "verdict": _verdict_payload(accepted, reviewed_sha="different-sha"),
            "diff": real_diff,
            "reviewed_sha": sha,
        }
        result = runner.invoke(
            main,
            [
                "review",
                "verify-fixes",
                "--worktree",
                str(repo),
                "--base",
                "main",
                "-",
            ],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["reviewed_sha"] == "different-sha"


class TestReviewDispositions:
    """#2232: the read-only "what is suppressing right now" view.

    ADR-0016 named this as a precondition for ever arming the fuzzy claim
    tier: the ledger silences findings durably and invisibly, and until this
    command there was no way to ask a ticket what it currently holds.
    """

    def _seed(
        self, *entries: tuple[str, str, dict[str, Any]], ticket_id: str = "T-2232"
    ) -> None:
        ledger: dict[str, FindingDisposition] = {}
        for file, summary, overrides in entries:
            key = _disposition_key(file, summary)
            assert key is not None
            payload: dict[str, Any] = {
                "outcome": "REJECTED",
                "rationale": "settled in an earlier round",
                "recorded_at": "2026-08-16T00:00:00Z",
                "actor": _OPERATOR,
                "reviewed_sha": "abc1234",
                "summary": summary,
            }
            payload.update(overrides)
            ledger[key] = FindingDisposition.model_validate(payload)
        add_ticket(
            TicketTask(
                ticket_id=ticket_id,
                client="acme",
                stage=Stage.REVIEW,
                finding_dispositions=ledger,
            )
        )

    def _invoke(self, runner: CliRunner, *args: str) -> Result:
        return runner.invoke(main, ["review", "dispositions", *args])

    def test_a_ticket_with_no_records_says_so(self, runner: CliRunner) -> None:
        self._seed()
        result = self._invoke(runner, "T-2232", "--client", "acme")

        assert result.exit_code == 0, result.output
        assert "No disposition records" in result.output

    def test_one_record_renders_its_identity_and_provenance(
        self, runner: CliRunner
    ) -> None:
        self._seed(("src/cw/foo.py", "Bug here", {}))
        result = self._invoke(runner, "T-2232", "--client", "acme")

        assert result.exit_code == 0, result.output
        assert "src/cw/foo.py" in result.output
        # #2232 round 3 fix: the table shows the verbatim summary, not the
        # normalized key half — an operator copying it into `cw review
        # settle` needs the text that actually matches the ledger key.
        assert "Bug here" in result.output
        assert "REJECTED" in result.output
        assert _OPERATOR in result.output
        assert "abc1234" in result.output

    def test_every_outcome_is_listed_with_its_own_label(
        self, runner: CliRunner
    ) -> None:
        """A withdrawal must never read as a live suppression (#2232).

        This is the command's whole safety purpose. Filtering REVERSED out
        would hide the record's existence entirely, which is worse than
        showing one clearly labelled — an operator about to arm the claim
        tier has to be able to tell the three apart at a glance.
        """
        self._seed(
            ("src/cw/a.py", "Rejected bug", {"outcome": "REJECTED"}),
            ("src/cw/b.py", "Accepted bug", {"outcome": "ACCEPTED"}),
            ("src/cw/c.py", "Reversed bug", {"outcome": "REVERSED"}),
        )
        result = self._invoke(runner, "T-2232", "--client", "acme")

        assert result.exit_code == 0, result.output
        for file in ("src/cw/a.py", "src/cw/b.py", "src/cw/c.py"):
            assert file in result.output
        for outcome in ("REJECTED", "ACCEPTED", "REVERSED"):
            assert outcome in result.output

    def test_json_round_trips_the_full_record_plus_its_key(
        self, runner: CliRunner
    ) -> None:
        self._seed(
            ("src/cw/a.py", "Rejected bug", {}),
            ("src/cw/c.py", "Reversed bug", {"outcome": "REVERSED"}),
        )
        result = self._invoke(runner, "T-2232", "--client", "acme", "--json")

        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) == 2
        assert {row["outcome"] for row in rows} == {"REJECTED", "REVERSED"}
        assert {row["file"] for row in rows} == {"src/cw/a.py", "src/cw/c.py"}
        for row in rows:
            assert row["key"] == _disposition_key(row["file"], row["summary"])
            assert row["actor"] == _OPERATOR
            # No worktree given, so drift is unanswerable rather than "no".
            assert row["stale"] == "?"

    def test_a_long_summary_is_visibly_truncated_in_the_table(
        self, runner: CliRunner
    ) -> None:
        """#2232 round 4: a silent cut turned this column into a bug.

        The table truncates SUMMARY to fit; a truncated cell must look
        truncated, or an operator mistakes the shortened text for the whole
        identity and pastes it into `cw review settle`, which matches on the
        full verbatim summary and silently no-ops.
        """
        long_summary = "This finding summary runs well past the column width"
        self._seed(("src/cw/a.py", long_summary, {}))
        result = self._invoke(runner, "T-2232", "--client", "acme")

        assert result.exit_code == 0, result.output
        assert long_summary not in result.output
        assert "…" in result.output

    def test_a_long_summary_is_not_truncated_in_json(self, runner: CliRunner) -> None:
        """The JSON surface is the documented payload source (#2232 round 4)."""
        long_summary = "This finding summary runs well past the column width"
        self._seed(("src/cw/a.py", long_summary, {}))
        result = self._invoke(runner, "T-2232", "--client", "acme", "--json")

        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert rows[0]["summary"] == long_summary

    def test_json_for_an_empty_ledger_is_an_empty_array(
        self, runner: CliRunner
    ) -> None:
        self._seed()
        result = self._invoke(runner, "T-2232", "--client", "acme", "--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []

    def test_an_unresolvable_client_is_a_clean_error(self, runner: CliRunner) -> None:
        self._seed()
        result = self._invoke(runner, "T-2232")

        assert result.exit_code != 0
        assert "Cannot resolve client" in result.output

    def test_an_unknown_ticket_is_a_clean_error_not_a_crash(
        self, runner: CliRunner
    ) -> None:
        self._seed()
        result = self._invoke(runner, "T-nope", "--client", "acme")

        assert result.exit_code != 0
        assert "No dev-queue task found" in result.output

    def test_a_drifted_record_is_flagged_stale_against_a_worktree(
        self,
        runner: CliRunner,
        make_git_repo: Callable[..., Path],
    ) -> None:
        worktree = make_git_repo("wt-2232-cli-drift")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        settled_at = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 2  # reworked\n")
        self._seed(("src/cw/foo.py", "Bug here", {"reviewed_sha": settled_at}))

        result = self._invoke(
            runner, "T-2232", "--client", "acme", "--worktree", str(worktree), "--json"
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["yes"]

    def test_an_undrifted_record_is_not_flagged_stale(
        self,
        runner: CliRunner,
        make_git_repo: Callable[..., Path],
    ) -> None:
        worktree = make_git_repo("wt-2232-cli-clean")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        settled_at = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/other.py", "b = 2\n")
        self._seed(("src/cw/foo.py", "Bug here", {"reviewed_sha": settled_at}))

        result = self._invoke(
            runner, "T-2232", "--client", "acme", "--worktree", str(worktree), "--json"
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["no"]

    def test_a_worktree_that_is_not_a_repo_degrades_to_unknown(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Showing the ledger matters more than answering the drift question."""
        self._seed(("src/cw/foo.py", "Bug here", {}))
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        result = self._invoke(
            runner,
            "T-2232",
            "--client",
            "acme",
            "--worktree",
            str(not_a_repo),
            "--json",
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["?"]

    def test_an_unrunnable_git_degrades_to_unknown(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No git on PATH must not cost the operator the ledger listing."""
        self._seed(("src/cw/foo.py", "Bug here", {}))

        def _raise(*_args: object, **_kwargs: object) -> None:
            msg = "git is gone"
            raise OSError(msg)

        monkeypatch.setattr("cw._git.subprocess.run", _raise)
        result = self._invoke(
            runner, "T-2232", "--client", "acme", "--worktree", str(tmp_path), "--json"
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["?"]

    def test_a_record_with_no_reviewed_sha_reads_unknown_not_clean(
        self,
        runner: CliRunner,
        make_git_repo: Callable[..., Path],
    ) -> None:
        """#2232 SHOULD_FIX 6: blank stored sha is 'cannot tell', not 'no'.

        ``disposition_drifted`` answers ``False`` for a blank reviewed sha so
        a missing field cannot manufacture drift on the suppression path. On
        a surface whose whole job is surfacing staleness, rendering that as
        "no" conflates "verified unchanged" with "nothing to compare against".
        """
        worktree = make_git_repo("wt-2232-cli-blank-sha")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        self._seed(("src/cw/foo.py", "Bug here", {"reviewed_sha": ""}))

        result = self._invoke(
            runner, "T-2232", "--client", "acme", "--worktree", str(worktree), "--json"
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["?"]

    def test_an_inherited_git_dir_cannot_redirect_the_head_lookup(
        self,
        runner: CliRunner,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2232 MUST_FIX 1: --worktree decides the repo, not the ambient env.

        Run from inside a git hook, an unsanitized ``git rev-parse HEAD``
        answers for the HOOK's repository — so the drift comparison would be
        against a sha from a tree the operator never named.
        """
        worktree = make_git_repo("wt-2232-cli-hook")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        settled_at = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 2  # reworked\n")
        decoy = make_git_repo("wt-2232-cli-hook-decoy")
        commit_tracked_file(decoy, "src/cw/foo.py", "a = 1\n")
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
        self._seed(("src/cw/foo.py", "Bug here", {"reviewed_sha": settled_at}))

        result = self._invoke(
            runner, "T-2232", "--client", "acme", "--worktree", str(worktree), "--json"
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["yes"]

    def test_the_table_carries_reason_and_age(self, runner: CliRunner) -> None:
        """#2232 MUST_FIX 5: the acceptance text asks for actor, reason, age, sha."""
        self._seed(
            (
                "src/cw/foo.py",
                "Bug here",
                {
                    "rationale": "intentional tradeoff",
                    "recorded_at": "2026-09-20T13:00:00Z",
                },
            )
        )
        with freeze_time("2026-09-22T13:00:00Z"):
            result = self._invoke(runner, "T-2232", "--client", "acme")

        assert result.exit_code == 0, result.output
        header, _rule, row = result.output.splitlines()
        assert "REASON" in header
        assert "AGE" in header
        assert "intentional tradeoff" in row
        assert "2d" in row

    @pytest.mark.parametrize(
        ("recorded_at", "expected"),
        [
            ("", "?"),
            ("not-a-timestamp", "?"),
            ("2026-09-22T11:00:00+00:00", "2h"),
            ("2026-09-22T13:00:00Z", "0m"),
            ("2026-09-19T13:00:00+00:00", "3d"),
            # A naive stamp is read as UTC, which is what `cw review settle`
            # writes; a future stamp floors at zero rather than going negative.
            ("2026-09-22T12:00:00", "1h"),
            ("2026-09-23T13:00:00+00:00", "0m"),
        ],
    )
    def test_age_is_computed_defensively(self, recorded_at: str, expected: str) -> None:
        """A malformed or missing timestamp renders unknown, never raises."""
        with freeze_time("2026-09-22T13:00:00Z"):
            assert _age_cell(recorded_at) == expected

    def test_the_drift_column_ignores_the_lane_gate(
        self,
        runner: CliRunner,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2232: this diagnostic is not gated by disposition_drift_check_enabled.

        The gate scopes the automatic check on the shared suppression path.
        An operator who turned it off to debug is precisely the one who still
        needs to be able to see what is stale.
        """
        worktree = make_git_repo("wt-2232-cli-gated")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
        settled_at = git_in(worktree, "rev-parse", "HEAD")
        commit_tracked_file(worktree, "src/cw/foo.py", "a = 2  # reworked\n")
        self._seed(("src/cw/foo.py", "Bug here", {"reviewed_sha": settled_at}))
        monkeypatch.setattr(
            "cw.cli.review.dispositions.load_effective_config",
            lambda: OrchestratorConfig(disposition_drift_check_enabled=False),
        )

        result = self._invoke(
            runner, "T-2232", "--client", "acme", "--worktree", str(worktree), "--json"
        )

        assert result.exit_code == 0, result.output
        assert [row["stale"] for row in json.loads(result.output)] == ["yes"]
