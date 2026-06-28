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

---

# F3b — Execution-model assessment (#885)

**Date:** 2026-06-28 (session 2)
**Question:** does the `LocalExecutor` **delegate** to an existing headless
coding agent pointed at the local model (Path B), or does cw **wrap raw LLM
calls** in its own tool loop (Path A)? And if delegation, which harness?

## Why the F3 spike did not settle this

The F3 probe (above) made **raw `/chat/completions` calls only** — one model
turn + a JSON-repair retry. It proved a local model can *emit* a schema-valid
`AutoDevResult`, but `scope.files` / `commits` / `lines` were model-**asserted
strings**, not real artifacts. It exercised **no** tool-use loop, file edits,
test runs, or `git apply`. Critically, `ClaudeNativeExecutor` does **not** make
raw LLM calls — it spawns `claude --bg`, a full coding **harness**. So the raw
probe validated a *weaker, structurally different* path than the one it mirrors.

## Decision: **Path B — delegate to a headless coding agent (aider)**

Confirmed by an end-to-end extended spike (below). Rationale:

- **Structural parity with `claude-native`.** That executor spawns a harness
  (`claude --bg`) that owns the tool loop; the local path should mirror it —
  spawn a harness, let it own edit/apply/test/commit, cw harvests the result.
- **Path A reinvents a coding agent.** Owning the tool-use loop (feed files,
  parse edits, apply, run tests, iterate) inside cw is a large build for no gain
  over mature OSS agents that already do it against an OpenAI-compatible
  endpoint.
- The extended spike showed delegation works **first try** on the same LM Studio
  endpoint with real edits, a real commit, and passing tests.

## Harness: **aider** (Goose = fallback)

Selected from an adversarially-verified shortlist (criteria: local /
OpenAI-compatible endpoint · headless one-shot · subprocess-harvestable ·
agentic file edits · OSS):

- **aider** ⭐ — `OPENAI_API_BASE=<host>/v1 --model openai/<id>`; headless
  `--message "task" --yes --auto-commits`; harvest via `git log`/`git diff`.
  Python, MIT. Longest-established headless/CI story; most predictable
  subprocess behavior. **Passed the extended spike.**
- **Goose** (Block) — `goose run -t "prompt"`, git-diff harvest; Rust,
  Apache-2.0. Natural fallback if a given local model's edit quality is weak for
  aider's SEARCH/REPLACE discipline. *(Not installed on this host; untested.)*
- **OpenCode** (sst) — `opencode run --format json`; cleanest structured output,
  but watch TTY/permission-prompt hangs in CI (upstream issue #10411).

**Disqualified:** Codex CLI (drives a *local* endpoint via the Responses API,
which most local providers don't implement — **note:** does NOT disqualify
Codex for #627's *hosted* review pass), Continue CLI (autocomplete, not
agentic), Cline / open-interpreter / RA.Aid (subprocess/harvest behavior
unverified), Plandex (abandoned), Amp / Sweep (hosted-/proprietary-only).

## Extended-spike run (the proof)

**Setup:** throwaway git repo, one stubbed function (`roman_to_int`, raises
`NotImplementedError`) + 3 failing pytest cases (subtractive notation).
**Harness:** `aider 0.86.2`, model `openai/qwen2.5-coder-32b-instruct`,
endpoint `http://192.168.4.24:1234/v1`.

```
aider --model openai/qwen2.5-coder-32b-instruct \
  --message "Implement roman_to_int ... so all tests pass." \
  --yes --auto-commits --no-auto-lint --no-auto-test \
  --map-tokens 0 --no-stream roman.py test_roman.py
```

**Result — full `spawn → handoff → schema-valid sentinel` contract survived:**

- aider applied a real SEARCH/REPLACE edit to `roman.py` (22 insertions,
  1 deletion) — verified working logic, not a copy of the stub.
- aider auto-committed it: `7109c11 feat: implement roman_to_int function`.
- All 3 tests **pass** (correctness, not just edits): `3 passed`.
- Harvested an `AutoDevResult` from **git facts** (commit hash from `git log`,
  `branch` from `rev-parse`, `fork_point_sha`, `scope.files`/`lines_actual`
  from `git diff --stat`) → `cw result validate -` returned **exit 0**.
- Tokens: 2.6k sent / 352 received (one model turn). Warm latency **149s**.

**Latency caveat (new):** the *cold-load* attempt was killed at 2 min before any
model turn even started (aider startup + LM Studio cold-load > 2 min). The
delegation budget must cover **aider start + model cold-load + N model turns**
(aider may take several turns on a real task) — budget generously (≥600s for a
32B model on a non-trivial task), well above the raw-call 200–250s figure from
F3.

## Implications for the #866 backend (PR2) — RESHAPES the F3 draft

The F3-era draft resolutions assumed **Path A** (raw LLM): an `openai`-SDK HTTP
client in cw, prompt-engineered compact schema, `parse_stdout` on the model's
own sentinel, `_coerce_local_output` as a *backfill*. Path B changes all of
that:

- **No in-cw HTTP client / tool loop / `openai` SDK dependency.** cw spawns an
  **aider subprocess** (parallel to how `spawn.py` launches `claude --bg`).
- **The model never emits the sentinel.** aider produces a *commit*; cw
  **synthesizes** the `AutoDevResult` from git state after aider exits. So
  `_coerce_local_output` is promoted from a backfill shim to the **primary
  sentinel builder** — it is now the whole sentinel-construction path, not a
  patch over model output. The compact-schema prompt work from F3 is **not
  needed** for IMPL under Path B.
- **`StageExecutorConfig`** still needs `endpoint` + `model`; **plus** aider
  invocation config (binary path, flag set, per-turn/wall-clock budget).
- **Tests:** unit = mock the subprocess + a fixture git repo (CI-safe, no model
  / no endpoint); integration = guarded/skipped in CI behind an env gate (e.g.
  `INTEGRATION_LOCAL_ENDPOINT`), mirroring the F3 `INTEGRATION_REAL_API`
  pattern. Reuse the #222 fake-binary seam for the CI dogfood.
- **Failure modes to handle:** aider exits 0 but makes **no commit** (model
  refused / produced no applicable edit) → emit `blocked`, not a phantom
  `stage_complete`; aider timeout (cold-load + turns) → `blocked` with a
  budget-exceeded reason; CoT-only models (GLM, per F3) still produce no usable
  output — detect and fail fast.

## Constraints honored

- **LAN-only model, no model in CI** — the extended spike ran against the LAN
  endpoint; PR2 integration tests stay guarded/skipped in CI.
- **Serialize calls (`max_parallel=1`)** — LM Studio still serves one model at a
  time; the delegation path inherits the F3 single-model-concurrency constraint
  (one lane per endpoint).
- **Model selectable by config** — harness + model + endpoint are all
  `StageExecutorConfig` fields, lane-overridable via the E1 (#625) mechanism.

## F3b run log

| harness | model | task | edit? | commit? | tests | sentinel | elapsed |
|---|---|---|---|---|---|---|---|
| aider 0.86.2 | qwen2.5-coder-32b-instruct | roman_to_int impl | yes (22+/1-) | `7109c11` | 3 passed | `cw result validate` exit 0 | 149s warm |
