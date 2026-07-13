# RFC 0011 — Availability- & Counterparty-Aware Holding

| Field | Value |
|-------|-------|
| Status | Proposed |
| Owner | Matt Warren |
| Date | 2026-07-12 |
| Supersedes | — |
| Related | RFC 0008 (orchestrator push channel), RFC 0009 (gate-recipe automation), RFC 0010 (native review-monitor), ADR-0006 (reap-policy fail-safe); epic #1120; epic #813 / #812 (finalize-resiliency escalation — the single-stage precedent A1/A2 generalize); #1117 (attention-signal-on-blocked, shipped); #1049 (push-auth park, shipped); #636 (classifier-deny observation, closed not_planned → #812); #1061 / #1020 (idle/budget axes); #1140 (finalize auto-merge silent-fail) |

## Summary

The autonomous loop today has a single, self-centered model of "waiting": when a
leg cannot proceed it either **dies silently** (the #1049 push-auth case, now
partially fixed) or **parks as generic `blocked_on_user`** (now at least visible,
post-#1117). Both behaviors implicitly assume the operator is the only party on
the other side of the wait. That assumption breaks in two directions this RFC
addresses:

1. **The operator is unavailable** (biometric-unlock / short-TTL SSH key locked
   overnight, network down, GitHub unreachable). Work that only *we* are waiting
   on should **hold cleanly and resume on return** — not die, not churn, not
   spam. This is the *inward* half.
2. **A teammate is waiting on us** (they requested our review; their PR is gated
   on our merge). Here a hold on *our* idleness is a **defect, not a courtesy** —
   the SLA is theirs, not ours. This is the *outward* half, and it is currently
   the least-automated path (RFC 0010's reactor is scoped to auto-dev PRs only;
   teammate PRs ride the legacy `/review-monitor` skill).

The seam that couples both is a **counterparty axis** on the hold model
(`self | external`): the *same* unavailability condition demands *opposite*
handling depending on who is blocked. This RFC defines that seam once, adds the
narrow native surface needed to see teammate review-requests at all, then builds
two independent epics on top:

- **Epic I — Availability-aware holding** (inward): detect the unavailable
  condition anywhere in the pipeline, hold as a distinct `awaiting_operator`
  class, batch the notification, and resume in one motion on return.
- **Epic II — Counterparty-aware collaboration** (outward): never silently
  hold/reap a wait whose counterparty is a teammate; and, under a **two-party
  consent** model — *my* configured enablement × *the target's explicit in-band
  GitHub action* — respond (never initiate) under strict anti-spam and
  self-fact-checking rules.

**Guiding constraint (repo principle): do not force our processes on others.**
Epic II adds no registry, label, or config a teammate must adopt. Consent for
automation-in-general is already settled by the team accepting my use of this
tool; per-interaction consent is carried by the target's own normal GitHub
actions. Every Epic II outbound is a *response to* something the target did.

Both epics ride the attention-routing contract that epic #1120's workstream 3 is
chartered to own, and reuse the `session.needs_attention` bus that #1117 just
broadened.

## Motivation

**The overnight scenario is the driver.** Operator runs a wave before bed. Under
HIPAA/SOC2 the git remote is SSH-over-biometric with a short-TTL key; while the
operator sleeps, the key is locked. Every finalize leg that needs to push or open
a PR hits an auth wall. Pre-#1049 they died silently; post-#1049 the *finalize
push sites* park as `push_auth_failed` — but that's two push sites in one stage.
The same locked key breaks intake fetch, the PR-hygiene sweep, tracker writes,
CI polling, and the review-recipe actors (`auto_fix_ci` / `address_review` /
`request_reviewer` all do remote writes or spawn sessions). Each surface has (or
lacks) its own ad-hoc handling. There is **no unified notion of "the operator is
unavailable, hold everything that needs them and tell me once."**

**The park class is undifferentiated.** `push_auth_failed` routes to
`BLOCKED_ON_USER` via `dispatch.py`'s existing fall-through (it is deliberately
kept out of `FINALIZE_REGRESS_BLOCKER_REASONS`, `auto_dev_result.py:114-119`, so
it doesn't auto-regress to IMPL against a still-locked key). But once parked it is
indistinguishable from a genuinely-wedged leg. The attention layer (`reconcile/
tasks.py:112`, `record_event(SESSION_NEEDS_ATTENTION)`) fires the same signal for
"your key is locked, resume when you're back" as for "this work is broken, come
look." An operator returning to 12 parks cannot tell the batch-resumable ones
from the ones needing thought.

**The flip side has no model at all.** The idle watchdog
(`reconcile/_shared.py:104`, `IDLE_WATCHDOG_SECONDS = 900`; detection in
`reconcile/idle.py`) parks/reaps any session that goes quiet, with no notion of
*who is blocked by the quiet*. A session mid-review of a **teammate's** PR
legitimately goes idle (waiting on CI, reading a diff) and can be reaped at the
flat 900s floor — literally blocking the teammate on our idleness, the exact
inverse of the courtesy the inward half provides. Worse, cw **cannot even see** a
teammate's review request today: `_is_candidate` (`pr_hydrate.py:281-295`) only
tracks dev-queue `TicketTask`s with a `pr_url`, and the webhook accepts only
`{ci_failed, review_received, mergeable, merged}` (`cw_pr_events_server.py:36`) —
no `review_requested`. The `external`-counterparty PR has no entry point at all.

**The plumbing to fix both already half-exists.** `#1117` made every
`blocked_on_user` park emit `session.needs_attention` — the notification
backbone. `--resume <ticket>` + `detect_current_stage()` is a cold-resume path
that derives stage from git/tracker state with no session file — the resume
backbone. RFC 0009's `gate_recipes.py` and RFC 0010's `review_recipes.py` are the
detect→act reactor template. What's missing is the *counterparty seam*, the
*narrow native surface* to see review-requests, and the two policy layers.

## Design

### S1 — The counterparty seam (shared; land first)

A single axis, read by both epics: **for any hold or idle event, who is blocked
by it?**

- `self` — only the operator/fleet is waiting (our own auto-dev PR; our own
  overnight wave). Holding is safe and courteous.
- `external` — a teammate is waiting (they requested our review). Holding on
  *our* unavailability is a defect.

**Shape (D-S1):** derived where cw already knows PR authorship — `pr_hydrate` —
rather than a stored field, to avoid drift. Auto-dev work is `self` by
construction (`_is_candidate` fences the auto-dev PR set, `pr_hydrate.py:257-261`);
`external` arises only for a review-requested teammate PR, which S2 makes visible.
A hold with no PR yet is always `self` — `external` cannot arise without a review
request, and a review request always carries a PR.

**Self-identity resolution (shared with S2).** cw has no stored GitHub self-login
today — `[cw identity]` is client-name-based (`prompts.py:64`). Both "author ≠ us"
(S1) and "requested of *me* vs my team" (S2) need a resolved operator login.
Resolved at runtime and cached, with a `clients.yaml` override for multi-account
edges (D-S2b).

### S2 — Native review-requested register (individually-targeted; PR-level first cut)

The narrowest possible slice of the `register` store RFC 0010 deferred — just
enough to let cw *see* a teammate review-request and route it through existing
rails. No `discover` (no scanning all PRs), no ported thread-state machine.

- **Trigger — individually-targeted only.** A new `review_requested` webhook type
  (+ a `cw review register <pr>` CLI entry) fires **only when the requested
  reviewer is the operator's own identity.** A team-review request the operator
  merely belongs to is explicitly **not** an action point (operator decision,
  2026-07-12). Requires S1's self-identity resolution.
- **Tracked entity.** A registered teammate PR becomes a **sibling "watched PR"
  record** (D-S2a) — *not* a `TicketTask` extension — so it hydrates through
  `pr_hydrate → attention-state → reactor/idle-watchdog` with `counterparty=external`
  while consuming no dispatch slot (keeps dev-queue lane/slot accounting clean).
- **First cut is PR-level.** The register + review-submission path lands first:
  respond to the individual review request with one self-fact-checked review (B4);
  re-review only on explicit individual re-request (B3). **Thread-level reply
  back-and-forth is deferred to a later slice** — it needs thread-event awareness
  (the bulk RFC 0010 deferred) and is infrequent (operator decision, 2026-07-12).

Everything downstream (idle-reap exemption, consent gate, response contract) rides
this entity — RFC 0010's machinery pointed outward, no new reactor.

### Epic I — Availability-aware holding (inward)

#### A1 — Distinct `awaiting_operator` park class *(keystone)*

A hold caused by operator/dependency unavailability carries a **distinct
disposition** from a generic `blocked`. It still routes to `BLOCKED_ON_USER`
(reuse the existing lane-slot + concierge semantics) but is tagged so the
attention layer and the resume path can treat it as *waiting-on-you*, not
*broken*. Like `push_auth_failed`, it MUST stay out of
`FINALIZE_REGRESS_BLOCKER_REASONS` (`auto_dev_result.py:114-119`) — an unavailable
operator is not fixed by re-running IMPL. **Shape (D-A1): a new `blocker.reason`
value + a disposition tag on the existing `BLOCKED_ON_USER` route — no new
`QueueItemStatus`, no parser bump.** `#1049`'s `push_auth_failed` is retro-classified
as the first *instance* of this class. This generalizes the finalize-resiliency
escalation (epic #813 / #812) from one stage to any stage.

#### A2 — Generalize the unavailability detector

`#1049` detects auth failure at finalize's two push sites from a fixed
4-signature list (`Permission denied (publickey)`, `could not read Username`,
`Host key verification failed`, `Authentication failed`). Generalize to a shared
classifier over the full failure family — {network-unreachable, GitHub
5xx/secondary-rate-limit, auth-failure, MCP-github-unreachable, auto-mode
classifier-deny (`tool_denied`, #636)} — mapping all to the A1 class, applied
wherever a leg touches the remote or the `gh`/MCP-github surface: intake fetch,
PR-hygiene sweep, tracker read/write, CI poll, and the push sites. **Rollout
(D-A2): one shared classifier, staged surface-by-surface — push sites first
(generalize #1049's already-shipped classifier), then intake / hygiene / tracker /
CI-poll.** The per-surface list of what can emit a structured blocker today vs.
what still dies silently is enumerated at ticket time (deferred detail).

#### A3 — Proactive stop-before-finalize hold

A per-run flag (`--hold-finalize`) and/or per-client config (`finalize_gate:
manual`) that, for an operator-away wave, runs intake→review to completion (none
of which needs the key) and holds **every** leg at the Stage 3→4 boundary as
`awaiting_operator`, emitting one digest instead of N noisy per-leg auth walls.
Morning = one `/auto-dev-finalize --resume` pass per held ticket under live auth.
**Arming (D-A3): both, layered** — the A5 probe auto-detects and holds; the
flag/config force-holds even when auth is live at dispatch. Under a force-hold,
Small-scope tickets (which never park today) are held too.

#### A4 — Auto-resume-on-return

`cw dev-queue resume --held` (name TBD): re-fire every `awaiting_operator` ticket
through the existing `--resume <ticket>` path (which jumps to
`detect_current_stage()`). Closes the loop: hold overnight → digest → one-command
drain under live biometric auth.

#### A5 — Availability preflight probe

A cheap probe (`git ls-remote` dry-run / `gh auth status`) at dispatch/tick time.
If the remote/auth is down, **do not spin up a full session that will just die** —
hold at the queue level. **Cadence (D-A5): probe at dispatch and immediately
before finalize — not on every reconcile tick — and cache the result with a short
(~1 min) TTL** so the network cost stays bounded.

#### A6 — Digest / batch on the attention channel

Coalesce N `awaiting_operator` parks into **one** operator signal rather than N
`session.needs_attention` pushes at 3am. Also the delivery vehicle for B5's
rejection flags. **Ownership (D-A6): a cw-side coalescer in the same layer as
#1117's emit; coalesce only `awaiting_operator` (held) signals — genuine
`blocked`/broken parks stay immediate so batching never delays an urgent signal.**

### Epic II — Counterparty-aware collaboration (outward)

Everything here rides S1 (counterparty axis) + S2 (native review-request
visibility). All outbound is a **response to** the target's in-band action.

#### B1 — Teammate-review idle-reap exemption

A watched-PR session whose counterparty (S1) is `external` is **exempt from silent
idle-reap** (`reconcile/idle.py`, gated by `resolve_idle_watchdog_budget`,
`_shared.py:1143`). Instead of reaping a quiet review-of-a-teammate's-PR session,
surface that a teammate is waiting and escalate. Coordinates with the per-stage
idle axis (#1061) — this adds the counterparty axis alongside stage/tier.

#### B2 — Two-party consent: *my enable* × *their in-band action*

Consent is **not** a registry, label, or opt-in record a teammate must maintain —
that would force our tooling on them (repo principle) and re-litigate consent the
team already gave by accepting my use of this tool. Instead:

- **Party 1 (operator):** outbound acting enabled in cw settings (the existing
  `review_recipes` master-switch/lane plumbing, default off).
- **Party 2 (target):** consent is an **explicit in-band GitHub action that opens
  the channel** — the individual review request itself (S2), or a re-review
  re-engagement (e.g. moving off "request changes" to re-review after we
  addressed feedback). Every outbound is a **scoped response to** that action.
  **We never initiate unsolicited outbound.**

Our own authored PRs remain **unrestricted** — B2 governs acting *toward others*,
not our own PRs.

#### B3 — Individual re-request gates re-review (anti-spam)

A re-review fires **only** on an explicit **individual** re-request. New commits
pushed to a PR we already reviewed do **not** auto-retrigger — and a *team*
re-request is never our action point. Deliberately diverges from the inward
reactor's "act on every transition" posture.

#### B4 — Response contract

The review we submit (and, later, thread replies) is **succinct and answer-first**,
leans toward *"here's how you can validate this claim,"* and is **self-fact-checked
before posting** — each asserted claim verified against the diff/CI/source first
(reuses the repo's No-Unverified-Claims + completion-artifacts discipline and the
adversarial-verify pattern). **Mechanism (D-B4): a pre-post verification pass
checks each claim against diff/CI/source; an unverifiable claim is dropped or
flagged-as-unverified and the rest posts — we do NOT hold the whole review on one
bad claim, since holding would block the teammate (the counterparty ethos).**

#### B5 — Graceful rejection (accept, don't argue)

If the target rejects/dismisses a section of our review, the default is to
**accept it — no push-back reply.** Instead, emit an **inbound** operator signal
(via `session.needs_attention` / the A6 digest) so the operator ensures follow-up
tickets/work are captured. Rejection is never an outbound trigger.

## Resolved constraints (operator, 2026-07-12)

- **Two epics, not one**, over a shared seam; #1120 stays discovery/audit shaped.
- **Shared seams (S1 + S2) land first.** Both epics read the counterparty axis;
  Epic II needs S2 to see review-requests.
- **In-house, minimally.** Epic II rides a *scoped* native register (S2), not the
  full RFC-0006 legacy port (#678/#686–690) — that stays available as separate
  cleanup.
- **Do not force our processes on others.** No collaborator registry / label /
  opt-in file a teammate must adopt. Automation consent is already settled by the
  team accepting the tool; per-interaction consent is the target's own in-band
  GitHub action.
- **Two-party consent = operator config-enable × target's explicit in-band
  channel-opening action.** Outbound is always a scoped *response*, never
  initiated.
- **Review requests act on the operator's individual identity only** — team
  requests are ignored (S2, B3).
- **Rejection of a review section ⇒ accept + flag operator for follow-up**, never
  push back (B5).
- **Thread-reply back-and-forth deferred** to a later slice (infrequent).
- **Anything we author is fair game** — the consent model guards only acting
  toward others' PRs.
- **Tracker of record is RFC + GitHub epic issues + milestone**, matching RFC
  0010. Notion is an optional roadmap layer, not where cw sprints live.

## Explicitly out of scope

- **A collaborator opt-in registry / standing per-teammate consent record** —
  explicitly rejected (forces our process on others; consent is in-band).
- **Acting on team-directed review requests** — individual-target only.
- **Free-form thread-reply back-and-forth** — deferred to a later slice, not this
  RFC's first cut.
- **Pushing back on a rejected review section** — we accept and flag, never argue.
- **The full teammate-PR tracking store** (`discover`, thread-level delta-review —
  the bulk of the legacy `review_monitor.py`). S2 is `register`-on-individual-
  review-request only.
- **Outbound nudge/DM queue** — outbound writes go to the PR the request came
  from; no general outbound-message drain (RFC 0010 boundary, retained).
- **Multi-operator / delegation** — "hand a teammate-blocking review to a
  *different* reviewer when I'm away" is deferred; Epic II escalates/signals, it
  does not reroute work to other humans.
- **Making unavailability recoverable without the operator** — we detect and hold;
  we do not cache credentials or keep the key unlocked (that would defeat the
  compliance posture that motivates the RFC).

## Phasing

Two epics over the shared seams. After the seams, the two tracks run largely in
parallel; within each track the keystone gates the rest.

| Wave | Track A (availability) | Track B (counterparty) |
|------|------------------------|------------------------|
| 0 (seams, blocking) | **S1 — counterparty axis + self-identity** | **S2 — native review-request register** (rides S1) |
| 1 | A1 (park class, keystone) · A2 (detector) · A5 (probe) | B2 (consent gate) · B1 (idle exemption) |
| 2 | A3 (stop-before-finalize) · A4 (auto-resume) · A6 (digest) | B3 (individual re-request) · B4 (response contract) · B5 (graceful rejection) |

Independent, any lane, no dependency on the above: RFC 0010 P5 (#1100), the
RFC-0006 B-series legacy-script consolidation (#678, #686–#690), #1140.

## Resolved decisions (hardening pass, operator, 2026-07-12)

Firm leans, decided to unblock sprint breakout. Each is reversible at
ticket-hardening if the code contradicts it.

- **D-S1 — Counterparty derivation.** Derive at `pr_hydrate`, no stored field. A
  hold with no PR is always `self` (`external` cannot arise without a review
  request, which always carries a PR).
- **D-S2a — Watched-PR entity.** A sibling "watched PR" record, *not* a
  `TicketTask` extension — consumes no dispatch slot, keeps dev-queue lane/slot
  accounting clean.
- **D-S2b — Self-identity.** Resolve the operator GitHub login at runtime
  (`get_me`-equivalent) and cache it; allow a `clients.yaml` override for the rare
  multi-account case.
- **D-A1 — Park-class shape.** New `blocker.reason` value + a disposition tag on
  the existing `BLOCKED_ON_USER` route. No new `QueueItemStatus`, no schema/parser
  bump. `push_auth_failed` is retro-classified as the first instance; it and the
  new reason both stay out of `FINALIZE_REGRESS_BLOCKER_REASONS`.
- **D-A2 — Detector rollout.** One shared classifier over the failure family,
  staged surface-by-surface: push sites first (generalize #1049), then intake /
  hygiene / tracker / CI-poll.
- **D-A3 — Arming.** Both, layered: A5 probe auto-detects and holds;
  `--hold-finalize` / `finalize_gate: manual` force-holds even when auth is live.
  Force-hold also holds Small-scope tickets (which never park today).
- **D-A5 — Probe cadence.** Probe at dispatch and immediately before finalize —
  not every reconcile tick — cached with a ~1 min TTL.
- **D-A6 — Digest ownership.** cw-side coalescer in #1117's layer; coalesce only
  `awaiting_operator` (held) signals — genuine `blocked`/broken parks stay
  immediate.
- **D-B4 — Self-fact-check.** A pre-post verification pass validates each claim
  against diff/CI/source; an unverifiable claim is dropped/flagged and the rest
  posts. Never hold the whole review on one bad claim (would block the teammate).

### Deferred to ticket-hardening (code-dependent; not blocking this RFC)

- The per-surface list for D-A2 of what can emit a structured blocker today vs.
  what still dies silently.
- Digest window/threshold tuning for D-A6.
- The exact `blocker.reason` string(s) and disposition-tag field name for D-A1,
  validated against the `Blocker` model and the headless contract.

## References

- `src/cw/reconcile/tasks.py:112` — `record_event(SESSION_NEEDS_ATTENTION)`,
  the attention emit broadened by #1117 (edge-triggered on the BLOCKED_ON_USER write)
- `src/cw/reconcile/_shared.py:104,1143` — `IDLE_WATCHDOG_SECONDS = 900`,
  `resolve_idle_watchdog_budget` (the idle budget B1 adds a counterparty axis to)
- `src/cw/reconcile/idle.py:311,352,610` — idle detect / route-by-policy / act
  (the reap path B1 exempts `external` from)
- `src/cw/auto_dev_result.py:114-119` — `FINALIZE_REGRESS_BLOCKER_REASONS`
  (the regress set A1/`push_auth_failed` must stay out of)
- `.claude/commands/auto-dev-finalize.md` — the `push_auth_failed` classifier &
  two push sites (#1049), the first instance A2 generalizes
- `src/cw/pr_hydrate.py:257-261,281-295` — `_is_candidate` (auto-dev-PR fencing);
  where S1 counterparty derivation and S2 hydration would live
- `src/cw/cw_pr_events_server.py:36` — `_VALID_EVENT_TYPES` (no `review_requested`
  today; S2 adds it)
- `src/cw/review_strategy.py:48` — existing `ReviewStrategyMode` reviewer-config
  pattern (`repo_owner`/`reviewer_team`) to mirror, not duplicate
- `src/cw/prompts.py:64` — client-name `[cw identity]` (no GitHub self-login yet;
  S1/S2 self-identity dependency)
- `src/cw/reconcile/review_recipes.py` — RFC 0010 reactor; the machinery S2 points
  outward
- `src/cw/reconcile/gate_recipes.py` — the detect/act/resolve template (RFC 0009)
- `docs/rfcs/0010-native-review-monitor.md` — the reactor this builds on; its
  Option-A (auto-dev-PRs-only) boundary is what S2/Epic II extends outward
- `#1117` (attention on all parks, shipped), `#1049` (push-auth park, shipped),
  `#1061`/`#1020` (idle/budget axes), `#1120` (orchestration epic; workstream-3
  attention-routing contract)
- `#813` / `#812` (finalize-resiliency epic; blocked-finalize escalation — the
  single-stage precedent A1/A2 generalize), `#636` (classifier-deny `gh pr create`,
  closed not_planned → #812), `#1140` (finalize auto-merge silent-fail)

Issues: (to be filed — two epic issues + S1/S2 seam tickets, then children per wave)
