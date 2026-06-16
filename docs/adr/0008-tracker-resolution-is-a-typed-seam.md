# Tracker resolution is a declared descriptor, not bespoke code

**Status:** Proposed
**Driven by:** #675 (tracker resolution is gh-only in practice despite "agnostic" prose); builds on #412 (headless workers hung on Linear OAuth), #569 (agnostic-prose + mapping table), #659 (RFC 0005 B1 froze prose delegation), #190 (the `github_issue_existing` plan-source bolt-on)

## Decision

A new ticket system is added by writing a **declarative descriptor** and making
a **conformance test pass** — never by writing a new code path. Tracker
resolution is therefore **neither** prose the LLM is trusted to apply **nor** a
`Tracker` Protocol with a hand-written `GitHubTracker` / `LinearTracker` /
`NotionTracker` class each. Both of those still cost *one bespoke implementation
per ticket system* — the thing #675 exists to kill. Instead:

1. A **closed operation contract**: a small, fixed set of typed operations cw's
   *deterministic Python* is permitted to need (below). cw may depend only on
   this set.
2. A **descriptor registry**: each tracker is a row of data declaring, per
   contract operation, *how* it is satisfied — a CLI-command template, an
   MCP-tool reference, or `unsupported` — plus its id-grammar and its doctor
   prerequisites. One generic interpreter executes descriptors; there is no
   per-tracker class.
3. A **conformance suite**: one parametrized test every descriptor must pass.
   Green = the tracker is wired. This is the enforceable artifact a reviewer
   cites.

The active descriptor is resolved once from `.claude/project-config.yaml` →
`tracking.primary.system` (the canonical **nested** form). `cw doctor`
validates the config and runs the active descriptor's declared prerequisite
check. The marginal cost of a new tracker is **a descriptor + a green
conformance run**, not a code module to maintain.

This does not make cw a tracker-*content* engine. The descriptor covers the
deterministic *queries* cw already performs in code; the *content* an agent
writes onto a ticket (stage trace comments, deferred-thread bodies) stays in
the executor/skill layer per **RFC 0005 D3**. See
[Reconciliation with RFC 0005](#reconciliation-with-rfc-0005).

## The closed contract (the budget)

cw's deterministic Python may depend only on this fixed set — all returning
bool/enum, so a descriptor realizes each as a CLI template + a trivial
extraction rule (a JSON path or an exit-code), never a bespoke parser:

| Operation | Signature | Realization example (github-issues) |
|---|---|---|
| `pr_merged` | `(ticket_id) -> bool \| None` | `gh pr view {pr} --json state` → `.state == "MERGED"` |
| `open_pr_exists` | `(branch) -> bool \| None` | `gh pr list --head {branch} --state open --json number` → non-empty |
| `issue_exists` | `(ticket_id) -> bool \| None` | `gh issue view {id}` → exit 0 |
| (id-grammar) | declared regex | `^\d+$` — drives `resolve_client` + validation |

`resolve_client` is generic over the descriptor's id-grammar (no forge I/O): a
bare integer (`403`) is a valid github-issues id; `PREFIX-NNN` is a valid linear
id. The grammar lives in the descriptor, not in a hardcoded `split("-")`.

**The contract is a budget, and the budget is the real invariant.** Every
operation in it is paid N times — once per descriptor. So a richer need (e.g.
*read an issue body* in deterministic Python) is not free: it would force a
parse rule beyond "json-path / exit-code," reintroducing per-system bespoke
code. The codified rule: **new tracker operations default to the agent/MCP
edge** (where an LLM normalizes divergent shapes); promoting one into the cw
contract is a deliberate, reviewed decision, not a reflex. Keeping the contract
small is what keeps trackers declarative.

## Invariant

1. **Trackers are declared, not coded.** A registered tracker is a descriptor
   row + a green conformance run. A PR that adds a `class <Name>Tracker` with
   per-method forge logic, or adds an operation to the closed contract, is the
   thing a reviewer challenges on sight.
2. **The contract is closed and budgeted** (above). cw's deterministic Python
   depends only on the fixed operation set + the descriptor's id-grammar.
3. **One resolution point.** The active descriptor is resolved exactly once,
   from `tracking.primary.system`, via a single `resolve_tracker(config)`
   factory. No module re-reads the config or re-derives the system. Absent /
   unparseable config is a **hard, named** `cw doctor` failure, never a silent
   fallback to `gh`.
4. **Every descriptor passes conformance.** The suite asserts: id-grammar
   compiles and accepts/rejects representative ids; every contract operation is
   realized (`cli` | `mcp` | `unsupported`) with no operation silently missing;
   `cli` realizations carry a valid argv template + extraction rule;
   prerequisites declare a check; the descriptor validates against its own
   schema. A registered-but-not-green descriptor is a build failure.
5. **No hardcoded forge outside the interpreter.** A `gh issue`/`gh pr`
   subprocess or an `mcp__plugin_linear_*` literal anywhere other than a
   descriptor's declared realization is a bug. `src/cw/gh.py`'s four functions
   become the github-issues descriptor's `cli` realizations.
6. **Recognized systems are a closed set.** `tracking.primary.system` ∈
   {`github-issues`, `linear`} for now (extensible by adding a descriptor). An
   unrecognized value fails doctor; it does not degrade to a default.
7. **Query vs content split.** Descriptors realize cw's deterministic queries.
   An `mcp`-kind realization is returned to the skill/executor to run — cw
   Python never executes it; if cw's deterministic path hits an `mcp`-only
   operation, that is the named "tracker unreachable headless" doctor failure
   (the #412 class), not a hang. Trace-comment *content* is not a contract
   operation (RFC 0005 D3).

## What this means for callers

- **`src/cw/gh.py`** → its four functions (`pr_is_merged_for_ticket`,
  `pr_exists_for_branch`, the two private fetchers) become the github-issues
  descriptor's `cli` realizations, invoked by the generic interpreter. Callers
  (`doctor.py`'s merged-PR check, reconcile/dispatch PR-state reads) call
  `resolve_tracker(config).pr_merged(...)`, not `gh`.
- **`src/cw/dev_queue.py` `resolve_client`** drops the `"-" in ticket_id` /
  `linear_prefix_map` assumption and resolves over the descriptor's id-grammar:
  a bare GitHub id resolves a single-client repo to its sole client;
  multi-client repos still require `--client`, but the error names the real
  cause, not Linear.
- **`src/cw/auto_dev_result.py`** — the twin `linear_existing` /
  `github_issue_existing` `PlanSource` literals derive from the active
  descriptor instead of being hand-maintained forge labels.
- **`cw doctor`** adds `_check_project_config()` returning the standard
  `CheckResult(name, ok, detail, warn)`: config parses, system recognized,
  descriptor's declared prerequisite check passes — actionable output instead
  of a downstream silent stall.
- **`.claude/commands/review-monitor.md`** (deferred-thread filing at ~:219,
  today a hardcoded `mcp__plugin_linear_linear__save_issue`) and
  **`.claude/skills/cw-smoke-test/scripts/preflight.py`** (hardcoded
  `gh issue`/`gh pr`) consume the active descriptor's realization for the
  operation instead of hardcoding one tracker — so neither hits the wrong
  tracker on a repo configured for the other.

## What this means for producers

- **The descriptor registry** (`src/cw/tracker.py` + a `descriptors/` data
  table) is the only place a concrete forge is bound. Adding Notion or a local
  tracker is one descriptor row + a green conformance run — no edits scattered
  across `gh.py`, `dev_queue`, and two skills.
- **`/setup`** must write the canonical nested `tracking.primary.system`. The
  pre-existing flat-vs-nested divergence (`/setup` writes flat
  `tracking.system`; `/auto-dev` + the deployed config read nested
  `tracking.primary.system`; `queue-issues` reads flat) is **resolved here**:
  doctor flags the flat-only form, and the resolver reads only the nested
  canonical path. (RFC 0005 explicitly punted this; this ADR is where it
  lands.)
- **The descriptor is a Pydantic model**, so it publishes through `cw schema`
  like every other contract — the conformance suite and any future tracker
  author work against a typed, versioned shape.

## Reconciliation with RFC 0005

RFC 0005 **D3** states "cw's Python stays tracker-blind — it owns `ticket_id`
as an opaque string; all tracker I/O is the executor/skill layer's job." This
ADR does not contradict it, once the two I/O classes are separated:

| I/O class | Example | Owner | This ADR |
|---|---|---|---|
| **cw deterministic query** | "is the PR for ticket X merged?", "open PR for branch B?", "what client owns id 403?", "is `gh` on PATH?" | cw Python | a contract operation, realized by the active descriptor |
| **tracker content** | "post the plan summary / review findings / deferred-thread comment onto the ticket" | executor/skill | stays prose/MCP per RFC 0005 D3 — **unchanged**; not a contract operation |

cw was **never** actually tracker-blind for the first class — `gh.py` already
shells `gh` from cw Python today; D3's prose described an aspiration the code
contradicts (the premise of #675). This ADR makes the *existing* cw-owned forge
calls declared and conformance-tested, and leaves D3's *content* boundary
intact.

## Consequences

- **A descriptor interpreter + conformance suite are new surface.** The payoff
  is that the per-system cost collapses from "a maintained class" to "a data row
  + a passing test," and the silent-stall failure mode (#412-class) becomes a
  named doctor failure when config and reality disagree.
- **The boolean-only contract is a real ceiling, by design.** Anything cw needs
  that isn't expressible as "CLI template → json-path / exit-code" cannot be a
  contract operation without reintroducing bespoke parsing — that pressure is
  meant to be felt and pushed to the agent/MCP edge (invariant 2). If a future
  need genuinely belongs in deterministic cw, adding it is an explicit ADR
  amendment, not a quiet method.
- **`mcp`-kind realizations cannot run headless** (the #412 OAuth hang). The
  descriptor model does not fix that; it declares it (`unsupported` / `mcp` for
  the headless context) so it surfaces as a doctor failure, not a hung worker.
  `github-issues` stays the pinned default for this repo.
- **Naming debt acknowledged.** `linear_url`, `linear_prefix_map` keep their
  names for one release (renaming is a config migration with its own blast
  radius); the descriptor reads them tracker-agnostically. Rename is a
  follow-up, not a blocker.

## Alternatives considered

- **A `Tracker` Protocol with a class per system.** Rejected — this was the
  first-draft direction and it still costs one bespoke, hand-maintained
  implementation per ticket system, which is exactly #675's complaint. A
  Protocol removes the *scatter* (hardcoded `gh` in five files) but not the
  *marginal cost*; the descriptor + conformance model removes both.
- **Prose / MCP-only + doctor (no code seam at all).** Keep cw tracker-blind,
  add only the doctor check and branch the two hardcoded skills. Rejected by the
  owner: once `.claude/project-config.yaml` is relied on to steer resolution,
  the steering must be reliable and verifiable in code — a prose table the LLM
  may or may not apply is the unreliability #675 documents. Doctor alone
  validates the config but does nothing to stop `gh.py` / `dev_queue` / the
  sentinel literals from being structurally gh-shaped.
- **Full tracker-content abstraction in cw** (stage comments, plan posting, all
  I/O as contract operations). Rejected: contradicts RFC 0005 D3, pulls
  narrative/LLM-shaped work into typed Python, and blows the contract budget —
  the query/content split is the line.

## Referenced by

- RFC 0005 (D3 content boundary), #675, #412, #569, #659, #190
