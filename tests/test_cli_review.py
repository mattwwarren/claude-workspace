"""Tests for the ``cw review`` CLI group (GitHub #1154, RFC 0011 S2; #1241)."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review import _build_captured_diff
from cw.codex_review import _parse_unified_diff
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
from cw.review_findings import ReviewerRunFailure

from .conftest import (
    _finding_kwargs,
    _make_escalation,
    _make_finding,
    _make_reviewer_doc,
    commit_tracked_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_URL = "https://github.com/acme/widgets/pull/42"
_OPERATOR = "mattwwarren"

# A single-file unified diff: hunk starts at new line 1, one context line
# (advances to 2), two added lines at 2 and 3. Mirrors the fixture shape in
# tests/test_codex_review.py's _MULTI_FILE_DIFF (#1236 precedent).
_CONSOLIDATE_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
"""


def _consolidate_payload(**overrides: object) -> dict[str, Any]:
    """Minimal-but-valid ``cw review consolidate`` request envelope (#1241)."""
    payload: dict[str, Any] = {
        "documents": [],
        "diff": _CONSOLIDATE_DIFF,
        "reviewed_sha": "abc1234",
        "failed_reviewers": [],
    }
    payload.update(overrides)
    return payload


def _doc_payload(*findings: dict[str, Any], **overrides: object) -> dict[str, Any]:
    """A raw ``ReviewerFindingsDocument`` dict (bypasses Pydantic construction
    so invalid payloads — e.g. a bogus severity — can be sent through the CLI).
    """
    payload: dict[str, Any] = {
        "reviewer_role": "Code Quality Reviewer",
        "status": "ok",
        "detail": "",
        "findings": list(findings),
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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


class TestReviewConsolidateCommand:
    """Tests for ``cw review consolidate`` (#1241, adopting the #1237 contract)."""

    def test_happy_path_single_clean_reviewer(self, runner: CliRunner) -> None:
        doc = _make_reviewer_doc(status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["blocking"] is False
        assert verdict["review"]["agents_run"] == 1

    def test_must_fix_finding_with_evidence_in_diff_sets_blocking(
        self, runner: CliRunner
    ) -> None:
        finding = _make_finding(
            severity="MUST_FIX", line_start=2, line_end=2, evidence="def broken():"
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["blocking"] is True
        assert verdict["review"]["must_fix_initial"] == 1
        assert len(verdict["must_fix"]) == 1

    def test_finding_whose_evidence_is_not_in_diff_is_rejected(
        self, runner: CliRunner
    ) -> None:
        finding = _make_finding(
            severity="MUST_FIX",
            line_start=2,
            line_end=2,
            evidence="this text is not in the diff anywhere",
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert len(verdict["rejected"]) == 1
        assert verdict["rejected"][0]["reason"] == "evidence_not_in_diff"
        assert verdict["must_fix"] == []
        assert verdict["review"]["must_fix_initial"] == 0

    def test_two_reviewers_reporting_identical_finding_dedupe_to_one(
        self, runner: CliRunner
    ) -> None:
        finding_kwargs = {
            "severity": "MUST_FIX",
            "line_start": 2,
            "line_end": 2,
            "evidence": "def broken():",
        }
        doc_a = _make_reviewer_doc(
            _make_finding(**finding_kwargs),
            reviewer_role="SysAdmin Reviewer",
            status="ok",
        )
        doc_b = _make_reviewer_doc(
            _make_finding(**finding_kwargs),
            reviewer_role="Code Quality Reviewer",
            status="ok",
        )
        payload = _consolidate_payload(
            documents=[doc_a.model_dump(mode="json"), doc_b.model_dump(mode="json")]
        )
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert len(verdict["accepted"]) == 1
        assert verdict["accepted"][0]["reviewers"] == [
            "Code Quality Reviewer",
            "SysAdmin Reviewer",
        ]

    def test_failed_reviewer_excluded_from_agents_run_but_recorded(
        self, runner: CliRunner
    ) -> None:
        doc = _make_reviewer_doc(status="ok")
        failure = ReviewerRunFailure(
            role="Test Reviewer", reason="unparseable_response"
        )
        payload = _consolidate_payload(
            documents=[doc.model_dump(mode="json")],
            failed_reviewers=[failure.model_dump(mode="json")],
        )
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["review"]["agents_run"] == 1
        assert len(verdict["agents_run"]) == 2
        failed_records = [r for r in verdict["agents_run"] if r["status"] == "failed"]
        assert len(failed_records) == 1

    def test_nit_and_principle_findings_pass_through_but_never_gate(
        self, runner: CliRunner
    ) -> None:
        finding = _make_finding(
            severity="NIT", line_start=3, line_end=3, evidence="    pass"
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert len(verdict["accepted"]) == 1
        assert verdict["review"]["must_fix_initial"] == 0
        assert verdict["review"]["should_fix"] == 0

    def test_escalation_with_evidence_not_in_diff_is_stripped_not_rejected(
        self, runner: CliRunner
    ) -> None:
        escalation = _make_escalation(
            evidence_quote="this quote is nowhere in the diff"
        )
        finding = _make_finding(
            severity="MUST_FIX",
            line_start=2,
            line_end=2,
            evidence="def broken():",
            escalation=escalation,
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert len(verdict["stripped_escalations"]) == 1
        assert len(verdict["accepted"]) == 1
        assert verdict["accepted"][0]["finding"]["escalation"] is None

    def test_malformed_json_prints_json_prefixed_error_and_exits_1(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input="{not valid json"
        )
        assert result.exit_code == 1
        assert result.output.startswith("json:")

    def test_missing_required_field_prints_field_path_message_and_exits_1(
        self, runner: CliRunner
    ) -> None:
        payload = _consolidate_payload()
        del payload["reviewed_sha"]
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("reviewed_sha:") for line in result.output.splitlines()
        )

    def test_invalid_severity_prints_nested_field_path(self, runner: CliRunner) -> None:
        raw_finding = _finding_kwargs(severity="BOGUS")
        payload = _consolidate_payload(documents=[_doc_payload(dict(raw_finding))])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("documents.0.findings.0.severity:")
            for line in result.output.splitlines()
        )

    def test_degraded_status_with_blank_detail_prints_field_path_and_exits_1(
        self, runner: CliRunner
    ) -> None:
        # #1806: a reviewer self-reporting status="degraded" with no stated
        # reason is a contract violation, same shape as an invalid severity --
        # the Claude-native path (this CLI) must reject it, not silently
        # accept it as a clean-looking degraded document.
        payload = _consolidate_payload(
            documents=[_doc_payload(status="degraded", detail="")]
        )
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("documents.0:") for line in result.output.splitlines()
        )

    def test_path_argument_reads_from_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        doc = _make_reviewer_doc(status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        payload_file = tmp_path / "req.json"
        payload_file.write_text(json.dumps(payload))
        result = runner.invoke(main, ["review", "consolidate", str(payload_file)])
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["review"]["agents_run"] == 1

    def test_output_json_has_review_verdict_shape(self, runner: CliRunner) -> None:
        doc = _make_reviewer_doc(status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert set(verdict) == {
            "schema_version",
            "blocking",
            "must_fix",
            "reviewed_sha",
            "accepted",
            "rejected",
            "agents_run",
            "review",
            "stripped_escalations",
            # #1714: the MUST_FIX-severity subset of `rejected`. Reaches the
            # Claude-native coordinator through this passthrough with no
            # Python-side change beyond the field itself.
            "rejected_must_fix",
            # #1709: which filesystem-capability mode the reviewers ran under.
            # Always emitted (null for executors that never probe) so a
            # consumer can tell "not probed" from "probed and degraded".
            "capability_mode",
            "capability_reason",
            # #1773: per-role record of where each reviewer's agent
            # specification resolved from. Always emitted (empty list for
            # paths that never resolve specs, e.g. this consolidate command)
            # so a consumer can tell "not resolved here" from "resolved and
            # unspecified".
            "agent_spec_status",
            # #1763: whether this persisted verdict is the terminal
            # disposition of its review pass or an intermediate fix-loop cycle
            # superseded by a later one. This command runs no fix loop, so it
            # always emits the True default.
            "is_terminal_snapshot",
            # #1805: adjudication entries that matched no accepted finding.
            # Always emitted, 0 here — only `cw review adjudicate` ever sets
            # it non-zero.
            "unmatched_adjudication_count",
            "previous_reviewed_sha",
            "debt",
        }
        assert verdict["unmatched_adjudication_count"] == 0
        assert verdict["capability_mode"] is None
        assert verdict["capability_reason"] is None
        assert verdict["is_terminal_snapshot"] is True
        assert set(verdict["review"]) == {
            "must_fix_initial",
            "should_fix",
            "fix_cycles_used",
            "deferred",
            "agents_run",
            # #1723: OR-across-cycles marker for whether the fix loop
            # actually committed a change, vs. converging on an all-no-op
            # run.
            "had_real_commit",
        }

    def test_empty_documents_all_failed_yields_zero_agents_run(
        self, runner: CliRunner
    ) -> None:
        failures = [
            ReviewerRunFailure(role="Code Quality Reviewer", reason="timeout"),
            ReviewerRunFailure(role="SysAdmin Reviewer", reason="unparseable_response"),
        ]
        payload = _consolidate_payload(
            documents=[],
            failed_reviewers=[f.model_dump(mode="json") for f in failures],
        )
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["review"]["agents_run"] == 0
        assert len(verdict["agents_run"]) == 2

    def test_build_captured_diff_matches_parse_unified_diff(self) -> None:
        file_diffs, file_line_text, file_window_text, _changed = _parse_unified_diff(
            _CONSOLIDATE_DIFF
        )
        diff = _build_captured_diff(_CONSOLIDATE_DIFF)
        assert diff.file_diffs == file_diffs
        assert diff.file_line_text == file_line_text
        assert diff.file_window_text == file_window_text
        assert diff.files == {f: sorted(lines) for f, lines in file_line_text.items()}


class TestReviewConsolidateWorktreeOption:
    """#1632: --worktree / --no-tree-evidence for unanchored-finding routing."""

    def test_consolidate_worktree_option_routes_unanchored_finding(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("real file, not in the diff")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main,
            ["review", "consolidate", "-", "--worktree", str(tmp_path)],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["blocking"] is True
        assert verdict["rejected"] == []

    def test_consolidate_default_worktree_is_cwd(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("real file, not in the diff")
        monkeypatch.chdir(tmp_path)
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["blocking"] is True
        assert verdict["rejected"] == []

    def test_consolidate_unanchored_without_tree_match_stays_rejected(
        self, runner: CliRunner
    ) -> None:
        # No --worktree, and the current directory (the repo checkout) has no
        # "docs.md" at its root — the tree check fails, stays unknown_file.
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["rejected"][0]["reason"] == "unknown_file"

    def test_consolidate_no_tree_evidence_flag_disables_worktree_check(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        (tmp_path / "docs.md").write_text("real file, not in the diff")
        finding = _make_finding(
            severity="MUST_FIX", file="docs.md", line_start=None, line_end=None
        )
        doc = _make_reviewer_doc(finding, status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "-",
                "--worktree",
                str(tmp_path),
                "--no-tree-evidence",
            ],
            input=json.dumps(payload),
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["rejected"][0]["reason"] == "unknown_file"
        assert verdict["blocking"] is False


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
            ["review", "consolidate", "-"],
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
        }
        result = runner.invoke(
            main, ["review", "verify-fixes", "-"], input=json.dumps(payload)
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
        }
        result = runner.invoke(
            main, ["review", "verify-fixes", "-"], input=json.dumps(payload)
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
            ["review", "verify-fixes", "-"],
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


# --------------------------------------------------------------------------
# #1924: consolidate-envelope integrity guards
# --------------------------------------------------------------------------

# The same file section as _CONSOLIDATE_DIFF but with its single hunk repeated
# verbatim -- the shape a session produces when it reconstructs a diff from
# memory and pastes the same hunk twice.
_DUPLICATED_HUNK_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
"""

# Two genuinely distinct hunks in one file -- the ordinary case the detector
# must never flag.
_TWO_HUNK_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
@@ -20,2 +21,3 @@
 other = 1
+def second():
+    pass
"""

# Byte-identical hunk text under two different files. Legitimate (the same
# one-line change applied to two modules), so the file path has to be part of
# the dedup key.
_SAME_HUNK_TWO_FILES_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
diff --git a/src/cw/bar.py b/src/cw/bar.py
index 333..444 100644
--- a/src/cw/bar.py
+++ b/src/cw/bar.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken():
+    pass
"""

# A real (if truncated) diff whose body carries a literal "..." -- proves the
# ellipsis token is matched whole-value-only, never as a substring.
_DIFF_CONTAINING_ELLIPSIS = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+def broken(*args: object) -> None:
+    print("...")
"""

# 36 characters -- under _PLACEHOLDER_LENGTH_FLOOR, but carrying a real
# `diff --git` header, so the conjunction the check tests does not hold.
_SHORT_REAL_DIFF = "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b"


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* with a GIT_*-free env, returning stdout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
        env=env,
    ).stdout


class TestReviewConsolidateDuplicatedHunkDetection:
    """#1924: the same hunk cannot appear twice for the same file."""

    def test_duplicated_hunk_exits_nonzero_with_named_error(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload(diff=_DUPLICATED_HUNK_DIFF)),
        )

        assert result.exit_code == 1
        assert "src/cw/foo.py" in result.output
        assert '"blocking"' not in result.output

    def test_distinct_hunks_same_file_not_flagged(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload(diff=_TWO_HUNK_DIFF)),
        )

        assert result.exit_code == 0, result.output

    def test_identical_hunk_body_different_files_not_flagged(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload(diff=_SAME_HUNK_TWO_FILES_DIFF)),
        )

        assert result.exit_code == 0, result.output


class TestReviewConsolidatePlaceholderDiff:
    """#1924: a diff field that never carried a real diff is rejected."""

    @pytest.mark.parametrize(
        "diff_text",
        ["<diff here>", "<insert diff>", "...", "", "not a diff"],
        ids=[
            "placeholder_token",
            "insert_diff_token",
            "ellipsis_only",
            "empty",
            "short_nonplaceholder_without_diff_git_header",
        ],
    )
    def test_placeholder_diff_rejected(self, runner: CliRunner, diff_text: str) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload(diff=diff_text)),
        )

        assert result.exit_code == 1
        assert '"blocking"' not in result.output

    def test_real_diff_containing_ellipsis_substring_not_rejected(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload(diff=_DIFF_CONTAINING_ELLIPSIS)),
        )

        assert result.exit_code == 0, result.output

    def test_short_diff_with_real_header_not_rejected_despite_under_floor(
        self, runner: CliRunner
    ) -> None:
        """The check rejects on (under floor) AND (no diff --git), not length."""
        assert len(_SHORT_REAL_DIFF) < 40
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload(diff=_SHORT_REAL_DIFF)),
        )

        assert result.exit_code == 0, result.output


def _write_doc(path: Path, **overrides: object) -> None:
    """Write a valid ReviewerFindingsDocument to *path* as JSON."""
    doc = _make_reviewer_doc(**overrides)
    path.write_text(doc.model_dump_json(), encoding="utf-8")


class TestReviewConsolidateDocumentsFrom:
    """#1924: reviewer documents read from disk instead of retyped inline."""

    def test_documents_from_directory_reads_json_lexicographically(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        # Creation order deliberately differs from lexicographic order.
        _write_doc(docs_dir / "c-third.json", reviewer_role="Gamma Reviewer")
        _write_doc(docs_dir / "a-first.json", reviewer_role="Alpha Reviewer")
        _write_doc(docs_dir / "b-second.json", reviewer_role="Beta Reviewer")

        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert [r["reviewer_role"] for r in verdict["agents_run"]] == [
            "Alpha Reviewer",
            "Beta Reviewer",
            "Gamma Reviewer",
        ]

    def test_documents_from_byte_identical_to_hand_built_envelope(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs = [
            _make_reviewer_doc(reviewer_role="Alpha Reviewer"),
            _make_reviewer_doc(
                _make_finding(line_start=2, line_end=2, evidence="def broken():"),
                reviewer_role="Beta Reviewer",
            ),
        ]
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        for index, doc in enumerate(docs):
            (docs_dir / f"{index}-doc.json").write_text(
                doc.model_dump_json(), encoding="utf-8"
            )

        inline = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(
                _consolidate_payload(
                    documents=[d.model_dump(mode="json") for d in docs]
                )
            ),
        )
        from_disk_payload = _consolidate_payload()
        del from_disk_payload["documents"]
        from_disk = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(from_disk_payload),
        )

        assert inline.exit_code == 0, inline.output
        assert from_disk.exit_code == 0, from_disk.output
        assert from_disk.output == inline.output

    def test_documents_from_glob_pattern(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "mixed"
        docs_dir.mkdir()
        _write_doc(docs_dir / "pfx-a.json", reviewer_role="Alpha Reviewer")
        _write_doc(docs_dir / "pfx-b.json", reviewer_role="Beta Reviewer")
        _write_doc(docs_dir / "other.json", reviewer_role="Ignored Reviewer")

        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "--documents-from",
                str(docs_dir / "pfx-*.json"),
                "-",
            ],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert [r["reviewer_role"] for r in verdict["agents_run"]] == [
            "Alpha Reviewer",
            "Beta Reviewer",
        ]

    def test_documents_from_nonexistent_directory_errors_naming_path(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        missing = tmp_path / "no-such-parent" / "review-findings"
        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(missing), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 1
        assert str(missing) in result.output

    def test_documents_from_empty_directory_yields_empty_documents_not_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        failure = ReviewerRunFailure(
            role="Test Reviewer", reason="unparseable_response"
        )
        payload = _consolidate_payload(
            failed_reviewers=[failure.model_dump(mode="json")]
        )
        del payload["documents"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["review"]["agents_run"] == 0
        assert len(verdict["agents_run"]) == 1

    def test_documents_from_malformed_json_file_names_offending_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        _write_doc(docs_dir / "a-good.json", reviewer_role="Alpha Reviewer")
        (docs_dir / "b-bad.json").write_text("{not valid json", encoding="utf-8")
        _write_doc(docs_dir / "c-good.json", reviewer_role="Gamma Reviewer")

        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 1
        assert "b-bad.json" in result.output
        assert "a-good.json" not in result.output

    def test_documents_from_schema_invalid_file_names_offending_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        bad = _doc_payload(dict(_finding_kwargs(severity="BOGUS")))
        (docs_dir / "b-bad.json").write_text(json.dumps(bad), encoding="utf-8")

        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 1
        assert "b-bad.json" in result.output

    def test_documents_from_unreadable_match_names_offending_path(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """An OSError on read is named too, not just a JSON/schema failure."""
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        # A directory named `*.json` matches the glob but cannot be read.
        (docs_dir / "b-dir.json").mkdir()

        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 1
        assert "b-dir.json" in result.output

    def test_documents_from_ignores_documents_key_in_path_json(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        _write_doc(docs_dir / "a.json", reviewer_role="Alpha Reviewer")
        ignored = _make_reviewer_doc(reviewer_role="Should Not Appear")

        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(
                _consolidate_payload(documents=[ignored.model_dump(mode="json")])
            ),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert [r["reviewer_role"] for r in verdict["agents_run"]] == ["Alpha Reviewer"]

    def test_documents_from_path_json_without_documents_key_is_valid(
        self, runner: CliRunner
    ) -> None:
        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["agents_run"] == []

    def test_documents_from_bare_directory_vs_glob_pattern_disambiguation(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        _write_doc(docs_dir / "a.json", reviewer_role="Alpha Reviewer")
        _write_doc(docs_dir / "b.json", reviewer_role="Beta Reviewer")
        _write_doc(docs_dir / "pfx-c.json", reviewer_role="Gamma Reviewer")
        payload = _consolidate_payload()
        del payload["documents"]

        # (a) A path that exists and is a directory -> <path>/*.json.
        bare = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )
        # (b) A path whose parent exists but which does not itself exist ->
        #     evaluated as a glob against that parent.
        globbed = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "--documents-from",
                str(docs_dir / "pfx-*.json"),
                "-",
            ],
            input=json.dumps(payload),
        )

        assert bare.exit_code == 0, bare.output
        assert [r["reviewer_role"] for r in json.loads(bare.output)["agents_run"]] == [
            "Alpha Reviewer",
            "Beta Reviewer",
            "Gamma Reviewer",
        ]
        assert globbed.exit_code == 0, globbed.output
        assert [
            r["reviewer_role"] for r in json.loads(globbed.output)["agents_run"]
        ] == ["Gamma Reviewer"]


def _branch_repo(
    make_git_repo: Callable[..., Path], name: str
) -> tuple[Path, str, str]:
    """A repo with a `feature` branch one commit ahead of `main`.

    Returns ``(repo, reviewed_sha, real_diff_text)`` where *real_diff_text* is
    the verbatim ``git diff --no-color main...<reviewed_sha>`` output.
    """
    repo = make_git_repo(name)
    _git(repo, "checkout", "-b", "feature")
    commit_tracked_file(repo, "src/thing.py", "x = 1\ny = 2\n")
    reviewed_sha = _git(repo, "rev-parse", "HEAD").strip()
    real_diff = _git(repo, "diff", "--no-color", f"main...{reviewed_sha}")
    return repo, reviewed_sha, real_diff


class TestReviewConsolidateBaseFlag:
    """#1924: --base proves the payload diff is the real diff."""

    def test_base_absent_is_inert(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload()),
        )
        assert baseline.exit_code == 0, baseline.output

        calls: list[object] = []

        def _boom(*args: object, **kwargs: object) -> object:
            calls.append(args)
            msg = "subprocess.run must not be called without --base"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.cli.review.subprocess.run", _boom)
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload()),
        )

        assert calls == []
        assert result.exit_code == 0, result.output
        assert result.output == baseline.output

    def test_base_matching_diff_passes(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, sha, real_diff = _branch_repo(make_git_repo, "match")
        payload = _consolidate_payload(diff=real_diff, reviewed_sha=sha)
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "--worktree",
                str(repo),
                "--base",
                "main",
                "-",
            ],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output

    def test_base_mismatched_diff_errors(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, sha, real_diff = _branch_repo(make_git_repo, "mismatch")
        mutated = real_diff.replace("+y = 2", "+y = 3")
        assert mutated != real_diff
        payload = _consolidate_payload(diff=mutated, reviewed_sha=sha)
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
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
        repo, sha, real_diff = _branch_repo(make_git_repo, "badref")
        payload = _consolidate_payload(diff=real_diff, reviewed_sha=sha)
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
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

    def test_base_with_no_tree_evidence_still_checks(
        self, runner: CliRunner, make_git_repo: Callable[..., Path]
    ) -> None:
        """--no-tree-evidence nulls `resolved_worktree`; --base must not no-op."""
        repo, sha, real_diff = _branch_repo(make_git_repo, "notree")
        args = [
            "review",
            "consolidate",
            "--worktree",
            str(repo),
            "--no-tree-evidence",
            "--base",
            "main",
            "-",
        ]

        mismatched = runner.invoke(
            main,
            args,
            input=json.dumps(
                _consolidate_payload(
                    diff=real_diff.replace("+y = 2", "+y = 3"), reviewed_sha=sha
                )
            ),
        )
        matching = runner.invoke(
            main,
            args,
            input=json.dumps(_consolidate_payload(diff=real_diff, reviewed_sha=sha)),
        )

        assert mismatched.exit_code == 1
        assert '"blocking"' not in mismatched.output
        assert matching.exit_code == 0, matching.output


class TestReviewConsolidateRegressionFixtures:
    """#1924: the two incidents the guards exist to catch."""

    def test_regression_duplicated_diff_reconstruction(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(
                _consolidate_payload(diff=_CONSOLIDATE_DIFF + _CONSOLIDATE_DIFF)
            ),
        )

        assert result.exit_code == 1
        assert "src/cw/foo.py" in result.output
        assert '"blocking"' not in result.output

    def test_regression_paraphrased_evidence_hand_typed_envelope_still_rejects(
        self, runner: CliRunner
    ) -> None:
        """Control: retyping the evidence one word off still fails the matcher."""
        doc = _make_reviewer_doc(
            _make_finding(line_start=2, line_end=2, evidence="def broke():"),
            status="ok",
        )
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(
                _consolidate_payload(documents=[doc.model_dump(mode="json")])
            ),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["rejected"][0]["reason"] == "evidence_not_in_diff"

    def test_regression_paraphrased_evidence_avoided_via_documents_from(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The same finding, written verbatim to disk instead, is accepted."""
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        doc = _make_reviewer_doc(
            _make_finding(line_start=2, line_end=2, evidence="def broken():"),
            status="ok",
        )
        (docs_dir / "a.json").write_text(doc.model_dump_json(), encoding="utf-8")
        payload = _consolidate_payload()
        del payload["documents"]

        result = runner.invoke(
            main,
            ["review", "consolidate", "--documents-from", str(docs_dir), "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["rejected"] == []
        assert len(verdict["accepted"]) == 1
