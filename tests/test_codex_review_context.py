"""Tests for cw.codex_review._context — reviewer selection and prompt-context
assembly, incl. ``_prepare_review_pass`` (#1236)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.codex_review import (
    _build_reviewer_prompt,
    _categorize_changed_files,
    _load_agent_spec_fallback_gate,
    _load_claude_md_quality_gates,
    _load_optional_text,
    _load_review_policy,
    _load_ruff_lint_config,
    _load_sensitive_hits,
    _load_ticket_context,
    _parse_reviewer_document,
    _prepare_review_pass,
    _read_sensitive_manifest,
    _render_lint_grounding_block,
    _resolve_agent_spec,
    _RuffLintConfig,
    _select_reviewer_roles,
)
from cw.codex_review._capability import _PROBE_SENTINEL
from cw.codex_review._context import (
    _CAPABLE_ONLY_MARKER,
    _DELTA_MODE_MARKER,
    _INLINED_ONLY_MARKER,
    _OUTPUT_INSTRUCTIONS_CAPABLE,
    _OUTPUT_INSTRUCTIONS_INLINED_ONLY,
    _REVIEWER_ROLE_AGENT_FILES,
    _load_finding_dispositions,
    _load_operator_comments,
    _load_pending_operator_comment_marker,
    _load_voided_findings,
    _render_adjudicated_findings_block,
    _select_output_instructions,
)
from cw.codex_runner import FakeCodexRunner
from cw.models import HOOK_CONTEXT_RELATIVE_PATH, SessionOrigin
from cw.review_adjudication import render_voided_findings_block
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    render_finding_disposition_block,
)
from cw.spawn import _write_hook_context
from tests._codex_review_helpers import (
    _doc_json,
    _finding_payload,
    _git,
    _populate_global_agents_dir,
    _task,
    _write,
)
from tests.conftest import _make_diff, _make_finding, _make_ticket_task
from tests.test_review_adjudication import _make_voided_finding

if TYPE_CHECKING:
    from collections.abc import Callable


def _write_real_hook_context(worktree: Path, *, pending: bool) -> None:
    """Materialize the hook context via spawn's REAL writer (#1730).

    Deliberately not a hand-placed fixture: driving ``_write_hook_context`` is
    what binds the reviewer-side read path to the spawn-side write path, so a
    future edit that moves either end fails the assertion instead of shipping a
    silently dead feature (#1628 — make the seam survive the fix).
    """
    _write_hook_context(
        worktree,
        session_id="s-1730",
        session_name="test-client/auto-dev/1730",
        client="test-client",
        purpose="review",
        ticket_id="1730",
        origin=SessionOrigin.DAEMON,
        task=_make_ticket_task(pending_operator_comment=pending),
    )


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
# _load_optional_text
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


# ---------------------------------------------------------------------------
# _load_sensitive_hits — scope-tier divergence
# ---------------------------------------------------------------------------


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


class TestLoadOperatorComments:
    """#1730: live comment fetch for the codex review path."""

    def _github_repo(self, tmp_path: Path) -> Path:
        _write(
            tmp_path / ".claude" / "project-config.yaml",
            "tracking:\n  primary:\n    system: github-issues\n",
        )
        return tmp_path

    def test_returns_none_when_tracker_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No project-config.yaml at all -- degrade, never guess a tracker."""

        def _fail_if_called(*_a: object, **_kw: object) -> None:
            msg = "must not fetch when the tracker is unknown"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", _fail_if_called
        )
        assert _load_operator_comments(tmp_path, "T-1") is None

    def test_returns_none_on_fetch_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gh failure surfaces as None from fetch_issue_comments -- degrade."""
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: None
        )
        assert _load_operator_comments(self._github_repo(tmp_path), "T-1") is None

    def test_returns_none_on_empty_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: []
        )
        assert _load_operator_comments(self._github_repo(tmp_path), "T-1") is None

    def test_skips_bodiless_comments_and_renders_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whitespace-only or body-less entry contributes nothing; a comment
        with no author/createdAt still renders under an 'unknown' header."""
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {"body": "   "},
                {"author": {"login": "op"}, "createdAt": "2026-08-10T00:00:00Z"},
                {"body": "REAL BODY"},
            ],
        )
        rendered = _load_operator_comments(self._github_repo(tmp_path), "T-1")
        assert rendered == "### unknown\nREAL BODY"

    def test_returns_none_when_every_comment_is_bodiless(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A thread of empty bodies is indistinguishable from no thread."""
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [{"body": ""}],
        )
        assert _load_operator_comments(self._github_repo(tmp_path), "T-1") is None

    def test_joins_multiple_comments_with_blank_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1730: this ticket delivers the full operator thread, not just a
        single comment -- the multi-comment join path must be covered."""
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {
                    "author": {"login": "a"},
                    "createdAt": "2026-08-10T00:00:00Z",
                    "body": "BODY1",
                },
                {
                    "author": {"login": "b"},
                    "createdAt": "2026-08-10T01:00:00Z",
                    "body": "BODY2",
                },
            ],
        )
        rendered = _load_operator_comments(self._github_repo(tmp_path), "T-1")
        assert rendered == (
            "### a (2026-08-10T00:00:00Z)\nBODY1\n\n### b (2026-08-10T01:00:00Z)\nBODY2"
        )


class TestLoadVoidedFindings:
    """#1814: the voided-findings sentinel read, degrading to [] throughout.

    Same tracker gate, same fetch op, and the same never-raise contract as
    :class:`TestLoadOperatorComments` above — a Stage-3 pass that cannot reach
    the tracker must still review, it just cannot honor a void it never saw.
    """

    def _github_repo(self, tmp_path: Path) -> Path:
        _write(
            tmp_path / ".claude" / "project-config.yaml",
            "tracking:\n  primary:\n    system: github-issues\n",
        )
        return tmp_path

    def test_returns_empty_when_tracker_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail_if_called(*_a: object, **_kw: object) -> None:
            msg = "must not fetch when the tracker is unknown"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", _fail_if_called
        )
        assert _load_voided_findings(tmp_path, "T-1") == []

    def test_returns_empty_on_fetch_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: None
        )
        assert _load_voided_findings(self._github_repo(tmp_path), "T-1") == []

    def test_returns_empty_on_empty_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: []
        )
        assert _load_voided_findings(self._github_repo(tmp_path), "T-1") == []

    def test_returns_empty_when_no_comment_carries_a_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [{"author": {"login": "a"}, "body": "just prose"}],
        )
        assert _load_voided_findings(self._github_repo(tmp_path), "T-1") == []

    def test_parses_the_sentinel_out_of_the_comment_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = _make_voided_finding()
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {"author": {"login": "a"}, "body": "unrelated send-back"},
                {"author": None, "body": None},
                {
                    "author": {"login": "b"},
                    "body": render_voided_findings_block([entry]),
                },
            ],
        )
        assert _load_voided_findings(self._github_repo(tmp_path), "T-1") == [entry]

    def test_prepare_review_pass_carries_voided_findings(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loader's output reaches ``_ReviewPassInputs`` (#1814)."""
        repo = make_git_repo("wt-prepare-voided")
        entry = _make_voided_finding()
        monkeypatch.setattr(
            "cw.codex_review._context._load_voided_findings",
            lambda *_a, **_kw: [entry],
        )
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-voided"
        )
        assert prepared.voided_findings == [entry]


class TestLoadPendingOperatorCommentMarker:
    """#1730: the queue_metadata marker read, fail-safe to False throughout.

    Every populated case goes through the REAL spawn-side writer or through
    ``HOOK_CONTEXT_RELATIVE_PATH``, the single constant both ends resolve —
    never a hand-placed file at a path this test picked. The first cut of this
    suite authored ``.cw/context.json`` itself and so agreed with a reader that
    pointed at the wrong file: it proved the parser worked while the production
    read missed every time and the banner never rendered. A reader/writer path
    disagreement now fails ``test_marker_true_from_real_writer``, and a
    regression back to the Stage 0 ticket-context file fails
    ``test_marker_not_read_from_stage0_ticket_context``.
    """

    def test_absent_context_json(self, tmp_path: Path) -> None:
        assert _load_pending_operator_comment_marker(tmp_path) is False

    def test_malformed_json(self, tmp_path: Path) -> None:
        _write(tmp_path / HOOK_CONTEXT_RELATIVE_PATH, "not json{{")
        assert _load_pending_operator_comment_marker(tmp_path) is False

    def test_non_dict_json(self, tmp_path: Path) -> None:
        _write(tmp_path / HOOK_CONTEXT_RELATIVE_PATH, "[1, 2, 3]")
        assert _load_pending_operator_comment_marker(tmp_path) is False

    def test_missing_queue_metadata(self, tmp_path: Path) -> None:
        _write(tmp_path / HOOK_CONTEXT_RELATIVE_PATH, json.dumps({"title": "t"}))
        assert _load_pending_operator_comment_marker(tmp_path) is False

    def test_marker_true_from_real_writer(self, tmp_path: Path) -> None:
        """Pipeline test: spawn writes, the reviewer reads, no fixture between."""
        _write_real_hook_context(tmp_path, pending=True)
        assert _load_pending_operator_comment_marker(tmp_path) is True

    def test_marker_false_from_real_writer(self, tmp_path: Path) -> None:
        _write_real_hook_context(tmp_path, pending=False)
        assert _load_pending_operator_comment_marker(tmp_path) is False

    def test_marker_not_read_from_stage0_ticket_context(self, tmp_path: Path) -> None:
        """The marker must NOT be honored out of ``.cw/context.json`` (#1730).

        Different layer: that file is Stage 0 *ticket* context and is deleted
        outright by ``_invalidate_stale_context_json`` (#1046) on a rescued
        respawn, so dispatch state read from there would vanish. Fails if the
        reader ever drifts back onto it.
        """
        _write(
            tmp_path / ".cw" / "context.json",
            json.dumps({"queue_metadata": {"pending_operator_comment": True}}),
        )
        assert _load_pending_operator_comment_marker(tmp_path) is False


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
        payload = _doc_json(findings=[_finding_payload()])
        doc = _parse_reviewer_document(payload)
        assert doc is not None
        assert doc.status == "ok"
        assert len(doc.findings) == 1
        assert doc.findings[0].severity == "MUST_FIX"


# ---------------------------------------------------------------------------
# _load_review_policy — scope-tier divergence
# ---------------------------------------------------------------------------


class TestLoadReviewPolicy:
    def test_small_returns_empty_without_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path / ".claude" / "review-policy.md", "## Code Quality Reviewer\nx")
        calls: list[str] = []
        import cw.codex_review._context as cr

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
# _load_ruff_lint_config (#1744)
# ---------------------------------------------------------------------------


class TestLoadRuffLintConfig:
    def test_reads_ignore_list_and_pylint_overrides_when_present(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "pyproject.toml",
            '[tool.ruff.lint]\nignore = ["PLR0913", "T201"]\n\n'
            "[tool.ruff.lint.pylint]\nmax-branches = 15\n",
        )
        result = _load_ruff_lint_config(tmp_path)
        assert result is not None
        assert result.ignore == ("PLR0913", "T201")
        assert result.pylint_overrides.get("max-branches") == 15

    def test_missing_pylint_subtable_yields_no_overrides(self, tmp_path: Path) -> None:
        # Mirrors this repo's actual real state: ignore-only, no pylint subtable.
        _write(tmp_path / "pyproject.toml", '[tool.ruff.lint]\nignore = ["PLR0913"]\n')
        result = _load_ruff_lint_config(tmp_path)
        assert result is not None
        assert result.ignore == ("PLR0913",)
        assert result.pylint_overrides == {}

    def test_missing_pyproject_returns_none(self, tmp_path: Path) -> None:
        assert _load_ruff_lint_config(tmp_path) is None

    def test_malformed_toml_returns_none(self, tmp_path: Path) -> None:
        _write(tmp_path / "pyproject.toml", "not [ valid toml{{{\n")
        assert _load_ruff_lint_config(tmp_path) is None

    def test_missing_tool_ruff_lint_section_returns_empty_not_none(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
        result = _load_ruff_lint_config(tmp_path)
        assert result is not None
        assert result.ignore == ()
        assert result.pylint_overrides == {}


# ---------------------------------------------------------------------------
# _load_claude_md_quality_gates (#1744)
# ---------------------------------------------------------------------------


class TestLoadClaudeMdQualityGates:
    def test_extracts_quality_gates_section_verbatim(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "CLAUDE.md",
            "## Quality Gates\nbody line 1\nbody line 2\n## Module Size\nother\n",
        )
        result = _load_claude_md_quality_gates(tmp_path)
        assert result is not None
        assert "body line 1" in result
        assert "body line 2" in result
        assert "Module Size" not in result
        assert "other" not in result

    def test_missing_claude_md_returns_none(self, tmp_path: Path) -> None:
        assert _load_claude_md_quality_gates(tmp_path) is None

    def test_missing_heading_returns_none(self, tmp_path: Path) -> None:
        _write(tmp_path / "CLAUDE.md", "## Some Other Heading\nbody\n")
        assert _load_claude_md_quality_gates(tmp_path) is None

    def test_against_real_repo_claude_md(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = _load_claude_md_quality_gates(repo_root)
        assert result is not None
        assert "PLR0912" in result
        assert "PLR0915" in result
        assert "PLR0911" in result
        assert "PLR0913" in result


# ---------------------------------------------------------------------------
# _render_lint_grounding_block (#1744)
# ---------------------------------------------------------------------------


class TestRenderLintGroundingBlock:
    def test_both_present_includes_ignore_list_and_quality_gates_text(self) -> None:
        config = _RuffLintConfig(ignore=("PLR0913",), pylint_overrides={})
        result = _render_lint_grounding_block(
            ruff_config=config,
            quality_gates_text="QUALITY GATES BODY",
        )
        assert result is not None
        assert "PLR0913" in result
        assert "QUALITY GATES BODY" in result

    def test_ruff_config_only_omits_quality_gates_subsection(self) -> None:
        config = _RuffLintConfig(ignore=("PLR0913",), pylint_overrides={})
        result = _render_lint_grounding_block(
            ruff_config=config,
            quality_gates_text=None,
        )
        assert result is not None
        assert "PLR0913" in result
        assert "Quality Gates" not in result

    def test_quality_gates_only_omits_ruff_subsection(self) -> None:
        result = _render_lint_grounding_block(
            ruff_config=None,
            quality_gates_text="QUALITY GATES BODY",
        )
        assert result is not None
        assert "QUALITY GATES BODY" in result
        assert "Globally Ignored Ruff Rules" not in result

    def test_both_absent_returns_none(self) -> None:
        assert (
            _render_lint_grounding_block(
                ruff_config=None,
                quality_gates_text=None,
            )
            is None
        )
        empty_config = _RuffLintConfig(ignore=(), pylint_overrides={})
        assert (
            _render_lint_grounding_block(
                ruff_config=empty_config,
                quality_gates_text=None,
            )
            is None
        )
        assert (
            _render_lint_grounding_block(
                ruff_config=empty_config,
                quality_gates_text="",
            )
            is None
        )

    def test_always_states_not_a_must_fix_instruction_when_rendered(self) -> None:
        result = _render_lint_grounding_block(
            ruff_config=_RuffLintConfig(ignore=("X",), pylint_overrides={}),
            quality_gates_text=None,
        )
        assert result is not None
        assert "is not a MUST_FIX" in result

    def test_ignored_security_rule_does_not_suppress_concrete_failure(self) -> None:
        result = _render_lint_grounding_block(
            ruff_config=_RuffLintConfig(ignore=("S603",), pylint_overrides={}),
            quality_gates_text=None,
        )
        assert result is not None
        assert "based solely on enforcing a ruff rule" in result
        assert "concrete security or correctness failure" in result
        assert "report such a failure as MUST_FIX" in result
        assert "S603" in result

    def test_distinguishes_statements_from_lines(self) -> None:
        result = _render_lint_grounding_block(
            ruff_config=_RuffLintConfig(ignore=("X",), pylint_overrides={}),
            quality_gates_text=None,
        )
        assert result is not None
        assert "PLR0915" in result
        lowered = result.lower()
        assert "statement" in lowered
        assert "not the number of lines" in lowered or "not lines" in lowered

    def test_no_parallel_thresholds_when_pylint_subtable_absent(self) -> None:
        result = _render_lint_grounding_block(
            ruff_config=_RuffLintConfig(ignore=("PLR0913",), pylint_overrides={}),
            quality_gates_text=None,
        )
        assert result is not None
        assert "Complexity Thresholds" not in result
        assert "max-branches" not in result
        assert "max-statements" not in result
        assert "max-returns" not in result


# ---------------------------------------------------------------------------
# code-reviewer.md "Concrete Numeric Thresholds" table (#1774)
# ---------------------------------------------------------------------------


class TestCodeReviewerAgentSpecFunctionLengthGate:
    """Regression guard for #1774: a row in the Concrete Numeric Thresholds
    table may assert MUST_FIX only if it cites a gate this repo actually
    configures; Function length, Nesting depth, Parameter count, and the
    positional-args row must not assert a bare/unbacked MUST_FIX threshold.
    See the inline #1774 note in the table's prose for the full rationale.
    """

    def _spec_text(self) -> str:
        repo_root = Path(__file__).resolve().parents[1]
        return (repo_root / ".claude" / "agents" / "code-reviewer.md").read_text()

    def test_function_length_row_does_not_assert_bare_line_count(self) -> None:
        text = self._spec_text()
        assert "| Function length | 50 lines | MUST_FIX" not in text

    def test_function_length_row_cites_configured_gate(self) -> None:
        text = self._spec_text()
        assert "PLR0915" in text

    def test_parameter_count_row_cites_configured_gate(self) -> None:
        text = self._spec_text()
        assert "PLR0913" in text

    def test_nesting_depth_row_no_longer_bare_must_fix(self) -> None:
        text = self._spec_text()
        assert "Nesting depth (if/for/with) | 4 levels | MUST_FIX" not in text

    def test_nesting_depth_row_is_non_blocking(self) -> None:
        text = self._spec_text()
        assert "Nesting depth (if/for/with) | 4 levels" in text
        assert "SHOULD_FIX only, as a readability suggestion" in text

    def test_positional_args_row_no_longer_bare_must_fix(self) -> None:
        text = self._spec_text()
        assert "Function calls with 2+ positional args | any | MUST_FIX" not in text

    def test_positional_args_row_is_non_blocking(self) -> None:
        text = self._spec_text()
        assert "Function calls with 2+ positional args | any" in text
        assert "SHOULD_FIX missing-named-args" in text

    def test_table_carries_1774_regression_note(self) -> None:
        text = self._spec_text()
        assert "#1774" in text


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

    def test_build_reviewer_prompt_renders_comments_section(self) -> None:
        """#1730: operator comments render as their own section, with the
        elevated-priority banner gated on the pending marker."""
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
            operator_comments_text="FOO",
        )
        assert "## Ticket Comments (live-fetched, chronological)" in prompt
        assert "FOO" in prompt
        assert "Pending Operator Send-Back" not in prompt

        banner_prompt = _build_reviewer_prompt(
            "SysAdmin Reviewer",
            agent_spec_text="SPEC",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
            operator_comments_text="FOO",
            pending_operator_comment=True,
        )
        assert "Pending Operator Send-Back" in banner_prompt

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
        assert "## Ticket Comments" not in prompt
        assert "## Approved Plan" not in prompt
        assert "## Ticket Context" not in prompt
        assert "## Project Rubrics" not in prompt
        assert "ELEVATED SCRUTINY" not in prompt
        assert "## Repo Lint Configuration" not in prompt

    def test_lint_grounding_section_included_when_provided(self) -> None:
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
            lint_grounding="GROUNDING BODY",
        )
        assert "## Repo Lint Configuration" in prompt
        assert "GROUNDING BODY" in prompt
        assert prompt.index("## Repo Lint Configuration") < prompt.index(
            "advisory here, not blocking"
        )

    def test_output_instructions_override_agent_spec_preconditions(self) -> None:
        """_OUTPUT_INSTRUCTIONS must countermand the inlined agent spec's own
        tool/verification preconditions and output conventions (#1543) — and
        must appear positionally after the inlined spec so it wins as the
        last word in the assembled prompt."""
        prompt = _build_reviewer_prompt(
            "Code Quality Reviewer",
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        override_substring = "advisory here, not blocking"
        assert override_substring in prompt
        assert prompt.index("AGENT SPEC BODY") < prompt.index(override_substring)

    # --- item 1: dangling-reference supplement (#1548) ---

    @pytest.mark.parametrize(
        "role",
        [
            "Architecture Reviewer",
            "Test Reviewer",
            "Performance Reviewer",
            "API Contract Validator",
            "Deployment Reviewer",
        ],
    )
    def test_output_format_roles_get_severity_taxonomy_supplement(
        self, role: str
    ) -> None:
        """The 5 roles whose spec points at output-formats.md (codex cannot open
        it — no live filesystem access) get the MUST_FIX/SHOULD_FIX/NIT mapping
        inlined instead (#1548 item 1)."""
        prompt = _build_reviewer_prompt(
            role,
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        assert "MUST_FIX" in prompt
        assert "SHOULD_FIX" in prompt
        assert "NIT" in prompt
        assert "Critical" in prompt
        assert "Major" in prompt

    def test_code_quality_reviewer_prompt_inlines_tone_guide(self) -> None:
        prompt = _build_reviewer_prompt(
            "Code Quality Reviewer",
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        assert "No Praise" in prompt or "no praise" in prompt.lower()
        assert "SHOULD_FIX" not in prompt

    def test_test_reviewer_prompt_inlines_testing_checklist(self) -> None:
        prompt = _build_reviewer_prompt(
            "Test Reviewer",
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        assert "AAA" in prompt
        assert "MUST_FIX" in prompt

    @pytest.mark.parametrize(
        "role",
        ["SysAdmin Reviewer", "Data Safety Reviewer", "Product Manager Reviewer"],
    )
    def test_roles_without_dangling_refs_get_no_supplement(self, role: str) -> None:
        """Roles whose spec has its own complete inline output format (no dangling
        file reference) must not have the supplement injected."""
        prompt = _build_reviewer_prompt(
            role,
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        assert "Severity Taxonomy" not in prompt

    def test_supplement_appears_before_output_instructions(self) -> None:
        """The new supplement must sit before _OUTPUT_INSTRUCTIONS (last-wins
        precedence, #1543) — never after."""
        prompt = _build_reviewer_prompt(
            "Deployment Reviewer",
            agent_spec_text="AGENT SPEC BODY",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
        )
        assert prompt.index("MUST_FIX") < prompt.index("advisory here, not blocking")

    # --- item 2: regression-lock (#1548, expected to pass immediately) ---

    def test_output_instructions_states_degraded_and_low_confidence_cases_distinctly(
        self,
    ) -> None:
        """Locks #1544's already-correct wording distinguishing document-level
        status="degraded" (a check could not run at all) from per-finding
        confidence="LOW" (groundable finding, verification step unperformed) —
        ticket #1548's item 2. Expected GREEN before any Phase 2 change."""
        from cw.codex_review._context import _OUTPUT_INSTRUCTIONS

        assert 'status="degraded"' in _OUTPUT_INSTRUCTIONS
        assert 'confidence: "LOW"' in _OUTPUT_INSTRUCTIONS
        assert "report the finding anyway" in _OUTPUT_INSTRUCTIONS
        assert "Never suppress a diff-groundable finding" in _OUTPUT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Capability-driven output-instruction selection (#1709)
# ---------------------------------------------------------------------------


class TestSelectOutputInstructions:
    def test_capable_selects_the_capable_variant(self) -> None:
        assert _select_output_instructions(True) is _OUTPUT_INSTRUCTIONS_CAPABLE

    def test_incapable_selects_the_inlined_only_variant(self) -> None:
        assert _select_output_instructions(False) is _OUTPUT_INSTRUCTIONS_INLINED_ONLY

    def test_inlined_only_variant_is_the_back_compat_alias(self) -> None:
        """``_OUTPUT_INSTRUCTIONS`` must stay byte-identical to the pre-#1709
        single-variant text so the #1548 regression-lock above keeps meaning
        what it meant."""
        from cw.codex_review._context import _OUTPUT_INSTRUCTIONS

        assert _OUTPUT_INSTRUCTIONS == _OUTPUT_INSTRUCTIONS_INLINED_ONLY

    def test_both_variants_keep_the_shared_schema_and_precedence_rules(self) -> None:
        for variant in (
            _OUTPUT_INSTRUCTIONS_CAPABLE,
            _OUTPUT_INSTRUCTIONS_INLINED_ONLY,
        ):
            assert 'status="degraded"' in variant
            assert 'confidence: "LOW"' in variant
            assert "advisory here, not blocking" in variant
            assert "Report no prose outside the JSON object." in variant
            # #1806: `detail` is required and non-empty whenever status is
            # "degraded" or "failed" -- must appear in both variants.
            assert (
                "`detail` is REQUIRED and MUST be non-empty whenever `status` "
                'is "degraded" or "failed"' in variant
            )


class TestBuildReviewerPromptCapability:
    """R8 anti-vacuous seam: each capability value must produce a prompt that
    carries its own marker and NOT the other's. Either direction of regression
    (always-capable, always-inlined) fails one of these two tests."""

    def _prompt(self, *, capable: bool) -> str:
        return _build_reviewer_prompt(
            "SysAdmin Reviewer",
            agent_spec_text="SPEC",
            diff=_make_diff(),
            changed_files=["src/cw/foo.py"],
            plan_text=None,
            ticket_text=None,
            project_rubrics=None,
            repo_policy_section=None,
            sensitive_hits=[],
            capable=capable,
        )

    def test_capable_prompt_grants_read_only_repo_access(self) -> None:
        prompt = self._prompt(capable=True)
        assert _CAPABLE_ONLY_MARKER in prompt
        assert _INLINED_ONLY_MARKER not in prompt

    def test_incapable_prompt_keeps_the_inlined_only_premise(self) -> None:
        prompt = self._prompt(capable=False)
        assert _INLINED_ONLY_MARKER in prompt
        assert _CAPABLE_ONLY_MARKER not in prompt

    def test_capable_defaults_to_false(self) -> None:
        """The parameter is additive-with-default so ``TestBuildReviewerPrompt``'s
        variant-agnostic call sites stay byte-identical; production always
        passes it explicitly."""
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
        assert _INLINED_ONLY_MARKER in prompt


# ---------------------------------------------------------------------------
# _load_agent_spec_fallback_gate (#1773)
# ---------------------------------------------------------------------------


class TestLoadAgentSpecFallbackGate:
    def test_missing_pyproject_defaults_to_enabled(self, tmp_path: Path) -> None:
        assert _load_agent_spec_fallback_gate(tmp_path) is True

    def test_missing_table_defaults_to_enabled(self, tmp_path: Path) -> None:
        _write(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
        assert _load_agent_spec_fallback_gate(tmp_path) is True

    def test_explicit_false_disables_the_fallback(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "pyproject.toml",
            "[tool.cw.codex_review]\nagent_spec_global_fallback = false\n",
        )
        assert _load_agent_spec_fallback_gate(tmp_path) is False

    def test_explicit_true_keeps_the_fallback_enabled(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "pyproject.toml",
            "[tool.cw.codex_review]\nagent_spec_global_fallback = true\n",
        )
        assert _load_agent_spec_fallback_gate(tmp_path) is True

    def test_malformed_toml_fails_safe_to_enabled(self, tmp_path: Path) -> None:
        _write(tmp_path / "pyproject.toml", "not [ valid toml{{{\n")
        assert _load_agent_spec_fallback_gate(tmp_path) is True


def test_config_reference_documents_agent_spec_global_fallback() -> None:
    """CONFIG_REFERENCE.md documents the #1773 fallback opt-out (#1782)."""
    doc = (
        Path(__file__).resolve().parent.parent / "config" / "CONFIG_REFERENCE.md"
    ).read_text(encoding="utf-8")
    assert "agent_spec_global_fallback" in doc
    assert "[tool.cw.codex_review]" in doc
    assert "#1773" in doc


# ---------------------------------------------------------------------------
# _resolve_agent_spec (#1773)
# ---------------------------------------------------------------------------


_ROLE = "Code Quality Reviewer"
_ROLE_FILE = "code-reviewer.md"


def _write_repo_spec(worktree: Path, content: str) -> None:
    _write(worktree / ".claude" / "agents" / _ROLE_FILE, content)


def _patch_global_agents(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("cw.codex_review._context._GLOBAL_AGENTS_DIR", path)


class TestResolveAgentSpec:
    """Repo-local -> global fallback resolution, its gate, and its diagnostics.

    Relies on conftest's autouse ``_isolate_global_agents_dir`` for the
    global-absent cases; the fallback-succeeds cases re-patch the same name
    with a populated directory.
    """

    def test_repo_local_present_wins_and_never_reads_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _write_repo_spec(worktree, "REPO SPEC BODY\n")
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(
            tmp_path / "global", code_reviewer="GLOBAL SPEC BODY\n"
        )

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=True)

        assert resolved.text == "REPO SPEC BODY\n"
        assert resolved.status.source == "repo"
        assert resolved.status.empty is False
        assert resolved.status.empty_repo_file is False

    def test_repo_absent_falls_back_to_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(
            tmp_path / "global", code_reviewer="GLOBAL SPEC BODY\n"
        )

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=True)

        assert resolved.text == "GLOBAL SPEC BODY\n"
        assert resolved.status.source == "global"
        assert resolved.status.empty is False
        assert resolved.status.empty_repo_file is False

    def test_repo_absent_with_gate_disabled_ignores_a_usable_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(
            tmp_path / "global", code_reviewer="GLOBAL SPEC BODY\n"
        )

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=False)

        assert resolved.text == ""
        assert resolved.status.source == "none"
        assert resolved.status.empty is True
        assert resolved.status.empty_repo_file is False

    def test_repo_and_global_both_absent(self, tmp_path: Path) -> None:
        resolved = _resolve_agent_spec(
            tmp_path / "wt", _ROLE, global_fallback_enabled=True
        )

        assert resolved.text == ""
        assert resolved.status.source == "none"
        assert resolved.status.empty is True
        assert resolved.status.empty_repo_file is False

    def test_empty_repo_file_recovers_via_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _write_repo_spec(worktree, "   \n")
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(
            tmp_path / "global", code_reviewer="GLOBAL SPEC BODY\n"
        )

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=True)

        assert resolved.text == "GLOBAL SPEC BODY\n"
        assert resolved.status.source == "global"
        assert resolved.status.empty is False
        assert resolved.status.empty_repo_file is True

    def test_empty_repo_file_with_global_absent_stays_unspecified(
        self, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt"
        _write_repo_spec(worktree, "")

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=True)

        assert resolved.text == ""
        assert resolved.status.source == "none"
        assert resolved.status.empty is True
        assert resolved.status.empty_repo_file is True

    def test_empty_repo_file_with_empty_global_reports_global_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _write_repo_spec(worktree, "\n")
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(tmp_path / "global", code_reviewer="  \n")

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=True)

        assert resolved.text == ""
        assert resolved.status.source == "global"
        assert resolved.status.empty is True
        assert resolved.status.empty_repo_file is True

    def test_empty_repo_file_with_gate_disabled_stays_repo_sourced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _write_repo_spec(worktree, "\n")
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(
            tmp_path / "global", code_reviewer="GLOBAL SPEC BODY\n"
        )

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=False)

        assert resolved.text == ""
        assert resolved.status.source == "repo"
        assert resolved.status.empty is True
        assert resolved.status.empty_repo_file is True

    def test_global_present_but_empty_with_repo_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(tmp_path / "global", code_reviewer="")

        resolved = _resolve_agent_spec(worktree, _ROLE, global_fallback_enabled=True)

        assert resolved.text == ""
        assert resolved.status.source == "global"
        assert resolved.status.empty is True
        assert resolved.status.empty_repo_file is False

    def test_genuine_absence_logs_a_warning_naming_role_and_paths(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        worktree = tmp_path / "wt"
        with caplog.at_level(logging.WARNING):
            resolved = _resolve_agent_spec(
                worktree, _ROLE, global_fallback_enabled=True
            )
        assert resolved.status.source == "none"
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings
        joined = "\n".join(warnings)
        assert _ROLE in joined
        assert str(worktree / ".claude" / "agents" / _ROLE_FILE) in joined

    def test_gate_disabled_absence_warning_names_only_the_repo_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        worktree = tmp_path / "wt"
        global_dir = tmp_path / "global"
        _patch_global_agents(monkeypatch, global_dir)
        with caplog.at_level(logging.WARNING):
            resolved = _resolve_agent_spec(
                worktree, _ROLE, global_fallback_enabled=False
            )
        assert resolved.status.source == "none"
        joined = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert str(worktree / ".claude" / "agents" / _ROLE_FILE) in joined
        assert str(global_dir / _ROLE_FILE) not in joined

    def test_empty_but_resolved_source_does_not_warn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        worktree = tmp_path / "wt"
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(tmp_path / "global", code_reviewer="")
        with caplog.at_level(logging.WARNING):
            resolved = _resolve_agent_spec(
                worktree, _ROLE, global_fallback_enabled=True
            )
        assert resolved.status.empty is True
        assert resolved.status.source == "global"
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_successful_global_fallback_does_not_warn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        worktree = tmp_path / "wt"
        _write_repo_spec(worktree, "\n")
        _patch_global_agents(monkeypatch, tmp_path / "global")
        _populate_global_agents_dir(
            tmp_path / "global", code_reviewer="GLOBAL SPEC BODY\n"
        )
        with caplog.at_level(logging.WARNING):
            resolved = _resolve_agent_spec(
                worktree, _ROLE, global_fallback_enabled=True
            )
        assert resolved.status.source == "global"
        assert resolved.status.empty_repo_file is True
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# _prepare_review_pass extraction (#1392)
# ---------------------------------------------------------------------------


class TestPrepareReviewPass:
    def test_assembles_roles_prompts_diff_and_sha(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-prepare")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-prepare",
        )

        # A python-only small-scope change selects code/sysadmin + data-safety
        # (python mutates persisted state) and no product-manager (no ticket ctx).
        assert prepared.roles == _select_reviewer_roles(
            "small",
            categories=_categorize_changed_files(["mod.py"]),
            mutates_persisted_state=True,
            has_ticket_context=False,
        )
        # Every selected role has a materialized prompt.
        assert set(prepared.prompts_by_role) == set(prepared.roles)
        assert all(prepared.prompts_by_role[r] for r in prepared.roles)
        # diff + reviewed_sha reflect the captured worktree state.
        assert "mod.py" in prepared.diff.files
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        assert prepared.reviewed_sha == head

    def _repo_with_change(
        self, make_git_repo: Callable[[str], Path], name: str, tracker: str | None
    ) -> Path:
        """A feature-branch repo with one python change and a tracker config."""
        repo = make_git_repo(name)
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")
        if tracker is not None:
            _write(
                repo / ".claude" / "project-config.yaml",
                f"tracking:\n  primary:\n    system: {tracker}\n",
            )
        return repo

    def test_prepare_review_pass_includes_live_operator_comment_in_prompt(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1730: the codex backend live-fetches ticket comments and inlines
        them — before this, only .cw/context.json's title/body ever reached it."""
        repo = self._repo_with_change(
            make_git_repo, "wt-1730-comments", "github-issues"
        )
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {
                    "author": {"login": "mattwwarren"},
                    "createdAt": "2026-08-10T00:00:00Z",
                    "body": "SENDBACK-MARKER-1730",
                }
            ],
        )

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-1730-comments",
        )

        assert prepared.roles
        for role in prepared.roles:
            assert "SENDBACK-MARKER-1730" in prepared.prompts_by_role[role]

    def test_prepare_review_pass_fetches_comments_only_once(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1814 SHOULD_FIX regression guard: one gh call, not two.

        Before the fix, `_load_operator_comments` and `_load_voided_findings`
        each called `fetch_issue_comments` independently, so every Stage-3
        pass shelled out to `gh issue view` twice for the identical comment
        list. `_fetch_ticket_comments` now fetches once and both readers reuse
        the result — this pins that call count so a future edit that drops
        the `comments=` passthrough (reintroducing the double-fetch) fails
        here instead of silently doubling the gh subprocess/API cost again.
        """
        repo = self._repo_with_change(
            make_git_repo, "wt-1814-fetch-once", "github-issues"
        )
        calls: list[str] = []

        def _counting_fetch(ticket_id: str, **_kw: object) -> list[dict[str, object]]:
            calls.append(ticket_id)
            return [{"author": {"login": "a"}, "body": "some comment"}]

        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", _counting_fetch
        )

        _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-fetch-once"
        )

        assert len(calls) == 1

    def test_prepare_review_pass_omits_comments_when_tracker_not_github(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: a non-github tracker has no fetch op here, so no
        comment section is rendered and no gh call is attempted."""
        repo = self._repo_with_change(make_git_repo, "wt-1730-linear", "linear")

        def _fail_if_called(*_a: object, **_kw: object) -> None:
            msg = "fetch_issue_comments must not run for a non-github tracker"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", _fail_if_called
        )

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-1730-linear",
        )

        for role in prepared.roles:
            assert "## Ticket Comments" not in prepared.prompts_by_role[role]

    def test_prepare_review_pass_renders_pending_operator_comment_banner(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1730: queue_metadata.pending_operator_comment=true elevates the
        comments to a binding-adjudication banner."""
        repo = self._repo_with_change(make_git_repo, "wt-1730-banner", "github-issues")
        _write_real_hook_context(repo, pending=True)
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [{"body": "SENDBACK-MARKER-1730"}],
        )

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-1730-banner",
        )

        assert prepared.roles
        for role in prepared.roles:
            assert "Pending Operator Send-Back" in prepared.prompts_by_role[role]

    def test_prepare_review_pass_omits_banner_when_marker_absent_or_false(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: comments still render, but without the banner."""
        repo = self._repo_with_change(
            make_git_repo, "wt-1730-no-banner", "github-issues"
        )
        _write_real_hook_context(repo, pending=False)
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [{"body": "SENDBACK-MARKER-1730"}],
        )

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-1730-no-banner",
        )

        assert prepared.roles
        for role in prepared.roles:
            prompt = prepared.prompts_by_role[role]
            assert "SENDBACK-MARKER-1730" in prompt
            assert "Pending Operator Send-Back" not in prompt

    def test_capable_probe_threads_into_every_role_prompt(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """A runtime whose probe returns the sentinel gets the capable prompt
        variant on EVERY selected role, and the verdict-bound capability on the
        prepared inputs (#1709)."""
        repo = make_git_repo("wt-prepare-capable")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(stdout=f"codex\n{_PROBE_SENTINEL}\n"),
            session_id="s-prepare-capable",
        )

        assert prepared.capability.capable is True
        assert prepared.roles
        for role in prepared.roles:
            assert _CAPABLE_ONLY_MARKER in prepared.prompts_by_role[role]
            assert _INLINED_ONLY_MARKER not in prepared.prompts_by_role[role]

    def test_incapable_probe_threads_the_inlined_only_variant(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-prepare-incapable")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(stdout="NO_FILESYSTEM_ACCESS\n"),
            session_id="s-prepare-incapable",
        )

        assert prepared.capability.capable is False
        for role in prepared.roles:
            assert _INLINED_ONLY_MARKER in prepared.prompts_by_role[role]

    def test_lint_grounding_included_when_pyproject_and_claude_md_present(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-prepare-lint-grounding")
        _write(
            repo / "pyproject.toml",
            '[tool.ruff.lint]\nignore = ["ZZZ9999_DISTINCTIVE_IGNORE"]\n',
        )
        _write(
            repo / "CLAUDE.md",
            "## Quality Gates\nDISTINCTIVE_QUALITY_GATE_MARKER_TEXT\n"
            "## Module Size\nother\n",
        )
        _git(repo, "add", "pyproject.toml", "CLAUDE.md")
        _git(repo, "commit", "-m", "add lint config")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-prepare-lint-grounding",
        )

        assert prepared.roles
        for role in prepared.roles:
            prompt = prepared.prompts_by_role[role]
            assert "ZZZ9999_DISTINCTIVE_IGNORE" in prompt
            assert "DISTINCTIVE_QUALITY_GATE_MARKER_TEXT" in prompt

    def test_lint_grounding_absent_when_repo_files_missing(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-prepare-no-lint-grounding")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-prepare-no-lint-grounding",
        )

        assert prepared.roles
        for role in prepared.roles:
            assert "## Repo Lint Configuration" not in prepared.prompts_by_role[role]

    def test_agent_spec_status_threaded_for_every_selected_role(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1773: every selected role carries a resolved spec status, and a
        repo whose ``.claude/agents/`` copy exists reports ``source="repo"``."""
        repo = make_git_repo("wt-prepare-agent-spec")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-prepare-agent-spec-repo",
        )
        for role in prepared.roles:
            _write(
                repo / ".claude" / "agents" / _REVIEWER_ROLE_AGENT_FILES[role],
                f"SPEC FOR {role}\n",
            )
        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-prepare-agent-spec-repo",
        )

        assert prepared.roles
        assert [s.role for s in prepared.agent_spec_status] == prepared.roles
        assert all(s.source == "repo" for s in prepared.agent_spec_status)
        assert all(s.empty is False for s in prepared.agent_spec_status)
        for role in prepared.roles:
            assert f"SPEC FOR {role}" in prepared.prompts_by_role[role]

    def test_no_agents_dir_at_all_fails_open_and_marks_every_role_unspecified(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """#1773 fail-open: an absent ``.claude/agents/`` (with the global
        fallback isolated to an empty dir) still produces prompts — it is
        diagnosed, not fatal."""
        repo = make_git_repo("wt-prepare-agent-spec-none")
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-prepare-agent-spec-none",
        )

        assert prepared.roles
        assert set(prepared.prompts_by_role) == set(prepared.roles)
        assert [s.role for s in prepared.agent_spec_status] == prepared.roles
        assert all(s.source == "none" for s in prepared.agent_spec_status)
        assert all(s.empty is True for s in prepared.agent_spec_status)
        assert all(s.empty_repo_file is False for s in prepared.agent_spec_status)


# ---------------------------------------------------------------------------
# Delta-mode review passes (#1837)
# ---------------------------------------------------------------------------


class TestDeltaModeReviewPass:
    """`delta_from_sha`/`prior_open_findings` wiring through the whole pass."""

    @staticmethod
    def _rev(repo: Path) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()

    def _two_commit_repo(
        self, make_git_repo: Callable[[str], Path], name: str
    ) -> tuple[Path, str]:
        """A repo whose first feature commit is python and second is markdown."""
        repo = make_git_repo(name)
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")
        first = self._rev(repo)
        (repo / "notes.md").write_text("# notes\n", encoding="utf-8")
        _git(repo, "add", "notes.md")
        _git(repo, "commit", "-m", "add notes.md")
        return repo, first

    def test_cycle_zero_default_is_unchanged(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Regression lock: omitting the new params reproduces today's output."""
        repo, _ = self._two_commit_repo(make_git_repo, "wt-delta-default")

        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-d0"
        )

        assert prepared.delta_diff is None
        assert prepared.delta_changed_files is None
        assert "mod.py" in prepared.diff.files
        assert "notes.md" in prepared.diff.files
        for prompt in prepared.prompts_by_role.values():
            assert "## Unresolved Prior Findings" not in prompt
            assert _DELTA_MODE_MARKER not in prompt

    def test_delta_mode_selects_roles_from_the_delta_only(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, first = self._two_commit_repo(make_git_repo, "wt-delta-roles")

        full = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-d1"
        )
        delta = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-d1",
            delta_from_sha=first,
        )

        # The delta touches only markdown, so the python-driven roles drop out.
        assert delta.roles == _select_reviewer_roles(
            "small",
            categories=_categorize_changed_files(["notes.md"]),
            mutates_persisted_state=False,
            has_ticket_context=False,
        )
        assert set(delta.roles) < set(full.roles)
        assert delta.delta_diff is not None
        assert set(delta.delta_diff.files) == {"notes.md"}
        assert delta.delta_changed_files == frozenset({"notes.md"})
        # `diff` is the SAME object as `delta_diff` in delta mode -- no second
        # full-branch diff is captured, since the fix loop's scope-violation
        # gate reads its own cycle-0-captured file set, not this field
        # (#1837 Performance SHOULD_FIX).
        assert delta.diff is delta.delta_diff
        assert "mod.py" not in delta.diff.files
        assert delta.reviewed_sha == self._rev(repo)
        # Prompts are built against the delta, not the full PR diff.
        for prompt in delta.prompts_by_role.values():
            assert "def broken():" not in prompt
            assert _DELTA_MODE_MARKER in prompt

    def test_prior_open_findings_reach_the_prompt(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, first = self._two_commit_repo(make_git_repo, "wt-delta-prior")

        prepared = _prepare_review_pass(
            _task(),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-d2",
            delta_from_sha=first,
            prior_open_findings=[_make_finding(summary="STILL BROKEN HERE")],
        )

        assert prepared.roles
        prompts = list(prepared.prompts_by_role.values())
        assert all("## Unresolved Prior Findings" in p for p in prompts)
        assert all("STILL BROKEN HERE" in p for p in prompts)


class TestBuildReviewerPromptDeltaKwargs:
    def _prompt(self, **overrides: object) -> str:
        kwargs: dict[str, object] = {
            "agent_spec_text": "SPEC",
            "diff": _make_diff(),
            "changed_files": ["src/cw/foo.py"],
            "plan_text": None,
            "ticket_text": None,
            "project_rubrics": None,
            "repo_policy_section": None,
            "sensitive_hits": [],
        }
        kwargs.update(overrides)
        return _build_reviewer_prompt("Code Quality Reviewer", **kwargs)  # type: ignore[arg-type]

    def test_prior_open_findings_section_lists_file_summary_evidence(self) -> None:
        prompt = self._prompt(
            prior_open_findings=[
                _make_finding(
                    file="src/cw/bar.py",
                    summary="Still unfixed",
                    evidence="def broken():",
                )
            ]
        )
        assert "## Unresolved Prior Findings" in prompt
        assert "src/cw/bar.py" in prompt
        assert "Still unfixed" in prompt
        assert "def broken():" in prompt

    def test_prior_open_findings_omitted_by_default(self) -> None:
        assert "## Unresolved Prior Findings" not in self._prompt()

    def test_delta_mode_block_carries_its_own_marker(self) -> None:
        prompt = self._prompt(delta_mode=True)
        assert _DELTA_MODE_MARKER in prompt
        assert "DEBT" in prompt
        assert "transitive_impact_evidence" in prompt
        assert "release_critical_exception" in prompt

    def test_delta_mode_block_absent_by_default(self) -> None:
        prompt = self._prompt()
        assert _DELTA_MODE_MARKER not in prompt
        assert "transitive_impact_evidence" not in prompt


# ---------------------------------------------------------------------------
# _render_adjudicated_findings_block / prompt injection (#1838, R4a + R5)
# ---------------------------------------------------------------------------


def _disposition_ledger(
    file: str = "src/cw/foo.py",
    summary: str = "Bug here",
    **overrides: object,
) -> dict[str, FindingDisposition]:
    key = _disposition_key(file, summary)
    assert key is not None
    payload: dict[str, object] = {
        "outcome": "REJECTED",
        "rationale": "intentional tradeoff, settled in an earlier round",
        "recorded_at": "2026-08-16T00:00:00Z",
    }
    payload.update(overrides)
    return {key: FindingDisposition.model_validate(payload)}


class TestLoadFindingDispositions:
    """#1838: the disposition-marker read, degrading to {} throughout.

    Same tracker gate, same fetch op, and the same never-raise contract as
    :class:`TestLoadVoidedFindings` — a Stage-3 pass that cannot reach the
    tracker must still review; it just cannot see an adjudication posted since
    the last pass persisted one onto the queue row.
    """

    def _github_repo(self, tmp_path: Path) -> Path:
        _write(
            tmp_path / ".claude" / "project-config.yaml",
            "tracking:\n  primary:\n    system: github-issues\n",
        )
        return tmp_path

    def test_returns_empty_when_tracker_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail_if_called(*_a: object, **_kw: object) -> None:
            msg = "must not fetch when the tracker is unknown"
            raise AssertionError(msg)

        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", _fail_if_called
        )
        assert _load_finding_dispositions(tmp_path, "T-1") == {}

    def test_returns_empty_on_fetch_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: None
        )
        assert _load_finding_dispositions(self._github_repo(tmp_path), "T-1") == {}

    def test_returns_empty_when_no_comment_carries_a_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [{"author": {"login": "a"}, "body": "just prose"}],
        )
        assert _load_finding_dispositions(self._github_repo(tmp_path), "T-1") == {}

    def test_fetches_fresh_and_parses_the_sentinel_out_of_the_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger = _disposition_ledger()
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {"author": {"login": "a"}, "body": "unrelated send-back"},
                {"author": None, "body": None},
                {
                    "author": {"login": "b"},
                    "body": render_finding_disposition_block(ledger),
                },
            ],
        )
        assert _load_finding_dispositions(self._github_repo(tmp_path), "T-1") == ledger


class TestRenderAdjudicatedFindingsBlock:
    """#1838 R5: the gated-on-non-empty render helper for the new ledger."""

    def test_empty_ledger_renders_nothing(self) -> None:
        assert _render_adjudicated_findings_block({}) is None

    def test_renders_one_line_per_entry_with_file_summary_outcome_rationale(
        self,
    ) -> None:
        block = _render_adjudicated_findings_block(_disposition_ledger())
        assert block is not None
        assert "src/cw/foo.py" in block
        assert "bug here" in block
        assert "REJECTED" in block
        assert "intentional tradeoff, settled in an earlier round" in block
        assert block.count("\n- ") == 1

    def test_accepted_entries_render_too(self) -> None:
        block = _render_adjudicated_findings_block(
            _disposition_ledger(outcome="ACCEPTED")
        )
        assert block is not None
        assert "ACCEPTED" in block


class TestBuildReviewerPromptAdjudicatedFindings:
    def _prompt(self, **overrides: object) -> str:
        kwargs: dict[str, object] = {
            "agent_spec_text": "SPEC",
            "diff": _make_diff(),
            "changed_files": ["src/cw/foo.py"],
            "plan_text": None,
            "ticket_text": None,
            "project_rubrics": None,
            "repo_policy_section": None,
            "sensitive_hits": [],
        }
        kwargs.update(overrides)
        return _build_reviewer_prompt("Code Quality Reviewer", **kwargs)  # type: ignore[arg-type]

    def test_block_is_injected_when_the_ledger_is_non_empty(self) -> None:
        prompt = self._prompt(adjudicated_findings=_disposition_ledger())
        assert "## Previously Adjudicated Findings" in prompt
        assert "do not re-raise" in prompt

    def test_prompt_is_byte_identical_when_the_kwarg_is_omitted_or_empty(self) -> None:
        baseline = self._prompt()
        assert self._prompt(adjudicated_findings=None) == baseline
        assert self._prompt(adjudicated_findings={}) == baseline
        assert "## Previously Adjudicated Findings" not in baseline


class TestPrepareReviewPassFindingDispositions:
    """#1838: the ledger is merged, prompted, and carried on the pass inputs."""

    def _repo(self, make_git_repo: Callable[[str], Path], name: str) -> Path:
        repo = make_git_repo(name)
        _git(repo, "checkout", "-b", "feature")
        (repo / "mod.py").write_text("def broken():\n    pass\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", "add mod.py")
        _write(
            repo / ".claude" / "project-config.yaml",
            "tracking:\n  primary:\n    system: github-issues\n",
        )
        return repo

    def test_stored_ledger_alone_reaches_the_prompt_and_the_inputs(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo(make_git_repo, "wt-1838-stored")
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: []
        )
        ledger = _disposition_ledger()
        prepared = _prepare_review_pass(
            _make_ticket_task(
                ticket_id="T-1", client="test", finding_dispositions=ledger
            ),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-1838-stored",
        )

        assert prepared.finding_dispositions == ledger
        assert prepared.roles
        for role in prepared.roles:
            assert (
                "## Previously Adjudicated Findings" in prepared.prompts_by_role[role]
            )

    def test_marker_entries_are_merged_with_the_stored_ledger(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo(make_git_repo, "wt-1838-merge")
        stored = _disposition_ledger(file="src/cw/stored.py", summary="Stored bug")
        fresh = _disposition_ledger(file="src/cw/fresh.py", summary="Fresh bug")
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {
                    "author": {"login": "op"},
                    "body": render_finding_disposition_block(fresh),
                }
            ],
        )
        prepared = _prepare_review_pass(
            _make_ticket_task(
                ticket_id="T-1", client="test", finding_dispositions=stored
            ),
            repo,
            "main",
            runner=FakeCodexRunner(),
            session_id="s-1838-merge",
        )

        assert prepared.finding_dispositions == {**stored, **fresh}

    def test_no_marker_and_no_stored_ledger_leaves_the_prompt_untouched(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo(make_git_repo, "wt-1838-absent")
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments", lambda *_a, **_kw: []
        )
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-1838-absent"
        )

        assert prepared.finding_dispositions == {}
        for role in prepared.roles:
            assert (
                "## Previously Adjudicated Findings"
                not in prepared.prompts_by_role[role]
            )

    def test_parsed_marker_entries_are_persisted_onto_the_running_row(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The merged ledger survives this session: R1's durability half."""
        repo = self._repo(make_git_repo, "wt-1838-persist")
        fresh = _disposition_ledger()
        monkeypatch.setattr(
            "cw.codex_review._context.fetch_issue_comments",
            lambda *_a, **_kw: [
                {
                    "author": {"login": "op"},
                    "body": render_finding_disposition_block(fresh),
                }
            ],
        )
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cw.codex_background._sync_finding_dispositions_to_running_task",
            lambda **kwargs: calls.append(kwargs),
        )
        task = _make_ticket_task(ticket_id="T-1", client="test")
        _prepare_review_pass(
            task, repo, "main", runner=FakeCodexRunner(), session_id="s-1838-persist"
        )

        assert calls == [
            {
                "client_name": "test",
                "ticket_id": "T-1",
                "dispositions": fresh,
            }
        ]
