"""Tests for cw.codex_review — prompt-driven codex reviewer orchestration (#1236)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    _build_generic_codex_argv,
    _build_reviewer_prompt,
    _capture_diff,
    _categorize_changed_files,
    _codex_scratch_dir,
    _load_optional_text,
    _load_review_policy,
    _load_sensitive_hits,
    _load_ticket_context,
    _parse_reviewer_document,
    _parse_unified_diff,
    _read_sensitive_manifest,
    _select_reviewer_roles,
    render_verdict_comment,
    run_codex_roles,
    synthesize_codex_review_result,
)
from cw.codex_runner import CodexRunResult
from cw.config import state_dir
from cw.models import Stage, TicketTask
from cw.review_findings import ReviewerRunFailure, consolidate_verdict
from tests.conftest import _make_diff, _make_finding, _make_reviewer_doc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _doc_json(*, role: str = "Code Quality Reviewer", status: str = "ok") -> str:
    return json.dumps(
        {"reviewer_role": role, "status": status, "detail": "", "findings": []}
    )


def _ok_result(role: str = "Code Quality Reviewer") -> CodexRunResult:
    return CodexRunResult(
        returncode=0, stdout="", stderr="", output_file_content=_doc_json(role=role)
    )


class _SequencedRunner:
    """CodexRunner double returning queued results, recording each call."""

    def __init__(self, results: list[CodexRunResult]) -> None:
        self._results = results
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {"argv": list(argv), "timeout": timeout_seconds, "stdin": stdin}
        )
        return self._results[len(self.calls) - 1]


class _Clock:
    """Deterministic monotonic() stand-in stepping through *values*."""

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        value = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return value


# ---------------------------------------------------------------------------
# _categorize_changed_files
# ---------------------------------------------------------------------------


class TestCategorizeChangedFiles:
    def test_python_and_tests(self) -> None:
        cats = _categorize_changed_files(["src/cw/foo.py", "tests/test_foo.py"])
        assert cats.python
        assert cats.tests
        assert not cats.frontend

    def test_frontend_and_infra_and_config(self) -> None:
        cats = _categorize_changed_files(["ui/app.tsx", "Dockerfile", "pyproject.toml"])
        assert cats.frontend
        assert cats.infra
        assert cats.config

    def test_package_json_not_config(self) -> None:
        cats = _categorize_changed_files(["package.json"])
        assert not cats.config

    def test_github_workflow_is_infra(self) -> None:
        cats = _categorize_changed_files([".github/workflows/ci.yml"])
        assert cats.infra


# ---------------------------------------------------------------------------
# _select_reviewer_roles
# ---------------------------------------------------------------------------


class TestSelectReviewerRoles:
    def test_small_mandatory_only(self) -> None:
        cats = _categorize_changed_files(["docs/readme.md"])
        roles = _select_reviewer_roles(
            "small",
            categories=cats,
            mutates_persisted_state=False,
            has_ticket_context=False,
        )
        assert roles == ["Code Quality Reviewer", "SysAdmin Reviewer"]

    def test_small_with_conditionals_ordered(self) -> None:
        cats = _categorize_changed_files(["src/cw/foo.py"])
        roles = _select_reviewer_roles(
            "small",
            categories=cats,
            mutates_persisted_state=True,
            has_ticket_context=True,
        )
        assert roles == [
            "Code Quality Reviewer",
            "SysAdmin Reviewer",
            "Data Safety Reviewer",
            "Product Manager Reviewer",
        ]

    def test_large_doc_only_sysadmin_heads_queue(self) -> None:
        # No code changed: Code Quality is NOT selected, SysAdmin heads the queue.
        cats = _categorize_changed_files(["Dockerfile"])
        roles = _select_reviewer_roles(
            "large",
            categories=cats,
            mutates_persisted_state=False,
            has_ticket_context=False,
        )
        assert roles[0] == "SysAdmin Reviewer"
        assert "Code Quality Reviewer" not in roles
        assert "Deployment Reviewer" in roles

    def test_large_python_and_frontend_full_set(self) -> None:
        cats = _categorize_changed_files(["src/cw/foo.py", "ui/app.tsx"])
        roles = _select_reviewer_roles(
            "large",
            categories=cats,
            mutates_persisted_state=True,
            has_ticket_context=True,
        )
        assert roles[:2] == ["Code Quality Reviewer", "SysAdmin Reviewer"]
        assert "Architecture Reviewer" in roles
        assert "Test Reviewer" in roles
        assert "Performance Reviewer" in roles
        assert "API Contract Validator" in roles
        assert "Data Safety Reviewer" in roles
        assert "Product Manager Reviewer" in roles

    def test_large_api_contract_needs_both(self) -> None:
        cats = _categorize_changed_files(["src/cw/foo.py"])
        roles = _select_reviewer_roles(
            "large",
            categories=cats,
            mutates_persisted_state=False,
            has_ticket_context=False,
        )
        assert "API Contract Validator" not in roles


# ---------------------------------------------------------------------------
# _parse_unified_diff / _capture_diff
# ---------------------------------------------------------------------------

_MULTI_FILE_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+added_one = 1
+added_two = 2
diff --git a/src/cw/bar.py b/src/cw/bar.py
index 333..444 100644
--- a/src/cw/bar.py
+++ b/src/cw/bar.py
@@ -5,3 +5,4 @@
 ctx = 1
-removed = 2
+bar_added = 3
"""

_DELETED_FILE_DIFF = """diff --git a/gone.py b/gone.py
deleted file mode 100644
index 555..000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line_one = 1
-line_two = 2
"""


class TestParseUnifiedDiff:
    def test_per_file_line_numbers(self) -> None:
        _file_diffs, file_line_text = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert file_line_text["src/cw/foo.py"] == {
            2: "added_one = 1",
            3: "added_two = 2",
        }
        # bar.py: hunk starts at new line 5; context advances to 6, removed does
        # not advance, added lands at 6.
        assert file_line_text["src/cw/bar.py"] == {6: "bar_added = 3"}

    def test_file_diffs_capture_hunk_text(self) -> None:
        file_diffs, _ = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert "+added_one = 1" in file_diffs["src/cw/foo.py"]
        assert "+bar_added = 3" in file_diffs["src/cw/bar.py"]

    def test_deleted_file_contributes_no_lines(self) -> None:
        file_diffs, file_line_text = _parse_unified_diff(_DELETED_FILE_DIFF)
        assert "gone.py" not in file_line_text
        assert file_diffs == {}

    def test_empty_diff(self) -> None:
        file_diffs, file_line_text = _parse_unified_diff("")
        assert file_diffs == {}
        assert file_line_text == {}


def _git(repo: Path, *args: str) -> None:
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=clean_env,
    )


class TestCaptureDiff:
    def test_captures_added_file(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-capture")
        _git(repo, "checkout", "-b", "feature")
        (repo / "new.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
        _git(repo, "add", "new.py")
        _git(repo, "commit", "-m", "add new.py")

        diff, reviewed_sha = _capture_diff(repo, "main")

        assert "new.py" in diff.files
        assert diff.file_line_text["new.py"] == {1: "alpha = 1", 2: "beta = 2"}
        assert diff.files["new.py"] == [1, 2]
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        assert reviewed_sha == head


# ---------------------------------------------------------------------------
# _load_optional_text / _codex_scratch_dir
# ---------------------------------------------------------------------------


class TestLoadOptionalText:
    def test_present(self, tmp_path: Path) -> None:
        p = tmp_path / "f.md"
        p.write_text("hello", encoding="utf-8")
        assert _load_optional_text(p) == "hello"

    def test_absent(self, tmp_path: Path) -> None:
        assert _load_optional_text(tmp_path / "missing.md") is None

    def test_non_utf8(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_bytes(b"\xff\xfe")
        assert _load_optional_text(p) is None


class TestCodexScratchDir:
    def test_under_state_dir_not_tmp(self) -> None:
        scratch = _codex_scratch_dir("sess-abc")
        # Must resolve under state_dir() (~/.local/share/cw in production, a
        # snap-readable home path) — never tempfile.TemporaryDirectory()'s /tmp.
        assert scratch.is_relative_to(state_dir())
        assert scratch == state_dir() / "codex-review" / "sess-abc"
        assert scratch.is_dir()


# ---------------------------------------------------------------------------
# _load_sensitive_hits — scope-tier divergence
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_MANIFEST = """sensitive_files:
  - path: "src/cw/auth.py"
    category: auth
    reason: "auth boundary"
"""

_GLOB_MANIFEST = """sensitive_files:
  - path: "src/cw/*.py"
    category: core
    reason: "core module"
"""


class TestLoadSensitiveHitsSmall:
    def test_small_matches_glob_claude_only(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude" / "sensitive-files.yml", _GLOB_MANIFEST)
        # A .github manifest that would match must be ignored by small scope.
        _write(tmp_path / ".github" / "sensitive-files.yml", _MANIFEST)
        hits = _load_sensitive_hits(tmp_path, ["src/cw/foo.py"], "small")
        assert len(hits) == 1
        assert hits[0].path == "src/cw/foo.py"
        assert hits[0].category == "core"

    def test_small_ignores_github_when_claude_absent(self, tmp_path: Path) -> None:
        # No .claude manifest: small scope never consults .github, so no hits.
        _write(tmp_path / ".github" / "sensitive-files.yml", _MANIFEST)
        hits = _load_sensitive_hits(tmp_path, ["src/cw/auth.py"], "small")
        assert hits == []

    def test_small_no_registry(self, tmp_path: Path) -> None:
        assert _load_sensitive_hits(tmp_path, ["src/cw/foo.py"], "small") == []


class TestLoadSensitiveHitsLarge:
    def test_large_substring_match_claude(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude" / "sensitive-files.yml", _MANIFEST)
        hits = _load_sensitive_hits(tmp_path, ["a/b/src/cw/auth.py"], "large")
        assert len(hits) == 1
        assert hits[0].category == "auth"

    def test_large_first_hit_wins_claude_over_github(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude" / "sensitive-files.yml", _MANIFEST)
        # .github has a different entry that would also match; must NOT be read
        # because .claude exists (first-hit-wins).
        _write(
            tmp_path / ".github" / "sensitive-files.yml",
            'sensitive_files:\n  - path: "other.py"\n    category: x\n    reason: y\n',
        )
        hits = _load_sensitive_hits(tmp_path, ["src/cw/auth.py"], "large")
        assert [h.category for h in hits] == ["auth"]

    def test_large_falls_back_to_github(self, tmp_path: Path) -> None:
        _write(tmp_path / ".github" / "sensitive-files.yml", _MANIFEST)
        hits = _load_sensitive_hits(tmp_path, ["src/cw/auth.py"], "large")
        assert len(hits) == 1
        assert hits[0].category == "auth"

    def test_large_no_registry(self, tmp_path: Path) -> None:
        assert _load_sensitive_hits(tmp_path, ["src/cw/auth.py"], "large") == []


class TestReadSensitiveManifest:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert _read_sensitive_manifest(tmp_path / "nope.yml") == []

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "m.yml"
        p.write_text("key: [unclosed\n", encoding="utf-8")
        assert _read_sensitive_manifest(p) == []

    def test_non_dict_root(self, tmp_path: Path) -> None:
        p = tmp_path / "m.yml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert _read_sensitive_manifest(p) == []

    def test_entries_not_a_list(self, tmp_path: Path) -> None:
        p = tmp_path / "m.yml"
        p.write_text("sensitive_files: nope\n", encoding="utf-8")
        assert _read_sensitive_manifest(p) == []

    def test_drops_entries_without_path(self, tmp_path: Path) -> None:
        p = tmp_path / "m.yml"
        p.write_text(
            "sensitive_files:\n  - category: x\n  - path: keep.py\n", encoding="utf-8"
        )
        entries = _read_sensitive_manifest(p)
        assert entries == [{"path": "keep.py"}]


class TestLoadTicketContext:
    def test_plan_and_context_present(self, tmp_path: Path) -> None:
        _write(tmp_path / ".cw" / "plan.md", "THE PLAN")
        _write(
            tmp_path / ".cw" / "context.json",
            json.dumps({"title": "Ticket Title", "body": "Ticket Body"}),
        )
        plan_text, ticket_text = _load_ticket_context(tmp_path)
        assert plan_text == "THE PLAN"
        assert ticket_text is not None
        assert "Ticket Title" in ticket_text
        assert "Ticket Body" in ticket_text

    def test_both_absent(self, tmp_path: Path) -> None:
        plan_text, ticket_text = _load_ticket_context(tmp_path)
        assert plan_text is None
        assert ticket_text is None

    def test_malformed_context_json(self, tmp_path: Path) -> None:
        _write(tmp_path / ".cw" / "context.json", "not json{{")
        _plan, ticket_text = _load_ticket_context(tmp_path)
        assert ticket_text is None

    def test_empty_title_body(self, tmp_path: Path) -> None:
        _write(tmp_path / ".cw" / "context.json", json.dumps({"title": "", "body": ""}))
        _plan, ticket_text = _load_ticket_context(tmp_path)
        assert ticket_text is None


class TestParseReviewerDocument:
    def test_none_content(self) -> None:
        assert _parse_reviewer_document(None) is None

    def test_invalid_json(self) -> None:
        assert _parse_reviewer_document("not json{{") is None

    def test_schema_invalid_dict(self) -> None:
        # Valid JSON but a failed status carrying findings is a schema violation.
        payload = json.dumps(
            {
                "reviewer_role": "R",
                "status": "failed",
                "detail": "",
                "findings": [{"severity": "MUST_FIX"}],
            }
        )
        assert _parse_reviewer_document(payload) is None

    def test_valid_document(self) -> None:
        payload = _doc_json()
        doc = _parse_reviewer_document(payload)
        assert doc is not None
        assert doc.status == "ok"


# ---------------------------------------------------------------------------
# _load_review_policy — scope-tier divergence
# ---------------------------------------------------------------------------


class TestLoadReviewPolicy:
    def test_small_returns_empty_without_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path / ".claude" / "review-policy.md", "## Code Quality Reviewer\nx")
        calls: list[str] = []
        import cw.codex_review as cr

        real = cr._load_optional_text

        def _spy(path: Path) -> str | None:
            calls.append(str(path))
            return real(path)

        monkeypatch.setattr(cr, "_load_optional_text", _spy)
        result = _load_review_policy(tmp_path, "small")
        assert result == {}
        assert not any("review-policy.md" in c for c in calls)

    def test_large_parses_valid_section(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".claude" / "review-policy.md",
            "## Code Quality Reviewer\nApply the house style.\n",
        )
        result = _load_review_policy(tmp_path, "large")
        assert result == {"Code Quality Reviewer": "Apply the house style."}

    def test_large_warns_and_skips_unmatched(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(
            tmp_path / ".claude" / "review-policy.md",
            "## Code Quality Reviewer\nvalid body\n\n## Bogus Reviewer\nignored\n",
        )
        with caplog.at_level(logging.WARNING):
            result = _load_review_policy(tmp_path, "large")
        assert result == {"Code Quality Reviewer": "valid body"}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Bogus Reviewer" in w.getMessage() for w in warnings)

    def test_large_missing_file(self, tmp_path: Path) -> None:
        assert _load_review_policy(tmp_path, "large") == {}


# ---------------------------------------------------------------------------
# _build_reviewer_prompt
# ---------------------------------------------------------------------------


class TestBuildReviewerPrompt:
    def test_all_sections_present(self) -> None:
        from cw.codex_review import _SensitiveHit

        prompt = _build_reviewer_prompt(
            "Code Quality Reviewer",
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text="PLAN BODY",
            ticket_text="TICKET BODY",
            project_rubrics="RUBRIC BODY",
            repo_policy_section="POLICY BODY",
            sensitive_hits=[_SensitiveHit("src/cw/foo.py", "core", "why")],
        )
        assert "# Reviewer Role: Code Quality Reviewer" in prompt
        assert "AGENT SPEC BODY" in prompt
        assert "PLAN BODY" in prompt
        assert "TICKET BODY" in prompt
        assert "RUBRIC BODY" in prompt
        assert "POLICY BODY" in prompt
        assert "ELEVATED SCRUTINY" in prompt
        assert "src/cw/foo.py (core) — why" in prompt
        assert "## Diff" in prompt

    def test_optional_sections_absent(self) -> None:
        prompt = _build_reviewer_prompt(
            "SysAdmin Reviewer",
            agent_spec_text="SPEC",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        assert "## Approved Plan" not in prompt
        assert "## Ticket Context" not in prompt
        assert "## Project Rubrics" not in prompt
        assert "ELEVATED SCRUTINY" not in prompt


# ---------------------------------------------------------------------------
# _build_generic_codex_argv
# ---------------------------------------------------------------------------


class TestBuildGenericCodexArgv:
    def test_with_model(self, tmp_path: Path) -> None:
        argv = _build_generic_codex_argv(
            model="gpt-5",
            schema_path=tmp_path / "s.json",
            output_path=tmp_path / "o.json",
        )
        assert argv[:2] == ["codex", "exec"]
        assert "review" not in argv
        assert "--base" not in argv
        assert argv[-2:] == ["-m", "gpt-5"]

    def test_no_model(self, tmp_path: Path) -> None:
        argv = _build_generic_codex_argv(
            model=None, schema_path=tmp_path / "s.json", output_path=tmp_path / "o.json"
        )
        assert "-m" not in argv


# ---------------------------------------------------------------------------
# run_codex_roles — shared deadline (Comment 3)
# ---------------------------------------------------------------------------


class TestRunCodexRoles:
    def test_all_complete_within_budget(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        docs, failures = run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=3600,
        )
        assert len(docs) == 2
        assert failures == []

    def test_none_budget_gives_none_timeout(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=None,
        )
        assert all(call["timeout"] is None for call in runner.calls)

    def test_floor_respected(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=3600,
        )
        for call in runner.calls:
            timeout = call["timeout"]
            assert isinstance(timeout, int)
            assert timeout >= 30

    def test_budget_exhausted_skips_later_role(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # deadline=100 (call0); role1 remaining=100 (call1); role2 remaining=100
        # (call2); role3 remaining=20<=30 -> skip budget_exhausted (call3).
        monkeypatch.setattr("cw.codex_review.time.monotonic", _Clock([0, 0, 0, 80]))
        runner = _SequencedRunner([_ok_result(), _ok_result()])
        with caplog.at_level(logging.WARNING):
            docs, failures = run_codex_roles(
                runner=runner,
                worktree=tmp_path,
                roles=[
                    "Code Quality Reviewer",
                    "SysAdmin Reviewer",
                    "Data Safety Reviewer",
                ],
                prompts_by_role={
                    "Code Quality Reviewer": "p1",
                    "SysAdmin Reviewer": "p2",
                    "Data Safety Reviewer": "p3",
                },
                model=None,
                wall_clock_budget_seconds=100,
            )
        assert len(docs) == 2
        assert len(failures) == 1
        assert failures[0].role == "Data Safety Reviewer"
        assert failures[0].reason == CODEX_BUDGET_EXHAUSTED
        assert len(runner.calls) == 2  # third role never ran

    def test_per_role_failure_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        with caplog.at_level(logging.WARNING):
            docs, failures = run_codex_roles(
                runner=runner,
                worktree=tmp_path,
                roles=["Code Quality Reviewer"],
                prompts_by_role={"Code Quality Reviewer": "p"},
                model=None,
                wall_clock_budget_seconds=None,
            )
        assert docs == []
        assert failures[0].reason == CODEX_ERROR
        assert any(
            "Code Quality Reviewer" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    def test_timeout_and_unparseable_failures(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [
                CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True),
                CodexRunResult(
                    returncode=0, stdout="", stderr="", output_file_content="not json"
                ),
            ]
        )
        _docs, failures = run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=None,
        )
        reasons = {f.role: f.reason for f in failures}
        assert reasons["Code Quality Reviewer"] == CODEX_TIMEOUT
        assert reasons["SysAdmin Reviewer"] == CODEX_REVIEW_UNPARSEABLE

    def test_stdin_carries_prompt(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "PROMPT BODY"},
            model=None,
            wall_clock_budget_seconds=None,
        )
        assert runner.calls[0]["stdin"] == "PROMPT BODY"


# ---------------------------------------------------------------------------
# synthesize_codex_review_result — disposition table
# ---------------------------------------------------------------------------


def _task() -> TicketTask:
    return TicketTask(ticket_id="T-1", client="test", stage=Stage.REVIEW)


class TestSynthesizeCodexReviewResult:
    def test_zero_documents_blocked_unparseable(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-zero")
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[],
            failures=[ReviewerRunFailure(role="R", reason="crash")],
            diff=_make_diff(),
            reviewed_sha="sha",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None

    def test_blocking_must_fix(self, make_git_repo: Callable[[str], Path]) -> None:
        worktree = make_git_repo("wt-synth-block")
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert result.review.must_fix_initial == 1
        assert verdict is not None
        assert verdict.blocking is True

    def test_stage_complete_non_blocking(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        worktree = make_git_repo("wt-synth-ok")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[],
            diff=_make_diff(),
            reviewed_sha="sha",
        )
        assert result.status == "stage_complete"
        assert result.stage_reached == "stage3_review"
        assert result.health.recommendation == "PROCEED"
        assert result.review.should_fix == 1
        assert verdict is not None
        assert verdict.blocking is False


# ---------------------------------------------------------------------------
# render_verdict_comment
# ---------------------------------------------------------------------------


class TestRenderVerdictComment:
    def test_blocking_lists_must_fix(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(
            _make_finding(severity="MUST_FIX", summary="bad thing")
        )
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "BLOCKING" in body
        assert "MUST_FIX" in body
        assert "bad thing" in body
        assert "src/cw/foo.py:10" in body

    def test_non_blocking_header(self) -> None:
        diff = _make_diff()
        doc = _make_reviewer_doc(_make_finding(severity="NIT"))
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "Non-blocking" in body
