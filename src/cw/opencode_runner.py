"""OpenCode subprocess runner for OpencodeExecutor (#1669).

Parallel to local_runner.py's AiderRunner seam. OpencodeExecutor delegates
the ``opencode run --format json`` invocation to an OpencodeRunner so tests
can drive every disposition without spawning a real subprocess.

Fire-and-forget: like AiderRunner, launch() returns a live Popen and the
caller captures PID + start-time as a LocalLivenessHandle. The opencode run
completes asynchronously; reconcile/local harvest detects the dead process
and parses the JSONL log for the ``<<<AUTO_DEV_RESULT>>>`` sentinel.

opencode has no ``--output-schema`` (unlike codex); the result travels as
free-form text in ``text`` event payloads, harvested via the sentinel pattern
(#1669 R3, probe-confirmed).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from cw.auto_dev_result import (
    AutoDevResult,
    Blocker,
    Health,
    Review,
    Scope,
    StageReached,
)
from cw.auto_dev_result.parse import parse_stdout
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)

if TYPE_CHECKING:
    from cw.models import TicketTask

_SCHEMA_VERSION: Literal[4] = 4

OPENCODE_LOG_RELATIVE_PATH: Path = Path(".cw", "opencode.log")
_OPENCODE_LOG_TAIL_CHARS = 4000  # matches local_runner's _AIDER_LOG_TAIL_CHARS

OPENCODE_NOT_FOUND = "opencode_not_found"
OPENCODE_NO_OUTPUT = "opencode_no_output"
UNEXPECTED_ERROR = "unexpected_error"
LIVENESS_UNAVAILABLE = "liveness_unavailable"

# The FINALIZE entry-point stage marker (mirrors STAGE3_REVIEW for codex).
# Used as the stage_reached for the stage-block on non-FINALIZE stages (#1670 R5).
STAGE4A_MERGE_GATE: StageReached = "stage4a_merge_gate"

_blocked_scope = Scope(
    tier="small",
    files=0,
    lines_estimate=0,
    lines_actual=0,
    forbidden_touched=False,
)
_FIXED_HEALTH = Health(
    lowest_agent_confidence="LOW",
    any_incomplete_risk=True,
    recommendation="EXIT_FOR_HUMAN_REVIEW",
)
_FIXED_REVIEW = Review(must_fix_initial=0, should_fix=0, fix_cycles_used=0)
_FIXED_NEXT_ACTIONS: list[str] = ["user_resolve_opencode_executor_failure"]

_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SLACK_MCP_CLIENT_ID",
        "SLACK_MCP_CLIENT_SECRET",
    }
)


def opencode_available() -> bool:
    """Return True if the opencode binary is on PATH."""
    return shutil.which("opencode") is not None


@runtime_checkable
class OpencodeRunner(Protocol):
    """Testability seam for the opencode subprocess launch (#1669).

    Mirrors AiderRunner in local_runner.py — fire-and-forget: the caller does
    NOT wait, it captures the PID + start-time as a liveness handle and returns
    immediately. reconcile/local harvest later detects the dead process and
    parses the JSONL log for the sentinel.
    """

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        """Fire-and-forget spawn of the opencode process; return the live Popen."""
        ...


class RealOpencodeRunner:
    """Production implementation: launches opencode as a detached subprocess.

    Mirrors RealAiderRunner — redirects stdout to a per-worktree log file
    (``.cw/opencode.log``) for the harvest path to parse. Truncated ("w") on
    every call so a retry into the same worktree does not bleed a prior
    attempt's output into the next harvest read.
    """

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as log_file:
            return subprocess.Popen(
                argv,
                env=env,
                cwd=worktree,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )


class FakeOpencodeRunner:
    """Test double: records the launch call; returns a real live subprocess.

    Mirrors FakeAiderRunner in local_runner.py. Returns
    ``Popen(["sleep", "60"])`` rather than a fast-exiting process so the
    caller's ``read_process_start_time_ns`` lookup does not race a just-exited
    PID. Spawned processes are tracked in ``self.procs`` so tests can kill them.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.procs: list[subprocess.Popen[bytes]] = []

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "env": dict(env),
            }
        )
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc


def build_argv(model: str | None, worktree: Path, prompt: str) -> list[str]:
    """Return the opencode argv for the given model, worktree, and prompt.

    Pins ``--format json`` (event stream for harvest), ``--pure`` (no external
    plugins — mechanical permission profile per #1669 R4), ``--auto`` (auto-
    approve permissions not explicitly denied — essential for headless fire-
    and-forget operation where no TTY is available to answer prompts), and
    ``--dir`` (run in the worktree). The prompt is the trailing positional,
    redacted in diagnostics by ``redact_argv``.
    """
    argv: list[str] = [
        "opencode",
        "run",
        "--format",
        "json",
        "--pure",
        "--auto",
        "--dir",
        str(worktree),
    ]
    if model is not None:
        argv.extend(["--model", model])
    argv.append(prompt)
    return argv


def build_env() -> dict[str, str]:
    """Return the subprocess env dict for opencode.

    Passes only an explicit allowlist of env vars. All operator shell secrets
    (AWS_*, tokens, etc.) are excluded by default. opencode reads its model
    config from its own config file (``~/.config/opencode/opencode.json``),
    not from env vars — no OPENAI_* overrides are needed (unlike aider's
    ``build_env``). MCP server auth env vars (SLACK_MCP_CLIENT_ID,
    SLACK_MCP_CLIENT_SECRET) are passed through because opencode's config
    references them via ``{env:...}`` substitution — without them, Slack MCP
    authentication silently fails in the subprocess. TMPDIR is required for
    tempfile access (macOS resolves to ``/var/folders/.../T/``).
    """
    return {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}


# Entry-point stage_reached marker per supported pipeline stage. A failure
# sentinel MUST carry the marker of the stage the task was dispatched AT:
# dispatch maps stage_reached back to a pipeline Stage and walks task.stage
# FORWARD when the sentinel's stage is later than the task's
# (``_resolve_stage_walk``, src/cw/dispatch/routing/stage_walk.py), so a
# stage2_impl marker on a PLAN-stage failure silently advances the task past
# planning. Mirrors codex's STAGE3_REVIEW convention (codex_review/_const.py).
_STAGE_ENTRY_MARKERS: dict[str, StageReached] = {
    "plan": "stage1_plan",
    "impl": "stage2_impl",
    "review": "stage3_review",
    "finalize": STAGE4A_MERGE_GATE,
}

SUPPORTED_STAGES: frozenset[str] = frozenset(_STAGE_ENTRY_MARKERS)


def stage_entry_marker(stage_value: str) -> StageReached:
    """Return the entry-point ``stage_reached`` for a pipeline stage value.

    Unsupported stage values (e.g. ``harden``) fall back to
    ``STAGE4A_MERGE_GATE``, preserving the #1670 R5 stage-block convention.
    """
    return _STAGE_ENTRY_MARKERS.get(stage_value, STAGE4A_MERGE_GATE)


# The FINALIZE stage executes via the auto-dev-finalize.md command file — the
# one stage whose flow was validated backend-neutral (#1670 R6: gh/git
# commands only). Resolution order: the worktree's own git-tracked copy first
# (per CLAUDE.md it is "the authoritative copy for anything a dispatched
# worker loads", and the global tree can silently drift), falling back to
# ``~/.claude/commands/`` for client-repo worktrees that carry no
# ``.claude/commands/`` of their own.
_FINALIZE_COMMAND_RELPATH = Path(".claude", "commands", "auto-dev-finalize.md")


def resolve_finalize_command_file(worktree: Path) -> Path:
    """Resolve the finalize command file: worktree copy first, home fallback."""
    worktree_copy = worktree / _FINALIZE_COMMAND_RELPATH
    if worktree_copy.is_file():
        return worktree_copy
    return Path.home() / _FINALIZE_COMMAND_RELPATH


# Shared prompt fragments. PLAN/IMPL/REVIEW prompts are self-contained: their
# auto-dev-{stage}.md command files orchestrate Claude Code-only machinery
# (Agent-tool subagent spawns, the Skill tool, $CW_SESSION, hook-written
# .claude/cw-context.json) that an opencode subprocess does not have, so
# pointing opencode at them stalls the run or invites an improvised,
# fabricated sentinel. Each prompt instead carries the stage's essential
# headless contract: the work, the durable artifacts, the honest failure
# exits, and the exact sentinel shape.


def _stage_preamble(stage_value: str, ticket_id: str) -> str:
    return (
        f"You are the auto-dev {stage_value.upper()} stage worker for ticket "
        f"{ticket_id}, running with --headless semantics: fire-and-forget, no "
        "TTY, no human available mid-run. Work only inside the current "
        "working directory (the session worktree); never run git mutations "
        "against any other checkout. If a required input or tool is missing, "
        "stop and emit an honest blocked sentinel instead of improvising "
        "around it.\n\n"
    )


_SENTINEL_RULES = (
    "\nSentinel contract: your FINAL message must contain exactly one block "
    "of the form\n"
    "<<<AUTO_DEV_RESULT\n{...one JSON object...}\nAUTO_DEV_RESULT>>>\n"
    "Report only facts that are true — never claim a push, passing test, or "
    "review that did not happen. If the `cw` CLI is on PATH, validate the "
    "JSON before emitting: printf '%s' \"$SENTINEL_JSON\" | cw result "
    "validate -\n"
)

_PLAN_SENTINEL_TEMPLATE = """
Sentinel template (stage1_plan — branch/fork_point_sha are null, commits [],
pr null, and scope.lines_actual MUST be null at this stage):
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<stage_complete | plan_pending_approval | no_op | blocked>",
  "stage_reached": "stage1_plan",
  "scope": {"tier": "<small|large>", "files": 0, "lines_estimate": 0,
            "lines_actual": null, "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": null,
  "worktree_path": "<absolute worktree path>",
  "fork_point_sha": null,
  "commits": [],
  "pr": null,
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
  "health": {"lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",
             "any_incomplete_risk": false, "shortcuts": [],
             "recommendation": "<PROCEED|EXIT_FOR_HUMAN_REVIEW>",
             "downgrade_applied": false, "fix_loop_escalated": false},
  "friction_highlights": [],
  "ambiguities": [],
  "blocker": null,
  "prior_pr_warnings": [],
  "next_actions": []
}
AUTO_DEV_RESULT>>>
"""

_IMPL_SENTINEL_TEMPLATE = """
Sentinel template (stage2_impl — scope.lines_actual, scope.tier, and
health.lowest_agent_confidence are REQUIRED non-null at this stage):
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "<ticket-id>",
  "status": "<stage_complete | blocked>",
  "stage_reached": "stage2_impl",
  "scope": {"tier": "<small|large>", "files": <changed files>,
            "lines_estimate": <plan estimate>,
            "lines_actual": <added+removed vs fork point>,
            "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<absolute worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<pushed sha1>", "<pushed sha2>"],
  "pr": null,
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
  "health": {"lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",
             "any_incomplete_risk": false, "shortcuts": [],
             "recommendation": "<PROCEED|EXIT_FOR_HUMAN_REVIEW>",
             "downgrade_applied": false, "fix_loop_escalated": false},
  "friction_highlights": [],
  "ambiguities": [],
  "blocker": null,
  "prior_pr_warnings": [],
  "next_actions": []
}
AUTO_DEV_RESULT>>>
"""

_REVIEW_SENTINEL_TEMPLATE = """
Sentinel template (stage3_review — scope.tier, scope.lines_actual, and
health.lowest_agent_confidence are REQUIRED non-null; review counts must be
the real ones, recorded before fixing):
<<<AUTO_DEV_RESULT
{
  "schema_version": 5,
  "ticket_id": "<ticket-id>",
  "status": "<stage_complete | review_pending_approval |
             empty_diff_blocked | blocked>",
  "stage_reached": "stage3_review",
  "scope": {"tier": "<small|large>", "files": <changed files>,
            "lines_estimate": 0,
            "lines_actual": <added+removed vs fork point>,
            "forbidden_touched": false},
  "plan_source": "<github_issue_existing | generated | free_text | none>",
  "branch": "<branch-name>",
  "worktree_path": "<absolute worktree path>",
  "fork_point_sha": "<fork point sha>",
  "commits": ["<pushed fix sha>"],
  "pr": null,
  "review": {"must_fix_initial": <initial count>,
             "should_fix": <initial count>,
             "fix_cycles_used": <cycles>, "deferred": 0, "agents_run": 1},
  "health": {"lowest_agent_confidence": "<HIGH|MEDIUM|LOW>",
             "any_incomplete_risk": false, "shortcuts": [],
             "recommendation": "<PROCEED|EXIT_FOR_HUMAN_REVIEW>",
             "downgrade_applied": false, "fix_loop_escalated": false},
  "friction_highlights": [],
  "ambiguities": [],
  "blocker": null,
  "prior_pr_warnings": [],
  "next_actions": []
}
AUTO_DEV_RESULT>>>
"""


def _plan_prompt(ticket_id: str) -> str:
    return (
        _stage_preamble("plan", ticket_id)
        + "Produce the implementation plan for this ticket:\n"
        "1. Context: read .cw/context.json if present. Live-fetch the ticket "
        f"body and ALL comments (`gh issue view {ticket_id} --json "
        "title,body,comments` for a GitHub-tracked repo; use the configured "
        "tracker's CLI otherwise). If the ticket cannot be fetched, emit "
        'blocked with blocker.reason "plan_missing_context".\n'
        "2. If the ticket already contains a sufficient plan (file paths plus "
        "a phased approach), extract it verbatim instead of regenerating "
        '(plan_source "github_issue_existing").\n'
        "3. Pre-flight: read the files the ticket targets. If every requested "
        'change is already in the desired state, emit status "no_op" with '
        'next_actions ["close_issue_as_completed"] and health.recommendation '
        '"EXIT_FOR_HUMAN_REVIEW" — write no plan, create no branch.\n'
        "4. Otherwise write a test-first plan: Phase 1 = tests to write "
        "first, Phase 2 = implementation, with exact repo-relative file "
        "paths. Read CLAUDE.md and ARCHITECTURE.md first if present; read the "
        "real code you plan to touch — never guess field or function names. "
        "The plan MUST contain:\n"
        "   - a `## Files Modified` section listing EVERY file to be created "
        "or modified, one bullet per file in the exact form `- <repo-relative "
        "path> (~<N> lines)` — a downstream gate parses this section "
        "mechanically, tests included;\n"
        "   - exactly one scope line `**Scope tier:** <small|large> (<N> "
        "files, ~<M> lines, forbidden_touched=<true|false>)` — small means "
        "at most 10 files AND at most 500 lines AND no touches to "
        "migrations, auth/security core, CI/CD pipeline behavior, or shared "
        "base classes with 3+ consumers; large otherwise;\n"
        "   - a `## Ambiguities` section: exactly `NO_AMBIGUITIES`, or each "
        "interpretive choice with the assumption adopted and why it is safe. "
        "An ambiguity only a human can settle → emit blocked with "
        'blocker.reason "plan_ambiguous" and the open question in '
        "blocker.details, rather than guessing.\n"
        "5. Write the full plan verbatim to .cw/plan.md in the worktree "
        "(the IMPL stage hard-requires this file), then post the same text "
        "as a ticket comment (the audit copy) when the tracker is writable.\n"
        "6. Exit status: scope small → stage_complete; scope large → "
        "plan_pending_approval (a human approves before implementation); "
        "already satisfied → no_op as above; anything unresolvable → blocked "
        "with a specific blocker.reason and details.\n"
        + _SENTINEL_RULES
        + _PLAN_SENTINEL_TEMPLATE
    )


def _impl_prompt(ticket_id: str) -> str:
    return (
        _stage_preamble("impl", ticket_id)
        + "Implement the approved plan for this ticket:\n"
        "1. Read .cw/plan.md — the approved plan. If absent, fetch the newest "
        "ticket comment containing the marker `<!-- plan-spec-reviewed` "
        f"(`gh issue view {ticket_id} --json comments`) and write its body to "
        ".cw/plan.md; if none exists either, emit blocked with "
        'blocker.reason "plan_missing".\n'
        "2. Sync: `git fetch origin <default-branch>` and merge "
        "origin/<default-branch> into the current branch (a merge conflict "
        'here is blocked with blocker.reason "impl_failed"). Record '
        "FORK_POINT=$(git merge-base origin/<default-branch> HEAD) and use "
        "it for every diff below.\n"
        "3. Work test-first: write the plan's Phase 1 tests, watch them "
        "fail, implement Phase 2, watch them pass. Stay within the plan's "
        "`## Files Modified` enumeration — unplanned files are scope drift "
        "a downstream gate blocks on.\n"
        "4. Commit incrementally with conventional messages — never one "
        "end-of-run commit for a non-trivial change. The final commit MUST "
        "carry the trailer `Auto-Dev-Stage: impl-complete` "
        '(`git commit --trailer "Auto-Dev-Stage: impl-complete" ...`) — the '
        "pipeline's resume detector reads it.\n"
        "5. Run the repo's quality gates before declaring done (read "
        "CLAUDE.md for the exact list; for Python typically `ruff check`, "
        "`ruff format --check`, `mypy`, `pytest`). A gate still failing "
        "after 2 fix attempts → emit blocked with blocker.reason "
        '"impl_failed" and the verbatim failure output in blocker.details. '
        "Never suppress a failure with `# noqa` / `# type: ignore` / test "
        "skips to get green.\n"
        "6. Push with an explicit refspec: `git push -u origin "
        "HEAD:refs/heads/<branch-name>` (branch naming: dev/<ticket-slug> "
        "unless the plan names one), then verify `git rev-parse "
        "origin/<branch-name>` matches `git rev-parse HEAD`. Unpushed work "
        "is lost work — the next stage reads only origin.\n"
        "7. Exit status: stage_complete (never shipped — IMPL does not "
        "create a PR; pr stays null) with branch, fork_point_sha, the pushed "
        "commit SHAs, and scope.lines_actual = added+removed from "
        "`git diff --stat $FORK_POINT`; otherwise blocked as above.\n"
        + _SENTINEL_RULES
        + _IMPL_SENTINEL_TEMPLATE
    )


def _review_prompt(ticket_id: str) -> str:
    return (
        _stage_preamble("review", ticket_id)
        + "Review the implementation branch for this ticket:\n"
        "1. Determine the default branch and record "
        "FORK_POINT=$(git merge-base origin/<default-branch> HEAD).\n"
        "2. Measure the diff first: if `git diff --numstat $FORK_POINT..HEAD` "
        "shows 0 files, emit status empty_diff_blocked with blocker.reason "
        '"empty_diff_no_commits" — a clean verdict over an empty branch '
        "reviewed nothing.\n"
        "3. Read .cw/plan.md if present. Review the ENTIRE diff "
        "adversarially: correctness, plan conformance, missing or weak "
        "tests, unhandled error paths, security. Run the repo's quality "
        "gates (read CLAUDE.md for the list). Classify each finding MUST_FIX "
        "or SHOULD_FIX and record the initial counts BEFORE fixing anything "
        "— those exact numbers go in the sentinel.\n"
        "4. Fix every MUST_FIX (at most 2 fix cycles), re-running tests and "
        "gates after each cycle; commit and push fixes with `git push origin "
        "HEAD:refs/heads/<branch-name>`. MUST_FIX findings still unresolved "
        'after 2 cycles → emit blocked with blocker.reason "review_blocked" '
        "and the findings verbatim in blocker.details.\n"
        "5. Resolve scope.tier: the `**Scope tier:**` line in .cw/plan.md if "
        "present, else derive from the diff (small = at most 10 files AND at "
        "most 500 lines AND no forbidden-area touches; large otherwise).\n"
        "6. Exit status: tier small with no unresolved MUST_FIX → "
        "stage_complete; tier large with no unresolved MUST_FIX → "
        "review_pending_approval (a human approves the ship). Set "
        "review.agents_run to 1 — you are the single reviewer; never report "
        "a review pass that did not happen.\n"
        + _SENTINEL_RULES
        + _REVIEW_SENTINEL_TEMPLATE
    )


def _finalize_prompt(ticket_id: str, worktree: Path) -> str:
    command_file = resolve_finalize_command_file(worktree)
    return (
        f"Run the auto-dev FINALIZE stage for ticket {ticket_id}. "
        f"Read and follow the instructions in {command_file} "
        f"(arguments: {ticket_id} --headless). "
        "The finalize flow runs: merge-gate check, PR creation, "
        "auto-merge enablement, read-back verification. "
        "When complete, emit the <<<AUTO_DEV_RESULT>>> sentinel with "
        "stage_reached set to stage4a_merge_gate, stage4b_pr_create, or "
        "stage5_post_create as appropriate."
    )


_STAGE_PROMPT_BUILDERS = {
    "plan": _plan_prompt,
    "impl": _impl_prompt,
    "review": _review_prompt,
}


def build_stage_prompt(stage_value: str, ticket_id: str, worktree: Path) -> str:
    """Build the opencode prompt for a supported auto-dev stage.

    FINALIZE points at the ``auto-dev-finalize.md`` command file (the one
    stage validated backend-neutral, #1670 R6), resolved worktree-first via
    ``resolve_finalize_command_file``. PLAN/IMPL/REVIEW get self-contained
    prompts carrying the stage's essential headless contract — their command
    files require Claude Code-only machinery an opencode subprocess cannot
    execute (see the shared-fragment note above). Raises ``KeyError`` for an
    unsupported stage value.
    """
    if stage_value == "finalize":
        return _finalize_prompt(ticket_id, worktree)
    return _STAGE_PROMPT_BUILDERS[stage_value](ticket_id)


def make_blocked(
    *,
    ticket_id: str,
    worktree: Path,
    reason: str,
    details: str = "",
    retry_eligible: bool | None = None,
    retry_delay_seconds: int | None = None,
    stage_reached: StageReached = "stage2_impl",
) -> AutoDevResult:
    """Return a typed blocked AutoDevResult for any OpencodeExecutor failure.

    Mirrors local_runner.make_blocked — opencode-specific ``next_actions`` only.

    Every production call site MUST pass ``stage_reached=stage_entry_marker(
    <stage value>)`` explicitly: the ``stage2_impl`` default exists only to
    mirror local_runner's signature (aider is IMPL-only, where it is always
    correct), and relying on it from a multi-stage caller mis-stages the
    failure — dispatch walks ``task.stage`` forward on a later-stage sentinel
    (see ``_STAGE_ENTRY_MARKERS``).
    """
    # §3.3 stage-coupled invariant (auto_dev_result/schema.py): a pre-impl
    # stage_reached requires scope.lines_actual to be null, post-impl requires
    # it non-null — so the fixed blocked scope must flip per marker.
    exited_pre_impl = stage_reached in ("stage1_plan", "stage1_pre_flight")
    scope = (
        _blocked_scope.model_copy(update={"lines_actual": None})
        if exited_pre_impl
        else _blocked_scope
    )
    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=ticket_id,
        status="blocked",
        stage_reached=stage_reached,
        scope=scope,
        plan_source="none",
        review=_FIXED_REVIEW,
        health=_FIXED_HEALTH,
        blocker=Blocker(
            stage=stage_reached,
            reason=reason,
            details=details,
            retry_eligible=retry_eligible,
            retry_delay_seconds=retry_delay_seconds,
        ),
        next_actions=_FIXED_NEXT_ACTIONS,
        worktree_path=str(worktree),
    )


def extract_text_from_jsonl(log_content: str) -> str:
    """Parse opencode JSONL events and return concatenated text content.

    opencode's ``--format json`` stream emits events with a ``type`` field.
    Text content lives in ``text`` events:
    ``{"type": "text", "part": {"text": "..."}}``. The sentinel
    (``<<<AUTO_DEV_RESULT>>>``) is embedded in these text events. Returns the
    concatenation of all text event payloads, or empty string if no text events
    are found or the JSONL is unparseable.
    """
    texts: list[str] = []
    for raw_line in log_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "".join(texts)


def _persist_opencode_no_output_diagnostics(*, session_id: str, log_tail: str) -> None:
    """Write a ``missing_output`` diagnostics bundle for an OPENCODE_NO_OUTPUT harvest.

    Mirrors local_runner._persist_aider_no_output_diagnostics. Never raises
    (persist swallows OSError).
    """
    failure = build_executor_failure(
        category="missing_output",
        executor_name="opencode",
        session_id=session_id,
        argv=[],
        stdout_excerpt=log_tail,
        stderr_excerpt="",
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="opencode",
        failure=failure,
    )


def synthesize_opencode_result(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str | None = None,
) -> AutoDevResult:
    """Harvest the opencode JSONL log for a sentinel (#1669).

    Called by reconcile/local harvest AFTER the fire-and-forget opencode process
    has exited. Reads ``.cw/opencode.log``, parses the JSONL event stream,
    extracts text content, and feeds it to ``parse_stdout`` for sentinel
    extraction.

    - sentinel found in text → the parsed ``AutoDevResult``
    - no sentinel / empty log / unparseable → ``OPENCODE_NO_OUTPUT`` (blocked,
      ``retry_eligible``, details from the log tail when readable). The
      synthesized blocker carries ``task.stage``'s own entry marker so a
      PLAN-stage no-output failure never reads as a later-stage
      self-escalation (see ``_STAGE_ENTRY_MARKERS``).

    *session_id* is optional: when set, the ``OPENCODE_NO_OUTPUT`` branch also
    persists a typed ``missing_output`` diagnostics bundle and appends a
    ``[diagnostics: <path>]`` pointer.
    """
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_content = ""
    with contextlib.suppress(OSError):
        log_content = log_path.read_text(encoding="utf-8", errors="replace")

    if log_content:
        text = extract_text_from_jsonl(log_content)
        if text:
            result = parse_stdout(text)
            if isinstance(result, AutoDevResult):
                return result

    details = log_content[-_OPENCODE_LOG_TAIL_CHARS:] if log_content else ""
    if session_id is not None:
        _persist_opencode_no_output_diagnostics(session_id=session_id, log_tail=details)
        details = append_diagnostics_pointer(details, session_id=session_id)
    return make_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=OPENCODE_NO_OUTPUT,
        details=details,
        retry_eligible=True,
        retry_delay_seconds=0,
        stage_reached=stage_entry_marker(task.stage.value),
    )
