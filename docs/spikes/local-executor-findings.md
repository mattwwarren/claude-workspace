# Local Executor Spike Findings — RFC 0005 F3

**Date:** 2026-06-28
**Branch:** agent-a9d943a277aa2f64d (spike worktree)
**Probe script:** `scripts/probe_executor.py`
**Endpoint:** `http://192.168.4.24:1234/v1` (LM Studio, OpenAI-compatible)

## Summary

The spike answers: can a locally-hosted coding model drive one IMPL stage and
emit a schema-valid `AutoDevResult` sentinel?

**Answer: Yes — with a compact example-based prompt, NOT the full JSON schema.**
Both `qwen2.5-coder-32b-instruct` and `mistralai/devstral-small-2-2512` passed
on the first attempt. `zai-org/glm-4.7-flash` fails due to a CoT/thinking-only
mode that produces empty output text.

## Task used for all runs

"Add a Google-style docstring to `resolve_executor_config` in
`src/cw/executor.py`." This is a self-contained ~15-line docstring addition —
trivially small, no state changes, isolates sentinel emission from task
complexity.

## Probe infrastructure

The harness (`scripts/probe_executor.py`) is config-driven and reusable:

- `--base-url` / `--model` / `--api-key` parameterize the endpoint
- `--compact-schema` switches between full JSON schema (~12k chars) and a
  compact example template (~1k chars)
- Validation reuses cw's own surfaces — no reimplementation:
  - Schema: `uv run cw schema stage-output <stage>`
  - Coercion + validation: `cw.auto_dev_result.parse_stdout` (which calls
    `_normalize_payload` → `AutoDevResult.model_validate`)
  - CLI gate: `uv run cw result validate -` run on the normalized JSON as a
    secondary confirmation
- HTTP: `urllib.request` (no httpx/openai in project venv)

---

## RFC risk question 1 — Sentinel validity

### `qwen2.5-coder-32b-instruct`

**Full schema (12,199 chars inlined):** FAIL — timeout >300s on every attempt.
The 13.9 kB request body (~3.5k tokens) caused LM Studio to take >5 minutes
generating a response. The model was callable with short prompts (responds in
<2s for "Reply: OK"), so this is an inference-time issue with the large
context, not a loading issue.

**Compact example prompt (~1,800 bytes, ~450 tokens):** PASS on attempt 1.

```
latency:  129.6s (model cold-load ~47s + generation ~83s)
attempts: 1
schema:   compact-example
```

Raw sentinel emitted (verbatim):

```
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "PROBE-001",
  "status": "stage_complete",
  "stage_reached": "stage2_impl",
  "plan_source": "free_text",
  "scope": {
    "tier": "small",
    "files": 1,
    "lines_estimate": 15,
    "lines_actual": 15,
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
}
AUTO_DEV_RESULT>>>
```

`parse_stdout` → `AutoDevResult.model_validate` → `cw result validate` all
passed without triggering any `_normalize_payload` coercion branch. No repair
needed.

The model also generated the correct docstring in prose before the sentinel
(not just copying the example template).

### `mistralai/devstral-small-2-2512`

**Full schema:** Bad Request (HTTP 400) on attempt 1, then Internal Server
Error (HTTP 500) on attempt 2 — consistent with the model's context window
being too small for the 13.9 kB system prompt, or LM Studio rejecting it.

**Compact example prompt:** PASS on attempt 1.

```
latency:  54.5s
attempts: 1
schema:   compact-example
```

Raw sentinel was structurally identical to qwen32b output. `lines_actual: 7`
vs qwen32b's `15` — both are model-estimated, not git-verified.

### `zai-org/glm-4.7-flash`

**All prompt sizes:** FAIL. GLM generates 0 output tokens (empty `content`
field) while reporting 5 `reasoning_tokens` in usage stats. This is a
thinking/CoT-only model variant that externally exposes internal reasoning but
produces no final answer. On 300s timeout → second attempt gets HTTP 400
(Bad Request), presumably because LM Studio is in a degraded state from the
prior aborted call.

```
attempt 1: timeout after 300s
attempt 2: HTTP 400 Bad Request
```

This is not a sentinel format issue — the model produces no text output at
all. The LocalExecutor backend would need to detect CoT-only models (via
`reasoning_tokens > 0` and empty `content`) and either use a different model
variant or skip them.

---

## RFC risk question 2 — Shim shape

The raw sentinel from both passing models was already schema-valid. No
`_normalize_payload` coercions fired. This means no
`_coerce_local_output()` analogous to `_normalize_payload` is needed for the
structural schema invariants.

However, the LocalExecutor backend (PR2) will need a **scope-completion shim**
that fills in fields the model cannot know without worktree inspection:

**Fields the model cannot fill reliably (must be shimmed from worktree facts):**

- `commits` — model outputs `[]`; real executor should populate from `git log`
  after applying the code change
- `branch` — model outputs `null`; real executor knows the worktree branch from
  `git rev-parse --abbrev-ref HEAD`
- `worktree_path` — model outputs `null`; the executor owns this path
- `fork_point_sha` — model outputs `null`; derive from `git merge-base HEAD
  origin/main`
- `scope.files` and `scope.lines_actual` — model gives a plausible estimate
  but not a verified count; real executor should compute from `git diff
  --stat` after applying the change
- `scope.forbidden_touched` — model hardcodes `false`; real executor should
  check the changed files against the forbidden-area config

**Fields the model got right without shimming:**

- All structural invariants (`status`, `stage_reached`, `plan_source`,
  `schema_version`)
- `health.recommendation`, `health.lowest_agent_confidence`
- `review.*` fields (set to 0, correct for an IMPL stage sentinel)
- `scope.tier`
- Null fields (`pr`, `blocker`) — both models respected the invariants

**Recommended shim call site:** After `parse_stdout` returns an
`AutoDevResult`, before passing it to the stage-advance logic, apply:

```python
def _coerce_local_output(
    result: AutoDevResult,
    worktree: Path,
    branch: str,
    fork_point_sha: str,
) -> AutoDevResult:
    """Back-fill worktree-inspectable fields the local model cannot know."""
    diff_stat = subprocess.check_output(
        ["git", "diff", "--stat", f"{fork_point_sha}..HEAD"],
        cwd=worktree,
        text=True,
    )
    files_changed, lines_added = _parse_diff_stat(diff_stat)
    commits = subprocess.check_output(
        ["git", "log", "--format=%H", f"{fork_point_sha}..HEAD"],
        cwd=worktree, text=True,
    ).splitlines()
    forbidden = _check_forbidden_area(worktree, client.forbidden_areas)
    return result.model_copy(
        update={
            "branch": branch,
            "worktree_path": str(worktree),
            "fork_point_sha": fork_point_sha,
            "commits": commits,
            "scope": result.scope.model_copy(
                update={
                    "files": files_changed,
                    "lines_actual": lines_added,
                    "forbidden_touched": forbidden,
                }
            ),
        }
    )
```

This keeps `_normalize_payload` (parse-boundary leniency for format drift)
separate from `_coerce_local_output` (semantic gap filling from worktree
state). They have different call sites and different contracts.

---

## RFC risk question 3 — Supervision and handoff

**Synchronous HTTP, no streaming.** The local model call is a single blocking
`urllib.request.urlopen`. For a 32B model this blocks the caller thread for
120–200s. PR2's `LocalExecutor.spawn()` should run the HTTP call on a
thread/executor (e.g. `asyncio.to_thread` or `ThreadPoolExecutor`) and use
a wall-clock budget that includes model load time.

**Cold-load latency is real.** LM Studio loads models from disk on first
request; qwen32b takes ~47s to load before generation begins. The
`wall_clock_budget_seconds` passed by the dispatch loop must budget for this.
Rule of thumb from observed data:
- 30B–32B models: 47s load + 80–130s generation = 180–200s minimum budget
- 20B–25B models (Devstral): 54s total observed (possibly already warm)
- Flash/reasoning models (GLM): may produce 0 output tokens — detect and fail
  fast rather than waiting the full budget

**Single-model concurrency.** LM Studio (and most local backends) load one
model at a time. Concurrent requests to different models cause HTTP 400/500
errors during model swap. The `LocalExecutor` must either:
(a) serialize all requests through a per-endpoint lock, or
(b) accept that only one lane can use a given endpoint at a time (enforced by
`max_parallel` lane config)

**Artifact handoff.** The sentinel provides `commits`, `branch`, and
`worktree_path` for the next stage to resume from. For the local executor,
these are backfilled by `_coerce_local_output` (above). The next stage (REVIEW
or FINALIZE) reads these from the sentinel; it does NOT need to re-examine the
worktree path from the spawner. This contract is the same as the
`ClaudeNativeExecutor` path.

**No streaming needed.** The sentinel is always at the END of the model's
output. Streaming would complicate sentinel extraction with no benefit. The
probe confirmed that `parse_stdout` on the complete response body is the right
call site.

---

## Prompt strategy for PR2

Use the compact example mode (`--compact-schema`) for all local models. The
full JSON schema (12,199 chars) causes:
- 32B models: >300s timeout (inference stalls on large context)
- Small models: HTTP 400 (context window exceeded for some models)

The compact prompt is ~1,800 bytes (~450 tokens). Models reliably fill in the
numeric fields (scope.files, lines_estimate, lines_actual) from task context.

Invariants the compact prompt must explicitly state (models respect these):
- `pr: null`
- `blocker: null`
- `next_actions: []`
- `scope.tier: "small" | "large"` (not null)
- `scope.lines_actual: <integer>` (not null)
- `health.lowest_agent_confidence: "HIGH" | "MEDIUM" | "LOW"` (not null)

**cw surfaces the LocalExecutor should reuse (not reimplement):**
- Schema: `uv run cw schema stage-output <stage>` for reference
- Validation: `cw.auto_dev_result.parse_stdout` (coercion + validate in one call)
- CLI gate: `cw result validate -` for operator debugging

---

## Run log

| model | schema | attempts | elapsed | result |
|---|---|---|---|---|
| qwen2.5-coder-32b-instruct | full (12k) | 3 | 240+s timeout | FAIL |
| zai-org/glm-4.7-flash | full (12k) | 3 | 0s (400/500) | FAIL |
| mistralai/devstral-small-2-2512 | full (12k) | 2 | 0s (400/500) | FAIL |
| qwen2.5-coder-32b-instruct | compact | 1 | 129.6s | **PASS** |
| mistralai/devstral-small-2-2512 | compact | 1 | 54.5s | **PASS** |
| zai-org/glm-4.7-flash | compact | 2 | 300s + 400 | FAIL (CoT mode) |
