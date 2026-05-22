# RFC 0002 — Orchestrator on Agent SDK + Channels

| Field | Value |
|---|---|
| Status | Draft — ready for review |
| Owner | @mattwwarren |
| Spike ticket | #106 |
| CLI version under test | Claude Code 2.1.148 |
| SDK version under test | `claude-agent-sdk` 0.2.85 |
| Date | 2026-05-22 |
| Sibling RFCs | 0001 (#105 backend), 0003 (#107 worktree) |

## Summary

cw's 0.8.x orchestrator substrate (`dispatch.py`, `daemon.py`, `events.py`, `pr_responder.py`) is a hand-rolled tick loop on top of cmux: poll the file-based queue, spawn workers via cmux, parse `<<<AUTO_DEV_RESULT>>>` sentinels from stdout, react to file-based PR events. The Claude Agent SDK (Python) plus MCP push channels cover almost all of this natively, and crucially, **the SDK closes the env-var injection gap** that RFC 0001 left as a Phase 1 open question.

## TL;DR

**Decisions:**
- ✅ **Green-light Phase 4** (rewrite dispatch on Agent SDK). Smoke test 1 passed. SDK signature fits cw's needs and is strictly cleaner than the cmux + sentinel approach.
- ⚠️ **Conditional green-light Phase 3** (channels for PR events). Architecture verified; flag exists; SDK types support the receive-side. A 50-line channel-server prototype was descoped from this spike — Phase 3 must build one before commit.
- 🎁 **Bonus:** `ClaudeAgentOptions.env: dict[str, str]` resolves RFC 0001's Row 10 env-injection gap. cw can pass `CW_CLIENT`/`CW_PURPOSE`/`CW_SESSION_ID` directly through the SDK without per-cwd `.claude/settings.json` hacks. Update RFC 0001's Phase 1 design accordingly.

## Smoke test 1 — three concurrent `ClaudeSDKClient` workers

**Script:** `/tmp/sdk_smoke.py` (~70 lines). Three `asyncio.gather`'d `ClaudeSDKClient` instances, each told to emit a `<<<AUTO_DEV_RESULT … AUTO_DEV_RESULT>>>` sentinel.

**Result:** ✅ All three completed successfully.

```jsonc
{"worker": 0, "session_id": "17cd2ff5-…", "sentinel": {"status": "completed", "ticket_id": "1001", "worker": 0}, "captured_count": 12}
{"worker": 1, "session_id": "a18cc016-…", "sentinel": {"status": "completed", "ticket_id": "1002", "worker": 1}, "captured_count": 12}
{"worker": 2, "session_id": "801ffb7e-…", "sentinel": {"status": "completed", "ticket_id": "1003", "worker": 2}, "captured_count": 12}
```

Each worker:
- Got a distinct `session_id` (available on the streamed assistant messages — round-trips for stale-event rejection per ticket #97)
- Streamed 12 message events through `client.receive_response()`
- Emitted the sentinel exactly as instructed; cw's existing `<<<AUTO_DEV_RESULT … >>>` regex parser worked unchanged

**Concurrency model:** straight `asyncio.gather()` works. The SDK is async-native — no need for cw's per-client semaphore-via-state if `asyncio.Semaphore` is used.

**API shape:**

```python
import claude_agent_sdk as sdk

options = sdk.ClaudeAgentOptions(
    cwd=str(worktree_path),
    permission_mode="bypassPermissions",  # or "acceptEdits"
    max_turns=1,
    env={
        "CW_CLIENT": client.name,
        "CW_PURPOSE": "impl",
        "CW_SESSION_ID": session_id,
    },
    model="claude-opus-4-7",
    system_prompt="...",
)

async with sdk.ClaudeSDKClient(options=options) as client:
    await client.query("/auto-dev #105 --headless")
    async for msg in client.receive_response():
        # msg.content[].text contains worker output
        ...
```

## Smoke test 2 — channel server (descoped, evidence-based architecture)

**Status:** Not built. A working MCP channel server is ~150 lines (HTTP server speaking MCP, with channel-emit + external-POST endpoint) — too much for an evidence-only spike.

**Architecture verified by other means:**

1. **`--channels` flag exists in 2.1.148** (hidden from `--help`). Probed via `claude --channels` (errors with arg-missing) and `claude --channels server:bogus-test` (accepted and dispatched a session — no allowlist check for `server:` entries; allowlist only applies to `plugin:` entries).
2. **`--dangerously-load-development-channels` flag exists** for `plugin:` entries that bypass the allowlist.
3. **SDK supports MCP servers natively** — `ClaudeAgentOptions.mcp_servers: dict[str, McpStdioServerConfig | McpSSEServerConfig | McpHttpServerConfig | McpSdkServerConfig]`. cw can spin up the channel server in-process (`McpSdkServerConfig`) without `--channels` at all.
4. **SDK has `Notification` hook** — `ClaudeAgentOptions.hooks={"Notification": [HookMatcher(...)]}` receives `NotificationHookInput(message, title, notification_type)`. That's how channel events arrive in the SDK session.

**Phase 3 build list (before commit):**
- a) Tiny `cw_pr_events_server.py` (MCP HTTP server, ≤80 lines)
- b) Endpoint: `POST /pr-event` accepting JSON `{repo, pr_number, event_type, payload}`
- c) Emit MCP notification with `notification_type="cw-pr-event"`
- d) Smoke: `curl -X POST localhost:8788/pr-event …` while a `claude --bg --channels server:cw-pr-events …` session is live; verify `Notification` hook fires

## SDK fit per ticket table

| cw behavior | SDK candidate | Verdict |
|---|---|---|
| `dispatch_tick()` spawns N workers per client up to `per_client_max_parallel` (dispatch.py:71-184) | `asyncio.gather()` over N `ClaudeSDKClient` with `asyncio.Semaphore` | ✅ verified by smoke test 1 |
| Worker invocation: `claude --print "/auto-dev <t> --headless"` with `origin=DAEMON` | `ClaudeSDKClient(options=ClaudeAgentOptions(cwd=, permission_mode=, system_prompt=, model=, env=))` then `await client.query("/auto-dev …")` | ✅ verified |
| Capture worker stdout for `<<<AUTO_DEV_RESULT` parsing (wrapper.py:94-162) | Iterate `client.receive_response()` collecting `AssistantMessage.content[].text` | ✅ verified — sentinel parsed unchanged |
| Structured output via Pydantic `AutoDevResult` with cross-field invariants | `output_format="json"` + `json_schema=…` returning `structured_output` field | ✅ supported via `ClaudeAgentOptions.output_format`. **Recommendation:** migrate sentinel → JSON Schema in Phase 4 |
| Worker session ID for stale-event rejection (#97) | `session_id` field on assistant messages; `ClaudeAgentOptions.session_id` + `resume` for explicit control | ✅ verified |
| Parent/worker linkage (cw `parent_session_id`, `worker_session_ids`) | SDK has no native parent-child; cw state owns this | ✅ unchanged — keep cw-owned |
| Per-client `model`/`effort` overrides | `ClaudeAgentOptions(model=…, effort=…)` | ✅ first-class field |
| Cost tracking (#82) | SDK exposes `total_cost_usd` via `output_format="json"` result message | ✅ drop custom cost-from-sentinel path in Phase 4 |
| **Env-var injection (RFC 0001 Row 10 gap)** | `ClaudeAgentOptions.env: dict[str, str]` | ✅ **gap closed** for SDK-dispatched sessions |
| Per-tool permission decisions | `ClaudeAgentOptions.can_use_tool: CanUseTool` callable | ✅ programmatic policy beats the `--allowedTools` list |
| Session resume / fork | `resume`, `fork_session`, `continue_conversation` fields | ✅ all there |
| Tool restriction | `allowed_tools`, `disallowed_tools` fields | ✅ matches cw's needs |
| Custom hooks (PreToolUse, PostToolUse, etc.) | `hooks: dict[Literal[...], list[HookMatcher]]` | ✅ enables cw's per-session policy without shell-out |

## Channel fit per ticket table

| cw behavior | Channel candidate | Verdict |
|---|---|---|
| `cw event record pr.ci_failed --payload …` writing to `inbox.jsonl` (events.py:50-76) | MCP notification: server pushes to subscribed sessions in real-time | ✅ better — no file polling |
| `pr_responder.respond_to_pr_events` cursor-based consumption (pr_responder.py:130-150) | Channel events delivered while session is live; no cursor | ⚠️ **gap: no durable replay-after-restart.** Acceptable iff cw's dispatcher session is always live |
| PR event decision table (CI failed → `/fix-ci`, etc.) (pr_responder.py:118-128) | Session-side `Notification` hook reads `notification_type="cw-pr-event"` and dispatches the right slash command | ✅ same logic, in-session transport |
| Throttling per `(repo, pr_number, role)` via `PRDispatchRecord` (pr_responder.py:47-51) | cw state stays source of truth; channel emits, dispatcher dedups | ✅ unchanged |
| Multiple consumers via cursor-per-consumer (events.py:advance_cursor) | Channel: every subscribed session receives every event | ⚠️ **semantic shift** — verify cw has no multi-consumer dependency today |
| External GitHub webhook integration | Channel server's HTTP POST endpoint binds to localhost:8788 | ✅ direct fit; webhook → POST → notification |

### The durable-replay gap (the one real concern)

If cw's dispatcher session crashes between a PR event firing and the responder reacting, the event is lost. `events.py`'s cursor model survives this case; channels don't.

**Mitigations:**
1. **Dispatcher always-alive invariant.** cw's `cw dev-queue run` daemon already wants to be always-up (#86 was about restart resilience; RFC 0001's `claude respawn --all` handles that automatically now). If dispatcher is up, channels never miss.
2. **Persist-on-emit at the server side.** The channel server writes each event to a file (essentially the same `inbox.jsonl`) *and* pushes to subscribers. On dispatcher restart, replay unconsumed entries on first subscription. Same durability as today, plus push for the happy path.
3. **Hybrid:** keep `events.py` as the canonical record; channels are the real-time delivery on top. Most invasive transition (both code paths during cutover), most robust outcome.

**Recommendation:** option 2 for Phase 3. Channel server is the single store; subscribers replay missed events on connect.

### `--dangerously-load-development-channels` allowlist concern

Per the ticket: "Custom channels are gated behind an Anthropic allowlist during research preview."

Probed in 2.1.148:
- `claude --channels server:bogus-test` → accepted, dispatched
- `claude --dangerously-load-development-channels server:bogus-test` → accepted, dispatched

The allowlist enforcement appears to apply **only to `plugin:<name>@<marketplace>` entries**, not `server:<name>` entries. cw's `cw-pr-events` is a `server:` entry, so no allowlist gate.

If that behavior changes, the `--dangerously-load-development-channels` flag is the escape hatch. Not pretty but acceptable for cw users.

## Decision

**Phase 4 (#1??):** green-light. Replace `dispatch.py` with an Agent SDK orchestrator:

```python
# rough shape — Phase 4 design
class SDKDispatcher:
    def __init__(self, max_parallel_per_client: dict[str, int]):
        self._semaphores = {c: asyncio.Semaphore(n) for c, n in max_parallel_per_client.items()}

    async def dispatch(self, ticket: Ticket) -> AutoDevResult:
        async with self._semaphores[ticket.client]:
            options = sdk.ClaudeAgentOptions(
                cwd=str(ticket.worktree_path),
                permission_mode="bypassPermissions",
                model=ticket.model_override,
                env={"CW_CLIENT": ticket.client, "CW_PURPOSE": ticket.purpose,
                     "CW_SESSION_ID": ticket.session_id},
                output_format="json",
                json_schema=AutoDevResult.model_json_schema(),
                system_prompt=load_prompt("auto-dev"),
                max_budget_usd=ticket.budget_usd,
            )
            async with sdk.ClaudeSDKClient(options=options) as client:
                await client.query(f"/auto-dev #{ticket.id} --headless")
                async for msg in client.receive_response():
                    # ResultMessage at end carries structured_output + total_cost_usd
                    if isinstance(msg, sdk.ResultMessage):
                        return AutoDevResult.model_validate(msg.structured_output)
```

**Phase 3 (#1??):** conditional green-light. Build the `cw_pr_events_server.py` channel + a smoke test (curl POST → Notification hook fires) before commit. Use option 2 (server-side persistence) for durable replay.

## Crossover with RFC 0001 (#105)

**Update needed for RFC 0001 (PR #130):** Row 10 (env-var injection gap) is closed for SDK-dispatched sessions via `ClaudeAgentOptions.env`. Phase 1 NativeBackend's design question #1 ("which workaround for env injection?") becomes simpler:

- If `NativeBackend` uses `claude --bg` CLI: the workarounds in RFC 0001 still apply (per-cwd settings.json, etc.)
- If `NativeBackend` uses `ClaudeSDKClient`: env vars passed directly — no workaround needed

This suggests Phase 1 should consider whether `NativeBackend` should be SDK-backed from day one, skipping the `claude --bg` shell layer entirely. Trade-off: SDK requires Python import in cw's hot path (already true since cw is Python), but loses the daemon's auto-respawn (SDK sessions are bound to cw's process lifetime).

**My recommendation:** keep `NativeBackend` on `claude --bg` (gets daemon-managed auto-respawn for free, per RFC 0001 Row 7). Use SDK only for the orchestrator's dispatch loop and PR-event handling (where the dispatcher *is* the always-alive process).

## Open questions for Phase 3 + Phase 4

1. **JSON Schema migration for sentinel.** Phase 4 should land an `AutoDevResult.model_json_schema()` → `json_schema` option migration. Backwards-compat: keep regex parser as fallback for 1-2 releases.
2. **Per-tool permission policy via `can_use_tool`.** Worth migrating cw's existing approve-by-default + per-client overrides to a programmatic `CanUseTool` callable?
3. **Channel server location.** In-process via `McpSdkServerConfig`, or out-of-process via `McpHttpServerConfig`? In-process is simpler; out-of-process survives orchestrator restart.
4. **Notification hook ergonomics.** SDK's `NotificationHookInput.notification_type` is a `str` — should cw enum it (`"cw-pr-event"`, `"cw-ci-status"`, etc.) and document?
5. **Cost-tracking migration.** Drop cw's sentinel `cost_usd` field, read `ResultMessage.total_cost_usd` instead. #82 can close.
6. **Multi-consumer semantics.** If two cw dispatcher instances run in parallel (e.g., during graceful restart), do they both receive every channel event? Verify or design dedup.

## References

- [Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python)
- [Channels](https://code.claude.com/docs/en/channels), [Channels reference](https://code.claude.com/docs/en/channels-reference)
- cw modules: `src/cw/dispatch.py`, `src/cw/daemon.py`, `src/cw/auto_dev_result.py`, `src/cw/pr_responder.py`, `src/cw/events.py`
- #82 (cost tracking), #97 (stale-event rejection), #99/#101/#103 (sentinel + wrapper completion path)
- `docs/headless-contract.md` §3, §6 (sentinel format)
- RFC 0001 (#105) — `Row 10` env-injection gap is **closed** via SDK
- Crossover handoff: `~/.claude/handoffs/2026-05-22-cw-spike-105-followup.md`

## Test artifact

The 3-worker SDK smoke test script is at `/tmp/sdk_smoke.py`. Drop into `tests/` as `test_sdk_dispatch_smoke.py` once Phase 4 starts and you want a recurring integration test.
