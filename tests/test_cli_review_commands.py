"""Tests for the ``cw review`` subcommands in ``cw.cli.review.commands``.

Covers ``register``, ``adjudicate``, ``check-voided`` and ``verify-fixes``
(GitHub #1154, RFC 0011 S2; #1241). Split out of ``tests/test_cli_review.py``
for #2049 so the test modules mirror the ``src/cw/cli/review/`` package seams.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.models.enums import OrchestratorEventType
from cw.review_adjudication import (
    Adjudication,
    VoidedFinding,
    parse_deferred_findings_md,
    parse_voided_findings_block,
    render_deferred_findings_md,
    render_voided_findings_block,
)
from tests._cli_review_helpers import (
    _CONSOLIDATE_DIFF,
    _branch_repo,
    _consolidate_payload,
)
from tests.conftest import _finding_kwargs, _make_finding, _make_reviewer_doc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


_URL = "https://github.com/acme/widgets/pull/42"
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
            msg = "subprocess.run must not be called without --base"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.cli.review._diff_integrity.subprocess.run", _boom)
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
