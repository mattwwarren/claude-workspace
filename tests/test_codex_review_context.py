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
    _RuffLintConfig,
    _select_reviewer_roles,
)
from cw.codex_review._capability import _PROBE_SENTINEL
from cw.codex_review._context import (
    _CAPABLE_ONLY_MARKER,
    _INLINED_ONLY_MARKER,
    _OUTPUT_INSTRUCTIONS_CAPABLE,
    _OUTPUT_INSTRUCTIONS_INLINED_ONLY,
    _select_output_instructions,
)
from cw.codex_runner import FakeCodexRunner
from tests._codex_review_helpers import (
    _doc_json,
    _finding_payload,
    _git,
    _task,
    _write,
)
from tests.conftest import _make_diff

if TYPE_CHECKING:
    from collections.abc import Callable


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
