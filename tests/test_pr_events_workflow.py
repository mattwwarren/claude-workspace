"""Guard tests for `.github/workflows/pr-events.yml`'s `review_requested` relay (#1169).

#1154 landed the server-side half of RFC 0011 S2 (`PREventType` and
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
from typing import Any

import pytest
import yaml

from tests._tool_fakes import shim_env, stub_gh

ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pr-events.yml"
MERGED_GATE = 'if [ "$PR_MERGED" != "true" ]'


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW_PATH.read_text())
    return workflow


def _on_block() -> dict[str, Any]:
    # PyYAML's SafeLoader follows YAML 1.1, which parses the bare `on` scalar
    # key as the boolean `True` rather than the string "on" -- a well-known
    # GitHub Actions YAML gotcha.
    on_block: dict[str, Any] = _workflow()[True]
    return on_block


def _resolve_step() -> dict[str, Any]:
    workflow = _workflow()
    steps = workflow["jobs"]["push-pr-event"]["steps"]
    step: dict[str, Any] = next(s for s in steps if s.get("id") == "resolve")
    return step


def _resolve_script() -> str:
    script: str = _resolve_step()["run"]
    return script


def _pull_request_arm_text() -> str:
    """Raw text of the `pull_request)` case arm, up to the next sibling arm."""
    raw = _resolve_script()
    start = raw.index("pull_request)")
    end = raw.index("pull_request_review)", start)
    return raw[start:end]


def _run_resolve_step(env_overrides: dict[str, str]) -> dict[str, str]:
    """Run the resolve step's script with synthetic env, parse $GITHUB_OUTPUT."""
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
        result = subprocess.run(
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
    assert (
        env["REQUESTED_REVIEWER_LOGIN"]
        == "${{ github.event.requested_reviewer.login }}"
    )
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


# ---------------------------------------------------------------------------
# PR channel v1 (#3173): ci_failed payload, Post-step body, triggers unchanged.
# ---------------------------------------------------------------------------

_V1_FIXTURES = Path(__file__).parent / "fixtures" / "pr_channel"


def _v1_fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_V1_FIXTURES / name).read_text())
    return loaded


def _post_step_script() -> str:
    raw = yaml.safe_load(WORKFLOW_PATH.read_text())
    step = next(
        s
        for s in raw["jobs"]["push-pr-event"]["steps"]
        if s.get("name") == "Post event to relay"
    )
    script: str = step["run"]
    return script


def _run_post_step(
    env_overrides: dict[str, str], record: Path
) -> subprocess.CompletedProcess[str]:
    """Run the Post step's ``run:`` text with an explicit env; curl is a fake."""
    base_env = {
        "RELAY_URL": "http://relay.invalid/pr-event",
        "HMAC_SECRET": "",
        "REPO": "acme/widgets",
        "PR_NUMBER": "42",
        "EVENT_TYPE": "merged",
        "PAYLOAD": "{}",
        "CURL_RECORD": str(record),
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", _post_step_script()],
        env={**base_env, **env_overrides},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _curl_stub(tmp_path: Path) -> Path:
    from tests._tool_fakes import write_bin_stub

    body = '#!/bin/bash\nprintf "%s\\0" "$@" > "$CURL_RECORD"\n'
    return write_bin_stub(tmp_path, "curl", body)


def _recorded_args(record: Path) -> list[str]:
    return record.read_bytes().decode().split("\0")[:-1]


def _body_from_args(args: list[str]) -> dict[str, Any]:
    idx = args.index("-d")
    loaded: dict[str, Any] = json.loads(args[idx + 1])
    return loaded


class TestPRChannelWorkflowV1:
    def test_triggers_unchanged(self) -> None:
        raw = yaml.safe_load(WORKFLOW_PATH.read_text())
        triggers = raw[True]
        assert triggers == {
            "pull_request": {"types": ["closed", "review_requested"]},
            "pull_request_review": {"types": ["submitted"]},
            "workflow_run": {"workflows": ["CI"], "types": ["completed"]},
        }

    def test_header_points_at_pr_channel_doc(self) -> None:
        assert "docs/pr-channel.md" in WORKFLOW_PATH.read_text()

    def test_ci_failed_payload_carries_conclusion(self, tmp_path: Path) -> None:
        bin_dir = stub_gh(tmp_path, exit_code=0, stdout="42\n")
        out = _run_resolve_step(
            {
                "EVENT_NAME": "workflow_run",
                "WORKFLOW_CONCLUSION": "failure",
                "WORKFLOW_HEAD_BRANCH": "dev/1",
                "REPO": "acme/widgets",
                **shim_env(bin_dir),
            }
        )
        assert out["event_type"] == "ci_failed"
        assert json.loads(out["payload"]) == {"workflow_conclusion": "failure"}

    def test_ci_cancelled_reflected_honestly(self, tmp_path: Path) -> None:
        bin_dir = stub_gh(tmp_path, exit_code=0, stdout="42\n")
        out = _run_resolve_step(
            {
                "EVENT_NAME": "workflow_run",
                "WORKFLOW_CONCLUSION": "cancelled",
                "WORKFLOW_HEAD_BRANCH": "dev/1",
                "REPO": "acme/widgets",
                **shim_env(bin_dir),
            }
        )
        assert json.loads(out["payload"]) == {"workflow_conclusion": "cancelled"}

    def test_ci_success_skips(self, tmp_path: Path) -> None:
        bin_dir = stub_gh(tmp_path, exit_code=0, stdout="42\n")
        out = _run_resolve_step(
            {
                "EVENT_NAME": "workflow_run",
                "WORKFLOW_CONCLUSION": "success",
                "WORKFLOW_HEAD_BRANCH": "dev/1",
                "REPO": "acme/widgets",
                **shim_env(bin_dir),
            }
        )
        assert out.get("skip") == "true"

    @pytest.mark.parametrize(
        "name",
        [
            "workflow_body_merged.json",
            "workflow_body_ci_failed.json",
            "workflow_body_review_received.json",
            "workflow_body_review_requested.json",
        ],
    )
    def test_post_body_matches_golden(self, name: str, tmp_path: Path) -> None:
        golden = _v1_fixture(name)
        record = tmp_path / "curl-args.bin"
        _run_post_step(
            {
                "EVENT_TYPE": golden["event_type"],
                "PAYLOAD": json.dumps(golden["payload"]),
                "PR_NUMBER": str(golden["pr_number"]),
                "REPO": golden["repo"],
                **shim_env(_curl_stub(tmp_path)),
            },
            record,
        )
        args = _recorded_args(record)
        assert _body_from_args(args) == golden
        assert "X-Cw-Signature: sha256=" not in " ".join(args)

    def test_post_signs_body_when_secret_set(self, tmp_path: Path) -> None:
        import hashlib
        import hmac

        golden = _v1_fixture("workflow_body_merged.json")
        record = tmp_path / "curl-args.bin"
        _run_post_step(
            {
                "HMAC_SECRET": "s3cret",
                "EVENT_TYPE": "merged",
                "PAYLOAD": "{}",
                **shim_env(_curl_stub(tmp_path)),
            },
            record,
        )
        args = _recorded_args(record)
        body = args[args.index("-d") + 1]
        expected = hmac.new(b"s3cret", body.encode(), hashlib.sha256).hexdigest()
        assert f"X-Cw-Signature: sha256={expected}" in args
        assert _body_from_args(args) == golden
