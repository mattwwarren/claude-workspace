# Design: session-scoped lane focus for the status line

**Date:** 2026-08-04
**Status:** approved design, not yet implemented
**Repo:** `claude-workspace` — all of it. Skills, commands, and the scripts they call are authored here and installed from here (`scripts/install-skills.sh`, `scripts/excluded-commands.txt`). `global-claude` is the install surface, not the authoring surface, and needs no change for this feature.

## Problem

17 clients are configured in `~/.config/cw/clients.yaml`, and `claude-workspace` alone declares 10 lanes (`default`, `dashboard`, `debt`, `dogfood`, `codex-trial`, `bugs`, `debt-sprint`, `wave-par`, `wave-loop`, `wave-route`). A status line that reports all queue activity is unreadable, so today it reports none: `~/.claude/statusline-command.sh` prints `[<random-word>@<host>] <cwd>` and nothing about `cw`.

The operator adds lanes to open new workstreams when spare token budget allows. The question a status line should answer is therefore two-sided:

1. *Do I have capacity right now?*
2. *What is happening in the one lane I currently care about?*

Neither is visible today.

## Key finding that shapes the design

The status line command receives a documented JSON object on stdin
(<https://code.claude.com/docs/en/statusline.md>) that already contains the *capacity* half:

```
rate_limits.five_hour  = { used_percentage, resets_at }   # Pro/Max only, whole object optional
rate_limits.seven_day  = { used_percentage, resets_at }
context_window.remaining_percentage                        # nullable
cost.total_cost_usd
```

So the trigger the operator described — "turn on new focuses when I have spare tokens" — is already available at the exact moment it would be acted on. The missing half is the *work* side, which lives in `cw` state. **Focus is the selector that joins the two and discards the other 16 clients.**

Also confirmed present and load-bearing: `session_id` (top level, documented, "stable for the lifetime of a session and unique per session"), `cwd`, `workspace.current_dir`, `workspace.git_worktree`.

Verified independently: `CLAUDE_CODE_SESSION_ID` is exported into the session's environment (observed value `a0debb66-eb6e-4272-930d-c2d261c5107c`, matching this session's scratchpad path). This is what makes injection possible — a command running *inside* a session can name that session.

> **Unverified, and deliberately not depended upon:** that `CLAUDE_CODE_SESSION_ID` is byte-identical to the `session_id` the status line receives. The evidence is strong (same UUID shape, same session, and the docs use `session_id` as a cache key for exactly this purpose) but neither doc asserts it. The design degrades safely if they differ — see *Failure posture* — and the first end-to-end run confirms or refutes it in one step.

## Components

### 1. `cw focus` (claude-workspace)

```
cw focus set <client>[/<lane>] [--session <id>]
cw focus clear [--session <id>]
cw focus show [--session <id>]
```

`--session` defaults to `$CLAUDE_CODE_SESSION_ID`. State lives in `~/.local/share/cw/focus.json` as a map keyed by session id:

```json
{ "a0debb66-...": { "client": "claude-workspace", "lane": "debt-sprint", "set_at": "2026-08-04T18:00:00Z" } }
```

`lane` is optional — `cw focus set claude-workspace` focuses a whole client.

Written under the existing file-lock discipline used for `dev_queue.json`, since multiple sessions write concurrently.

### 2. `cw statusline render` (claude-workspace)

One command that emits the *work* segment and nothing else. Reads exactly three small local JSON files — `focus.json`, `dev_queue.json`, `concurrency_overrides.json` — and exits.

**Hard constraint: no `gh`, no `git`, no network, no subprocess.** The status line is debounced at 300ms and fires on every assistant message; the docs state that a slow script blocks the bar and that an in-flight run is *cancelled* (not queued) when the next trigger arrives.

Resolution order — this is the entire noise-control mechanism:

1. Explicit focus for this `session_id` → render that lane (or client aggregate if no lane).
2. Else map `cwd` → client via `clients.yaml` → render that client's aggregate, no lane detail.
3. Else emit nothing.

Step 3 is why an unfocused shell in an unmapped directory stays silent.

### 3. `statusline-command.sh` (claude-workspace, installed)

Parses stdin **once**, composes `[work] [capacity] cwd`, and becomes a **tracked, installed** file in this repo alongside the other skill/command scripts.

Today `global-claude/settings.json` declares `statusLine` pointing at `~/.claude/statusline-command.sh`, but that script is untracked in global-claude and `install.sh` never mentions it. The config travels to every machine; the script does not. On a fresh machine this is a `statusLine` pointing at a file that does not exist. Authoring and installing the script from here closes that gap by construction — it lands on the same install path as everything else.

### 4. Orchestrator injection (`orchestrate-sprint` skill, this repo)

No config flag, no automatic cw behavior. The skill gains one documented step: after a successful enqueue, call `cw focus set <client>/<lane>`; on queue drain, `cw focus clear`. Injection is the orchestrator's editorial act, which is where the judgment lives.

## Output shape

```
[cw-ws/debt-sprint 2▶ 1⧗ !1] [5h 34% · ctx 61%] ~/proj/claude-workspace
[cw-ws/debt-sprint PAUSED 0▶ 1⧗] [5h 34% · ctx 61%] ~/proj/claude-workspace
```

- `2▶` running, `1⧗` pending, `!1` needing attention
- a circuit-paused lane renders the literal word `PAUSED` after the lane name — deliberately **not** a glyph, and deliberately not the same marker as pending, because "1 pending" and "lane cannot claim" are the two states that must never be confusable (that confusion is exactly what hid the 9h27m stall)
- capacity segment omitted entirely when `rate_limits` is absent (it is Pro/Max-only and documented optional)
- work segment omitted entirely under resolution step 3

Nothing else earns space. Anything that does not change a decision is noise by definition.

## Lifetime

**Focus survives `/clear`** — the session id is unchanged across a clear, so focus persists. This is intended; stickiness is the feature. `cw focus clear` is the explicit out.

**Resume is best-effort.** A resume usually means the underlying machine state has moved, so a stale focus may point at a lane that no longer matters. If focus survives resume, good. If it does not, or if making it survive needs special handling, **drop it** — do not engineer for it. No retry logic, no reconciliation.

**No expiry, no TTL.** Entries accumulate in `focus.json`; it is a small map of short strings. If it ever needs pruning that is a separate, later concern.

## Failure posture

A non-zero exit or empty stdout **blanks the entire status line**, and stderr is swallowed. So:

- the `cw statusline render` call is wrapped; any non-zero exit, timeout, or empty output degrades to the capacity + cwd line
- if `cw` is not on `PATH` at all, the script still prints the original `[<word>@<host>] <cwd>` line
- if `session_id` from stdin does not match any key in `focus.json` — including the case where `CLAUDE_CODE_SESSION_ID` turns out *not* to equal the status line's `session_id` — resolution falls through to step 2 (cwd → client) and the bar stays useful. That is the safety net for the one unverified assumption above.

The script must never be the reason the bar goes blank.

## Testing

- `cw focus set/clear/show` round-trip, including the no-lane (client-only) form and the concurrent-write path.
- `cw statusline render` at each of the three resolution steps, including the silent case.
- Render with a circuit-paused lane → `⏸` marker present. (This is the same state that went unnoticed for 9h27m on 2026-08-04; see #1630.)
- Render with `focus.json` absent, malformed, and containing an unknown session id — all three must exit 0 with usable output, never non-zero.
- Render performance: assert it completes well inside the 300ms debounce budget with a realistic `dev_queue.json`.
- Shell-level test that a failing/missing `cw` still produces a non-empty line.

## Scope split for implementation

Two tickets, both in `claude-workspace`, sequenced — the script depends on the commands existing:

1. `cw focus` command group + `cw statusline render` + tests.
2. `statusline-command.sh` (tracked + installed here, with the degradation wrapper) + the injection step in the `orchestrate-sprint` skill.

A **separate, unrelated** ticket belongs against `global-claude`: a `statusline.d`-style composite setup so several independent fragments can contribute to one status line, rather than a single script owning the whole width. That is an install/compose-mechanism concern, which is the only category of work that belongs in global-claude. It is **not** a dependency of this design — this feature ships as a single script first, and would later become one fragment among several.

## Explicitly out of scope

- Per-terminal (`CW_FOCUS` env var) and machine-global sticky focus. Session scope was chosen; the others are not additive fallbacks and would confuse the resolution order.
- Any change to the `lane.paused` recurrence gap — that is #1630 and is already in flight.
- Multi-lane focus (focusing two lanes at once). One lane is the point.
- Pruning/expiring `focus.json`.
