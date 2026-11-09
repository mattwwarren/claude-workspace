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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import click
import pytest
from click.testing import CliRunner

from cw.auto_dev_result import (
    EMPTY_DIFF_BLOCKER_REASON,
    KNOWN_BLOCKER_REASONS,
    AutoDevResult,
    StageReached,
)
from cw.cli import main
from cw.cli.review.confirm import (
    CONFIRM_ROUND_FAILED_EXIT_CODE,
    CONFIRM_UNAVAILABLE_EXIT_CODE,
    review_confirm,
)
from cw.codex_review import (
    CODEX_MUST_FIX_FINDINGS,
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
)
from cw.models import OPENCODE_BACKEND
from cw.opencode_clean_review import (
    CLEAN_REVIEW_SENTINEL_TEMPLATE,
    CONFIRM_RECORD_JQ,
    CONFIRM_TIMEOUT_SECONDS,
    CONFIRM_TOOL_TIMEOUT_MS,
    clean_review_body,
)
from cw.opencode_runner import build_stage_prompt
from cw.review_convergence import (
    STOP_DECISIONS,
    Decision,
    NextAction,
    RoundInput,
    status_summary,
)
from tests._review_convergence_helpers import (
    DEFAULT_SHA,
    confirm_verdict_payload,
    consolidated_verdict,
    fresh_ledger,
    record,
    write_ledger,
)

if TYPE_CHECKING:
    from cw.review_convergence import ConfirmBackend

pytestmark = pytest.mark.platform_sensitive

# Skip only off CI: a CI runner without jq must fail, not silently pass unrun.
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)

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
    review = main.get_command(click.Context(main), "review")
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


#: Where each ``status`` ``next_action`` sends the agent, keyed by the real
#: ``NextAction`` literal so a new or renamed action fails the keys test.
_NEXT_ACTION_TARGET = {
    "stopped": "exit table",
    "confirm": "Step 4",
    "done": "Step 5",
    "loop_round": "Step 3",
    "fix": "Step 3",
}
#: ``stop_confirm_escalated`` only comes back from ``confirm-failed``, so the
#: Step 3 record-round branch list never names it.
_CONFIRM_ONLY_DECISION = "stop_confirm_escalated"


def _word_in(word: str, text: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text) is not None


def _is_branch(decision: str, text: str) -> bool:
    """``decision`` is in the comma list before the arrow of a Step 3 branch line.

    A bare word match is not enough: ``clean`` and ``continue`` appear in prose
    all over the prompt ("never continue as clean"), so only the
    ``      - <decisions> → `` bullet shape proves a branch exists.
    """
    pattern = rf"^      - (?:[a-z_]+, )*{re.escape(decision)}(?:, [a-z_]+)* → "
    return re.search(pattern, text, re.MULTILINE) is not None


def _confirm_rc_branch(prompt: str, code: int) -> str:
    """The ``CONFIRM_RC=<code>`` branch, up to the next ``CONFIRM_RC=`` marker."""
    marker = f"   - CONFIRM_RC={code}"
    start = prompt.index(marker)
    end = prompt.find("CONFIRM_RC=", start + len(marker))
    return prompt[start:] if end == -1 else prompt[start:end]


def _loop_round_step_text(prompt: str) -> str:
    return prompt[prompt.index("3. Loop round") : prompt.index("4. Confirm round")]


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

    def test_every_loop_backend_the_prompt_names_is_the_opencode_constant(
        self, prompt: str
    ) -> None:
        """A drifted literal would stamp the ledger with the wrong loop backend."""
        values = [seg.split()[0] for seg in _segments(prompt, "--loop-backend ")]
        assert len(values) == 2  # config guard + converge init
        assert set(values) == {OPENCODE_BACKEND}
        assert f'"kind":"loop","backend":"{OPENCODE_BACKEND}"' in prompt
        assert f'"roster":["{OPENCODE_BACKEND}"]' in prompt


class TestShellSafety:
    """Model-authored text never lands inside a quoted shell argument."""

    def test_no_single_quoted_inline_json_payload_remains(self, prompt: str) -> None:
        assert "printf '%s' '{" not in prompt
        # The shared sentinel rules print "$SENTINEL_JSON" (a variable, not
        # inline text), so scope the no-printf check to the clean body.
        assert "printf" not in clean_review_body(TICKET)

    def test_the_loop_round_payload_goes_through_a_quoted_heredoc(
        self, prompt: str
    ) -> None:
        # Column 0: a heredoc ends only at a line that is exactly the delimiter.
        assert "\ncat > .cw/loop-round.json <<'JSON'\n" in prompt
        assert "\ncw review converge record-round .cw/loop-round.json\n" in prompt
        assert "record-round -\n" not in prompt
        heredoc = prompt.index("<<'JSON'")
        findings = prompt.index('"findings":<FINDINGS>}', heredoc)
        assert prompt.index("\nJSON\n", findings) > findings
        assert " JSON\n" not in prompt[findings:]

    def test_the_confirm_failed_detail_is_read_from_the_captured_files(
        self, prompt: str
    ) -> None:
        assert '--cause "$CAUSE" --detail "$DETAIL"' in prompt
        assert "tail -n 20 .cw/confirm-stderr.txt" in prompt
        for unsafe in ('--detail "<', "--cause <", '--detail "{'):
            assert unsafe not in prompt


class TestTypedExits:
    @pytest.mark.parametrize(
        "token",
        [
            '"review_not_converging", retry_eligible false',
            '"review_blocked"',
            '"review_operator_actionable"',
            '"confirm_backend_unavailable", retry_eligible true',
            '"agent_block"',
            f"CONFIRM_RC={CONFIRM_ROUND_FAILED_EXIT_CODE}",
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

    def test_a_tip_fetch_failure_reruns_finalize_never_a_round(
        self, prompt: str
    ) -> None:
        """#2910: an unreachable origin is not a review problem."""
        branch = prompt[prompt.index("reason tip_check_fetch_failed") :]
        branch = branch[: branch.index("\n   - ")]
        assert "re-run finalize once" in branch
        assert "agent_block" in branch
        assert "Step 3" not in branch

    def test_an_overrun_is_never_a_backend_outage(self, prompt: str) -> None:
        overrun = prompt[prompt.index("CONFIRM_RC=124") :]
        overrun = overrun[: overrun.index("\n")]
        assert "agent_block" in overrun
        assert "Never confirm_backend_unavailable" in overrun

    def test_a_backend_outage_is_exit_3_only(self, prompt: str) -> None:
        outage = _confirm_rc_branch(prompt, CONFIRM_UNAVAILABLE_EXIT_CODE)
        assert "confirm_backend_unavailable" in outage
        assert "cw review converge confirm-failed" in outage
        assert "stop_confirm_unavailable" in outage
        assert "stop_confirm_escalated" in outage

    def test_a_healthy_backend_failure_is_exit_4_and_records_nothing(
        self, prompt: str
    ) -> None:
        failed = _confirm_rc_branch(prompt, CONFIRM_ROUND_FAILED_EXIT_CODE)
        assert "agent_block" in failed
        assert "confirm_backend_unavailable" not in failed
        assert "cw review converge confirm-failed" not in failed
        assert "record nothing" in failed

    def test_the_usage_error_exit_is_an_agent_block(self, prompt: str) -> None:
        # No constant for 2: it is Click's own usage-error exit, not a cw code.
        usage = _confirm_rc_branch(prompt, 2)
        assert "agent_block" in usage
        assert "confirm_backend_unavailable" not in usage

    def test_every_blocker_reason_and_stage_is_in_the_real_vocabulary(self) -> None:
        """A typo in the prompt would otherwise fail only in the worker's sentinel."""
        body = clean_review_body(TICKET)
        reasons = set(re.findall(r'\breason "(\w+)"', body))
        assert {"agent_block", "review_not_converging"} <= reasons
        assert reasons <= KNOWN_BLOCKER_REASONS, reasons - KNOWN_BLOCKER_REASONS
        stages = set(re.findall(r'blocker\.stage "(\w+)"', body))
        assert stages == {"stage3_review"}
        assert stages <= set(get_args(StageReached))

    def test_every_next_action_has_a_known_target(self) -> None:
        assert set(_NEXT_ACTION_TARGET) == set(get_args(NextAction))

    @pytest.mark.parametrize("action", get_args(NextAction))
    def test_every_next_action_has_a_branch_line(
        self, prompt: str, action: str
    ) -> None:
        branch = re.search(
            rf"^ {{3}}- (?:\w+ or )?{action}(?: or \w+)? → (?P<rest>.*)$",
            prompt,
            re.MULTILINE,
        )
        assert branch is not None, f"no next_action branch line for {action}"
        assert _NEXT_ACTION_TARGET[action] in branch["rest"]

    def test_stop_decisions_are_the_stop_prefixed_decisions(self) -> None:
        stops = {d for d in get_args(Decision) if d.startswith("stop_")}
        assert stops == set(STOP_DECISIONS)

    @pytest.mark.parametrize(
        "decision", [d for d in get_args(Decision) if d != _CONFIRM_ONLY_DECISION]
    )
    def test_the_loop_round_step_branches_on_the_decision(
        self, prompt: str, decision: str
    ) -> None:
        assert _is_branch(decision, _loop_round_step_text(prompt))

    def test_the_confirm_only_decision_is_not_a_loop_round_branch(
        self, prompt: str
    ) -> None:
        assert not _word_in(_CONFIRM_ONLY_DECISION, _loop_round_step_text(prompt))
        assert _word_in(
            _CONFIRM_ONLY_DECISION,
            _confirm_rc_branch(prompt, CONFIRM_UNAVAILABLE_EXIT_CODE),
        )

    def test_the_verdict_branch_uses_the_real_blocker_reasons(
        self, prompt: str
    ) -> None:
        verdict = prompt[
            prompt.index("   - CONFIRM_RC=0") : prompt.index(
                f"   - CONFIRM_RC={CONFIRM_UNAVAILABLE_EXIT_CODE}"
            )
        ]
        record = next(ln for ln in verdict.splitlines() if "record it:" in ln)
        for reason in (CODEX_MUST_FIX_FINDINGS, "scope_tier_unresolvable"):
            assert _word_in(reason, record)
        rejected = next(
            ln
            for ln in verdict.splitlines()
            if CODEX_MUST_FIX_MECHANICALLY_REJECTED in ln
        )
        assert '"review_blocked"' in rejected
        assert EMPTY_DIFF_BLOCKER_REASON in record


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
        ledger = fresh_ledger(confirm=backend, loop_backend=OPENCODE_BACKEND)
        node: object = json.loads(json.dumps(status_summary(ledger, head_sha=_SHA)))
        for key in path.group(1).strip(".").split("."):
            assert isinstance(node, dict)
            node = node[key]
        assert node == backend


#: Stub ``cw``: ``confirm`` records its argv, sleeps, then exits with
#: ``STUB_CONFIRM_RC``; ``diff-base`` exits with ``STUB_DIFF_RC``.
_CW_STUB = """#!/usr/bin/env bash
case "$1 $2 $3" in
  "review converge status") echo '{"sentinel_review": {}}' ;;
  "review converge diff-base") echo '{"diff_base": "base"}'; exit "$STUB_DIFF_RC" ;;
  "review confirm "*)
    echo "$@" > confirm-args.txt
    sleep "$STUB_CONFIRM_SLEEP"
    exit "$STUB_CONFIRM_RC" ;;
esac
"""
#: Stub ``jq``: answers the two reads the fence makes, from ``STUB_BACKEND``.
_JQ_STUB = """#!/usr/bin/env bash
case "$2" in
  .sentinel_review.confirm_backend) printf '%s\\n' "$STUB_BACKEND" ;;
  .client) echo client-a ;;
esac
"""
#: Stub ``timeout`` (coreutils ``timeout SECONDS COMMAND...``): stock macOS CI has
#: no unprefixed ``timeout``, so the fence test installs this shim to stay hermetic.
#: Runs the command, returns its status, and exits 124 on overrun (killing the
#: command's whole process group so a lingering child cannot hold the pipes open).
_TIMEOUT_STUB = f"""#!{sys.executable}
import os
import signal
import subprocess
import sys

seconds = float(sys.argv[1])
proc = subprocess.Popen(sys.argv[2:], start_new_session=True)
try:
    sys.exit(proc.wait(timeout=seconds))
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    sys.exit(124)
"""


def _shell_block(prompt: str, first: str, last: str) -> str:
    """The prompt text from the line starting *first* through the line *last*."""
    start = prompt.index(first)
    end = prompt.index(last, start) + len(last)
    return prompt[start:end]


def _confirm_fence(prompt: str) -> str:
    """The Step 4 shell block, exactly as the agent is told to run it."""
    return _shell_block(
        prompt,
        '      CONFIRM_BACKEND="$(cw review converge status',
        "      esac\n",
    )


class TestConfirmFenceExecutes:
    """The one executable block in the prompt, run under bash with stub CLIs."""

    @staticmethod
    def _run(
        prompt: str,
        tmp_path: Path,
        *,
        backend: str,
        diff_base_rc: int = 0,
        confirm_rc: int = 0,
        confirm_sleep: float = 0,
        timeout: int = CONFIRM_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("cw", _CW_STUB),
            ("jq", _JQ_STUB),
            ("timeout", _TIMEOUT_STUB),
        ):
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        work = tmp_path / "work"
        (work / ".cw").mkdir(parents=True)
        fence = _confirm_fence(prompt)
        shortened = fence.replace(
            f"timeout {CONFIRM_TIMEOUT_SECONDS} cw review confirm",
            f"timeout {timeout} cw review confirm",
        )
        assert timeout == CONFIRM_TIMEOUT_SECONDS or shortened != fence
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_BACKEND": backend,
            "STUB_DIFF_RC": str(diff_base_rc),
            "STUB_CONFIRM_RC": str(confirm_rc),
            "STUB_CONFIRM_SLEEP": str(confirm_sleep),
        }
        return subprocess.run(
            ["bash", "-c", shortened],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    @pytest.mark.parametrize("backend", ["none", "", "null"])
    def test_a_none_empty_or_null_backend_skips_the_confirm(
        self, prompt: str, tmp_path: Path, backend: str
    ) -> None:
        ran = self._run(prompt, tmp_path, backend=backend)
        assert "CONFIRM_SKIPPED" in ran.stdout.splitlines()
        assert "CONFIRM_RC" not in ran.stdout
        assert "DIFF_BASE_RC" not in ran.stdout
        assert not (tmp_path / "work" / "confirm-args.txt").exists()

    @pytest.mark.parametrize("backend", ["claude", "codex"])
    def test_a_failing_confirm_reports_its_own_exit_status(
        self, prompt: str, tmp_path: Path, backend: str
    ) -> None:
        ran = self._run(
            prompt, tmp_path, backend=backend, confirm_rc=CONFIRM_UNAVAILABLE_EXIT_CODE
        )
        lines = ran.stdout.splitlines()
        assert f"CONFIRM_BACKEND={backend}" in lines
        assert "DIFF_BASE_RC=0" in lines
        assert f"CONFIRM_RC={CONFIRM_UNAVAILABLE_EXIT_CODE}" in lines
        assert "CONFIRM_SKIPPED" not in lines
        args = (tmp_path / "work" / "confirm-args.txt").read_text(encoding="utf-8")
        assert f"--backend {backend}" in args
        assert f"--ticket-id {TICKET}" in args
        assert "-c client-a" in args

    def test_a_failing_diff_base_never_reaches_the_confirm(
        self, prompt: str, tmp_path: Path
    ) -> None:
        ran = self._run(prompt, tmp_path, backend="claude", diff_base_rc=7)
        assert "DIFF_BASE_RC=7" in ran.stdout.splitlines()
        assert "CONFIRM_RC" not in ran.stdout
        assert not (tmp_path / "work" / "confirm-args.txt").exists()

    def test_an_overrun_reports_exit_124(self, prompt: str, tmp_path: Path) -> None:
        ran = self._run(prompt, tmp_path, backend="claude", confirm_sleep=5, timeout=1)
        assert "CONFIRM_RC=124" in ran.stdout.splitlines()


_LOOP_ROUND_FIRST = "cat > .cw/loop-round.json <<'JSON'\n"
_LOOP_ROUND_LAST = "cw review converge record-round .cw/loop-round.json\n"
_CONFIRM_FAILED_FIRST = "       CAUSE=\"$(jq -r '.cause // empty'"
_CONFIRM_FAILED_LAST = '-c "$(jq -r .client .claude/cw-context.json)"\n'
#: Stub ``cw`` for the recording blocks: argv goes NUL-separated to ``cw-argv.bin``
#: (one record per call), so an argument holding a newline stays one argument.
_RECORDING_CW_STUB = """#!/usr/bin/env bash
printf '%s\\0' "$@" >> cw-argv.bin
printf '\\n' >> cw-argv.bin
"""
_TRICKY_TEXT = 'it\'s "quoted" $(touch pwned) `touch pwned2` $HOME\nsecond line'


def _run_recording_block(
    block: str, tmp_path: Path, *, files: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run *block* under bash with a recording ``cw`` stub; return its argv calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cw").write_text(_RECORDING_CW_STUB, encoding="utf-8")
    (bin_dir / "cw").chmod(0o755)
    work = tmp_path / "work"
    (work / ".cw").mkdir(parents=True)
    (work / ".claude").mkdir()
    (work / ".claude" / "cw-context.json").write_text(
        '{"client": "client-a"}', encoding="utf-8"
    )
    for name, text in files.items():
        (work / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    ran = subprocess.run(
        ["bash", "-c", block],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    argv_file = work / "cw-argv.bin"
    raw = argv_file.read_text(encoding="utf-8") if argv_file.exists() else ""
    calls = [call.split("\0") for call in raw.split("\0\n") if call]
    return ran, calls


class TestLoopRoundHeredocExecutes:
    """The loop-round heredoc, run under bash exactly as the prompt shows it."""

    def test_the_block_writes_the_payload_and_records_it_once(
        self, prompt: str, tmp_path: Path
    ) -> None:
        findings = json.dumps(
            [
                {
                    "severity": "MUST_FIX",
                    "file": "src/a.py",
                    "summary": _TRICKY_TEXT,
                    "outcome": "fix",
                    "rationale": "why",
                }
            ]
        )
        block = _shell_block(prompt, _LOOP_ROUND_FIRST, _LOOP_ROUND_LAST)
        block = block.replace("<FINDINGS>", findings).replace(
            "<git rev-parse HEAD>", _SHA
        )
        ran, calls = _run_recording_block(block, tmp_path, files={})
        assert ran.returncode == 0, ran.stderr
        assert "delimited by end-of-file" not in ran.stderr
        assert calls == [["review", "converge", "record-round", ".cw/loop-round.json"]]
        work = tmp_path / "work"
        payload = json.loads((work / ".cw" / "loop-round.json").read_text("utf-8"))
        assert payload["kind"] == "loop"
        assert payload["backend"] == OPENCODE_BACKEND
        assert payload["reviewed_sha"] == _SHA
        assert payload["roster"] == [OPENCODE_BACKEND]
        assert payload["findings"][0]["summary"] == _TRICKY_TEXT
        assert not (work / "pwned").exists()
        assert not (work / "pwned2").exists()


@requires_jq
class TestConfirmFailedBlockExecutes:
    """The exit-3 block: stderr- and model-derived text stays one argument."""

    @staticmethod
    def _block(prompt: str) -> str:
        return _shell_block(prompt, _CONFIRM_FAILED_FIRST, _CONFIRM_FAILED_LAST)

    def test_a_hostile_detail_arrives_verbatim_as_one_argument(
        self, prompt: str, tmp_path: Path
    ) -> None:
        stdout = json.dumps({"cause": "backend_error", "detail": _TRICKY_TEXT})
        ran, calls = _run_recording_block(
            self._block(prompt),
            tmp_path,
            files={
                ".cw/confirm-stdout.json": stdout,
                ".cw/confirm-stderr.txt": "ignored\n",
            },
        )
        assert ran.returncode == 0, ran.stderr
        assert calls == [
            [
                "review",
                "converge",
                "confirm-failed",
                "--cause",
                "backend_error",
                "--detail",
                _TRICKY_TEXT,
                "-c",
                "client-a",
            ]
        ]
        assert not (tmp_path / "work" / "pwned").exists()
        assert not (tmp_path / "work" / "pwned2").exists()

    @pytest.mark.parametrize(
        "stdout",
        [None, "", "not json at all", '{"cause": null}'],
        ids=["missing", "empty", "invalid", "no-fields"],
    )
    def test_no_usable_stdout_falls_back_to_no_output_and_the_stderr_tail(
        self, prompt: str, tmp_path: Path, stdout: str | None
    ) -> None:
        stderr_lines = [f"err line {n} $(touch pwned)" for n in range(30)]
        files = {".cw/confirm-stderr.txt": "\n".join(stderr_lines) + "\n"}
        if stdout is not None:
            files[".cw/confirm-stdout.json"] = stdout
        ran, calls = _run_recording_block(self._block(prompt), tmp_path, files=files)
        assert ran.returncode == 0, ran.stderr
        assert calls == [
            [
                "review",
                "converge",
                "confirm-failed",
                "--cause",
                "no_output",
                "--detail",
                "\n".join(stderr_lines[-20:]),
                "-c",
                "client-a",
            ]
        ]
        assert not (tmp_path / "work" / "pwned").exists()


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

    @requires_jq
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

    @requires_jq
    def test_real_jq_output_records_through_defer_should_fix(
        self, tmp_path: Path
    ) -> None:
        """Real jq output chains into `record-round --defer-should-fix` and records."""
        jq = shutil.which("jq")
        assert jq is not None
        # An awaiting_confirm ledger at the confirm round's sha: an opencode loop
        # round already recorded clean, with the claude confirm still owed.
        ledger, loop = record(
            fresh_ledger(confirm="claude", loop_backend=OPENCODE_BACKEND),
            sha=DEFAULT_SHA,
            backend=OPENCODE_BACKEND,
        )
        assert loop.decision == "confirm_required"
        ledger_path = write_ledger(tmp_path, ledger)
        verdict = consolidated_verdict(accepted=[("SHOULD_FIX", "Style")])
        assert verdict.accepted[0].disposition == "fixed"
        stdout = confirm_verdict_payload(verdict, backend="claude")
        mapped = subprocess.run(
            [jq, CONFIRM_RECORD_JQ],
            input=json.dumps(stdout),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        result = CliRunner().invoke(
            main,
            [
                "review",
                "converge",
                "record-round",
                "-",
                "--defer-should-fix",
                "--ledger",
                str(ledger_path),
            ],
            input=mapped,
        )

        assert result.exit_code == 0, result.stderr
        report = json.loads(result.stdout)
        # The raw SHOULD_FIX (disposition "fixed") would gate the round; the flag
        # decides it, so the real jq output records a clean confirm round.
        assert report["decision"] == "clean"
        persisted = (
            ledger_path.parent
            / "review-convergence"
            / (f"round-{report['round']}-verdict.json")
        )
        (accepted,) = json.loads(persisted.read_text(encoding="utf-8"))["accepted"]
        assert accepted["disposition"] == "deferred"


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


class TestEngineContextLeavesOpencodeUntouched:
    """#3039: a context carrying ``review.engine`` changes nothing on opencode."""

    def _config(self, tmp_path: Path, *args: str) -> dict[str, object]:
        context = tmp_path / "cw-context.json"
        context.write_text(
            json.dumps(
                {
                    "schema_version": 19,
                    "session_id": "abc",
                    "ticket_id": TICKET,
                    "headless": True,
                    "review": {
                        "convergence": "clean",
                        "max_cycles": None,
                        "confirm_backend": None,
                        "engine": {"skill": "repo-review", "guarded_paths": []},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            main, ["review", "converge", "config", "--context", str(context), *args]
        )
        assert result.exit_code == 0, result.output
        report: dict[str, object] = json.loads(result.stdout)
        return report

    def test_opencode_loop_backend_reports_no_engine(self, tmp_path: Path) -> None:
        report = self._config(tmp_path, "--loop-backend", OPENCODE_BACKEND)

        assert report["engine"] is None
        assert report["engine_error"] is None
        assert report["convergence"] == "clean"
        assert report["loop_backend"] == OPENCODE_BACKEND

    def test_the_same_context_reports_the_engine_on_the_claude_lane(
        self, tmp_path: Path
    ) -> None:
        assert self._config(tmp_path)["engine"] == "repo-review"

    def test_rendered_prompt_never_mentions_the_engine(self, prompt: str) -> None:
        lowered = prompt.lower()

        assert "review engine" not in lowered
        assert "engine preflight" not in lowered
        assert "review.engine" not in lowered

    def test_rendered_prompt_is_independent_of_the_context(
        self, tmp_path: Path, prompt: str
    ) -> None:
        self._config(tmp_path, "--loop-backend", OPENCODE_BACKEND)

        again = build_stage_prompt(
            "review", TICKET, Path("/wt"), session_id="abc123", clean_review=True
        )

        assert again == prompt
