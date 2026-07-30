"""Guard tests for `.github/workflows/pr-events.yml`'s `review_requested` relay (#1169).

#1154 landed the server-side half of RFC 0011 S2 (`_VALID_EVENT_TYPES` and
`_handle_review_requested_sync` in `cw_pr_events_server.py`). This file covers the
producer side: the workflow must declare the `review_requested` trigger and must not
fall into the pre-existing `pull_request)` merged-gate (every `review_requested`
delivery has `merged=false`, which would otherwise be silently skipped as "closed
without merge").

Group A mirrors `test_plan_format_only_findings.py`'s `read_text()` +
literal-substring convention for prose/YAML-shape assertions. Group B actually
executes the resolve step's shell script via `subprocess.run(["bash", "-c", ...])`
with synthetic env vars -- the only way to falsify the "bare login string" trap,
since Group A can confirm shape but not runtime behavior.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pr-events.yml"
MERGED_GATE = 'if [ "$PR_MERGED" != "true" ]'


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _on_block() -> dict:
    # PyYAML's SafeLoader follows YAML 1.1, which parses the bare `on` scalar
    # key as the boolean `True` rather than the string "on" -- a well-known
    # GitHub Actions YAML gotcha.
    return _workflow()[True]


def _resolve_step() -> dict:
    workflow = _workflow()
    steps = workflow["jobs"]["push-pr-event"]["steps"]
    return next(step for step in steps if step.get("id") == "resolve")


def _resolve_script() -> str:
    return _resolve_step()["run"]


def _pull_request_arm_text() -> str:
    """Raw text of the `pull_request)` case arm, up to the next sibling arm."""
    raw = _resolve_script()
    start = raw.index("pull_request)")
    end = raw.index("pull_request_review)", start)
    return raw[start:end]


def _run_resolve_step(env_overrides: dict[str, str]) -> dict[str, str]:
    """Execute the resolve step's shell script with synthetic env, parse $GITHUB_OUTPUT."""
    script = _resolve_script()
    base_env = {
        "EVENT_NAME": "",
        "PR_MERGED": "",
        "PR_NUMBER_FROM_PR": "",
        "REVIEW_STATE": "",
        "WORKFLOW_CONCLUSION": "",
        "WORKFLOW_HEAD_BRANCH": "",
        "REPO": "",
        "ACTION": "",
        "REQUESTED_REVIEWER_LOGIN": "",
        "REQUESTED_TEAM_SLUG": "",
        "REQUESTER_LOGIN": "",
        "GH_TOKEN": "",
        "PATH": "/usr/bin:/bin",
    }
    env = {**base_env, **env_overrides}
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".env") as handle:
        output_path = Path(handle.name)
    try:
        env["GITHUB_OUTPUT"] = str(output_path)
        result = subprocess.run(  # noqa: S603
            ["/bin/bash", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs: dict[str, str] = {}
        for line in output_path.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return outputs
    finally:
        output_path.unlink(missing_ok=True)


# --- Group A: prose/YAML-level structural assertions ---


def test_on_block_declares_review_requested_trigger() -> None:
    types = _on_block()["pull_request"]["types"]
    assert "review_requested" in types
    assert "closed" in types


def test_pull_request_case_dispatches_on_action() -> None:
    arm = _pull_request_arm_text()
    first_case_index = arm.index('case "$ACTION"')
    merged_gate_index = arm.index(MERGED_GATE)
    assert first_case_index < merged_gate_index


def test_closed_action_still_gates_on_merged() -> None:
    arm = _pull_request_arm_text()
    closed_index = arm.index("closed)")
    merged_gate_index = arm.index(MERGED_GATE)
    review_requested_index = arm.index("review_requested)")
    assert closed_index < merged_gate_index < review_requested_index


def test_review_requested_sets_event_type_literal() -> None:
    assert "EVENT_TYPE=review_requested" in _resolve_script()


def test_env_maps_requested_reviewer_and_team() -> None:
    env = _resolve_step()["env"]
    assert env["ACTION"] == "${{ github.event.action }}"
    assert env["REQUESTED_REVIEWER_LOGIN"] == "${{ github.event.requested_reviewer.login }}"
    assert env["REQUESTED_TEAM_SLUG"] == "${{ github.event.requested_team.slug }}"
    assert env["REQUESTER_LOGIN"] == "${{ github.event.sender.login }}"


def test_no_second_bare_if_alongside_existing_guard() -> None:
    assert _resolve_script().count(MERGED_GATE) == 1


# --- Group B: literal shell/jq exercise ---


def test_individual_reviewer_produces_object_payload() -> None:
    outputs = _run_resolve_step(
        {
            "EVENT_NAME": "pull_request",
            "ACTION": "review_requested",
            "PR_NUMBER_FROM_PR": "42",
            "REQUESTED_REVIEWER_LOGIN": "octocat",
            "REQUESTER_LOGIN": "requester-login",
        }
    )
    assert outputs["event_type"] == "review_requested"
    assert outputs["pr_number"] == "42"
    payload = json.loads(outputs["payload"])
    assert payload["reviewer"] == {"login": "octocat"}
    assert payload["requester_login"] == "requester-login"


def test_team_reviewer_produces_object_payload() -> None:
    outputs = _run_resolve_step(
        {
            "EVENT_NAME": "pull_request",
            "ACTION": "review_requested",
            "PR_NUMBER_FROM_PR": "42",
            "REQUESTED_TEAM_SLUG": "eng-team",
            "REQUESTER_LOGIN": "requester-login",
        }
    )
    payload = json.loads(outputs["payload"])
    assert payload["reviewer"] == {"slug": "eng-team"}


def test_malformed_review_requested_event_skips() -> None:
    outputs = _run_resolve_step(
        {
            "EVENT_NAME": "pull_request",
            "ACTION": "review_requested",
            "PR_NUMBER_FROM_PR": "42",
        }
    )
    assert outputs.get("skip") == "true"


def test_closed_merged_and_closed_unmerged_still_work() -> None:
    merged = _run_resolve_step(
        {
            "EVENT_NAME": "pull_request",
            "ACTION": "closed",
            "PR_MERGED": "true",
            "PR_NUMBER_FROM_PR": "7",
        }
    )
    assert merged["event_type"] == "merged"
    assert merged["pr_number"] == "7"
    assert json.loads(merged["payload"]) == {}

    unmerged = _run_resolve_step(
        {
            "EVENT_NAME": "pull_request",
            "ACTION": "closed",
            "PR_MERGED": "false",
            "PR_NUMBER_FROM_PR": "7",
        }
    )
    assert unmerged.get("skip") == "true"
