"""Tests for ``cw review consolidate`` (``cw.cli.review.consolidate``).

Covers the command itself, its ``--documents-from`` loader, and the
``cw.cli.review._diff_integrity`` guards as exercised through this command
(#1241, #1924, #2029) — ``_diff_integrity`` is also imported by
``cw.cli.review.commands``, so these guards are not exclusive to
``consolidate``. Split out of ``tests/test_cli_review.py`` for #2049 so
the test modules mirror the ``src/cw/cli/review/`` package seams.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review import _build_captured_diff
from cw.codex_review import _parse_unified_diff
from cw.review_findings import ReviewerRunFailure
from tests._cli_review_helpers import (
    _CONSOLIDATE_DIFF,
    _branch_repo,
    _consolidate_payload,
)
from tests.conftest import (
    _doc_payload,
    _finding_kwargs,
    _make_escalation,
    _make_finding,
    _make_reviewer_doc,
    _without_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestReviewConsolidateCommand:
    """Tests for ``cw review consolidate`` (#1241, adopting the #1237 contract)."""

    def test_happy_path_single_clean_reviewer(self, runner: CliRunner) -> None:
        doc = _make_reviewer_doc(status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input="{not valid json",
        )
        assert result.exit_code == 1
        assert result.output.startswith("json:")

    def test_missing_required_field_prints_field_path_message_and_exits_1(
        self, runner: CliRunner
    ) -> None:
        payload = _consolidate_payload()
        del payload["reviewed_sha"]
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("reviewed_sha:") for line in result.output.splitlines()
        )

    def test_invalid_severity_finding_is_rescued_leaving_document_needing_justification(
        self, runner: CliRunner
    ) -> None:
        # #2042: the inline `documents` path now rescues a schema-invalid
        # finding (bogus severity) the same way --documents-from always has.
        # The lone finding here is the only one in its document, so rescuing
        # it away leaves `findings=[]` with a blank `detail` (`_doc_payload`'s
        # default) -- the doc-level "ok status needs justification" invariant
        # fires instead of the old per-finding severity field error, so the
        # field-path prefix shifts from `documents.0.findings.0.severity:` to
        # `documents.0:`.
        raw_finding = _finding_kwargs(severity="BOGUS")
        payload = _consolidate_payload(documents=[_doc_payload(dict(raw_finding))])
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("documents.0:") for line in result.output.splitlines()
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        assert any(
            line.startswith("documents.0:") for line in result.output.splitlines()
        )

    def test_bare_list_payload_still_exits_1_cleanly(self, runner: CliRunner) -> None:
        # #2042 guard: the new `_rescue_inline_documents` hook must no-op on
        # anything that isn't a dict, falling through to Pydantic's own
        # top-level validation -- not raise an unguarded AttributeError that
        # would regress this into an unhandled traceback. CliRunner reports
        # exit_code == 1 for both a clean exit and a crash, so this also
        # checks `result.exception` is the clean `click.exceptions.Exit`
        # (surfaced by CliRunner as `SystemExit`), not an arbitrary exception.
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input="[]",
        )
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)

    def test_non_list_documents_field_still_exits_1_cleanly(
        self, runner: CliRunner
    ) -> None:
        # #2042 guard: `documents` present but not a list must also no-op the
        # hook rather than index into a shape it hasn't confirmed.
        payload = _consolidate_payload(documents="not-a-list")
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)

    def test_path_argument_reads_from_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        doc = _make_reviewer_doc(status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        payload_file = tmp_path / "req.json"
        payload_file.write_text(json.dumps(payload))
        result = runner.invoke(
            main, ["review", "consolidate", "--no-base-check", str(payload_file)]
        )
        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["review"]["agents_run"] == 1

    def test_output_json_has_review_verdict_shape(self, runner: CliRunner) -> None:
        doc = _make_reviewer_doc(status="ok")
        payload = _consolidate_payload(documents=[doc.model_dump(mode="json")])
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            # #2029: the reviewer run failures whose unusable payload was
            # claiming a MUST_FIX or SHOULD_FIX finding — the residual signal
            # for the discards no per-finding rescue could recover.
            "run_failures_with_should_fix_discards",
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
            # #2000: the all-severity tally of `rejected` (a superset of
            # `rejected_must_fix` above), plus the verify-fixes downgrade
            # counter. All three always emitted, 0/{} here — this command
            # rejects nothing in the fixture and runs no verify-fixes pass.
            "rejected_count",
            "rejected_count_by_severity",
            "downgraded_disposition_count",
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
            # #2000: the same two rejection counts as the top-level verdict
            # above, mirrored here so they survive into the terminal
            # AUTO_DEV_RESULT sentinel rather than living only in this
            # artifact. Identical values by construction, not by convention.
            "rejected_count",
            "rejected_count_by_severity",
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "-",
                "--worktree",
                str(tmp_path),
            ],
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
                "--no-base-check",
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


class TestReviewConsolidateDuplicatedHunkDetection:
    """#1924: the same hunk cannot appear twice for the same file."""

    def test_duplicated_hunk_exits_nonzero_with_named_error(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload(diff=_DUPLICATED_HUNK_DIFF)),
        )

        assert result.exit_code == 1
        assert "src/cw/foo.py" in result.output
        assert '"blocking"' not in result.output

    def test_distinct_hunks_same_file_not_flagged(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload(diff=_TWO_HUNK_DIFF)),
        )

        assert result.exit_code == 0, result.output

    def test_identical_hunk_body_different_files_not_flagged(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
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
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload(diff=diff_text)),
        )

        assert result.exit_code == 1
        assert '"blocking"' not in result.output

    def test_real_diff_containing_ellipsis_substring_not_rejected(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
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
            ["review", "consolidate", "--no-base-check", "-"],
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
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
            ["review", "consolidate", "--no-base-check", "-"],
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
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
                "--no-base-check",
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(missing),
                "-",
            ],
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
        # A non-empty inline `documents` entry the CLI must ignore once
        # --documents-from is set: if the flag were silently dropped and the
        # code fell through to `parsed.documents`, this reviewer would leak
        # into the verdict even though the (empty) directory contributed
        # nothing. This is what makes the assertions below load-bearing —
        # see #1957.
        ignored_doc = _make_reviewer_doc(reviewer_role="Should Not Appear")
        payload = _consolidate_payload(
            documents=[ignored_doc.model_dump(mode="json")],
            failed_reviewers=[failure.model_dump(mode="json")],
        )
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["review"]["agents_run"] == 0
        assert [r["reviewer_role"] for r in verdict["agents_run"]] == ["Test Reviewer"]

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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
            input=json.dumps(payload),
        )

        assert result.exit_code == 1
        assert "b-bad.json" in result.output
        assert "a-good.json" not in result.output

    def test_documents_from_schema_invalid_file_names_offending_file(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Still exits 1 after #2029, but for a DIFFERENT reason: the bogus
        # severity is now dropped as `schema_invalid`, leaving findings=[] —
        # and `_doc_payload` defaults `status="ok"` with a blank `detail`, so
        # the document's own _check_ok_empty_findings_has_justification
        # invariant is what raises. A red assertion here is not evidence the
        # per-finding rescue regressed; the sibling-survival tests below are
        # what prove it works.
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        bad = _doc_payload(dict(_finding_kwargs(severity="BOGUS")))
        (docs_dir / "b-bad.json").write_text(json.dumps(bad), encoding="utf-8")

        payload = _consolidate_payload()
        del payload["documents"]
        result = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
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
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
            input=json.dumps(payload),
        )
        # (b) A path whose parent exists but which does not itself exist ->
        #     evaluated as a glob against that parent.
        globbed = runner.invoke(
            main,
            [
                "review",
                "consolidate",
                "--no-base-check",
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


def _anchored_finding(**overrides: object) -> dict[str, Any]:
    """A raw finding dict anchored to ``_CONSOLIDATE_DIFF``'s real added line."""
    return dict(_finding_kwargs(line_start=2, line_end=2, **overrides))


def _schema_invalid_finding(**overrides: object) -> dict[str, Any]:
    """A raw finding dict with ``evidence`` removed — schema-invalid (#2029)."""
    return _without_evidence(_anchored_finding(**overrides))


def _consolidate_from(runner: CliRunner, docs_dir: Path) -> Any:
    """Invoke ``cw review consolidate --documents-from <docs_dir>``."""
    payload = _consolidate_payload()
    del payload["documents"]
    return runner.invoke(
        main,
        [
            "review",
            "consolidate",
            "--no-base-check",
            "--documents-from",
            str(docs_dir),
            "-",
        ],
        input=json.dumps(payload),
    )


class TestReviewConsolidateDocumentsFromSchemaInvalidFindings:
    """#2029: one unparseable finding must not delete the whole document."""

    def test_sibling_findings_survive_and_still_block(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        doc = _doc_payload(
            _schema_invalid_finding(severity="NIT"),
            _anchored_finding(severity="MUST_FIX", summary="real problem"),
            detail="reviewed the diff",
        )
        (docs_dir / "a-mixed.json").write_text(json.dumps(doc), encoding="utf-8")

        result = _consolidate_from(runner, docs_dir)

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert [r["reason"] for r in verdict["rejected"]] == ["schema_invalid"]
        assert [a["finding"]["summary"] for a in verdict["accepted"]] == [
            "real problem"
        ]
        assert verdict["blocking"] is True

    def test_schema_invalid_must_fix_alone_parks_via_rejected_must_fix(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        doc = _doc_payload(
            _schema_invalid_finding(severity="MUST_FIX"),
            detail="reviewed the diff",
        )
        (docs_dir / "a-bad-must-fix.json").write_text(json.dumps(doc), encoding="utf-8")

        result = _consolidate_from(runner, docs_dir)

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert len(verdict["rejected_must_fix"]) == 1
        assert verdict["rejected_must_fix"][0]["reason"] == "schema_invalid"
        assert verdict["blocking"] is False

    def test_one_bad_file_does_not_delete_the_other_reviewers_findings(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "review-findings"
        docs_dir.mkdir()
        mixed = _doc_payload(
            _schema_invalid_finding(severity="NIT"),
            _anchored_finding(severity="SHOULD_FIX", summary="alpha kept"),
            reviewer_role="Alpha Reviewer",
            detail="reviewed the diff",
        )
        # A distinct anchor+evidence, or dedupe_findings would merge the two
        # into one AcceptedFinding and the assertion below would prove nothing.
        clean = _doc_payload(
            dict(
                _finding_kwargs(
                    severity="SHOULD_FIX",
                    summary="beta kept",
                    line_start=3,
                    line_end=3,
                    evidence="pass",
                )
            ),
            reviewer_role="Beta Reviewer",
            detail="reviewed the diff",
        )
        (docs_dir / "a-mixed.json").write_text(json.dumps(mixed), encoding="utf-8")
        (docs_dir / "b-clean.json").write_text(json.dumps(clean), encoding="utf-8")

        result = _consolidate_from(runner, docs_dir)

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert sorted(a["finding"]["summary"] for a in verdict["accepted"]) == [
            "alpha kept",
            "beta kept",
        ]
        assert len(verdict["rejected"]) == 1
        assert verdict["rejected"][0]["reviewer_role"] == "Alpha Reviewer"


class TestReviewConsolidateInlineSchemaInvalidFindings:
    """#2042: the inline ``documents=[...]`` payload gets the same per-finding
    rescue ``TestReviewConsolidateDocumentsFromSchemaInvalidFindings`` already
    covers for ``--documents-from`` (#2029) -- one schema-invalid finding must
    not delete its document's surviving siblings, nor its document's fellow
    documents in the same payload.
    """

    def test_sibling_findings_survive_and_still_block(self, runner: CliRunner) -> None:
        doc = _doc_payload(
            _schema_invalid_finding(severity="NIT"),
            _anchored_finding(severity="MUST_FIX", summary="real problem"),
            detail="reviewed the diff",
        )
        payload = _consolidate_payload(documents=[doc])
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert [r["reason"] for r in verdict["rejected"]] == ["schema_invalid"]
        assert [a["finding"]["summary"] for a in verdict["accepted"]] == [
            "real problem"
        ]
        assert verdict["blocking"] is True

    def test_schema_invalid_must_fix_alone_parks_via_rejected_must_fix(
        self, runner: CliRunner
    ) -> None:
        doc = _doc_payload(
            _schema_invalid_finding(severity="MUST_FIX"),
            detail="reviewed the diff",
        )
        payload = _consolidate_payload(documents=[doc])
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert len(verdict["rejected_must_fix"]) == 1
        assert verdict["rejected_must_fix"][0]["reason"] == "schema_invalid"
        assert verdict["blocking"] is False

    def test_one_bad_document_does_not_delete_the_other_documents_findings(
        self, runner: CliRunner
    ) -> None:
        mixed = _doc_payload(
            _schema_invalid_finding(severity="NIT"),
            _anchored_finding(severity="SHOULD_FIX", summary="alpha kept"),
            reviewer_role="Alpha Reviewer",
            detail="reviewed the diff",
        )
        # A distinct anchor+evidence, or dedupe_findings would merge the two
        # into one AcceptedFinding and the assertion below would prove nothing.
        clean = _doc_payload(
            dict(
                _finding_kwargs(
                    severity="SHOULD_FIX",
                    summary="beta kept",
                    line_start=3,
                    line_end=3,
                    evidence="pass",
                )
            ),
            reviewer_role="Beta Reviewer",
            detail="reviewed the diff",
        )
        payload = _consolidate_payload(documents=[mixed, clean])
        result = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert sorted(a["finding"]["summary"] for a in verdict["accepted"]) == [
            "alpha kept",
            "beta kept",
        ]
        assert len(verdict["rejected"]) == 1
        assert verdict["rejected"][0]["reviewer_role"] == "Alpha Reviewer"


class TestReviewConsolidateBaseFlag:
    """#1924: --base proves the payload diff is the real diff."""

    def test_neither_base_nor_no_base_check_is_usage_error(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "-"],
            input=json.dumps(_consolidate_payload()),
        )
        assert result.exit_code == 2, result.output
        assert "--base" in result.output
        assert "--no-base-check" in result.output

    def test_base_and_no_base_check_together_is_usage_error(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main,
            ["review", "consolidate", "--base", "main", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload()),
        )
        assert result.exit_code == 2, result.output
        assert "mutually exclusive" in result.output

    def test_no_base_check_skips_verification(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = runner.invoke(
            main,
            ["review", "consolidate", "--no-base-check", "-"],
            input=json.dumps(_consolidate_payload()),
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
            ["review", "consolidate", "--no-base-check", "-"],
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
            ["review", "consolidate", "--no-base-check", "-"],
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
            ["review", "consolidate", "--no-base-check", "-"],
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
            [
                "review",
                "consolidate",
                "--no-base-check",
                "--documents-from",
                str(docs_dir),
                "-",
            ],
            input=json.dumps(payload),
        )

        assert result.exit_code == 0, result.output
        verdict = json.loads(result.output)
        assert verdict["rejected"] == []
        assert len(verdict["accepted"]) == 1
