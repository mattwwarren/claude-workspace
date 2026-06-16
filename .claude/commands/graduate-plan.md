---
description: Route an approved plan to the right implementation track
argument-hint: [--plan <path>] [--track <single|multi|incremental>]
allowed-tools: Bash, Read, Write, Glob, Grep, Agent, Edit, AskUserQuestion, Skill
---

# /graduate-plan — Plan Exit Router

Takes an approved plan and routes it to the right implementation track: single-ticket, multi-phase, or incremental. Creates tracking tickets, starts implementation, and manages the plan-to-PR lifecycle.

## Arguments

- **--plan <path>**: Path to plan file (default: auto-detect from recent ExitPlanMode)
- **--track <single|multi|incremental>**: Force a specific track (default: assess automatically)

## Instructions

### Step 1: Find the Plan

If `--plan` provided, use that path.

Otherwise, detect the most recently approved plan:
1. Check `~/.claude/plans/` for directories modified in the last 10 minutes (plan-exit-structure.sh moves plans to `<name>/main.md`)
2. Check `~/.claude/plans/` for `.md` files modified in the last 10 minutes
3. If multiple found, ask the user which one

Read the plan file completely. Extract:
- **Title**: H1 heading
- **Phases**: H2 sections (count them, note their names)
- **Tasks per phase**: Count `- [ ]` checkboxes in each phase
- **Total scope**: Sum of all tasks
- **Signals**: Look for "TBD", "depends on phase", "revisit after", "to be determined" — these indicate incremental planning

### Step 2: Read Project Config

Look for `.claude/project-config.yaml`. Read `plan_exit.mode` and `tracking.system`.

If no config exists, use defaults: `mode: suggest`, `tracking.system: local`.

### Step 3: Assess Scope and Propose Track

Count phases and tasks, then classify:

| Signal | Track | Description |
|--------|-------|-------------|
| 1 phase, ≤6 tasks, no TBD signals | **single** | One work item, implement directly |
| 2–4 phases, known decomposition | **multi** | Create tickets per phase, work sequentially |
| 5+ phases, OR any TBD/depends signals | **incremental** | Plan phase 1 only, re-plan after it ships |

If `--track` was provided, use that instead of assessment.

### Step 4: Confirm with User (per plan_exit.mode)

**mode: suggest** (default):
- Present: "This plan has N phases and M tasks. I'd suggest the **[track]** track because [reason]. Go with that, or adjust?"
- Wait for user confirmation or override

**mode: auto**:
- Print: "Auto-dispatching as **[track]** (N phases, M tasks)"
- Proceed without confirmation

**mode: ask**:
- Present all three options with the plan summary
- Always wait for explicit user choice

### Step 5: Create Tracking Tickets

Read `tracking.system` from config.

**single track:**

Create one ticket with the full plan content.

| System | Command |
|--------|---------|
| `github-issues` | `gh issue create --title "<plan title>" --body "<plan content formatted as checklist>"` |
| `notion` | Use Notion MCP to create a page with plan content |
| `linear` | Use Linear MCP to create an issue |
| `local` | Add frontmatter to plan file: `status: in-progress`, `track: single` |

**multi track:**

Create a parent/epic ticket plus one child ticket per phase.

| System | Parent | Children |
|--------|--------|----------|
| `github-issues` | `gh issue create --title "<plan title>"` with phase list | `gh issue create --title "Phase N: <name>"` per phase, referencing parent |
| `notion` | Create parent page | Create child pages under parent |
| `linear` | Create project/epic | Create issues under project |
| `local` | Update plan frontmatter: `status: in-progress`, `track: multi`, `current_phase: 1` |

**incremental track:**

Create ONLY a phase 1 ticket. Note in the plan that remaining phases need re-planning.

| System | Command |
|--------|---------|
| `github-issues` | `gh issue create --title "<plan title> — Phase 1: <name>"` with phase 1 tasks only |
| `notion` | Create page for phase 1 only |
| `linear` | Create issue for phase 1 only |
| `local` | Update plan frontmatter: `status: in-progress`, `track: incremental`, `current_phase: 1` |

For all tracks, store the ticket reference (issue number, page ID, etc.) in the plan frontmatter or a sibling `state.yaml` file so `/session-done` and `/prep-pr` can find it.

### Step 6: Hand Off to Implementation

Present the user with options based on track:

**single track:**
- "Ticket created. Ready to start implementing?"
- If user says go: begin working through the plan tasks directly, or hand off to `/auto-dev <ticket>` for the full pipeline.

**multi track:**
- "Created parent + N phase tickets. Ready for Phase 1?"
- If go: begin Phase 1 tasks (or `/auto-dev` on the Phase 1 ticket).

**incremental track:**
- "Created Phase 1 ticket. Ready to start?"
- If go: begin Phase 1 tasks.
- Note: "After Phase 1 ships, we'll re-plan remaining phases with fresh context."

### Step 7: Phase Completion Handling

When implementation completes (detected by todos being done, or user running `/session-done`):

**single track:**
- Run `/prep-pr` to review and create PR

**multi track:**
- Close/complete the current phase ticket
- If more phases remain:
  - Check context budget — if conversation is getting long, generate handoff for next phase
  - Otherwise, start next phase
- If all phases done: run `/prep-pr`

**incremental track:**
- Close/complete Phase 1 ticket
- Run `/prep-pr` for Phase 1 changes
- Then decide on re-planning:
  - **If context budget is healthy (conversation not too long)**: "Phase 1 is done. Let's re-plan the remaining work. Entering plan mode with what we learned."
    - Enter plan mode with prompt: "Phase 1 of [plan title] is complete. Here's what was done: [summary]. The original plan had phases 2-N sketched. Revise the remaining phases based on what we know now."
  - **If context budget is low**: Generate handoff with re-planning prompt for next session:
    - "Phase 1 shipped. Next session should re-plan remaining phases."
    - Include in handoff: original plan, phase 1 results, what changed

## Scope Reflection (for incremental re-planning)

When re-entering plan mode after a phase, include:
1. **What was planned vs what happened** — did scope grow, shrink, or hold?
2. **What we learned** — new constraints, discoveries, or simplifications
3. **Remaining sketch** — the original plan's remaining phases as starting context
4. **Recommendation** — "Based on phase 1, I'd suggest [adjusting/keeping] the original plan because [reason]"
