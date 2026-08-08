"""Tests for the ``cw review`` CLI group (GitHub #1154, RFC 0011 S2; #1241)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review import _build_captured_diff
from cw.codex_review import _parse_unified_diff
from cw.dev_queue import load_dev_queue
from cw.review_findings import ReviewerRunFailure

from .conftest import (
    _finding_kwargs,
    _make_escalation,
    _make_finding,
    _make_reviewer_doc,
)

if TYPE_CHECKING:
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
        payload = _consolidate_payload(documents=[_doc_payload(raw_finding)])
        result = runner.invoke(
            main, ["review", "consolidate", "-"], input=json.dumps(payload)
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("documents.0.findings.0.severity:")
            for line in result.output.splitlines()
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
        }
        assert verdict["capability_mode"] is None
        assert verdict["capability_reason"] is None
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
        file_diffs, file_line_text, _changed = _parse_unified_diff(_CONSOLIDATE_DIFF)
        diff = _build_captured_diff(_CONSOLIDATE_DIFF)
        assert diff.file_diffs == file_diffs
        assert diff.file_line_text == file_line_text
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
