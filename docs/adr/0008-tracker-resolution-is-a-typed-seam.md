# Tracker resolution is a typed seam, not prose

**Status:** Proposed
**Driven by:** #675 (tracker resolution is gh-only in practice despite "agnostic" prose); builds on #412 (headless workers hung on Linear OAuth), #569 (agnostic-prose + mapping table), #659 (RFC 0005 B1 froze prose delegation), #190 (the `github_issue_existing` plan-source bolt-on)

## Decision

Tracker resolution becomes a **typed seam in `src/cw/`** rather than a prose
mapping table the LLM is trusted to apply. A `Tracker` Protocol
(`src/cw/tracker.py`) is the single point through which **cw's own
programmatic tracker queries** flow; the active implementation is resolved
once from `.claude/project-config.yaml` → `tracking.primary.system` (the
canonical **nested** form). `src/cw/gh.py` becomes the `GitHubTracker`
implementation; a `LinearTracker` (MCP-backed) is the second. `cw doctor`
gains a check that validates `project-config.yaml` parses, names a recognized
system, and that the resolved tracker's prerequisites are present (`gh` on
PATH for `github-issues`; Linear MCP reachable for `linear`).

This **does not** make cw a tracker-content engine. The seam covers the
deterministic queries cw already performs in code (PR merged-state, open-PR
existence, issue existence, client resolution, doctor prerequisites). The
*content* of stage trace comments — what an agent writes onto a ticket — stays
in the executor/skill layer per **RFC 0005 D3**. See
[Reconciliation with RFC 0005](#reconciliation-with-rfc-0005).

## Invariant

1. **One resolution point.** The active tracker is resolved exactly once, from
   `tracking.primary.system` in `.claude/project-config.yaml`, via a single
   `resolve_tracker(config) -> Tracker` factory. No module re-reads the config
   or re-derives the system. Absent/unparseable config is a **hard, named
   failure** surfaced by `cw doctor`, never a silent fallback to `gh`.
2. **No hardcoded forge in code paths cw owns.** `src/cw/` MUST NOT shell `gh`
   (or call a Linear MCP) outside a `Tracker` implementation. A `gh issue` /
   `gh pr` subprocess, or an `mcp__plugin_linear_*` literal, anywhere other
   than `GitHubTracker` / `LinearTracker` is a bug a reviewer can reject on
   sight.
3. **`ticket_id` carries its own grammar.** Client resolution MUST NOT assume a
   `PREFIX-NNN` shape. A bare integer (`403`, a GitHub issue number) is a valid
   ticket id; resolution falls through to the active tracker's id grammar and,
   failing that, to an explicit `--client` — never to a `CwError` that only
   mentions `linear_prefix_map`.
4. **Recognized systems are a closed set.** `tracking.primary.system` ∈
   {`github-issues`, `linear`} for now (extensible). An unrecognized value
   fails doctor; it does not degrade to a default.
5. **The seam is query-only for cw.** Trace-comment *content* is not a
   `Tracker` method; it stays executor/skill-owned (RFC 0005 D3). The seam
   exposes the deterministic reads/writes cw itself makes, not the narrative an
   agent posts.

## What this means for callers

- **`src/cw/gh.py`** → folds into `GitHubTracker`. Its four functions
  (`pr_is_merged_for_ticket`, `pr_exists_for_branch`, the two private fetchers)
  become `Tracker` methods. Callers (`doctor.py`'s merged-PR check,
  reconcile/dispatch PR-state reads) call the resolved tracker, not `gh`
  directly.
- **`src/cw/dev_queue.py` `resolve_client`** (the `"-" in ticket_id` /
  `linear_prefix_map` path) gains a tracker-aware branch so a bare GitHub id
  resolves a client (single-client repos resolve to the sole client;
  multi-client repos still require `--client`, but the error names the real
  cause, not Linear).
- **`src/cw/auto_dev_result.py`** — the twin `linear_existing` /
  `github_issue_existing` `PlanSource` literals stop being hand-maintained
  forge labels; the plan-source is derived from the active tracker.
- **`cw doctor`** adds `_check_project_config()` returning the standard
  `CheckResult(name, ok, detail, warn)` — config parses, system recognized,
  prerequisites present — with actionable output instead of a downstream
  silent stall.
- **`.claude/commands/review-monitor.md`** (the deferred-thread filing at
  ~:219, today a hardcoded `mcp__plugin_linear_linear__save_issue`) and
  **`.claude/skills/cw-smoke-test/scripts/preflight.py`** (hardcoded `gh
  issue`/`gh pr`) gain a tracker branch keyed on `tracking.primary.system`, so
  neither hits the wrong tracker on a repo configured for the other.

## What this means for producers

- **`GitHubTracker` / `LinearTracker`** are the only modules permitted to bind
  a concrete forge. Adding a third tracker (Notion, local) is one new Protocol
  implementation plus one doctor prerequisite branch — no edits scattered
  across `gh.py`, `dev_queue.py`, and two skills.
- **`/setup`** must write the canonical nested `tracking.primary.system`. The
  pre-existing flat-vs-nested divergence (`/setup` writes flat
  `tracking.system`; `/auto-dev` + the deployed config read nested
  `tracking.primary.system`; `queue-issues` reads flat) is **resolved here**:
  doctor flags the flat-only form as a misconfiguration and the resolver reads
  only the nested canonical path. (RFC 0005 explicitly punted this divergence;
  this ADR is where it lands.)

## Reconciliation with RFC 0005

RFC 0005 **D3** states "cw's Python stays tracker-blind — it owns `ticket_id`
as an opaque string; all tracker I/O is the executor/skill layer's job," and
routes stage trace comments through the active tracker at the *skill* layer.
This ADR appears to contradict that. It does not, once the two I/O classes are
separated:

| I/O class | Example | Owner | This ADR |
|---|---|---|---|
| **cw deterministic query** | "is the PR for ticket X merged?", "does an open PR exist for branch B?", "what client owns id 403?", "is `gh` on PATH?" | cw Python | behind the `Tracker` seam |
| **stage trace content** | "post the plan summary / review findings comment onto the ticket" | executor/skill | stays prose/MCP per RFC 0005 D3 — **unchanged** |

cw was **never** actually tracker-blind for the first class — `gh.py` already
shells `gh` from cw Python today; D3's prose described an aspiration the code
contradicts (the whole premise of #675). This ADR makes the *existing*
cw-owned forge calls honest and typed, and leaves D3's *content* boundary
intact. `ticket_id` remains an opaque string to everything except the
`Tracker` that understands its grammar.

## Consequences

- **New module + indirection.** `src/cw/tracker.py` and a resolution factory
  are new surface; every existing `gh.py` caller gains one hop. The payoff is a
  single audit point for "which forge are we talking to" and the end of the
  silent-stall failure mode (#412-class) when config and reality disagree.
- **`LinearTracker` is MCP-backed and cannot run headless** (the #412 OAuth
  hang). The seam does not fix that; it makes it a **named doctor failure**
  ("linear selected but MCP unreachable in this context") instead of a daemon
  worker hanging until the watchdog reaps it. `github-issues` stays the pinned
  default for this repo.
- **Two skills change behavior** (review-monitor filing, smoke-test preflight)
  — both gain a branch they lacked, so a github-issues repo no longer hits a
  Linear MCP call. Low blast radius; both are currently broken for the
  non-default tracker anyway.
- **Naming debt acknowledged.** `linear_url`, `linear_prefix_map` keep their
  names for one release (renaming is a config-migration with its own blast
  radius); the seam reads them tracker-agnostically. Rename is a follow-up, not
  a blocker.

## Alternatives considered

- **Prose/MCP-only + doctor + seam fixes (no code seam).** Keep cw
  tracker-blind, add only the doctor check and branch the two hardcoded skills.
  Rejected by the owner: the moment `.claude/project-config.yaml` is relied on
  to steer resolution, the steering must be *reliable and verifiable in code* —
  a prose table the LLM may or may not apply is exactly the unreliability #675
  documents. The doctor check alone validates the config but does nothing to
  stop `gh.py` / `dev_queue` / the sentinel literals from being structurally
  gh-shaped.
- **Full tracker-content abstraction in cw** (stage comments, plan posting,
  all I/O behind the seam). Rejected: contradicts RFC 0005 D3 for real, pulls
  narrative/LLM-shaped work into typed Python, and balloons scope. The
  query/content split above is the line.

## Referenced by

- RFC 0005 (D3 content boundary), #675, #412, #569, #659, #190
