"""RFC 0005 F3 de-risking probe — local executor sentinel harness.

Drives ONE pipeline stage (default: impl) against an OpenAI-compatible HTTP
endpoint and validates the resulting AutoDevResult sentinel via cw's own
coercion/validation stack.  Designed to be re-run against different
endpoints and models as hardware or service endpoints change.

Usage
-----
    uv run scripts/probe_executor.py [options]

Validation surfaces reused (not reimplemented)
----------------------------------------------
- Schema:      cw schema stage-output <stage>
- Coercion:    cw.auto_dev_result.parse_stdout  (_normalize_payload inside)
- Validation:  cw.auto_dev_result.AutoDevResult.model_validate (via parse_stdout)
- Gate CLI:    uv run cw result validate -   (authoritative pre-emit gate)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPEN_SENTINEL = "<<<AUTO_DEV_RESULT"
_CLOSE_SENTINEL = "AUTO_DEV_RESULT>>>"

_STAGE_TO_REACHED: dict[str, str] = {
    "impl": "stage2_impl",
    "review": "stage3_review",
    "plan": "stage1_plan",
}

_STAGE_TO_STATUS: dict[str, str] = {
    "impl": "stage_complete",
    "review": "stage_complete",
    "plan": "plan_pending_approval",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_executor",
        description=(
            "RFC 0005 F3 — probe a local coding model for schema-valid "
            "AutoDevResult sentinel emission."
        ),
    )
    p.add_argument(
        "--base-url",
        default="http://192.168.4.24:1234/v1",
        help="Base URL of the OpenAI-compatible endpoint (default: LM Studio on"
        " 192.168.4.24)",
    )
    p.add_argument(
        "--model",
        default="qwen2.5-coder-32b-instruct",
        help="Model id to call (default: qwen2.5-coder-32b-instruct)",
    )
    p.add_argument(
        "--api-key",
        default="lm-studio",
        help="API key header value (dummy is fine for LM Studio, default: lm-studio)",
    )
    p.add_argument(
        "--stage",
        default="impl",
        choices=list(_STAGE_TO_REACHED.keys()),
        help="Pipeline stage to probe (default: impl)",
    )
    p.add_argument(
        "--task-file",
        default=None,
        help=(
            "Path to a file whose contents are used as the task description. "
            "If omitted, uses a built-in trivial impl task."
        ),
    )
    p.add_argument(
        "--task",
        default=None,
        help="Inline task description (overrides --task-file and the built-in task).",
    )
    p.add_argument(
        "--ticket-id",
        default="PROBE-001",
        help="Ticket id to embed in the sentinel (default: PROBE-001)",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum prompt-repair retries on validation failure (default: 3)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="HTTP call timeout in seconds (default: 180)",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Sampling temperature (default: 0.1 — low for deterministic JSON)",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Max tokens to generate per call (default: 4096)",
    )
    p.add_argument(
        "--compact-schema",
        action="store_true",
        help=(
            "Use a compact example-based sentinel template instead of the full "
            "AutoDevResult JSON schema. Reduces prompt size from ~12k to ~1k chars — "
            "necessary for models with small context windows or slow inference on "
            "large prompts. The model fills in numeric fields; structural validity "
            "is still checked via parse_stdout."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print raw model output on each attempt.",
    )
    return p


# ---------------------------------------------------------------------------
# Schema fetch (reuses cw CLI, no reimplementation)
# ---------------------------------------------------------------------------


def _fetch_schema(stage: str) -> str:
    """Return the JSON schema string for the given stage via cw schema stage-output."""
    result = subprocess.run(
        ["uv", "run", "cw", "schema", "stage-output", stage],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# HTTP call
# ---------------------------------------------------------------------------


def _chat_completions(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: int,
    temperature: float,
    max_tokens: int,
) -> tuple[str, float]:
    """POST /chat/completions; return (content, elapsed_seconds).

    Uses stdlib urllib — no openai/httpx required.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read()
    elapsed = time.monotonic() - t0
    data = json.loads(raw)
    content: str = data["choices"][0]["message"]["content"]
    return content, elapsed


# ---------------------------------------------------------------------------
# Validation via cw's own stack (parse_stdout + cw result validate)
# ---------------------------------------------------------------------------


def _validate_with_cw(content: str) -> tuple[bool, str, str]:
    """Validate model output via cw.auto_dev_result.parse_stdout.

    Returns (passed, inner_json_or_empty, error_details).
    Reuses cw's coercion stack (parse_stdout → _normalize_payload → model_validate).
    Also runs the authoritative CLI gate (cw result validate) on success to
    double-check the inner JSON independently.
    """
    # Import here (inside uv run context) so the probe can run standalone.
    try:
        from cw.auto_dev_result import (  # noqa: PLC0415
            AutoDevResult,
            BlockedResult,
            parse_stdout,
        )
    except ImportError as exc:
        return False, "", f"cw import failed: {exc}"

    result = parse_stdout(content)
    if isinstance(result, BlockedResult):
        blocker = result.blocker
        return False, "", f"parse_stdout → BlockedResult: {blocker}"
    if not isinstance(result, AutoDevResult):
        return False, "", f"parse_stdout returned unexpected type: {type(result)}"

    inner_json = result.model_dump_json(indent=2)

    # Secondary gate: cw result validate (CLI) for independent confirmation.
    cli_result = subprocess.run(
        ["uv", "run", "cw", "result", "validate", "-"],
        input=inner_json,
        capture_output=True,
        text=True,
        check=False,
    )
    if cli_result.returncode != 0:
        cli_err = (cli_result.stderr or cli_result.stdout).strip()
        return False, inner_json, f"cw result validate rejected: {cli_err}"

    return True, inner_json, ""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _default_task(stage: str) -> str:
    """Return a trivial, self-contained task for the given stage."""
    if stage == "impl":
        return textwrap.dedent(
            """\
            Add a Google-style docstring to the `resolve_executor_config`
            function in `src/cw/executor.py`.

            The function signature is:
                def resolve_executor_config(
                    stage: Stage,
                    task: TicketTask,
                    client: ClientConfig,
                ) -> StageExecutorConfig:

            The existing comment above it reads:
                "Return the effective StageExecutorConfig for a stage, with
                lane override (E1). Three-level priority: lane stage config >
                client stage config > default."

            That comment should become a proper Google-style docstring.
            No other code changes are required.
            """
        )
    return f"Perform a trivial {stage} stage task for probe purposes."


def _build_repair_hint(errors: str) -> str:
    """Return a repair instruction to append on retry."""
    return textwrap.dedent(
        f"""\
        Your previous response failed sentinel validation with these errors:

        {errors}

        Common fixes:
        - Make sure schema_version is an integer (not a string).
        - status must be exactly "stage_complete" for a completed impl stage.
        - stage_reached must be exactly "stage2_impl".
        - scope.tier must be "small" or "large" (not null).
        - scope.lines_actual must be a non-null integer.
        - health.lowest_agent_confidence must be "HIGH", "MEDIUM", or "LOW".
        - health.recommendation must be "PROCEED" or "EXIT_FOR_HUMAN_REVIEW".
        - pr must be null (not shipped).
        - blocker must be null (not blocked).
        - next_actions must NOT contain "wait_for_ci".
        - The sentinel block MUST use the exact markers:
            <<<AUTO_DEV_RESULT
            {{ ... json ... }}
            AUTO_DEV_RESULT>>>

        Emit ONLY the sentinel block, no prose before or after.
        """
    )


_COMPACT_SENTINEL_TEMPLATE = """{
  "schema_version": 4,
  "ticket_id": "PROBE-001",
  "status": "stage_complete",
  "stage_reached": "stage2_impl",
  "plan_source": "free_text",
  "scope": {
    "tier": "small",
    "files": <COUNT_OF_CHANGED_FILES>,
    "lines_estimate": <ESTIMATED_LINES>,
    "lines_actual": <ACTUAL_LINES_CHANGED>,
    "forbidden_touched": false
  },
  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
  "health": {
    "any_incomplete_risk": false,
    "recommendation": "PROCEED",
    "lowest_agent_confidence": "HIGH",
    "shortcuts": [],
    "agent_health_summary": []
  },
  "commits": [],
  "next_actions": [],
  "friction_highlights": [],
  "pr": null,
  "blocker": null,
  "branch": null,
  "worktree_path": null,
  "fork_point_sha": null
}"""


def _build_compact_system(stage: str, ticket_id: str) -> str:
    """Return a compact example-based system prompt (~1k chars vs 13k for full schema).

    Use when the model's effective context window or inference speed cannot handle
    the full AutoDevResult JSON schema (~12k chars).  Structurally identical output;
    numeric fields are filled by the model from the task context.
    """
    stage_reached = _STAGE_TO_REACHED[stage]
    expected_status = _STAGE_TO_STATUS[stage]
    template = _COMPACT_SENTINEL_TEMPLATE.replace('"PROBE-001"', f'"{ticket_id}"')
    template = template.replace('"stage_complete"', f'"{expected_status}"')
    template = template.replace('"stage2_impl"', f'"{stage_reached}"')
    return textwrap.dedent(
        f"""\
        You are a coding agent completing the {stage.upper()} stage.

        After completing your work, emit EXACTLY this sentinel (fill in the
        <PLACEHOLDER> values with real numbers; keep everything else verbatim):

        <<<AUTO_DEV_RESULT
        {template}
        AUTO_DEV_RESULT>>>

        CRITICAL INVARIANTS — violating these will fail machine validation:
        - pr MUST be null
        - blocker MUST be null
        - next_actions MUST be an empty list []
        - scope.lines_actual MUST be a non-null integer
        - scope.tier MUST be "small" or "large"
        - health.lowest_agent_confidence MUST be "HIGH", "MEDIUM", or "LOW"

        Emit ONLY the sentinel block. No prose before or after.
        """
    )


def _build_full_system(stage: str, ticket_id: str, schema_json: str) -> str:
    """Return the full JSON-schema system prompt (~13k chars).

    Use when the model has sufficient context capacity and inference budget.
    The authoritative AutoDevResult JSON schema is inlined verbatim so the
    model can self-validate against it.
    """
    stage_reached = _STAGE_TO_REACHED[stage]
    expected_status = _STAGE_TO_STATUS[stage]
    return textwrap.dedent(
        f"""\
        You are a coding agent completing the {stage.upper()} stage of an
        automated software pipeline.

        After completing your work, you MUST emit a single structured sentinel
        block to signal completion. The sentinel is machine-parsed; format it
        EXACTLY as shown.

        SENTINEL FORMAT
        ===============
        Emit the block using these exact delimiters (no extra spaces or text):

            <<<AUTO_DEV_RESULT
            {{ <JSON object> }}
            AUTO_DEV_RESULT>>>

        The JSON object must conform to this schema (AutoDevResult):

        {schema_json}

        REQUIRED FIELD VALUES FOR THIS RUN
        ===================================
        - schema_version: 4
        - ticket_id: "{ticket_id}"
        - status: "{expected_status}"
        - stage_reached: "{stage_reached}"
        - plan_source: "free_text"
        - scope.tier: "small"  (required for post-impl; must be "small" or "large")
        - scope.files: <count of files actually changed>
        - scope.lines_estimate: <lines estimated before impl>
        - scope.lines_actual: <lines actually changed; required non-null for post-impl>
        - scope.forbidden_touched: false
        - review.must_fix_initial: 0
        - review.should_fix: 0
        - review.fix_cycles_used: 0
        - health.any_incomplete_risk: false
        - health.recommendation: "PROCEED"
        - health.lowest_agent_confidence: "HIGH"  (required for post-impl)
        - health.shortcuts: []
        - health.agent_health_summary: []
        - commits: []   (no git commits in this probe run)
        - next_actions: []
        - friction_highlights: []

        Optional fields you may omit: branch, worktree_path, fork_point_sha,
        pr, pr_created, blocker, ambiguities, premises, cost_usd.

        CRITICAL INVARIANTS (will fail validation if violated)
        ======================================================
        - pr MUST be null (only non-null when status="shipped")
        - blocker MUST be null (only non-null when status="blocked")
        - next_actions MUST NOT contain "wait_for_ci" (only for status="shipped")
        - scope.lines_actual MUST be a non-null integer (post-impl requirement)
        - scope.tier MUST be non-null (post-impl requirement)
        - health.lowest_agent_confidence MUST be non-null (post-impl requirement)

        After completing the task, emit ONLY the sentinel block. No prose.
        """
    )


def _build_messages(
    *,
    stage: str,
    ticket_id: str,
    task_desc: str,
    schema_json: str,
    repair_hint: str | None,
    compact: bool = False,
) -> list[dict[str, str]]:
    """Build the messages list for the chat completions call."""
    if compact:
        system = _build_compact_system(stage, ticket_id)
    else:
        system = _build_full_system(stage, ticket_id, schema_json)

    user_content = f"Task:\n\n{task_desc}"
    if repair_hint:
        user_content += f"\n\n---\nREPAIR REQUIRED:\n{repair_hint}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Main probe loop
# ---------------------------------------------------------------------------


def _run_attempt(
    attempt: int,
    args: argparse.Namespace,
    task_desc: str,
    schema_json: str,
    repair_hint: str | None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Execute one probe attempt; return (done, next_repair_hint, log_entry).

    done=True means PASS (caller should stop the loop and print the result).
    """
    print(f"[probe] attempt {attempt}/{args.max_attempts} ...", flush=True)
    messages = _build_messages(
        stage=args.stage,
        ticket_id=args.ticket_id,
        task_desc=task_desc,
        schema_json=schema_json,
        repair_hint=repair_hint,
        compact=args.compact_schema,
    )
    try:
        content, elapsed = _chat_completions(
            args.base_url,
            args.api_key,
            args.model,
            messages,
            timeout=args.timeout,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        print(f"[probe] HTTP error: {reason}", file=sys.stderr)
        entry: dict[str, Any] = {
            "attempt": attempt,
            "elapsed": 0.0,
            "passed": False,
            "error": reason,
        }
        return False, f"HTTP call failed: {reason}", entry
    except TimeoutError:
        msg = f"timeout after {args.timeout}s"
        print(f"[probe] {msg}", file=sys.stderr)
        entry = {
            "attempt": attempt,
            "elapsed": float(args.timeout),
            "passed": False,
            "error": msg,
        }
        return False, msg, entry

    print(f"[probe] response received in {elapsed:.1f}s")
    if args.verbose:
        print("--- raw response ---")
        print(content)
        print("--------------------")

    passed, inner_json, error = _validate_with_cw(content)
    entry = {
        "attempt": attempt,
        "elapsed": round(elapsed, 2),
        "passed": passed,
        "error": error,
    }
    if passed:
        print(f"\n✓ PASS on attempt {attempt} ({elapsed:.1f}s)")
        print("\n--- validated sentinel (normalized JSON) ---")
        print(inner_json)
        print("--------------------------------------------")
        return True, None, entry

    print(f"[probe] attempt {attempt} FAILED: {error}")
    return False, _build_repair_hint(error), entry


def _resolve_task(args: argparse.Namespace) -> str:
    """Return the task description from --task, --task-file, or the built-in default."""
    if args.task:
        return args.task
    if args.task_file:
        return Path(args.task_file).read_text()
    return _default_task(args.stage)


def _probe(args: argparse.Namespace) -> int:
    """Run the probe; return exit code (0=PASS, 1=FAIL)."""
    task_desc = _resolve_task(args)

    schema_mode = "compact-example" if args.compact_schema else "full-json-schema"
    print(f"[probe] endpoint:  {args.base_url}")
    print(f"[probe] model:     {args.model}")
    print(f"[probe] stage:     {args.stage}")
    print(f"[probe] schema:    {schema_mode}")
    print(f"[probe] ticket_id: {args.ticket_id}")
    print(f"[probe] task:\n{textwrap.indent(task_desc.strip(), '    ')}")
    print()

    print("[probe] fetching schema via cw schema stage-output ...", flush=True)
    schema_json = _fetch_schema(args.stage)
    print(f"[probe] schema fetched ({len(schema_json)} chars)")
    print()

    repair_hint: str | None = None
    attempt_log: list[dict[str, Any]] = []

    for attempt in range(1, args.max_attempts + 1):
        done, repair_hint, entry = _run_attempt(
            attempt, args, task_desc, schema_json, repair_hint
        )
        attempt_log.append(entry)
        if done:
            _print_summary(attempt_log)
            return 0

    print(f"\n✗ FAIL — all {args.max_attempts} attempts failed")
    _print_summary(attempt_log)
    return 1


def _print_summary(attempt_log: list[dict[str, Any]]) -> None:
    """Print a concise attempt table."""
    print("\n--- attempt summary ---")
    for entry in attempt_log:
        status_tag = "PASS" if entry["passed"] else "FAIL"
        err_snippet = str(entry.get("error", ""))[:120]
        print(
            f"  attempt {entry['attempt']}: {status_tag} "
            f"({entry['elapsed']}s) {err_snippet}"
        )
    print("-----------------------")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the probe and exit with the appropriate code."""
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(_probe(args))


if __name__ == "__main__":
    main()
