---
description: Select open tickets from the project's configured tracking system and enqueue them for parallel /auto-dev dispatch via cw dev-queue
argument-hint: "[--client <name>] [--priority <N>] [--state <state>] [--label <label>] [--assignee <user>] [--limit <N>] [--all] [--dry-run]"
allowed-tools: ["Bash", "Read", "Grep", "AskUserQuestion"]
---

# Queue Issues for /auto-dev Dispatch

Reads `.claude/project-config.yaml` to discover which tracking system the project uses
(GitHub Issues, Linear, Notion, or local), lists open tickets, lets the user select
some, and enqueues the selected IDs on the **cw orchestrator dev-queue**
(`cw dev-queue`) so the daemon can dispatch them as parallel `/auto-dev` sessions —
one worktree per ticket, up to the per-client concurrency cap in
`~/.claude-workspace/orchestrator.yaml`.

This is the GitHub-Issues-era counterpart to `/queue-plan` / `/queue-debt` (which feed
the per-session `cw queue`, not the orchestrator dev-queue).

**Arguments:** "$ARGUMENTS"

---

## Step 1: Read Project Config

```bash
test -f .claude/project-config.yaml || {
  echo "No .claude/project-config.yaml — run /setup first."
  exit 1
}
SYSTEM=$(yq -r '.tracking.system // "local"' .claude/project-config.yaml)
MCP=$(yq -r '.tracking.mcp_server // ""' .claude/project-config.yaml)
```

Accepted values for `SYSTEM`: `github-issues`, `linear`, `notion`, `local`.
Anything else → abort with: "Unknown tracking.system: `<value>`. Supported: github-issues, linear, notion, local."

## Step 2: Resolve Client

Resolve the cw client name (which identifies the target dev-queue):

1. If `--client <name>` arg provided → use it.
2. Else if `$CW_CLIENT` env var set → use it.
3. Else match the current working directory against `cw config` output:
   ```bash
   cw config 2>/dev/null | awk '/:$/{c=$1} /path:/{print c,$2}' \
     | while read name path; do
         case "$PWD" in "$path"*) echo "${name%:}"; break ;; esac
       done
   ```
   If exactly one match → use it. If none or multiple → ask via AskUserQuestion.

## Step 3: Parse Filter Args

Common filters (map to tracking-system-specific flags in Step 4):

| Flag | Default | Meaning |
|------|---------|---------|
| `--state <state>` | `open` | ticket state (open/closed/all — system-dependent) |
| `--label <label>` | (none) | comma-separated labels |
| `--assignee <user>` | `@me` | who it's assigned to; use `""` for unassigned, `*` for anyone |
| `--limit <N>` | `20` | max tickets to list |
| `--all` | false | skip the interactive picker, enqueue everything returned |
| `--priority <N>` | (none) | passed through to `cw dev-queue add -p N` |
| `--dry-run` | false | show what would be enqueued without calling cw |

## Step 4: List Tickets (Dispatch by System)

### github-issues

Requires `gh` on PATH and auth (`gh auth status`). Runs in the current repo.

```bash
gh issue list \
  --state "${STATE:-open}" \
  --assignee "${ASSIGNEE:-@me}" \
  ${LABEL:+--label "$LABEL"} \
  --limit "${LIMIT:-20}" \
  --json number,title,labels,assignees,url
```

Build the ID format the pipeline expects: `#<number>` (just the number prefixed
with `#`). `/auto-dev`'s Stage 0 currently treats this as free-text input but
preserves the reference in the plan/commit history.

### linear

Requires a Linear MCP server. Use the `list_issues` MCP tool with filters:
state (default: `"Todo"`), assignee (default: `me`), team, project, label as provided.
IDs are already in canonical form (e.g., `GEN-123`) — pass through verbatim.

### notion

Requires a Notion MCP server and a configured tasks database. Use `database-query`
to fetch open items. For each result, build an ID from the database slug + page
short-id (e.g., `NOT-abc123`). Since `/auto-dev` doesn't natively resolve these,
also pre-fetch each page's title and include it as part of the queued prompt
(Notion-specific fallback — see Step 5).

### local

No external tracking — list `.claude/plans/*.md` files. Each plan becomes a
candidate ticket, using the plan filename as the ID. (This path is essentially
equivalent to `/queue-plan` for the dev-queue and is here for completeness.)

## Step 5: Present and Select

If no tickets returned, report "No tickets match filters" and exit 0.

Build a numbered list:

```
Open <SYSTEM> tickets matching filters (<N>):
 1. <ID>  <title>                      [<labels>]
 2. ...
```

Unless `--all` was passed, **AskUserQuestion:**

```
Select tickets to enqueue (e.g. "1,3,5", "all", or "abort"):
```

Parse selection into a list of IDs.

## Step 6: Pre-flight

1. **Confirm target:**
   Show the user:
   - Client: `<client>`
   - Tickets: `<count>` — list the IDs
   - Priority: `<N>` if set, else "(default)"
   - Concurrency cap: `yq -r '.per_client_max_parallel.<client> // .per_client_max_parallel.default' ~/.claude-workspace/orchestrator.yaml`
   - Daemon running? `pgrep -f "cw daemon" >/dev/null && echo "yes" || echo "NO — start with \`cw daemon\` or \`systemctl --user start cw-daemon\` so items actually dispatch"`

2. **If `--dry-run`**: stop here, print the `cw dev-queue add` command that would run.

## Step 7: Enqueue

```bash
cw dev-queue add --client "$CLIENT" ${PRIORITY:+--priority "$PRIORITY"} $IDS
```

`$IDS` is the space-separated list of ticket identifiers. `cw dev-queue add`
writes them to the orchestrator queue; the daemon's dispatch loop claims them
subject to the concurrency cap and spawns `/auto-dev <id>` sessions.

## Step 8: Confirm and Next Steps

Report:
- N tickets enqueued for `<client>`
- Current dev-queue status: `cw dev-queue status`
- Remind:
  - If the daemon is running, dispatch is automatic.
  - Otherwise, run `cw dev-queue run --once` to dispatch a single tick, or
    `cw dev-queue run` for a continuous loop in the foreground.
  - Use `cw dev-queue plan` first if you want an orchestrator-agent-produced
    ordering (then `cw dev-queue run --use-plan`).

---

## Notes / Known Gaps

- `/auto-dev` Stage 0 is currently Linear-tuned — it recognizes `GEN-xxxx` IDs
  via `get_issue`. GitHub `#N` IDs fall through to the free-text branch, which
  works (the ticket ID ends up in commit messages / PR body via the prompt),
  but Stage 1d ("Post plan to Linear") doesn't have a GitHub equivalent yet.
  When that gets wired up, this skill is already passing the right IDs.
- The skill writes only to `cw dev-queue`, never to `cw queue`. Use `/queue-plan`
  or `/queue-debt` for the per-session queue (`/pull-and-execute` consumers).
