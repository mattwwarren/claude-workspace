# claude-workspace Roadmap

**North star:** JARVIS. **Today's deliverable:** Tony's workbench.

---

## v1: The Workbench (Current)

**Theme:** Manage multiple Claude Code sessions without losing context.

Core workflow: start → work → background → switch client → resume.

- Zellij layout engine (tabs per client, panes per purpose)
- `cw` CLI for session lifecycle (start, bg, resume, list, switch)
- Integration with existing handoff pipeline (`/session-done`)
- Per-client config with workspace path mapping
- Yazi file tree pane for navigation

**Guiding principles:**
1. Context is the bottleneck - make it cheap to spin off and resume work
2. Spin off anything too heavy for the current context
3. Native first - Claude Code's terminal stays untouched
4. Agent delegation as architecture - multi-session enables agent-to-agent patterns

---

## v2: Worktrees & Multi-Purpose Sessions

**Theme:** Full parallel development per client.

- `cw start client --worktree feat/search` - auto-create worktree, set pane cwd
- `cw start client --purpose review` - spin up review session targeting impl's branch
- Purpose-aware CLAUDE.md injection: review sessions get review-focused instructions, debt sessions get debt-focused instructions
- Worktree cleanup: `cw done client/impl` closes session, optionally cleans worktree
- Session templates per client: define which purposes auto-start (e.g., "sigma always gets impl + review")

---

## v3: Cross-Session Awareness

**Theme:** Sessions that know about each other.

- Shared state directory per client: `~/.local/share/cw/clients/<name>/` with session summaries
- `cw handoff impl→review` - impl generates a handoff, review auto-resumes with that context on the same branch
- Session event hooks: "when impl backgrounds, notify review"
- Health monitoring: detect dead/crashed Claude sessions, mark as completed, surface in `cw status`
- `cw plan <client>` - show active plan progress across all sessions for a client

---

## v4: Autonomous Delegation

**Theme:** Spin off anything too heavy for the current context.

- `cw delegate "run ruff --fix across all services" --purpose debt` - fire-and-forget task delegation to a new session
- Queue system: `cw queue client/debt "review PR #42"` - add work items that debt session picks up
- Auto-background on context exhaustion: hook into Claude Code's context usage, auto-trigger `cw bg` at 80%
- Agent-to-agent handoffs: structured format for passing work between sessions (machine-parseable task specs, not just human-readable handoffs)
- Background debt runner: `cw daemon client/debt` - long-running session that pulls from queue, runs linting, opens PRs, pauses for human review

---

## v5: Dashboard & Visibility

**Theme:** See everything at a glance.

- `cw status` as a rich TUI (like lazygit but for sessions): clients on left, sessions in center, recent handoffs on right
- Zellij plugin (Rust/WASM): live status bar showing all session states, context usage, recent activity
- Desktop notifications via `notify-send`: "sigma/review found 3 issues in PR #89"
- Cost tracking: aggregate Claude Code token usage per client/purpose
- Session history: `cw history client` - timeline of all sessions, handoffs, and outcomes

---

## v6: JARVIS

**Theme:** The AI development environment.

- Voice interface: "Hey cw, what's the status on sigma?"
- Proactive suggestions: "You haven't run debt paydown on lgbtqplus-map in 5 days"
- Smart scheduling: run debt/review sessions during idle periods automatically
- Cross-client insights: "The same pattern you fixed in sigma exists in meta-work"
- Natural language orchestration: "Start reviewing all open PRs across my clients"
- Full IDE integration: when Claude Code has a programmatic API, replace keystroke injection with proper IPC

---

## Design Philosophy

### Context is the Bottleneck

The single most valuable resource in agentic work is context window capacity. Everything `cw` does serves one goal: protect context by making it cheap to spin work off, background it, and resume it later with full fidelity.

### Spin Off Anything Too Heavy

If a task is too significant for the current context (or a single context), it should be trivially easy to:
- **Background it** (`cw bg`) - generate handoff, free the context
- **Delegate it** (`cw start client --purpose debt`) - new session, fresh context, focused scope
- **Fork it** - implementation discovers a review concern? Spin up a review session on the same branch

The cost of starting a new session must approach zero.

### Native First

Claude Code's terminal experience is the product. `cw` is scaffolding around it, never a replacement. Every pane runs real `claude`. Every handoff uses the existing `/session-done` pipeline. No wrappers, no proxies, no reimplemented UIs.

### Intra vs Inter Session Delegation

- **Intra-session** (Claude's `Task` tool): Quick, parallel subtasks that share context (running tests, linting, exploring code)
- **Inter-session** (`cw`): Larger, independent work streams that need their own context (full feature review, debt paydown campaign, multi-file refactor)

Rule of thumb: If you'd use `/handoff` to pass the work, it belongs in a separate `cw` session. If you'd use `Task` to delegate, it stays in the current session.
