# Provider-portable ticket work is agent work; cw keeps one GitHub-only programmatic client

**Status:** Accepted
**Driven by:** #1240 (rescoped by operator comment 2026-07-15; supersedes
the ticket's original `TicketProvider` Protocol direction); builds on
ADR-0008 (query/content split — note: ADR-0008's own Status is
`Proposed`, not yet Accepted; this ADR's invariants below do not depend
on ADR-0008's acceptance), RFC 0005 D3 (content boundary, already
established)

## Decision

Provider-portable ticket I/O — reading and writing ticket content across
whatever tracker a client happens to use — is agent work, done through
the agent's own native tools (`gh` CLI in-session, Linear MCP). `cw`'s
deterministic Python keeps exactly one minimal, GitHub-only programmatic
tracker client, `gh.py`, for the narrow set of autonomous daemon actions
that run with no agent/LLM session present (PR-state polling, gate/review
recipe actions, executor comment posting). No provider abstraction is
built on top of it.

## Invariant

1. No `TicketProvider` Protocol / N-provider abstraction is built in cw
   Python. A future tracker is onboarded by giving agents its tool, not
   by teaching cw Python its API. This is an explicit non-goal — the
   closest prior alternative, "a `Tracker` Protocol with a class per
   system," is exactly what ADR-0008 itself already rejects (see
   docs/adr/0008-tracker-resolution-is-a-typed-seam.md:181-185: "this was
   the first-draft direction and it still costs one bespoke,
   hand-maintained implementation per ticket system").
2. `gh.py` is the sanctioned direct-`gh`-CLI-subprocess surface. Three
   pre-existing daemon-side direct callers — `doctor.py`,
   `worktree_gc.py`, `reconcile/salvage.py` — are named as **temporary,
   grandfathered exceptions**, not a permanent expansion of the
   sanctioned surface. Their consolidation into `gh.py` is the committed
   subject of a filed follow-up ticket, **#1284** ("refactor: consolidate
   direct gh CLI callers (doctor.py, worktree_gc.py, reconcile/salvage.py)
   into gh.py") — not open-ended prose. Convention: each module is
   removed from the allowlist in the same PR that consolidates it into
   `gh.py`; the allowlist may only shrink as #1284 lands, never grow by
   default. `tests/test_ticket_boundary_guard.py` is the enforcing
   artifact.
3. No Linear/tracker SDK Python dependency is added to cw. Generic HTTP
   client libraries (`requests`, `httpx`) are treated the same way: cw
   has zero HTTP-client dependency today, and adding one for tracker I/O
   would be exactly the regression this ADR exists to prevent.
4. `linear_prefix_map` is client/repo routing — which client a ticket-id
   prefix belongs to in a multi-client deployment — not provider
   selection. It is unaffected by this decision (see
   `src/cw/dev_queue.py`'s `resolve_client`, which reads
   `config.linear_prefix_map` purely to map a `PREFIX-NNN` ticket id to a
   client name, never to choose a tracker implementation).

## What this means for callers

- Executors (`executor.py`), gate recipes (`reconcile/gate_recipes.py`),
  and review workflows import `cw.gh`'s typed functions (e.g.
  `post_issue_comment`, `fetch_approved_plan_comment`) — they never
  construct raw `gh` subprocess calls or a provider HTTP client
  themselves.
- PR hydration (`pr_hydrate.py`) similarly imports from `cw.gh`
  (`fetch_pr_view`, `_GH_PR_STATE_MERGED`) rather than shelling out
  directly.
- Any new code path that needs ticket *content* (post a comment, read a
  ticket body, resolve a ticket's tracker-specific state) belongs at the
  agent/skill layer per RFC 0005 D3 — not as a new cw Python module.

## What this means for producers

- `gh.py` stays a small, flat module of `gh` CLI wrapper functions — no
  `TicketProvider`/`Ticket`/`TicketComment`/`PostCommentResult` model
  layer is added on top of it.
- The three grandfathered callers (`doctor.py`, `worktree_gc.py`,
  `reconcile/salvage.py`) do not gain new direct `gh` call sites beyond
  what they already have; new daemon-side GitHub needs route through
  `gh.py`.

## Rationale

The daemon's ~30-second dispatch-tick cadence
(`config/CONFIG_REFERENCE.md`'s `tick_interval_seconds: 30` default,
under "Orchestrator Configuration") and the daemon's no-LLM-session
nature make programmatic access unavoidable for PR hydration polling,
gate/review-recipe actions, and executor comment posting. That access is
GitHub-specific by nature — PRs are a GitHub concept — so a GitHub-only
client is the right shape, not a gap to be generalized. Everything else
(ticket reads/writes proper) already flows through agent tools (`gh` CLI
in-session, Linear MCP) per RFC 0005 D3; this ADR does not change that
boundary, it names and freezes it.

## Explicit non-goal

To restate plainly: this ADR does **not** introduce a `TicketProvider`
Protocol, does **not** introduce `Ticket`/`TicketComment`/
`PostCommentResult` models, and does **not** introduce a Linear Python
adapter. The original ticket's direction along those lines is
superseded by the rescope this ADR records.

## Consequences

- The 4-module allowlist for direct `gh` calls (`gh.py`, `doctor.py`,
  `worktree_gc.py`, `reconcile/salvage.py`) is pre-existing scatter, not
  newly introduced or newly fixed by this ADR. Honestly naming it here
  is a cost accepted in exchange for making it enforceable.
- The three non-`gh.py` entries are temporary exceptions with a
  committed consolidation path — #1284 — not a permanent grant: each is
  removed from the allowlist in the same PR that consolidates it into
  `gh.py`, so over time the allowlist can only shrink toward `{gh.py}`,
  never silently grow.
- A future contributor adding a new direct `gh` call site outside the
  allowlist trips `tests/test_ticket_boundary_guard.py` and must either
  route through `gh.py` or get explicit review sign-off to extend the
  allowlist under this same temporary-exception-plus-tracked-follow-up
  discipline.
- No new capability is added to cw by this ADR — it is purely a
  boundary-freezing decision. A genuine future need for cw Python to
  perform provider-portable ticket I/O requires a new ADR that
  explicitly supersedes this one.

## Alternatives considered

- **The original ticket's `TicketProvider` Protocol + Linear adapter.**
  Rejected — this costs a bespoke adapter per future tracker, exactly
  restating ADR-0008's own "#675" complaint (per-system bespoke
  maintenance cost) for the write path instead of the read path.
- **An N-provider descriptor system per ADR-0008, extended to cover
  writes.** Rejected — ADR-0008's contract is deliberately
  read-only/boolean-shaped (see ADR-0008 invariant 7, "query vs content
  split"); comment-posting content is out of its budget by design, and
  stretching it to cover writes would blow that budget.

## Referenced by

- #1240, ADR-0008, RFC 0005 D3
