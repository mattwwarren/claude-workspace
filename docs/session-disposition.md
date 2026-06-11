# Session Disposition Contract

How to read a `cw` session's outcome correctly. The three gotchas in this
document each caused a wrong disposition during the 2026-06-10/11 sprint.

---

## 1. Authoritative source of truth

The terminal `AUTO_DEV_RESULT` sentinel in the session transcript is the
authoritative outcome. Do not rely on:

- `Session.last_result` — lags reconcile.
- Queue task status — lags reconcile.
- Daemon roster presence — cannot distinguish finished from stalled.

Read the sentinel via `_parse_sentinel_from_transcript` (`src/cw/cli.py`),
which uses `extract_block` + `parse_stdout` from `cw.auto_dev_result`. Never
apply a raw-text regex to the transcript file.

Transcript location is resolved by `_locate_session_transcript(session)` in
`src/cw/reconcile.py` (surface_ref-prefix glob, #541):

1. If `session.claude_session_id` is set: `<project_dir>/<csid>.jsonl` directly.
2. Else if `session.surface_ref` is set: newest `<project_dir>/<surface_ref>*.jsonl`
   with `mtime > session.started_at`.
3. Otherwise: None.

Do NOT fall back to an unscoped `*.jsonl` glob — that silently reads a
different session's transcript in a reused worktree.

---

## 2. The three gotchas

### Gotcha 1 — Fixture/example blocks ≠ the terminal emit

A worker's transcript can contain `<<<AUTO_DEV_RESULT … >>>` blocks it wrote
as test fixtures or copied from `auto-dev.md` for sentinel-related tickets.
A naive "first match" false-terminates.

**Rule:** take the **last** block that parses to a real terminal `AutoDevResult`,
and only trust it after the worker has left the daemon roster. This is what
`_parse_sentinel_from_transcript` does — it scans all assistant text blocks
and returns the first parseable result (which lands at the end of the
transcript, after all fixture/example output that precedes the real emit).
If you are reading the transcript manually, take the final complete block.

### Gotcha 2 — Sentinels are JSON-escaped in the transcript

Claude stores session transcripts as JSONL. Each line is a JSON object; the
`assistant` event's `message.content[].text` field holds the model output as
a JSON string, so the sentinel's quotes and newlines are `\"` and `\n` in the
raw file. A regex over the raw bytes misses sentinels that are valid only
after decoding.

**Rule:** `json.loads` each transcript line, extract the `text` field, then
run `extract_block` against the decoded text. `_parse_sentinel_from_transcript`
does this via `_iter_assistant_text_blocks`. Never regex the raw JSONL.

### Gotcha 3 — `claude_session_id` is often None on first lookup

`session.claude_session_id` is populated by the backfill inside `reconcile()`,
which fires only after the first reconcile tick. Before that tick, the field
is None.

**Rule:** if `claude_session_id` is None, call `_csid_from_transcript(session)`
(`src/cw/reconcile.py`) to derive it from the transcript filename via the
surface_ref-prefix glob. Only attempt transcript reads once a csid is
available.

---

## 3. Sentinel status → operator action

| Status | Action |
|---|---|
| `shipped` | Done. PR live with auto-merge enabled. |
| `no_op` | Done. Ticket already satisfied; close as completed. |
| `ambiguities_pending_resolution` | Resolve ambiguities posted on the issue; re-dispatch. |
| `premises_pending_verification` | Verify flagged premises, record on issue; re-dispatch. |
| `plan_pending_approval` | Read the plan comment, post `<!-- auto-dev-plan-approved -->`; re-dispatch for impl. |
| `review_pending_approval` | Review the pushed branch diff, run gates, ship (PR + auto-merge). |
| `merge_gate_blocked` | Prior pipeline PR still open; merge or close it; re-dispatch. |
| `scope_exceeded` | Scope rejection; close or relax constraint. |
| `forbidden_area` | Forbidden-area rejection; update constraints or reroute. |
| `validation_failed` | Transient (malformed sentinel); re-dispatch. Use `cw result validate <json>` to inspect the raw payload if it recurs. |
| `blocked` | Triage `blocker.reason`, `blocker.retry_eligible`, `blocker.recovery_hint`. |

For the full status-enum semantics and `blocker` shape, see
[`docs/headless-contract.md §4`](headless-contract.md).

---

## 4. The orphan condition

A session that emits a terminal sentinel (`shipped` / `no_op`) but is reaped
as idle *before* `reconcile()` consumes the sentinel leaves its dev-queue task
reverted to **PENDING** — a phantom task for finished work. A subsequent
dispatch tick will re-spawn it.

**Verify true state:**

1. Read the transcript sentinel directly (§1 above).
2. Check the PR or issue for completion evidence.
3. If the work is already done, clean up manually:
   ```bash
   cw done <session-name>
   cw dev-queue remove <TICKET-ID> --client <client> --all
   ```

**Observability:** reconcile emits `queue.session_reaped` on the queue-events
bus (`cw event tail`) whenever it disposes of a session. The `reason` field
uses the `ReapReason` taxonomy; see
[`docs/headless-contract.md §BLOCKED_ON_USER`](headless-contract.md) for the
full `ReapReason` table. Two hardening measures make false reaps less likely:

- **Confirm-before-reap** (`OrchestratorConfig.idle_confirm_observations`,
  default `2`): reconcile requires `session.idle_observation_count` to reach
  this threshold across consecutive watchdog ticks before dispositing. A single
  stale tick no longer triggers an immediate reap.
- **Widened liveness windows** (#544/#548): the per-tier idle-watchdog budgets
  are wider, reducing the window where a healthy-but-slow session looks idle.

If a reap occurs and you need to force reconcile to re-examine state:

```bash
cw doctor --reap
```

---

## 5. Cross-references

- [`docs/dispatch-runbook.md`](dispatch-runbook.md) — full end-to-end dispatch procedure.
- [`docs/headless-contract.md`](headless-contract.md) — `AUTO_DEV_RESULT` schema, status enum, `ReapReason` taxonomy, `queue.session_reaped` event.
- `src/cw/cli.py:_parse_sentinel_from_transcript` — transcript sentinel reader.
- `src/cw/reconcile.py:_locate_session_transcript` — transcript path resolver.
- `src/cw/reconcile.py:_csid_from_transcript` — claude_session_id derivation.
