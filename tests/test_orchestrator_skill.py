"""Structural smoke tests for the cw-orchestrator subagent definition."""

from pathlib import Path

AGENT_FILE = Path(__file__).parent.parent / ".claude" / "agents" / "cw-orchestrator.md"


def test_orchestrator_agent_file_exists() -> None:
    assert AGENT_FILE.exists(), f"{AGENT_FILE} not found"


def test_orchestrator_agent_frontmatter_valid() -> None:
    content = AGENT_FILE.read_text()
    assert "name: cw-orchestrator" in content
    assert "description:" in content
    assert "tools:" in content  # agents/ convention (NOT allowed-tools:)
    assert "allowed-tools:" not in content


def test_orchestrator_agent_retains_mergeable_and_merged_rows() -> None:
    content = AGENT_FILE.read_text()
    for event in ["pr.mergeable", "pr.merged"]:
        assert event in content, f"Event '{event}' missing from decision table"


def test_orchestrator_agent_retired_ci_failed_and_review_received_rows() -> None:
    # RFC 0010 P4 (#1099): auto_fix_ci / address_review review recipes now own
    # the ci_failing / changes_requested reactions daemon-side, so the
    # orchestrator agent no longer routes pr.ci_failed / pr.review_received.
    content = AGENT_FILE.read_text()
    for event in ["pr.ci_failed", "pr.review_received"]:
        assert event not in content, f"Retired event '{event}' still in decision table"


def test_orchestrator_agent_dedup_command_documented() -> None:
    content = AGENT_FILE.read_text()
    assert "cw dev-queue status" in content, "Dedup command not documented"


def test_orchestrator_agent_listens_on_cw_operator_channel() -> None:
    # #3170: cw orchestrator-start subscribes the session to cw-operator.
    content = AGENT_FILE.read_text()
    assert '<channel source="cw-operator"' in content
    description = next(
        line for line in content.splitlines() if line.startswith("description:")
    )
    assert "cw-operator" in description


def test_orchestrator_agent_not_pr_events_channel() -> None:
    assert "cw-pr-events" not in AGENT_FILE.read_text()


def test_orchestrator_agent_uses_operator_tag_attributes() -> None:
    # cw-operator's _build_meta carries only event_type, correlation_id, client.
    content = AGENT_FILE.read_text()
    assert "event_type=... correlation_id=... client=..." in content
    assert "pr_number=" not in content
    assert "role=" not in content


def test_orchestrator_agent_documents_other_event_types() -> None:
    content = AGENT_FILE.read_text()
    for needle in [
        "Other event types",
        "session.needs_attention",
        "operator.escalation",
        "ignore and continue",
    ]:
        assert needle in content, f"{needle!r} missing from the agent file"
