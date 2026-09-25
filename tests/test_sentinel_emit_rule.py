"""Doc-guard tests for the *Sentinel emit rule* (#2382).

Every headless stage command ends by recording its payload with
``cw result emit -`` and only then framing it; the former ``cw result
validate -`` pre-emit gate is gone. These tests pin the producer side of that
contract in the repo-tracked command prose, the way
``test_plan_approval_fingerprint_binding.py`` pins the fingerprint rule.
"""

from __future__ import annotations

import pytest

from tests.conftest import _REPO_ROOT, _cmd

_RULE_HEADING = "## Sentinel emit rule (#2382)"
_EMIT_LINE = "printf '%s' \"$SENTINEL_JSON\" | cw result emit -"
_VALIDATE_LINE = "printf '%s' \"$SENTINEL_JSON\" | cw result validate -"
_STAGE_DOCS = (
    "auto-dev-intake.md",
    "auto-dev-plan.md",
    "auto-dev-impl.md",
    "auto-dev-review.md",
    "auto-dev-finalize.md",
)


def _rule_section() -> str:
    text = _cmd("auto-dev.md")
    start = text.index(_RULE_HEADING)
    end = text.index("## Guard Matrix", start)
    return text[start:end]


class TestRuleDefinition:
    def test_rule_is_defined_once_in_the_monolith(self) -> None:
        assert _cmd("auto-dev.md").count(_RULE_HEADING) == 1

    def test_rule_states_the_fix_and_rerun_loop(self) -> None:
        section = _rule_section()
        assert _EMIT_LINE in section
        assert "field.path: message" in section
        assert "re-run" in section or "run the command again" in section
        assert "exits 0" in section

    def test_rule_names_emit_precedence_over_the_transcript(self) -> None:
        assert "#536" in _rule_section()

    def test_rule_keeps_the_frame_as_the_final_characters(self) -> None:
        section = _rule_section()
        assert "Recording is not framing (#1890)" in section
        assert "final characters" in section

    def test_rule_carves_out_a_denied_or_unknown_command(self) -> None:
        """A denied/unknown `cw result emit` must not become a tool_denied
        exit -- the transcript frame is the fallback, as before #2382."""
        section = _rule_section()
        assert "not** a `tool_denied` exit" in section
        assert _VALIDATE_LINE in section
        # SysAdmin review: the fallback is never silent -- it lands in the
        # sentinel so the fallback rate is visible fleet-wide.
        assert "cw_result_emit_fallback:" in section
        assert "friction_highlights" in section
        # ...and on that path the worker computes the digest itself.
        assert "Plan-draft fingerprint rule" in section

    def test_rule_explains_fingerprint_recomputation(self) -> None:
        section = _rule_section()
        assert "plan_draft_fingerprint" in section
        assert "Plan-draft fingerprint rule" in section
        assert "--plan-draft" in section


class TestStageCommandsCiteTheRule:
    @pytest.mark.parametrize("doc", _STAGE_DOCS)
    def test_stage_gate_is_emit_not_validate(self, doc: str) -> None:
        text = _cmd(doc)
        assert "Emit through cw, then frame (#2382)" in text
        assert "*Sentinel emit rule*" in text
        assert _EMIT_LINE in text
        assert _VALIDATE_LINE not in text
        assert "Validating is not emitting" not in text

    def test_monolith_appendix_gate_is_emit_not_validate(self) -> None:
        text = _cmd("auto-dev.md")
        appendix = text[text.index("## Appendix: Structured Output") :]
        assert "Emit through cw, then frame (#2382)" in appendix
        assert _EMIT_LINE in appendix
        assert _VALIDATE_LINE not in appendix
        assert "Pre-emit validation gate" not in appendix


class TestContractAndAdrRecordTheChange:
    def test_headless_contract_names_the_producer_push(self) -> None:
        text = (_REPO_ROOT / "docs" / "headless-contract.md").read_text(
            encoding="utf-8"
        )
        assert "**Producer push (#2382).**" in text
        assert "Sentinel emit rule" in text

    def test_rfc_0012_retires_its_out_of_scope_bullet(self) -> None:
        """RFC 0012 scoped worker-invoked emit OUT; the two accepted docs must
        not contradict each other, so the RFC carries its own amendment."""
        rfc = _REPO_ROOT / "docs" / "rfcs" / "0012-unified-result-publishing.md"
        text = rfc.read_text(encoding="utf-8")
        assert "## Amendment (#2382)" in text
        assert "Superseded" in text
        assert "primary" in text
        assert "fallback" in text

    def test_harvest_table_orders_the_claude_rows(self) -> None:
        text = (_REPO_ROOT / "docs" / "headless-contract.md").read_text(
            encoding="utf-8"
        )
        assert "detached Claude daemon — **primary**" in text
        assert "detached Claude daemon — **fallback**" in text

    def test_adr_0003_carries_the_amendment(self) -> None:
        adr_dir = _REPO_ROOT / "docs" / "adr"
        adr = adr_dir / "0003-stop-hook-canonical-completion-signal.md"
        text = adr.read_text(encoding="utf-8")
        assert "## Amendment (#2382)" in text
        assert "write-only" in text
