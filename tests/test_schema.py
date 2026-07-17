"""Tests for cw.schema — schema inspection commands."""

from __future__ import annotations

import json

from click.testing import CliRunner

from cw.cli import main


class TestSchemaList:
    def test_list_contains_all_schemas(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "list"])
        assert result.exit_code == 0
        assert "auto-dev-result" in result.output
        assert "ticket-task" in result.output
        assert "session" in result.output
        assert "review-verdict" in result.output

    def test_list_json_is_valid_array(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "list", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert "auto-dev-result" in parsed
        assert "ticket-task" in parsed
        assert "session" in parsed
        assert "review-verdict" in parsed
        assert len(parsed) == 4


class TestSchemaShow:
    def test_json_format_parses(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schema", "show", "auto-dev-result", "--format=json"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "properties" in parsed
        assert "required" in parsed

    def test_json_format_no_envelope(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schema", "show", "auto-dev-result", "--format=json"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "cw_version" not in parsed
        assert "generated_at" not in parsed

    def test_tldr_includes_status_enums(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schema", "show", "auto-dev-result", "--format=tldr"]
        )
        assert result.exit_code == 0
        assert "shipped" in result.output
        assert "blocked" in result.output

    def test_tldr_tier_is_conditional(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schema", "show", "auto-dev-result", "--format=tldr"]
        )
        assert result.exit_code == 0
        # tier is | None, so tldr must NOT say it's unconditionally required
        # It should either show "null" in its type or show a condition annotation
        assert "stage1_plan" in result.output or "null" in result.output

    def test_tldr_ticket_task(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "show", "ticket-task", "--format=tldr"])
        assert result.exit_code == 0

    def test_tldr_session(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "show", "session", "--format=tldr"])
        assert result.exit_code == 0

    def test_unknown_schema_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "show", "unknown-schema"])
        assert result.exit_code != 0
        assert "unknown-schema" in result.output.lower()


class TestSchemaShowReviewVerdict:
    def test_json_format_parses(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schema", "show", "review-verdict", "--format=json"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "blocking" in parsed["properties"]
        assert "must_fix" in parsed["properties"]
        assert "reviewed_sha" in parsed["properties"]
        assert "stripped_escalations" in parsed["properties"]
        assert "EscalationMetadata" in parsed["$defs"]
        assert "StrippedEscalation" in parsed["$defs"]

    def test_tldr_renders(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schema", "show", "review-verdict", "--format=tldr"]
        )
        assert result.exit_code == 0
        assert "blocking" in result.output


class TestSchemaStageOutput:
    def test_valid_stage_returns_json_with_properties(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "stage-output", "impl"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "properties" in parsed

    def test_invalid_stage_errors_with_bad_value(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["schema", "stage-output", "bogus"])
        assert result.exit_code != 0
        assert "bogus" in result.output.lower()

    def test_all_stages_return_identical_schema(self) -> None:
        runner = CliRunner()
        result_impl = runner.invoke(main, ["schema", "stage-output", "impl"])
        result_plan = runner.invoke(main, ["schema", "stage-output", "plan"])
        assert result_impl.exit_code == 0
        assert result_plan.exit_code == 0
        assert result_impl.output == result_plan.output
