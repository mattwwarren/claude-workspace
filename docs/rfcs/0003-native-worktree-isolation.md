# RFC 0003 — Native worktree isolation

| Field | Value |
|---|---|
| Status | Draft — ready for review |
| Owner | @mattwwarren |
| Spike ticket | #107 |
| CLI version under test | Claude Code 2.1.148 |
| Date | 2026-05-22 |
| Sibling RFCs | 0001 (#105 backend), 0002 (#106 SDK/channels) |

## Summary

cw's `worktree.py` pre-creates git worktrees for daemon-spawned sessions: per-client base directory, slugified branch names, 64-char path cap with hash fallback, `.gitmodules` init, and idempotent reuse. The ticket assumed Claude Code's native supervisor handles all of this automatically ("every `claude --bg` session moves itself into a worktree under `.claude/worktrees/`"). **It doesn't.** Native worktree creation is user-driven (`--worktree=NAME` flag or subagent `isolation: worktree` frontmatter), and the auto-isolate-on-edit behavior the ticket cites is gated by an unset-by-default `worktree.bgIsolation` setting.

## TL;DR

**Decision: keep `worktree.py` as a thin policy layer.** Five gaps vs cw's current behavior, three of them load-bearing. Native primitives are useful inside `worktree.py`, but they don't replace it.

| Surface | Verdict |
|---|---|
| Worktree creation | ❌ **not automatic** in default settings; opt-in via `--worktree=NAME` or `worktree.bgIsolation` setting |
| Worktree path | ⚠️ root checkout's `.claude/worktrees/<name>` only — no per-client base |
| Branch naming | ⚠️ prefixes `worktree-` to user-supplied name |
| Same-name reuse | ✅ idempotent (matches cw) |
| Slug validation | ✅ `[A-Za-z0-9._-]` only; cw's existing slugifier suffices |
| 64-char cap | unknown — native didn't error on the test name, but a longer one might |
| Submodule init | unknown — defer to Phase 1 verification |
| Cleanup | ✅ `claude rm` honors uncommitted-work safety; cw should adopt |
| `worktree.bgIsolation: "none"` | untested |
| Subagent `isolation: worktree` | ⚠️ requires interactive disclaimer (`claude --dangerously-skip-permissions` once) before `--bg` can use bypass mode; otherwise blocks on permission dialog |

## Smoke test results

### Test 1 — Three `claude --bg` against the same branch

**Setup:** `cd /home/matthew/.cw/wt/758234be/auto-dev-107` (a worktree on branch `auto-dev/107`), then three `claude --bg "Create SPIKE-X.txt..."` dispatches.

**Result:** ❌ No worktrees created. All three sessions wrote `SPIKE-{A,B,C}.txt` directly into `auto-dev-107/`. The ticket's assumed auto-isolate behavior is **not the default**.

| Session | state | output |
|---|---|---|
| `af3ad4d1` | `working/blocked` | hung on `"approve Write: …/SPIKE-A.txt"` — permission gate |
| `22358c20` | `done` | created `SPIKE-B.txt` in `auto-dev-107/` |
| `f4a68dd6` | `done` | created `SPIKE-C.txt` in `auto-dev-107/` |

Notes:
- The permission gate fired for session A but not B/C — session-level approval cache appears to propagate within the same dispatch batch (corroborating finding for RFC 0001 Row 10 workarounds).
- A new `status` value `waiting` appeared in `agents --json` (alongside `idle`/`busy`).

### Test 2 — `claude --bg --worktree=NAME`

**Setup:** Same cwd; `claude --bg --worktree=spike-c-test "Create SPIKE-WT.txt..."`.

**Result:** ✅ Worktree created at `/home/matthew/workspace/personal/claude-workspace/.claude/worktrees/spike-c-test` — that is, in the **root checkout's** `.claude/worktrees/`, NOT under the calling worktree. `SPIKE-WT.txt` was created inside the new worktree.

`state.json` gained two fields:

```jsonc
{
  "worktreePath": "/home/matthew/workspace/.../claude-workspace/.claude/worktrees/spike-c-test",
  "worktreeBranch": "worktree-spike-c-test",      // prefixed with "worktree-"
  "respawnFlags": ["--worktree=spike-c-test"],    // preserved across respawn
  ...
}
```

Re-spawning with the same `--worktree=spike-c-test` name reused the existing worktree (created `SPIKE-WT2.txt` alongside `SPIKE-WT.txt`). Idempotent reuse confirmed.

**Naming gotchas:**
- `claude --bg --worktree "Print READY..."` (positional, no `=`) consumed the prompt **as the worktree name** and errored: `Invalid worktree name "Print READY then exit.": each "/"-separated segment must be non-empty and contain only letters, digits, dots, underscores, and dashes`. cw must always supply `--worktree=name` form, never positional.
- Branch name is `worktree-<user-name>` — Claude prefixes. cw's existing branch-name policy (#84 slugify) can stay in front, but the published branch name will differ from what cw passes.

### Test 3 — Subagent `isolation: worktree`

**Setup:** Wrote `.claude/agents/spike-isolated.md` with `isolation: worktree` frontmatter, then `claude --bg --agent spike-isolated`.

**Result:** ⚠️ Session entered `working/blocked` with `detail: "stuck on a startup dialog"`, `needs: "open this session to continue setup"`. Subagent dispatched but couldn't run autonomously.

Subsequent attempt with `--permission-mode bypassPermissions`:

```
--bg with bypassPermissions requires accepting the disclaimer first.
Run `claude --dangerously-skip-permissions` once interactively.
```

So cw's NativeBackend needs:
- **Onboarding step:** detect whether the user has accepted the bypass disclaimer; if not, surface a one-line "run `claude --dangerously-skip-permissions` once" message at first dispatch.
- **Fallback:** without bypass, dispatch via `--allowedTools` enumeration (the existing `~/.claude/settings.json` allow list is what makes interactive sessions like this one work).

State.json fields specific to subagent dispatch:

```jsonc
{
  "template": "spike-isolated",          // == agent name (was "bg" otherwise)
  "respawnFlags": ["--agent", "spike-isolated"],
}
```

Whether `isolation: worktree` frontmatter would create a worktree if the session had run autonomously is **not verified** by this spike. Defer to Phase 1.

### Test 4 — `claude rm` cleanup safety

Issued `claude rm bf57fea0` against a session whose worktree had uncommitted changes:

```
removed bf57fea0
  worktree has uncommitted changes — kept at .../spike-c-test
```

- Session record (`~/.claude/jobs/bf57fea0/`) deleted
- Worktree **preserved** because of uncommitted work
- Branch `worktree-spike-c-test` retained

cw should adopt this safety in `cw done --cleanup`: never delete a worktree with uncommitted work unless `--force` is supplied.

## Gap analysis per ticket table

| cw behavior | Native | Verdict |
|---|---|---|
| Pre-create worktree at predictable path before spawn (worktree.py:127-176) | Native creates when `--worktree=NAME` is passed; **no auto-creation** in default settings | ⚠️ partial — cw can still hand creation to native by passing `--worktree=<slugified-branch>`, but path is fixed to `<root>/.claude/worktrees/`, not `client.worktree_base` |
| Slugify branch name (worktree.py:34-42, charset `[A-Za-z0-9._-]`, #84) | Native validates exactly the same charset (observed in error message) | ✅ cw's existing slugifier works; rule matches |
| 64-char cap with hash fallback (worktree.py:80-94, cmux-specific) | Not directly tested; native's underlying mechanism is `git worktree add`, which has no hard cap | ✅ becomes irrelevant once cmux retires |
| Per-client `worktree_base` override (clients.example.yaml:24) | **No native equivalent.** All native worktrees land in `<root>/.claude/worktrees/<name>` | ❌ **gap** — see below |
| Submodule init via `.gitmodules` (worktree.py:166-174) | Not tested in this spike | unknown — defer to Phase 1 verification |
| Branch already exists vs new branch (#92, #93) | Native creates `worktree-<name>` branch; presumably fails if exists | unknown — defer |
| Idempotent reuse (worktree.py:129) | ✅ `--worktree=NAME` reuses if path exists | matches cw |
| Cleanup on `cw done --cleanup` (cli.py:220) | `claude rm <id>` deletes session + worktree, but **refuses** if uncommitted | ✅ better — preserves uncommitted work |
| Parallel sessions on same branch | Each `--worktree=NAME` reuses; sessions share the worktree (race conditions possible) | ⚠️ same behavior as cw, but worth documenting |

### The per-client `worktree_base` gap

cw users with multi-client setups configure `worktree_base` in `clients.yaml` to land worktrees on a fast scratch disk:

```yaml
ClientA:
  workspace_path: /work/clienta
  worktree_base: /scratch/wt-clienta
```

Native always puts worktrees at `<root>/.claude/worktrees/<name>` — there is no way to override the base directory via flag or setting (no `--worktree-base` flag in `claude --help`; no `worktree.path` in the settings docs).

**Workaround paths:**

1. **cw creates the worktree manually** (current behavior), then `claude --bg` runs *inside* that worktree without `--worktree=…`. Pros: keeps `worktree_base` policy. Cons: cw still owns worktree lifecycle (can't retire `worktree.py`).
2. **Symlink** `<root>/.claude/worktrees → /scratch/<root>-claude-worktrees`. Brittle; needs setup per repo.
3. **Upstream ask** for `--worktree-base <path>` flag. Lowest-friction long-term fix.

**Phase 1 recommendation:** keep option 1 — `worktree.py` continues to own creation; NativeBackend's `spawn()` `cd`s into the cw-created worktree and dispatches a plain `claude --bg` (without `--worktree`). This loses native's `worktreePath`/`worktreeBranch` state.json fields, but those are derivable from cw's own state.

## Decision

**Retain `worktree.py` as a thin policy layer.** Phase 1 NativeBackend's `spawn()`:

1. cw creates the worktree at `client.worktree_base / slugified-branch` (existing `create_worktree()`)
2. cw initializes submodules (existing `.gitmodules` step)
3. cw dispatches `claude --bg [prompt]` with `cwd=` the new worktree
4. cw does **not** pass `--worktree=…` (avoids the path lock-in and the `worktree-` branch-name prefix)
5. cw maps `cw done --cleanup` to:
   - `claude rm <short>` first (gets the safety check for uncommitted work)
   - if rm refused (uncommitted), prompt the user before falling back to `git worktree remove --force`

**Close-as-obsolete candidates:**
- **#92** ("hand worktree creation back to claude"): close as **rejected** — gap analysis shows native's path policy doesn't fit multi-client cw users.
- **#93** ("worktree edge cases"): keep open — cw still owns creation, so these edge cases remain in scope.

## Open questions for Phase 1

1. **Submodule init.** Does native run `git submodule update --init`? Quick test once Phase 1 starts. If not, cw's post-worktree hook stays.
2. **`worktree.bgIsolation` setting.** Does setting it in `~/.claude/settings.json` change defaults? (e.g., `bgIsolation: "auto"` triggers per-session worktrees with auto-named paths.) Worth testing — if it works, cw users could opt into native auto-creation per-repo.
3. **Subagent `isolation: worktree`.** Smoke test 3 didn't finish due to the bypass disclaimer. Verify in Phase 1 whether the frontmatter produces a worktree (auto-named) per dispatch. If yes, Phase 4's per-ticket subagents inherit isolation for free.
4. **Long path names.** Native didn't error on `spike-c-test` (12 chars). cw's `worktree-` prefix + slug for real branch names could push past 60 chars. Verify what native does with a 70-char name.
5. **`claude --dangerously-skip-permissions` onboarding.** Where does cw surface this requirement? Setup wizard? First-dispatch error message?

## Phase 4 implications (per-ticket worktrees)

Subagent isolation (Test 3) is the intended Phase 4 substrate: each `/auto-dev` ticket dispatches `claude --bg --agent ticket-worker`, and the subagent's `isolation: worktree` frontmatter creates a per-ticket worktree. This RFC didn't finish that verification — Phase 4 design should not assume it works until Phase 1 confirms.

## Crossover with RFC 0001 (#105)

- `claude rm` retains worktree on uncommitted work — useful as policy in Phase 1, file referenced in RFC 0001 Row 4.
- `state.json.worktreePath` + `worktreeBranch` only populated when `--worktree=NAME` was used at dispatch. NativeBackend that doesn't pass `--worktree` (per this RFC's recommendation) won't get these fields — cw's own `Session.worktree_path` remains the source of truth.

## References

- [Agent view § how file edits are isolated](https://code.claude.com/docs/en/agent-view#how-file-edits-are-isolated)
- [Settings § worktree-settings](https://code.claude.com/docs/en/settings#worktree-settings)
- [Subagents § isolation frontmatter](https://code.claude.com/docs/en/sub-agents)
- cw: `src/cw/worktree.py`
- RFC 0001 (#105 native session backend)
- #92, #93 (worktree edge cases), #84 (slugify charset), #90 (stale-worktree pruning)
- Crossover handoff: `~/.claude/handoffs/2026-05-22-cw-spike-105-followup.md`
