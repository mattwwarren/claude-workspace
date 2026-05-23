# No-sentinel patterns

When `parse_sentinel.py` reports `sentinel_found: false` the `/auto-dev` skill exited without emitting the `<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>` block. The likely causes, ranked by how often each one fires in practice:

| Cause | Diagnosis |
|---|---|
| **Step 1 mid-dispatch failure** | The skill spawned a Plan Quality sub-agent and the parent's Stop hook fired before the sub-agent returned. Look in the JSONL transcript for an `assistant` block whose tool calls end with a background dispatch and no subsequent assistant message. See issues #175 / #176 — addressed by the serial-headless landing in `global-claude` `82e2e02`. |
| **Stop-hook orphan with no `background_tasks`** | The Layer 1 budget check only fires on Stop hooks; if the agent stalled mid-turn (e.g., waiting on a tool denial) the Stop hook never fires and the JSONL just ends. See issue #185. |
| **Pre-flight `no_op` exit but emit step skipped** | The skill detected the ticket was already satisfied (e.g., PR already merged) and exited before reaching the sentinel emit step. The transcript ends with a `gh issue` lookup. The producer should always emit `status=no_op` here — if missing, that's a producer bug, file a ticket. |
| **Tool denial deadlock** | A tool call (typically `gh issue comment`) was denied; the skill stalls indefinitely with no recovery path. Issue #182. Catch via the Layer 1 30-min TIMED_OUT backstop landed in PR #179. |
| **Process killed externally** | Disconnect, OOM, manual SIGKILL. The JSONL is truncated mid-write and the last record may be malformed. |

When in doubt, scan the JSONL for the last few records to see what the agent was doing at exit:

```bash
TRANSCRIPT=$(jq -r '.transcript_path' <<<"$RESULT")
tail -n 30 "$TRANSCRIPT" | jq -c 'select(.type == "assistant") | .message.content[0].text // .message.content[0].input' 2>/dev/null | head -5
```

If the final assistant turn ended in a `Bash` or `Task` tool call and there is no subsequent assistant message, the orphan-style explanation (Step 1 mid-dispatch or stalled mid-turn) is most likely. If the final assistant turn is a normal text block that just stops short of emitting the sentinel, the producer skipped the emit step — likely cause: pre-flight `no_op` not yet wired through the emit path.
