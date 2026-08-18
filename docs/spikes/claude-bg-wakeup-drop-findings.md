# `claude --bg` Async-Completion Wakeup Drop — Findings (#1889)

**Date:** 2026-08-18
**Environment:** `claude` 2.1.234 (Claude Code CLI), `cw` (claude-workspace) at
`dev/1889` / fork point `2cf7550b`, Linux
**Scope:** root-cause investigation + upstream repro, NOT a cw code fix (this
is not cw-fixable — see "Why this is upstream, not cw" below)

## Summary

Sessions spawned via `claude --bg` (cw's `NativeDaemonClient.spawn_bg`) that
dispatch a long-running async operation — a backgrounded Bash command, or an
`Agent`-tool subagent — and then wait for its completion sometimes never
receive the wakeup that operation's completion is supposed to deliver. The
session's transcript goes flat indefinitely: no new turn, no error, nothing.
From cw's side this looks identical to a session that quietly died, except
the daemon roster still lists it as live.

This has hit real operators at least three times (#1801, #1838, #1751). In
each case the session was still nominally "running" per the daemon roster,
burning a slot and blocking dispatch, while doing nothing. Recovery required
manual intervention (`cw spawn close --confirmed-dead` + a requeue).

**This document packages a filing-ready upstream issue report.** Per the
ticket's Decisions (round 2, ALT-c), the actual `gh issue create --repo
anthropics/claude-code` step is reserved for the human operator — not because
the agent lacks the capability (see "Filing capability" below), but by
explicit policy: publishing under the operator's identity to a third-party
OSS repo is a human-executed action.

- [ ] **Operator action required:** file the issue body below against
      `anthropics/claude-code` and record the resulting URL here:
      `<URL — not yet filed>`

## Why this is upstream, not cw-fixable

`src/cw/native_daemon.py` (445 lines) is `cw`'s entire surface onto the
`claude` background daemon, and it is exactly three one-shot/passive
operations:

```python
def spawn_bg(self, *, cwd, prompt, extra_args=None, permission_mode=None) -> str:
    # subprocess.run(["claude", "--bg", ...]) — fire-and-forget
def list_live_session_short_ids(self) -> set[str]:
    # reads ~/.claude/daemon/roster.json — passive poll
def stop(self, short_id: str) -> None:
    # subprocess.run(["claude", "stop", short_id]) — one-way kill signal
```
(`native_daemon.py:187-222` protocol, `256-355` real impl; `spawn_bg` at 256,
`list_live_session_short_ids` at 321, `stop` at 339 — verified against
current source.)

There is no code path anywhere in `src/cw/` that injects a message,
re-resumes a session, or delivers any notification *into* a running `claude
--bg` process. `cw` is a client of the daemon, never a participant in its
internal wakeup-delivery machinery. The mechanism that is supposed to
re-inject a turn into the session on background-Bash completion or
`Agent`-tool subagent completion lives entirely inside the `claude` binary's
own supervisor/runtime — a closed surface from `cw`'s side. A cw-side "fix"
here can only be a mitigation for the symptom (see the shipped `--requeue`
flag, and the pre-existing liveness distress signal), never a fix for the
cause.

## Evidence: three real incident transcripts, one shape

Investigated by reading the actual on-disk transcripts for three of the six
incidents the ticket cites, still present under `~/.claude/projects/` at
investigation time. All three show the **identical** shape, which corrects
an assumption in the ticket's own prose (that this looks like a dangling,
unresolved `tool_use`):

### Incident 1 — `565e0fcc` (#1801, background-Bash channel)

The Bash tool's `tool_result` reads (verbatim):

> "Command did not complete within its 120s timeout and was moved to the
> background... You will be notified when it completes."

This **is a completed `tool_use`/`tool_result` pair** — the tool call
resolved normally. The assistant's final turn is then plain `end_turn` text:

> "Waiting for that background command to finish (it should be near-instant
> — likely just slow shell startup)."

No pending tool call. The session then goes flat. The completion
notification promised in the tool_result body never arrives as a new turn.

### Incident 2 — `ffe62dea` (#1838, `Agent`-tool subagent channel)

`TOOL_USE: Agent` → `TOOL_RESULT`:

> "Async agent launched successfully... You will be notified automatically
> [when it completes]..."

— also an immediately-resolved pair, not a dangling call. Final assistant
text:

> "Fix-cycle 1 agent dispatched (`a30d3f56fd68cc778`) in an isolated
> worktree to apply the 6-item action list. Waiting for it to finish."

Same shape: resolved tool call, plain `end_turn` text, then silence.

### Incident 3 — `286032f7` (#1751, retry-after-529 variant)

Same shape again: `Agent` tool_use resolves immediately, final text "Retry
#4 launched... Waiting on completion." Then flat.

### Why this matters for the diagnosis

`cw.reconcile._shared._awaiting_subagent` (`_shared.py:1460-1519`) — the
function that models "session is legitimately waiting on an in-flight
subagent" — returns `False` immediately in all three transcripts, because
there is no *pending* (unresolved) tool call to find. That function is built
for a genuinely different case. The bug here is not "cw fails to recognize a
pending subagent wait" — it's "the `claude` binary itself never delivers the
wakeup it told the user (via the tool_result text) to expect."

## Root cause (best available evidence)

The `claude --bg` supervisor appears to schedule a background-Bash or
`Agent`-tool completion wakeup as a re-injected turn into the session, but
that re-injection is not reliably delivered — the daemon process for the
session remains registered live (present in
`~/.claude/daemon/roster.json`, confirmed independently by `list_live_
session_short_ids` continuing to report the short id as live in the
matching incidents), yet no further transcript activity ever occurs. This
points at the delivery/scheduling path inside the `claude` binary's own
async-completion handling for backgrounded operations under `--bg` /
non-interactive daemon mode specifically — the interactive (foreground) CLI
does not exhibit this because the wakeup is delivered synchronously to a
terminal the user is watching, not asynchronously into a daemon-managed
session with no attached terminal.

This is a hypothesis grounded in the observed symptom (promised-but-
undelivered wakeup) and the closed nature of the `claude` binary from cw's
side — `cw` has no instrumentation into the binary's internals to confirm
the exact failure point (a scheduling race, a lost signal, a dropped queue
entry). The upstream issue report below is scoped to the observable
symptom and repro steps, not a claimed internal mechanism.

## Filing-ready upstream issue body

Copy-paste the block below into `gh issue create --repo anthropics/claude-code
--title "..." --body-file <this-file, extracted>` or the GitHub web UI.

---

**Title:** `claude --bg` sessions can silently stop receiving async-completion
wakeups (backgrounded Bash, `Agent`-tool subagents) and hang indefinitely

**Body:**

### Summary

A session started with `claude --bg` that dispatches a long-running async
operation — a Bash command that moves to the background after its timeout,
or an `Agent`-tool subagent launch — and then waits for that operation's
completion sometimes never receives the promised completion wakeup. The
tool result explicitly tells the model "you will be notified when it
completes," the model's final turn says it is waiting, and then the
transcript goes flat forever. The daemon (`claude --bg` roster) continues
to report the session as live/running throughout.

### Environment

- `claude --version`: 2.1.234 (also separately observed at 2.1.150 in an
  earlier investigation of a related error path — this is not a
  single-version regression as far as we can tell)
- OS: Linux
- Invocation: `claude --bg` (background daemon mode), no attached terminal
- Reproduced across at least 3 independent real sessions over multiple
  weeks, so this is not a one-off flake

### Repro steps

1. Launch a session via `claude --bg` with a prompt that will, at some
   point, either:
   - (a) run a Bash command expected to exceed the 120s foreground timeout
     (so it's moved to the background), or
   - (b) launch a subagent via the `Agent` tool for a task expected to take
     more than a few minutes
2. Let the model's turn end normally after the tool call resolves (i.e.
   `tool_result` for the Bash/Agent call comes back immediately with a
   "you'll be notified" message — this is NOT a hung/pending tool call, the
   turn completes normally with plain text like "Waiting for X to finish")
3. Poll `claude --bg` roster / session status: the session remains listed
   as live
4. Wait past the point where the backgrounded command or subagent should
   have completed
5. Observe: no new transcript turn is ever appended. The session sits
   indefinitely with no error, no timeout, no notification — despite the
   daemon still reporting it live.

### Evidence (redacted transcript excerpts from 3 real occurrences)

**Occurrence A — background-Bash channel:**
```
tool_result: "Command did not complete within its 120s timeout and was
moved to the background... You will be notified when it completes."
[end_turn] "Waiting for that background command to finish (it should be
near-instant — likely just slow shell startup)."
<transcript goes flat — no further turns>
```

**Occurrence B — Agent-tool subagent channel:**
```
tool_use: Agent
tool_result: "Async agent launched successfully... You will be notified
automatically [when it completes]..."
[end_turn] "Fix-cycle 1 agent dispatched (`<id>`) in an isolated worktree
to apply the 6-item action list. Waiting for it to finish."
<transcript goes flat — no further turns>
```

**Occurrence C — Agent-tool, retry-after-529 variant:** same shape; final
text "Retry #4 launched... Waiting on completion." then flat.

In all three, the tool call itself resolved cleanly (this is not a hung
tool call) — the gap is strictly in the promised follow-up wakeup never
being delivered as a new turn.

### Expected behavior

Either the backgrounded Bash command's completion, or the `Agent`-tool
subagent's completion, should reliably re-inject a turn into the `--bg`
session reporting the result — matching what the tool_result text promises
("you will be notified").

### Actual behavior

The wakeup is sometimes never delivered. The session hangs indefinitely
with no error surfaced anywhere, while the daemon roster continues to
report it as live.

### Impact

Any orchestration layered on top of `claude --bg` (ours is
[claude-workspace/cw](https://github.com/mattwwarren/claude-workspace),
open source) has no visibility into this — from the outside, a hung session
and a healthy-but-slow session are indistinguishable via the daemon roster
alone. We've had to build transcript-mtime-based staleness heuristics (45
minutes of flat transcript with no terminal sentinel) purely to detect this
class of hang from the outside, plus manual recovery tooling
(`cw spawn close --confirmed-dead`) since there is no way to resume or
signal the stuck session directly.

---

## Filing capability (informational, not an instruction to auto-file)

Investigated whether the agent drafting this document *could* file the
issue directly: `gh auth status` shows token scope includes `repo`, and `gh
api repos/anthropics/claude-code` reports `has_issues: true`. So `gh issue
create --repo anthropics/claude-code` would very likely succeed if invoked.
Filing stays human-executed anyway, per the ticket's explicit policy
decision (round 2, ALT-c / P1 REFUTED) — this is a scope choice, not a
capability gap.

## Mitigation shipped alongside this doc (in scope for #1889)

Full automatic recovery is deliberately NOT introduced here — ADR-0014
Invariant 3 requires a superseding ADR before promoting any quietness-
derived signal to an automatic disposition, and that governance step hasn't
happened. What ships instead, consistent with ADR-0006/0014:

1. **Detection already existed and needed only a regression test.**
   `cw.reconcile.liveness`'s distress path (`_act_on_liveness_candidates`,
   `liveness.py:248-329`) already emits `SESSION_NEEDS_ATTENTION` with
   `paused_status="session_unresponsive"` on exactly this composed
   signature (resolved async-dispatch tool_result + "waiting" text +
   45-minute flat transcript + no terminal sentinel). Two new regression
   tests in `tests/test_no_kill_timeouts.py` pin this against the real
   captured transcript text from occurrences A and B above, so a future
   change to the heuristic can't silently regress coverage of the actual
   incident shape.
2. **One-command recovery: `cw spawn close --confirmed-dead --requeue
   <sid>`.** Collapses the previously-manual two-command recovery (`cw
   spawn close --confirmed-dead` then `cw dev-queue requeue --from-cancelled`)
   into a single explicit, operator-invoked command. No timer, no automatic
   trigger — an operator (or an operator-triggered script) still has to run
   it in response to the distress signal above. See
   `docs/dispatch-runbook.md` §7 for the full recovery procedure.

## Related

- Ticket: #1889
- Real incidents: #1801, #1838, #1751 (transcripts read directly for this
  investigation), plus 3 more cited on the ticket but not independently
  re-read here
- `docs/adr/0006-reaping-is-gated-by-an-authority.md`
- `docs/adr/0014-timers-never-destroy-work.md`
- `docs/dispatch-runbook.md` §7 (CANCELLED row recovery) and §11.1
  (concierge mechanical recovery reactor)
