"""Tests for cw.codex_review — prompt-driven codex reviewer orchestration (#1236)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from typing import TYPE_CHECKING, get_args

import pytest

from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_ERROR,
    CODEX_MUST_FIX_FINDINGS,
    CODEX_REVIEW_PARTIAL,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    _CATEGORY_TO_REASON,
    _build_generic_codex_argv,
    _build_reviewer_prompt,
    _capture_diff,
    _categorize_changed_files,
    _classify_codex_failure,
    _codex_scratch_dir,
    _format_failures_detail,
    _load_optional_text,
    _load_review_policy,
    _load_sensitive_hits,
    _load_ticket_context,
    _parse_reviewer_document,
    _parse_unified_diff,
    _read_sensitive_manifest,
    _run_codex_role,
    _select_reviewer_roles,
    render_verdict_comment,
    run_codex_roles,
    run_review,
    synthesize_codex_review_result,
)
from cw.codex_runner import CodexRunResult
from cw.config import state_dir
from cw.executor_diagnostics import (
    ExecutorFailure,
    ExecutorFailureCategory,
    diagnostics_bundle_dir,
)
from cw.models import Stage, TicketTask
from cw.review_findings import ReviewerRunFailure, consolidate_verdict
from tests.conftest import (
    _make_diff,
    _make_finding,
    _make_reviewer_doc,
    _make_ticket_task,
)

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


def _bundle_file(session_id: str, role_slug: str, category: str) -> Path:
    """Return the single bundle JSON matching *role_slug*/*category* (#1330).

    Filenames now carry an ``occurred_at`` timestamp suffix (item 7), so exact
    filenames are no longer stable across a test run — glob on the stable
    prefix instead.
    """
    matches = [
        p
        for p in diagnostics_bundle_dir(session_id).glob(f"{role_slug}-{category}-*.json")
        if not p.name.endswith(("-schema.json", "-output.json"))
    ]
    assert len(matches) == 1, (
        f"expected exactly one {role_slug}-{category}-*.json bundle file, "
        f"found {matches}"
    )
    return matches[0]


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
        _file_diffs, file_line_text, _changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert file_line_text["src/cw/foo.py"] == {
            2: "added_one = 1",
            3: "added_two = 2",
        }
        # bar.py: hunk starts at new line 5; context advances to 6, removed does
        # not advance, added lands at 6.
        assert file_line_text["src/cw/bar.py"] == {6: "bar_added = 3"}

    def test_file_diffs_capture_hunk_text(self) -> None:
        file_diffs, _, _changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert "+added_one = 1" in file_diffs["src/cw/foo.py"]
        assert "+bar_added = 3" in file_diffs["src/cw/bar.py"]

    def test_changed_files_in_diff_order(self) -> None:
        # SHOULD_FIX 11 (#1236): changed_files is derived from the same parse
        # pass as file_diffs/file_line_text — no second subprocess needed.
        _file_diffs, _file_line_text, changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert changed == ["src/cw/foo.py", "src/cw/bar.py"]

    def test_deleted_file_contributes_no_lines_but_is_changed(self) -> None:
        file_diffs, file_line_text, changed = _parse_unified_diff(_DELETED_FILE_DIFF)
        assert "gone.py" not in file_line_text
        assert file_diffs == {}
        # A pure deletion has no hunk text/added lines, but it IS a changed
        # file and must still appear in the changed-file list.
        assert changed == ["gone.py"]

    def test_empty_diff(self) -> None:
        file_diffs, file_line_text, changed = _parse_unified_diff("")
        assert file_diffs == {}
        assert file_line_text == {}
        assert changed == []


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

        diff, reviewed_sha, changed_files = _capture_diff(repo, "main")

        assert "new.py" in diff.files
        assert diff.file_line_text["new.py"] == {1: "alpha = 1", 2: "beta = 2"}
        assert diff.files["new.py"] == [1, 2]
        assert changed_files == ["new.py"]
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

    def test_read_only_sandbox_always_set(self, tmp_path: Path) -> None:
        # MUST_FIX 4 (#1236): ticket AC requires read-only sandboxing on
        # every generic codex exec invocation, model or no model.
        argv = _build_generic_codex_argv(
            model=None, schema_path=tmp_path / "s.json", output_path=tmp_path / "o.json"
        )
        idx = argv.index("--sandbox")
        assert argv[idx + 1] == "read-only"


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
            session_id="s-review",
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
            session_id="s-review",
        )
        assert all(call["timeout"] is None for call in runner.calls)

    def test_floor_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Test Reviewer SHOULD_FIX 9 (#1236): a real clock + 3600s budget
        # never approaches the floor, so the old version of this test never
        # actually exercised `max(int(remaining), _MIN_ROLE_TIMEOUT_SECONDS)`.
        # Deterministic clock: deadline=100 (call0); role1's remaining =
        # 100 - 69 = 31 seconds — just above the 30s skip threshold, close
        # enough to the floor that the clamp is genuinely in play.
        monkeypatch.setattr("cw.codex_review.time.monotonic", _Clock([0, 69]))
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p1"},
            model=None,
            wall_clock_budget_seconds=100,
            session_id="s-review",
        )
        assert runner.calls[0]["timeout"] == 31

    def test_budget_exhausted_skips_later_role(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # deadline=100 (call0). Each successful _run_codex_role now consumes two
        # extra monotonic() reads (start/end duration capture, #1239), so the
        # deadline-remaining reads land at call1 (role1: 100>30 run), call4
        # (role2: 100>30 run), call7 (role3: remaining=100-80=20<=30 -> skip).
        monkeypatch.setattr(
            "cw.codex_review.time.monotonic", _Clock([0, 0, 0, 0, 0, 0, 0, 80])
        )
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
                session_id="s-review",
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
                session_id="s-review",
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
            session_id="s-review",
        )
        reasons = {f.role: f.reason for f in failures}
        assert reasons["Code Quality Reviewer"] == CODEX_TIMEOUT
        assert reasons["SysAdmin Reviewer"] == CODEX_REVIEW_UNPARSEABLE

    def test_native_review_schema_mismatch_one_role_others_succeed(
        self, tmp_path: Path
    ) -> None:
        # MUST_FIX 5 (#1236): synthetic fixture reproducing the historical
        # native-review schema/prose mismatch. Pre-#1236, ``codex exec
        # review`` was fed a schema it sometimes ignored, replying with the
        # OLD ``{must_fix_initial, should_fix, deferred}``-shaped payload (or
        # raw prose) instead of the per-role ReviewerFindingsDocument shape.
        # That payload fails schema validation for the role that produced it
        # (correctly classified as unparseable) but must NOT take down the
        # whole run — the other, well-behaved roles' documents still survive.
        old_shape_payload = json.dumps(
            {"must_fix_initial": 1, "should_fix": 2, "deferred": 0}
        )
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=0,
                    stdout="",
                    stderr="",
                    output_file_content=old_shape_payload,
                ),
                _ok_result(role="SysAdmin Reviewer"),
            ]
        )
        docs, failures = run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer", "SysAdmin Reviewer"],
            prompts_by_role={
                "Code Quality Reviewer": "p1",
                "SysAdmin Reviewer": "p2",
            },
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert len(docs) == 1
        assert docs[0].reviewer_role == "SysAdmin Reviewer"
        assert len(failures) == 1
        assert failures[0].role == "Code Quality Reviewer"
        assert failures[0].reason == CODEX_REVIEW_UNPARSEABLE

    def test_stdin_carries_prompt(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "PROMPT BODY"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert runner.calls[0]["stdin"] == "PROMPT BODY"

    def test_scratch_dir_cleaned_up_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MUST_FIX 1 (#1236): the scratch dir under state_dir() must not leak
        # after a normal, fully-successful run.
        fixed_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
        monkeypatch.setattr("cw.codex_review.uuid.uuid4", lambda: fixed_uuid)
        runner = _SequencedRunner([_ok_result()])
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert not (state_dir() / "codex-review" / fixed_uuid.hex).exists()

    def test_scratch_dir_cleaned_up_on_role_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cleanup must happen on the failure path too, not only on success.
        fixed_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
        monkeypatch.setattr("cw.codex_review.uuid.uuid4", lambda: fixed_uuid)
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p"},
            model=None,
            wall_clock_budget_seconds=None,
            session_id="s-review",
        )
        assert not (state_dir() / "codex-review" / fixed_uuid.hex).exists()


# ---------------------------------------------------------------------------
# synthesize_codex_review_result — disposition table
# ---------------------------------------------------------------------------


def _task() -> TicketTask:
    return _make_ticket_task(ticket_id="T-1", client="test", stage=Stage.REVIEW)


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
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert verdict is None

    @pytest.mark.parametrize(
        ("reason", "expect_retry"),
        [
            (CODEX_BUDGET_EXHAUSTED, True),
            (CODEX_TIMEOUT, True),
            (CODEX_ERROR, None),
        ],
    )
    def test_zero_documents_retry_eligible_by_reason(
        self,
        make_git_repo: Callable[[str], Path],
        reason: str,
        expect_retry: bool | None,
    ) -> None:
        # MUST_FIX 2 (#1236): retry_eligible tracks whether the failure(s) are
        # transient (timeout/budget_exhausted self-heal via reconcile); a hard
        # codex_error is not retried automatically. failures must also survive
        # into details rather than being dropped.
        worktree = make_git_repo(f"wt-synth-zero-{reason}")
        result, _verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[],
            failures=[ReviewerRunFailure(role="Code Quality Reviewer", reason=reason)],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.blocker is not None
        assert result.blocker.retry_eligible == expect_retry
        assert "Code Quality Reviewer" in result.blocker.details
        assert reason in result.blocker.details

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
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
        assert result.review.must_fix_initial == 1
        assert verdict is not None
        assert verdict.blocking is True

    @pytest.mark.parametrize(
        "reason", [CODEX_BUDGET_EXHAUSTED, CODEX_TIMEOUT, CODEX_ERROR]
    )
    def test_partial_review_blocked(
        self, make_git_repo: Callable[[str], Path], reason: str
    ) -> None:
        # Decision 7 (#1236): a non-blocking verdict (no MUST_FIX among the
        # roles that DID run) still blocks when at least one selected role
        # skipped or errored without a document, regardless of reason — an
        # incomplete review must not silently ship as stage_complete.
        worktree = make_git_repo(f"wt-synth-partial-{reason}")
        doc = _make_reviewer_doc(_make_finding(severity="SHOULD_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[ReviewerRunFailure(role="Performance Reviewer", reason=reason)],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_PARTIAL
        # The review counts derived from the roles that DID run still survive
        # onto the blocked sentinel — same "don't drop the parsed data"
        # discipline as the zero-documents and must-fix paths.
        assert result.review.should_fix == 1
        assert verdict is not None
        assert verdict.blocking is False

    def test_must_fix_takes_priority_over_partial(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        # When a role that DID run reports a real MUST_FIX finding, that is
        # the more actionable/specific block reason even if another role also
        # failed to run — CODEX_MUST_FIX_FINDINGS wins over CODEX_REVIEW_PARTIAL.
        worktree = make_git_repo("wt-synth-mf-and-partial")
        doc = _make_reviewer_doc(_make_finding(severity="MUST_FIX"))
        result, verdict = synthesize_codex_review_result(
            task=_task(),
            worktree=worktree,
            documents=[doc],
            failures=[
                ReviewerRunFailure(role="Performance Reviewer", reason=CODEX_TIMEOUT)
            ],
            diff=_make_diff(),
            reviewed_sha="sha",
            session_id="s-synth",
        )
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_MUST_FIX_FINDINGS
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
            session_id="s-synth",
        )
        assert result.status == "stage_complete"
        assert result.stage_reached == "stage3_review"
        assert result.health.recommendation == "PROCEED"
        assert result.review.should_fix == 1
        # Fully-documented review (no failures) → unchanged, and agents_run
        # counts exactly the one role that produced a document.
        assert result.review.agents_run == 1
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

    def test_mixed_must_fix_and_should_fix_both_render(self) -> None:
        # Test Reviewer SHOULD_FIX 10 (#1236): the old test suite never
        # rendered an actual SHOULD_FIX finding — assert both headings and
        # both findings' summaries appear, MUST_FIX first.
        diff = _make_diff(
            "def broken():",
            "second_line = 2",
            files={"src/cw/foo.py": [10, 11]},
        )
        must_fix = _make_finding(severity="MUST_FIX", summary="bad thing")
        should_fix = _make_finding(
            severity="SHOULD_FIX",
            summary="minor nit",
            line_start=11,
            line_end=11,
            evidence="second_line = 2",
        )
        doc = _make_reviewer_doc(must_fix, should_fix)
        verdict = consolidate_verdict([doc], diff, reviewed_sha="sha")
        body = render_verdict_comment(verdict)
        assert "BLOCKING" in body
        assert "### MUST_FIX" in body
        assert "### SHOULD_FIX" in body
        assert "bad thing" in body
        assert "minor nit" in body
        assert body.index("### MUST_FIX") < body.index("### SHOULD_FIX")


# ---------------------------------------------------------------------------
# _classify_codex_failure — typed failure taxonomy (#1239)
# ---------------------------------------------------------------------------


class TestClassifyCodexFailure:
    def test_timeout(self) -> None:
        result = CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)
        assert _classify_codex_failure(result) == "timeout"

    def test_spawn_error_command_not_found(self) -> None:
        result = CodexRunResult(
            returncode=127, stdout="", stderr="codex: command not found"
        )
        assert _classify_codex_failure(result) == "spawn_error"

    def test_nonzero_exit(self) -> None:
        result = CodexRunResult(returncode=1, stdout="", stderr="boom")
        assert _classify_codex_failure(result) == "nonzero_exit"

    def test_missing_output(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content=None
        )
        assert _classify_codex_failure(result) == "missing_output"

    def test_empty_output(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content="   "
        )
        assert _classify_codex_failure(result) == "empty_output"

    def test_invalid_json(self) -> None:
        result = CodexRunResult(
            returncode=0, stdout="", stderr="", output_file_content="{not json"
        )
        assert _classify_codex_failure(result) == "invalid_json"

    def test_schema_mismatch(self) -> None:
        result = CodexRunResult(
            returncode=0,
            stdout="",
            stderr="",
            output_file_content='{"unexpected": "shape"}',
        )
        assert _classify_codex_failure(result) == "schema_mismatch"


def test_category_to_reason_mapping_is_total() -> None:
    """Every ExecutorFailureCategory member is a key in _CATEGORY_TO_REASON —
    guards the total-dict design decision against a future silent KeyError
    (item 5, #1330)."""
    for category in get_args(ExecutorFailureCategory):
        assert category in _CATEGORY_TO_REASON


def test_run_codex_role_spawn_error_surfaces_codex_error_reason(
    tmp_path: Path,
) -> None:
    """A spawn_error-shaped CodexRunResult (codex binary missing) surfaces as
    ReviewerRunFailure.reason == CODEX_ERROR through _CATEGORY_TO_REASON, while
    the persisted bundle's category stays the fine-grained 'spawn_error'."""
    runner = _SequencedRunner(
        [CodexRunResult(returncode=127, stdout="", stderr="codex: command not found")]
    )
    _doc, failure = _run_one_role(runner, tmp_path, session_id="sess-spawn")
    assert failure is not None
    assert failure.reason == CODEX_ERROR
    path = _bundle_file("sess-spawn", "code-quality-reviewer", "spawn_error")
    persisted = ExecutorFailure.model_validate_json(path.read_text())
    assert persisted.category == "spawn_error"


# ---------------------------------------------------------------------------
# _run_codex_role — diagnostics persistence on failure (#1239)
# ---------------------------------------------------------------------------


def _run_one_role(
    runner: _SequencedRunner,
    tmp_path: Path,
    *,
    session_id: str = "sess-diag",
    role: str = "Code Quality Reviewer",
) -> tuple[object, object]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return _run_codex_role(
        runner=runner,
        worktree=tmp_path,
        role=role,
        prompt="p",
        model=None,
        timeout_seconds=None,
        scratch_dir=scratch,
        session_id=session_id,
    )


class TestRunCodexRolePersistsDiagnostics:
    def test_persists_diagnostics_on_timeout(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=-1, stdout="", stderr="", timed_out=True)]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", "timeout")
        assert path.exists()
        failure = ExecutorFailure.model_validate_json(path.read_text())
        assert failure.category == "timeout"
        assert failure.executor_name == "codex"
        assert failure.reviewer_role == "Code Quality Reviewer"
        assert failure.session_id == "sess-diag"

    def test_persists_diagnostics_on_nonzero_exit(self, tmp_path: Path) -> None:
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr="boom")]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", "nonzero_exit")
        assert path.exists()
        assert (
            ExecutorFailure.model_validate_json(path.read_text()).category
            == "nonzero_exit"
        )

    @pytest.mark.parametrize(
        ("output_content", "category"),
        [
            (None, "missing_output"),
            ("", "empty_output"),
            ("{not json", "invalid_json"),
            ('{"unexpected": "shape"}', "schema_mismatch"),
        ],
    )
    def test_persists_diagnostics_on_unparseable_output_variants(
        self, tmp_path: Path, output_content: str | None, category: str
    ) -> None:
        runner = _SequencedRunner(
            [
                CodexRunResult(
                    returncode=0,
                    stdout="",
                    stderr="",
                    output_file_content=output_content,
                )
            ]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", category)
        assert path.exists()
        assert (
            ExecutorFailure.model_validate_json(path.read_text()).category == category
        )

    def test_success_does_not_persist_diagnostics(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        doc, failure = _run_one_role(runner, tmp_path)
        assert doc is not None
        assert failure is None
        assert not diagnostics_bundle_dir("sess-diag").exists()

    def test_secret_shaped_stderr_is_redacted_in_persisted_bundle(
        self, tmp_path: Path
    ) -> None:
        # Drives a secret-shaped string through the real production path
        # (_run_codex_role -> _persist_codex_role_diagnostics) rather than
        # unit-testing redact() in isolation, so a future call site that
        # forgets to route stderr through the ExecutorFailure validator would
        # be caught here.
        secret = "sk-" + "a" * 40
        runner = _SequencedRunner(
            [CodexRunResult(returncode=1, stdout="", stderr=f"boom: {secret}")]
        )
        _run_one_role(runner, tmp_path)
        path = _bundle_file("sess-diag", "code-quality-reviewer", "nonzero_exit")
        failure = ExecutorFailure.model_validate_json(path.read_text())
        assert secret not in failure.stderr_excerpt
        assert "<redacted>" in failure.stderr_excerpt


# ---------------------------------------------------------------------------
# _run_codex_role — writes an OpenAI strict-mode schema file (#1364)
# ---------------------------------------------------------------------------


class TestRunCodexRoleWritesStrictSchema:
    def test_schema_file_content_is_strict(self, tmp_path: Path) -> None:
        runner = _SequencedRunner([_ok_result()])
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        _run_codex_role(
            runner=runner,
            worktree=tmp_path,
            role="Code Quality Reviewer",
            prompt="p",
            model=None,
            timeout_seconds=None,
            scratch_dir=scratch,
            session_id="sess-strict",
        )
        schema_path = scratch / "code-quality-reviewer-schema.json"
        schema = json.loads(schema_path.read_text())

        assert schema["additionalProperties"] is False
        assert schema["$defs"]["Finding"]["additionalProperties"] is False
        assert schema["$defs"]["EscalationMetadata"]["additionalProperties"] is False

        nodes = [
            schema,
            schema["$defs"]["Finding"],
            schema["$defs"]["EscalationMetadata"],
        ]
        for node in nodes:
            assert set(node["required"]) == set(node["properties"].keys())


def test_run_codex_roles_scratch_dir_still_removed_after_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even when a role failure triggers a diagnostics copy from the scratch dir,
    # run_codex_roles still removes the scratch dir before returning; the
    # persisted bundle (under a different tree) survives.
    fixed_uuid = uuid.UUID("33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr("cw.codex_review.uuid.uuid4", lambda: fixed_uuid)
    runner = _SequencedRunner([CodexRunResult(returncode=1, stdout="", stderr="boom")])
    run_codex_roles(
        runner=runner,
        worktree=tmp_path,
        roles=["Code Quality Reviewer"],
        prompts_by_role={"Code Quality Reviewer": "p"},
        model=None,
        wall_clock_budget_seconds=None,
        session_id="sess-scratch",
    )
    assert not (state_dir() / "codex-review" / fixed_uuid.hex).exists()
    assert _bundle_file(
        "sess-scratch", "code-quality-reviewer", "nonzero_exit"
    ).exists()


def test_format_failures_detail_includes_diagnostics_path() -> None:
    failures = [ReviewerRunFailure(role="Code Quality Reviewer", reason=CODEX_TIMEOUT)]
    detail = _format_failures_detail(failures, session_id="sess-fmt")
    assert "Code Quality Reviewer (codex_timeout)" in detail
    # tmp_config_dir relocates state_dir() away from the real home, so
    # _render_bundle_path takes its absolute-fallback branch: the rendered
    # pointer is exactly "[diagnostics: <absolute bundle dir>]".
    bundle = diagnostics_bundle_dir("sess-fmt")
    assert detail == f"Code Quality Reviewer (codex_timeout) [diagnostics: {bundle}]"


def test_run_review_threads_session_id_to_run_codex_role(
    make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = make_git_repo("wt-run-review-thread")
    captured: dict[str, object] = {}

    def _spy_run_codex_role(**kwargs: object) -> tuple[object, object]:
        captured["session_id"] = kwargs["session_id"]
        return _make_reviewer_doc(), None

    monkeypatch.setattr("cw.codex_review._run_codex_role", _spy_run_codex_role)
    run_review(
        runner=_SequencedRunner([]),
        task=_task(),
        worktree=worktree,
        default_branch="main",
        model=None,
        wall_clock_budget_seconds=None,
        session_id="sess-thread",
    )
    assert captured["session_id"] == "sess-thread"


def test_duration_captured_without_extending_codex_run_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CodexRunResult stays free of a duration attribute; the persisted
    # ExecutorFailure.duration_seconds is populated from a monotonic() delta.
    monkeypatch.setattr("cw.codex_review.time.monotonic", _Clock([100.0, 105.5]))
    runner = _SequencedRunner([CodexRunResult(returncode=1, stdout="", stderr="boom")])
    _run_one_role(runner, tmp_path, session_id="sess-dur")
    result = runner._results[0]
    assert not hasattr(result, "duration")
    path = _bundle_file("sess-dur", "code-quality-reviewer", "nonzero_exit")
    failure = ExecutorFailure.model_validate_json(path.read_text())
    assert failure.duration_seconds == pytest.approx(5.5)
