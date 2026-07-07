# Spike: Native Agent Teams as the Parent/Worker Primitive

**Date:** 2026-05-22
**Provenance:** promoted from issue #118 (closed as obsolete during the 2026-07-07 ticket audit — the spike was premised on the SDK orchestrator #116, which was never adopted). The *evaluation* below remains the durable record; re-file a fresh ticket if native agent teams matures.
**Status:** Deferred. No adoption. Revisit when the blockers below clear.

## Question

cw models the parent → worker session hierarchy via `Session.parent_session_id` /
`Session.worker_session_ids` (`models.py`), populated in `spawn.py`. Native Claude Code
has **agent teams** (experimental, gated by `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) —
one team lead + N teammates, a shared task list, a mailbox-based `SendMessage` tool, and
a team config file. Could agent teams replace cw's hand-rolled parent/worker linkage?

## Finding

Surface-level fit is good, but three hard limitations preclude adoption:

| Limitation (per agent-teams docs) | Impact on cw |
|---|---|
| Experimental, requires env flag | OK — cw users opt in |
| **No nested teams** (only the lead spawns; teammates can't recurse) | **Blocker** — `/orchestrate-plan` spawns workers that may need to spawn sub-workers |
| Lead is fixed for the team's lifetime | OK — cw's parent is the dispatcher session |
| **No session resumption for in-process teammates** (`/resume` doesn't restore them) | **Blocker** — cw resume is core UX |
| **One team at a time per lead** | **Blocker** — cw runs concurrent dispatches across clients |
| Split panes require tmux/iTerm2 | OK for native; in-process mode covers headless |

**Recommendation: defer.** The native tick loop (`dispatch.py`) with cw-managed
parent/worker linkage works today and has none of these constraints.

## Revisit checklist (if/when unblocked)

1. Re-read the agent-teams docs after each Claude Code release; look for nested-team
   support, resume support, and multi-team-per-lead.
2. Prototype: cw dispatcher as team lead, tickets dispatched as teammates.
3. Measure token cost vs. the current orchestrator — agent teams scale linearly per the
   docs, so likely **more expensive**.
4. Decide: full migration, partial (specific delegation patterns only), or drop.

## Related

- Native dispatch: `src/cw/dispatch.py`, `src/cw/spawn.py`
- [Agent teams docs](https://code.claude.com/docs/en/agent-teams)
