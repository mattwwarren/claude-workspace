"""Tests for ``cw.opencode_clean_review``: the opencode clean REVIEW prompt (#2806).

The prompt is prose a weaker agent follows, so these tests pin it against the
real surfaces it drives: every ``cw review converge <sub>`` / ``cw review
confirm`` flag it names exists on the Click command, the ``jq`` paths it reads
match what ``converge status`` really prints, the confirm-stdout ``jq`` map
yields a payload ``record-round`` accepts, and the sentinel template validates
once its placeholders are filled.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import click
import pytest

from cw.auto_dev_result import AutoDevResult
from cw.cli import main
from cw.cli.review.confirm import review_confirm
from cw.opencode_clean_review import (
    CLEAN_REVIEW_SENTINEL_TEMPLATE,
    CONFIRM_RECORD_JQ,
    CONFIRM_TIMEOUT_SECONDS,
    CONFIRM_TOOL_TIMEOUT_MS,
    clean_review_body,
)
from cw.opencode_runner import build_stage_prompt
from cw.review_convergence import RoundInput, status_summary
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    confirm_verdict_payload,
    consolidated_verdict,
    fresh_ledger,
)

if TYPE_CHECKING:
    from cw.review_convergence import ConfirmBackend

TICKET = "T-1"
_SHA = "a" * 40
_LONG_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
#: A command segment ends at the closing backtick, a newline, a pipe, a
#: statement separator or a shell redirect (`` >`` / `` 2>``, never the ``>``
#: that closes a ``<placeholder>``).
_SEGMENT_END = re.compile(r"[`\n|;]|\s\d?>")


@pytest.fixture
def prompt() -> str:
    return build_stage_prompt(
        "review", TICKET, Path("/wt"), session_id="abc123", clean_review=True
    )


def _converge_group() -> click.Group:
    review = main.commands["review"]
    assert isinstance(review, click.Group)
    converge = review.commands["converge"]
    assert isinstance(converge, click.Group)
    return converge


def _options(command: click.Command) -> set[str]:
    return {
        opt for param in command.params for opt in (*param.opts, *param.secondary_opts)
    }


def _segments(prompt: str, head: str) -> list[str]:
    """The text after each ``head`` occurrence, up to the end of that command."""
    found: list[str] = []
    for match in re.finditer(re.escape(head), prompt):
        rest = prompt[match.end() :]
        end = _SEGMENT_END.search(rest)
        found.append(rest if end is None else rest[: end.start()])
    return found


class TestCliContract:
    """Every command and flag the prompt names exists on the real CLI."""

    def test_every_converge_subcommand_and_flag_exists(self, prompt: str) -> None:
        converge = _converge_group()
        seen: dict[str, set[str]] = {}
        for segment in _segments(prompt, "cw review converge "):
            sub = segment.split()[0]
            assert sub in converge.commands, sub
            flags = set(_LONG_FLAG.findall(segment))
            assert flags <= _options(converge.commands[sub]), (sub, flags)
            seen.setdefault(sub, set()).update(flags)
        assert set(seen) >= {
            "config",
            "init",
            "status",
            "diff-base",
            "record-round",
            "confirm-failed",
            "finalize",
        }
        assert "--loop-backend" in seen["config"]
        assert "--loop-backend" in seen["init"]
        assert "--defer-should-fix" in seen["record-round"]
        assert {"--cause", "--detail"} <= seen["confirm-failed"]

    def test_every_confirm_flag_exists(self, prompt: str) -> None:
        segments = _segments(prompt, "cw review confirm ")
        assert segments
        flags = set().union(*(set(_LONG_FLAG.findall(s)) for s in segments))
        assert {"--ticket-id", "--backend"} <= flags
        assert flags <= _options(review_confirm)


class TestTypedExits:
    @pytest.mark.parametrize(
        "token",
        [
            '"review_not_converging", retry_eligible false',
            '"review_blocked"',
            '"review_operator_actionable"',
            '"confirm_backend_unavailable", retry_eligible true',
            '"agent_block"',
            "CONFIRM_RC=4",
            "CONFIRM_RC=2",
            "CONFIRM_RC=124",
            "any other CONFIRM_RC",
            "confirm_invocation_error: exit=timeout",
            "convergence_record_round_failed: exit=<n>",
            "stop.release_hint",
            "stop.settle_payload",
        ],
    )
    def test_the_prompt_names_the_exit(self, prompt: str, token: str) -> None:
        assert token in prompt

    def test_an_overrun_is_never_a_backend_outage(self, prompt: str) -> None:
        overrun = prompt[prompt.index("CONFIRM_RC=124") :]
        overrun = overrun[: overrun.index("\n")]
        assert "agent_block" in overrun
        assert "Never confirm_backend_unavailable" in overrun


class TestConfirmCall:
    def test_the_confirm_is_a_foreground_capped_call(self, prompt: str) -> None:
        assert CONFIRM_TOOL_TIMEOUT_MS == CONFIRM_TIMEOUT_SECONDS * 1000
        assert f"timeout {CONFIRM_TIMEOUT_SECONDS} cw review confirm" in prompt
        assert f"set explicitly to {CONFIRM_TOOL_TIMEOUT_MS}" in prompt
        assert "run_in_background" not in prompt
        assert "never run it in the background" in prompt

    def test_the_backend_comes_from_the_ledger_not_the_cli_default(
        self, prompt: str
    ) -> None:
        assert (
            'CONFIRM_BACKEND="$(cw review converge status | jq -r '
            '.sentinel_review.confirm_backend)"'
        ) in prompt
        assert '--backend "$CONFIRM_BACKEND"' in prompt
        for literal in ("--backend none", "--backend codex", "--backend claude"):
            assert literal not in prompt

    def test_a_none_backend_never_runs_the_confirm(self, prompt: str) -> None:
        assert '*) echo "CONFIRM_SKIPPED" ;;' in prompt
        skipped = prompt[prompt.index("   - CONFIRM_SKIPPED") :]
        skipped = skipped[: skipped.index("\n")]
        assert "none, empty or null" in skipped
        assert "agent_block" in skipped
        assert "Do NOT run cw review confirm" in skipped

    @pytest.mark.parametrize("backend", ["claude", "codex", "none"])
    def test_the_status_jq_path_matches_status_summary(
        self, prompt: str, backend: ConfirmBackend
    ) -> None:
        """A rename of the status key breaks this test, not the live prompt."""
        path = re.search(r"jq -r (\.sentinel_review\.\w+)\)", prompt)
        assert path is not None
        ledger = fresh_ledger(confirm=backend, loop_backend="opencode")
        node: object = json.loads(json.dumps(status_summary(ledger, head_sha=_SHA)))
        for key in path.group(1).strip(".").split("."):
            assert isinstance(node, dict)
            node = node[key]
        assert node == backend


class TestPromptOrder:
    def test_config_then_init_then_status(self, prompt: str) -> None:
        config = prompt.index("cw review converge config")
        init = prompt.index("cw review converge init")
        status = prompt.index("cw review converge status")
        assert config < init < status

    @pytest.mark.parametrize(
        "line",
        [
            "   - stopped → ",
            "   - confirm → Step 4.",
            "   - done → Step 5.",
            "   - loop_round or fix → Step 3",
        ],
    )
    def test_every_next_action_is_obeyed(self, prompt: str, line: str) -> None:
        assert line in prompt

    def test_the_body_is_ticket_specific(self) -> None:
        body = clean_review_body("PROJ-9")
        assert "--ticket-id PROJ-9" in body
        assert TICKET not in body


def _confirm_record_payload(stdout: dict[str, object]) -> dict[str, object]:
    """Python-equivalent of :data:`CONFIRM_RECORD_JQ` (Adopted Assumption 13)."""
    verdict = stdout["verdict"]
    assert isinstance(verdict, dict)
    agents = verdict["agents_run"]
    assert isinstance(agents, list)
    return {
        "kind": "confirm",
        "backend": stdout["backend"],
        "reviewed_sha": stdout["reviewed_sha"],
        "roster": [record["reviewer_role"] for record in agents],
        "verdict": verdict,
    }


def _confirm_stdout() -> dict[str, object]:
    verdict = consolidated_verdict(
        accepted=[("MUST_FIX", "Bug"), ("SHOULD_FIX", "Style")]
    )
    return confirm_verdict_payload(verdict, backend="claude")


class TestConfirmRecordJq:
    def test_the_prompt_pipes_the_filter_into_record_round(self, prompt: str) -> None:
        assert (
            f"jq '{CONFIRM_RECORD_JQ}' .cw/confirm-stdout.json | cw review "
            "converge record-round - --defer-should-fix"
        ) in prompt

    def test_the_mapped_payload_is_a_confirm_round_input(self) -> None:
        stdout = _confirm_stdout()
        payload = RoundInput.model_validate(_confirm_record_payload(stdout))
        assert payload.kind == "confirm"
        assert payload.backend == "claude"
        assert payload.reviewed_sha == DEFAULT_SHA
        assert payload.verdict is not None
        assert payload.roster == [r.reviewer_role for r in payload.verdict.agents_run]
        assert payload.roster
        assert payload.findings is None

    @pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
    def test_real_jq_matches_the_python_equivalent(self) -> None:
        stdout = _confirm_stdout()
        jq = shutil.which("jq")
        assert jq is not None
        ran = subprocess.run(
            [jq, "-c", CONFIRM_RECORD_JQ],
            input=json.dumps(stdout),
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(ran.stdout) == _confirm_record_payload(stdout)


_PLACEHOLDER = re.compile(r'"<[^<>"]*>"|<[^<>]*>')


def _fill(values: dict[str, object]) -> dict[str, object]:
    """The template's JSON with every placeholder replaced from *values*.

    Keyed by the placeholder's whitespace-normalized text, so a new or renamed
    placeholder is a ``KeyError`` here rather than a silent gap.
    """
    start = CLEAN_REVIEW_SENTINEL_TEMPLATE.index("<<<AUTO_DEV_RESULT\n")
    end = CLEAN_REVIEW_SENTINEL_TEMPLATE.index("\nAUTO_DEV_RESULT>>>")
    body = CLEAN_REVIEW_SENTINEL_TEMPLATE[start + len("<<<AUTO_DEV_RESULT\n") : end]

    def _sub(match: re.Match[str]) -> str:
        key = " ".join(match.group(0).strip('"<>').split())
        return json.dumps(values[key])

    filled: dict[str, object] = json.loads(_PLACEHOLDER.sub(_sub, body))
    return filled


def _values(
    *, status: str, converged: bool, clean_sha: str | None
) -> dict[str, object]:
    return {
        "ticket-id": TICKET,
        "stage_complete | review_pending_approval | empty_diff_blocked | blocked": (
            status
        ),
        "small|large": "small",
        "changed files": 3,
        "added+removed vs fork point": 40,
        "github_issue_existing | generated | free_text | none": "generated",
        "branch-name": "dev/t-1",
        "absolute worktree path": "/wt",
        "fork point sha": "f" * 40,
        "pushed fix sha": _SHA,
        "first loop round MUST_FIX count": 1,
        "first loop round SHOULD_FIX count": 1,
        "sentinel_review.fix_cycles_used": 1,
        "final_round_clean_sha, or HEAD when blocked": _SHA,
        "sentinel_review.rounds_run": 3,
        "sentinel_review.converged": converged,
        "sentinel_review.final_round_clean_sha": clean_sha,
        "sentinel_review.confirm_backend": "claude",
        "HIGH|MEDIUM|LOW": "HIGH",
        "PROCEED|EXIT_FOR_HUMAN_REVIEW": "PROCEED",
    }


class TestSentinelTemplate:
    def test_a_converged_review_validates(self) -> None:
        data = _fill(_values(status="stage_complete", converged=True, clean_sha=_SHA))
        result = AutoDevResult.model_validate(data)
        assert result.review.convergence_mode == "clean"
        assert result.review.converged is True
        assert result.review.final_round_clean_sha == result.review.reviewed_sha
        assert result.review.confirm_backend == "claude"

    def test_a_not_converging_stop_validates(self) -> None:
        data = _fill(_values(status="blocked", converged=False, clean_sha=None))
        data["blocker"] = {
            "stage": "stage3_review",
            "reason": "review_not_converging",
            "details": "oscillation; release_hint: settle the recurring finding",
            "retry_eligible": False,
        }
        result = AutoDevResult.model_validate(data)
        assert result.status == "blocked"
        assert result.review.converged is False
        assert result.blocker is not None
        assert result.blocker.reason == "review_not_converging"

    def test_a_stage_complete_that_did_not_converge_is_refused(self) -> None:
        data = _fill(_values(status="stage_complete", converged=False, clean_sha=None))
        with pytest.raises(ValueError, match="converged"):
            AutoDevResult.model_validate(data)
