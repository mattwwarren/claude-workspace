# claude-workspace (`cw`)

Multi-session workspace orchestrator for Claude Code. `cw` lets you drive parallel autonomous Claude workers across your repos — enqueue tickets, dispatch workers, monitor progress, and handle gates — while staying the coordinator rather than the implementer.

The core loop: **harden a ticket → dispatch it → workers implement, review, and ship → you triage gates and clean up.**

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — AI coding assistant (required)
- [uv](https://docs.astral.sh/uv/) — Python package manager (required)

## Installation

```bash
# Install from GitHub
uv tool install "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git"

# Pin to a specific release (see github.com/mattwwarren/claude-workspace/tags for the latest)
uv tool install "claude-workspace[mcp] @ git+https://github.com/mattwwarren/claude-workspace.git@v1.20.0"

# Install from local clone (development)
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
uv tool install --editable ".[mcp]"
```

> **Stale binary warning:** `uv tool install --force` caches by version string. After pulling new code,
> use `--reinstall --no-cache` and verify with a subcommand `--help`, not `cw --version`.

## Getting Started

```bash
# Register a project
cw init my-project --path /path/to/repo

# Orient: verify queue and health
cw dev-queue status
cw doctor

# Harden a ticket (sweep for ambiguities before dispatch)
# In a Claude Code session: /harden-ticket PROJ-123

# Dispatch
cw dev-queue add PROJ-123 --client my-project --scope large
cw dev-queue run --once

# Monitor
cw watch                                    # live TUI work board
cw dev-queue wait PROJ-123 -c my-project   # block until terminal, JSON exit codes

# Clean up after terminal
cw done <session-name>
cw spawn close <session-id>
cw dev-queue remove PROJ-123 -c my-project --all
```

## The Daily Workflow

### Sprint recipe (operator perspective)

1. **Orient** — `cw dev-queue status` (expect empty/known), `cw doctor` (expect healthy)
2. **Scope** — take the epic; split into sub-tickets if large; note sequential deps
3. **Harden** — run `/harden-ticket` on each ticket to resolve technical ambiguities upfront
4. **Dispatch** — `cw dev-queue add <id> -c <client> -s large` → `cw dev-queue run --once`
5. **Watch** — `cw watch` or `cw dev-queue wait`; monitor for transcript silence >25 min
6. **Triage gates** — respond to `plan_pending_approval`, `ambiguities_pending_resolution`, `review_pending_approval`, `blocked` as they surface
7. **Verify** — read the worker's sentinel, run the gate, check the PR
8. **Clean up** — `cw done` → `cw spawn close` → `cw dev-queue remove`

### Information flow

```
You (coordinator)
    │
    ├─ /harden-ticket <id>          ← pre-flight: resolve ambiguities before dispatch
    │
    ├─ cw dev-queue add <id>        ← enqueue
    │
    ├─ cw dev-queue run [--once]    ← dispatch: spawns claude --bg worker per ticket
    │         │
    │         ▼
    │   Worker runs /auto-dev --headless
    │         │
    │         ├── Stage 1: Plan     → posts plan to Linear / GitHub Issue
    │         ├── Stage 2: Impl     → pushes branch to origin
    │         ├── Stage 3: Review   → runs reviewers, fix loop
    │         ├── Stage 4: PR       → opens PR, enables auto-merge
    │         └── emits AUTO_DEV_RESULT sentinel (JSON)
    │                 │
    │                 ▼
    │   reconcile() reads sentinel → updates queue task status
    │
    ├─ cw watch / cw dev-queue wait ← monitor terminal status
    │
    └─ triage gates, clean up
```

### Gate handling (by sentinel status)

| Status | Action |
|---|---|
| `shipped` | Done. PR is live with auto-merge. |
| `no_op` | Ticket already satisfied. Close as completed. |
| `stage_complete` | Staged pipeline: one stage (HARDEN/PLAN/IMPL/REVIEW) finished cleanly — not terminal. `cw` auto-advances the ticket to the next stage. |
| `merge_pending` | PR created but CI/merge gate hasn't cleared yet. Not a failure — don't re-dispatch, just monitor the PR. |
| `ambiguities_pending_resolution` | Answer questions on the issue, re-dispatch. |
| `premises_pending_verification` | Verify flagged premises on the issue, re-dispatch. |
| `plan_pending_approval` | Read the plan comment, post `<!-- auto-dev-plan-approved -->`, re-dispatch. |
| `review_pending_approval` | Review the diff yourself, ship (`gh pr create` + `gh pr merge --squash --auto`). |
| `merge_gate_blocked` | A prior pipeline PR is still open. Merge or close it, re-dispatch. |
| `scope_exceeded` | Diff grew past the declared scope tier. Re-scope the ticket or approve manually. |
| `forbidden_area` | Change touches a forbidden path (see client config). Route to a human. |
| `blocked` | Triage `blocker.reason` and `blocker.retry_eligible`. Re-dispatch if eligible. |

Use the `/cw-session-watch` skill to read a session's exit status without hand-grepping events and transcripts, and `/cw-followup` to act on the sentinel automatically (close `no_op`, rebase+PR for `merge_gate_blocked`, draft a Decisions section for ambiguities/premises, escalate real blockers).

## CLI Reference

### Session lifecycle

| Command | Description |
|---------|-------------|
| `cw start <client>` | Start or resume a Claude Code session for a client |
| `cw bg [session]` | Background the current session (injects `/session-done`, generates handoff) |
| `cw resume <session>` | Resume a backgrounded session with handoff context injected |
| `cw done [session]` | Mark a session as completed (not resumable) |
| `cw list` | List all sessions across clients |
| `cw status` | Show session health dashboard |
| `cw watch` | Live TUI work board (sessions + dev-queue tickets) |
| `cw guide` | Print the operator guide (`cw/data/GUIDE.md`) |

### Dispatch queue

| Command | Description |
|---------|-------------|
| `cw dev-queue add <ticket> -c <client> [--stage <s>]` | Enqueue one or more tickets for dispatch (optionally at a given stage) |
| `cw dev-queue run [--once]` | Dispatch pending tasks (up to concurrency cap) |
| `cw dev-queue serve` | Run the dispatch loop with automatic restart on crash |
| `cw dev-queue status` | Show queue state and last tick summary |
| `cw dev-queue tasks` | List dev-queue tasks with typed field output |
| `cw dev-queue wait <ticket>` | Block until terminal; structured JSON exit codes |
| `cw dev-queue approve <ticket>` | Approve a plan/review gate, or clear an operator-signoff hold |
| `cw dev-queue requeue <ticket>` | Requeue a `BLOCKED_ON_USER` ticket back to pending |
| `cw dev-queue drain --held -c <client>` | Resume every held (Rule-5 availability-park) ticket back to PENDING at its own stage |
| `cw dev-queue unblock <ticket>` | Clear salvage/park markers and requeue a `SALVAGE_PARKED` ticket |
| `cw dev-queue move <ticket> --to <lane>` | Re-lane a ticket |
| `cw dev-queue cancel <ticket>` | Cancel a pending ticket and stop any running session |
| `cw dev-queue remove <ticket> --all` | Remove a ticket from the queue |
| `cw dev-queue clear -c <client> [-s <status>] --confirm` | Delete tasks for a client (previews by default; add `--confirm` to delete). With no `-s`, RUNNING/BLOCKED_ON_USER/AWAITING_OPERATOR_SIGNOFF are excluded unless named explicitly |
| `cw dev-queue plan` | Spawn `/orchestrate-plan` to produce a DispatchPlan |
| `cw dev-queue refresh-all` | Fast-forward all client repos to origin/main |
| `cw queue peek` | In-flight inspection of RUNNING dev-queue sessions (age, idle gap, sentinel, PR state) |

`cw dev-queue wait` exit codes: `0`=shipped/no_op · `1`=failed/cancelled · `2`=blocked/pending-human · `3`=attention (stale transcript, or a mid-wait reap confirmed by `reap_proposed_at`) · `4`=parked awaiting operator signoff · `124`=timeout

For a wave of tickets, the `/cw-fanout` skill wraps this whole table — pre-flight, enqueue, dispatch, and monitor — into one orchestrated motion, using the `/cw-queue-peek` skill's WAIT/PEEK/STOP ladder to decide whether to keep a long-running session alive.

**Operator signoff gate** (RFC 0007 Phase 3): configure `--signoff operator` on `cw dev-queue add`, a lane, or the global default to force a ship checkpoint a ticket can't clear on its own. A gated ticket parks as `AWAITING_OPERATOR_SIGNOFF` at the REVIEW→FINALIZE boundary; `cw dev-queue approve` clears it forward (large-tier tickets need it twice — once for the ordinary review gate, once for signoff), and `cw dev-queue requeue --stage <earlier> --regress` sends it backward instead.

**Proactive finalize hold** (RFC 0011 A3): configure `--hold-finalize` on `cw dev-queue add`, `finalize_gate: manual` on a lane, or `default_finalize_gate: manual` globally to stop a ticket before an *unattended* finalize. A held ticket parks as `BLOCKED_ON_USER` with disposition `finalize_gate_held` at the REVIEW→FINALIZE boundary, wins outright over the signoff gate when both are armed, and is released only by a human `cw dev-queue approve` — an automatic gate-recipe approve declines and emits `gate.auto_approve_held` instead. `cw dev-queue drain` deliberately does not batch-release it.

### Orchestrator

| Command | Description |
|---------|-------------|
| `cw orchestrate status` | Snapshot of orchestrator pipeline state |
| `cw orchestrate watch` | **Deprecated** — live orchestrator dashboard; use `cw board` |
| `cw orchestrate workers` | List active worker sessions |
| `cw orchestrate run --lane <name>` | Run the reap-authority loop for a lane |
| `cw orchestrate start --lane <name>` | Bind lane reap authority |

### Spawn (low-level worker management)

| Command | Description |
|---------|-------------|
| `cw spawn -c <client>` | Spawn a daemon worker directly |
| `cw spawn close <session-id>` | Stop a live daemon worker |
| `cw spawn complete <session-id>` | Mark a worker session complete with result |

### Lanes

| Command | Description |
|---------|-------------|
| `cw lane ls -c <client>` | List lanes for a client |
| `cw lane add <name> -c <client>` | Add a dispatch lane to a client |
| `cw lane pause <name> -c <client>` | Pause a lane (stop new dispatch into it) |
| `cw lane resume <name> -c <client>` | Resume a paused lane |
| `cw lane rm <name> -c <client>` | Remove a lane from a client |

### Sprint buildout

| Command | Description |
|---------|-------------|
| `cw sprint plan <rfc-path> --out <file>` | Parse an RFC into a reviewable buildout plan |
| `cw sprint apply <plan-file>` | Idempotently apply the plan: milestone, epics, tickets |

The `/sprint-buildout` skill wraps this pair end-to-end — presenting the one approval gate, running an adjacent-bug pull-in scan, and (config-gated) mirroring pages to Notion.

### Maintenance

| Command | Description |
|---------|-------------|
| `cw init <name> --path <path>` | Register a new project |
| `cw doctor [--reap]` | Health check; `--reap` repairs common wedge conditions |
| `cw upgrade-workers` | Restart all daemon-managed background sessions on the latest model |
| `cw board` | Lane × stage pipeline cockpit (`--once` for a static/CI-friendly snapshot) |
| `cw schema` | Inspect Pydantic model schemas (`AutoDevResult`, etc.) |
| `cw session show/list/result/wait` | Inspect session state without going through `cw list`/`cw status` |
| `cw peek <session>` | Emit the last N lines of a worker's transcript output |

### Worktree management

| Command | Description |
|---------|-------------|
| `cw worktree gc` | Prune worktrees for squash-merged or closed branches (checks PR state) |

### Other

| Command | Description |
|---------|-------------|
| `cw config show` / `cw config concurrency` | Show configuration / manage concurrency overrides |
| `cw completion <shell>` | Print shell completion snippet |
| `cw result validate -` / `cw result emit` | Validate, or record onto a session, an AUTO_DEV_RESULT sentinel |
| `cw event record/tail/wait/prune` | Record, read, block-until, or prune events on the orchestrator bus |
| `cw review register <pr-url>` | Register a PR you were asked to review as a watched PR |
| `cw pr-channel` / `cw queue-channel` / `cw operator-channel` | MCP notification servers — wired into a session's `.mcp.json` to push PR/queue/operator events into Claude Code |

## Slash Commands (Claude Code Skills)

These are invoked inside a Claude Code session and form the daily operational toolkit.

### Core pipeline

| Command | When to use |
|---------|-------------|
| `/auto-dev <ticket-id>` | Full automated pipeline: intake → plan → impl → review → PR. Main driver for individual tickets in interactive mode. |
| `/auto-dev --headless` | Same pipeline, no interactive prompts. This is what `cw dev-queue run` dispatches workers to run. |
| `/harden-ticket <id>` | Pre-flight sweep: resolves technical ambiguities, escalates product forks, posts **Pre-flight Resolutions** comment. Run before every non-trivial dispatch. |

### Planning and queuing

| Command | When to use |
|---------|-------------|
| `/auto-dev-plan` | Stage 1 only: draft plan, run spec + soundness reviewers, post to issue tracker. |
| `/queue-issues` | Select open tickets from the tracker and enqueue them for parallel dispatch via `cw dev-queue`. |
| `/auto-debt <ticket-id>` | Constrained auto-dev for small-scope tech debt tickets. |
| `/sprint-buildout` | Turn a hardened RFC into a filed GitHub sprint block — milestone, epics, tickets, an adjacent-bug pull-in scan, and (config-gated) Notion mirror pages. Wraps `cw sprint plan`/`apply`. |

### Dispatch monitoring and follow-up (skills)

Once tickets are enqueued, this is the toolkit for watching a wave and closing it out without hand-rolling queue inspection every time:

| Skill | When to use |
|-------|-------------|
| `/cw-fanout` | Enqueue a batch of tickets and drive the whole wave to terminal in one motion — pre-flight, dispatch, monitor via the queue-peek ladder, close gate tickets inline. |
| `/cw-queue-peek` | In-flight check on RUNNING sessions — age, idle gap, sentinel status, PR state — recommends WAIT / PEEK / STOP. |
| `/cw-session-watch` | Reliably determine whether a dispatched session has ended and what its exit status was, without grepping events/transcripts by hand. |
| `/cw-followup` | React to a finished session's sentinel: close `no_op`, rebase + open PR for `merge_gate_blocked`, draft a Decisions section for ambiguities/premises, or escalate a real blocker. |
| `/cw-validate-result` | Forensic read on a completed run: extract the `AUTO_DEV_RESULT` sentinel and validate it against the headless contract. |
| `/cw-smoke-test` | End-to-end dogfood of the `/auto-dev --headless` pipeline against one ticket — pre-flight through sentinel validation, PASS/FAIL. |

### Review and shipping

| Command | When to use |
|---------|-------------|
| `/review` | Run the review suite on the current branch. |
| `/review-sweep` | Sweep all open PRs for feedback and CI status. |
| `/review-monitor` | Watch a PR and respond to review events. |
| `/address-review` | Address review comments on a GitHub PR and post replies. |
| `/prep-pr` | Prepare a PR: format title/body, wire auto-merge, post to tracker. |
| `/ship-it` | Final gate check + merge. **Project-scoped** — each repo supplies its own `.claude/commands/ship-it.md`; `/prep-pr` resolves it from the current project and blocks if absent. Never installed globally. |
| `/post-review` | Post-merge cleanup and debt filing. |

### Session management

| Command | When to use |
|---------|-------------|
| `/session-done` | End-of-session cleanup: generate handoff, background session. |
| `/handoff` | Generate a handoff document for resume or context transfer. |
| `/setup` | Onboard a new project into `cw` (config, hooks, MCP servers). |
| `/install-cw` | Install `cw` and configure Claude Code integration on a fresh machine. |

### Orchestration (advanced)

| Command | When to use |
|---------|-------------|
| `/orchestrate-phase` | Run a single pipeline phase in an orchestrated multi-session setup. |
| `/graduate-plan` | Route an approved plan to the right implementation track. |

## The `/auto-dev` Pipeline

`/auto-dev` is the core automated pipeline. In headless mode (what `cw` dispatches), it runs without interactive prompts and emits a structured `AUTO_DEV_RESULT` sentinel at the end.

```
Stage 0: Intake          ← fetch ticket, resolve tracker, origin sync check
Stage 1: Plan            ← draft or validate plan, ambiguity scan, spec+soundness review
Stage 2: Implement       ← spawn impl agent in worktree, push branch
Stage 3: Review          ← spawn review agents, adjudicate findings, fix loop (≤5 cycles)
Stage 4: PR Creation     ← merge gate check, create PR, enable auto-merge
Stage 5: CI Wait         ← skipped headless (orchestrator concern)
```

**Scope tiers** control approval automation:
- **Small** (≤10 files, ≤500 lines, no forbidden areas): most gates auto-skip
- **Large** (>10 files or >500 lines or forbidden areas): plan and review require human approval

**Sentinel output** (headless only):

```json
<<<AUTO_DEV_RESULT
{
  "schema_version": 4,
  "ticket_id": "PROJ-123",
  "status": "shipped",
  "stage_reached": "stage5_post_create",
  "scope": {"tier": "small", "files": 3, "lines_estimate": 40, "lines_actual": 47, "forbidden_touched": false},
  "plan_source": "github_issue_existing",
  "branch": "dev/proj-123-fix-login",
  "pr": {"number": 42, "url": "https://github.com/org/repo/pull/42", "base": "main", "auto_merge": true},
  "review": {"must_fix_initial": 0, "should_fix": 1, "fix_cycles_used": 0},
  "health": {"lowest_agent_confidence": "HIGH", "any_incomplete_risk": false, "recommendation": "PROCEED"},
  "blocker": null,
  "next_actions": ["wait_for_ci"]
}
AUTO_DEV_RESULT>>>
```

This is the minimum payload that actually passes `cw result validate -` — `scope.lines_estimate`, `scope.forbidden_touched`, `plan_source`, `pr.base`, and `health` are all required once `stage_reached` is past `stage1_plan`/`stage1_pre_flight`, and a `shipped` status requires `"wait_for_ci"` in `next_actions`.

`cw` parses this sentinel via `reconcile()` to route tasks to terminal queue states. See [`docs/headless-contract.md`](docs/headless-contract.md) for the full schema.

## Configuration

Config lives at `~/.config/cw/clients.yaml`.

```yaml
clients:
  my-project:
    workspace_path: /path/to/repo
    default_branch: main
    auto_purposes: [impl, idea, debt]
    worker_model: claude-sonnet-4-6   # pin model for autonomous workers
    lanes:
      - name: default
        max_parallel: 2
        reap_policy: signal_only      # signal_only (default) or auto
    purpose_prompts:
      impl: |
        Focus on implementation. Follow existing patterns.
```

Key fields:

| Field | Description |
|-------|-------------|
| `workspace_path` | Absolute path to the project repo (or use `repo_path` + `branch` for worktree mode) |
| `worker_model` | Model for DAEMON-origin workers (`claude --bg`). USER-origin sessions inherit the operator's default. |
| `lanes` | Named dispatch lanes with `max_parallel`, `reap_policy`, and `priority` |
| `auto_purposes` | Session purposes to start with `cw start`: `impl`, `idea`, `debt` (a fourth purpose, `orchestrate`, exists only for `cw orchestrate start` and is never selected via `auto_purposes`) |

See [config/CONFIG_REFERENCE.md](config/CONFIG_REFERENCE.md) for all options and worktree-mode configuration.

## Architecture

- **Daemon workers** — `cw` spawns workers via `claude --bg`, tracked by short hex session ID in `~/.claude/daemon/roster.json`. No multiplexer required.
- **Reconcile** — `cw status`, `cw list`, `cw start`, and each dispatch tick call `reconcile()` to detect phantom sessions (in state but absent from the daemon roster). Default `reap_policy: signal_only` emits `SESSION_REAP_PROPOSED` and routes to `BLOCKED_ON_USER` without destructive mutation. `reap_policy: auto` or `cw doctor --reap` performs actual cleanup.
- **Sentinel parsing** — the `/auto-dev --headless` worker emits a structured `AUTO_DEV_RESULT` JSON block at session end. `reconcile()` reads this from the transcript to advance the queue task.
- **File-based locking** — prevents concurrent state corruption from parallel session operations.
- **Event bus** — `~/.local/share/cw/events/inbox.jsonl` provides an audit trail; MCP servers (`cw pr-channel`, `cw queue-channel`, `cw operator-channel`) push filtered events to Claude Code sessions.
- **Worktrees** — impl agents work in isolated git worktrees; `cw worktree gc` prunes merged ones.

### Key files

| Path | Purpose |
|------|---------|
| `~/.config/cw/clients.yaml` | Client configuration |
| `~/.local/share/cw/sessions.json` | Session state |
| `~/.local/share/cw/dev_queue.json` | Dispatch queue |
| `~/.local/share/cw/events/inbox.jsonl` | Event history |
| `~/.claude-workspace/orchestrator.yaml` | Dispatch-loop tuning (created with defaults on first run) |
| `~/.claude/daemon/roster.json` | Native daemon session roster |

### Architecture decisions

See [`docs/adr/`](docs/adr/) for formal ADRs. Key decisions:

- **Keystroke injection** — `cw bg` injects `/session-done` into active Claude sessions via the daemon API.
- **On-demand reconciliation** — no background daemon; `reconcile()` runs on every read path.
- **Sentinel as state** — pipeline state lives in Linear comments, git commit trailers, and GitHub PR fields. No `.auto-dev-state.json` files. Resume detection is pure derivation from these durable signals.
- **Fact gates advancement** — orchestrator completion gates (diff non-empty, tests pass, branch pushed) are deterministic; agent self-assessment is advisory only.

## Shell Completion

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"

# Fish (~/.config/fish/config.fish)
_CW_COMPLETE=fish_source cw | source
```

## Further Reading

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — codified system principles and anti-patterns (§7/§8), source of truth for plan-time soundness review
- [`cw guide`](src/cw/data/GUIDE.md) — operator how-to, always matches your installed version
- [`docs/INSTALL.md`](docs/INSTALL.md) — detailed install and upgrade instructions
- [`docs/dispatch-runbook.md`](docs/dispatch-runbook.md) — end-to-end `cw dev-queue` dispatch procedure
- [`docs/session-disposition.md`](docs/session-disposition.md) — reading a session's outcome from transcript sentinels
- [`docs/headless-contract.md`](docs/headless-contract.md) — `AUTO_DEV_RESULT` schema and event taxonomy
- [`docs/events.md`](docs/events.md) — event bus reference
- [`docs/operator-channel.md`](docs/operator-channel.md) — operator MCP notification channel reference
- [`config/CONFIG_REFERENCE.md`](config/CONFIG_REFERENCE.md) — full configuration reference

## License

This is free and unencumbered software released into the public domain. See [UNLICENSE](LICENSE).
