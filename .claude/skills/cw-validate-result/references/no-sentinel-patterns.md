# No-sentinel patterns

When `validate_sentinel.py` reports `outcome=no_sentinel`, the `/auto-dev` run exited before emitting `<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>`. Same diagnostic table as the `/cw-followup` skill — see `../../cw-followup/references/no-sentinel-patterns.md`.

Quick reference (most common to least):

1. **Step 1 mid-dispatch failure** — Plan Quality sub-agent spawned, parent's Stop hook fired first. Issues #175 / #176; fixed by serial-headless landing in `global-claude` `82e2e02`.
2. **Stop-hook orphan with no `background_tasks`** — agent stalled mid-turn (often waiting on a tool denial), Stop hook never fires, JSONL just ends. Issue #185.
3. **Pre-flight `no_op` exit without emit** — skill detected ticket already satisfied and exited before reaching the sentinel emit step. Producer bug — should always emit `no_op` here.
4. **Tool denial deadlock** — tool call denied, skill stalls indefinitely. Issue #182. Catch via Layer 1 30-min `TIMED_OUT` backstop (PR #179).
5. **Process killed externally** — disconnect, OOM, manual SIGKILL. JSONL truncated mid-write.

Diagnosis: pull the last few assistant turns from the transcript and check whether the agent was mid-tool-call when the JSONL ended (orphan-style) versus emitted normal text and just stopped short of the sentinel (emit-step skipped).
