# RFC 0011 — Availability- & Counterparty-Aware Holding

| Field | Value |
|-------|-------|
| Status | Draft |
| Owner | Matt Warren |
| Date | 2026-07-12 |
| Supersedes | — |
| Related | RFC 0008 (orchestrator push channel), RFC 0009 (gate-recipe automation), RFC 0010 (native review-monitor), ADR-0006 (reap-policy fail-safe); epic #1120; #1117 (attention-signal-on-blocked, shipped); #1049 (push-auth park, shipped); #1061 / #1020 (idle/budget axes) |

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
handling depending on who is blocked. This RFC defines that seam once, then
builds two independent epics on top of it:

- **Epic I — Availability-aware holding** (inward): detect the unavailable
  condition anywhere in the pipeline, hold as a distinct `awaiting_operator`
  class, batch the notification, and resume in one motion on return.
- **Epic II — Counterparty-aware collaboration** (outward): never silently
  hold/reap a wait whose counterparty is a teammate; and, behind a **2-party
  opt-in**, act on others' PRs under strict anti-spam and self-fact-checking
  rules.

Both epics ride the attention-routing contract that epic #1120's workstream 3
is chartered to own, and reuse the `session.needs_attention` bus that #1117
just broadened.

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
inverse of the courtesy the inward half provides. And there is no gate, no
anti-spam rule, and no response-quality contract for the outward writes at all,
because the native reactor never had to make them (RFC 0010 §Scope: auto-dev PRs
only).

**The plumbing to fix both already half-exists.** `#1117` made every
`blocked_on_user` park emit `session.needs_attention` — the notification
backbone. `--resume <ticket>` + `detect_current_stage()` is a cold-resume path
that derives stage from git/tracker state with no session file — the resume
backbone. RFC 0009's `gate_recipes.py` and RFC 0010's `review_recipes.py` are the
detect→act reactor template. What's missing is the *counterparty seam* and the
two policy layers built on it.

## Design

### S1 — The counterparty seam (shared; land first)

A single axis, read by both epics: **for any hold or idle event, who is blocked
by it?**

- `self` — only the operator/fleet is waiting (our own auto-dev PR; our own
  overnight wave). Holding is safe and courteous.
- `external` — a teammate is waiting (they requested our review; their PR gates
  on our merge/reply). Holding on *our* unavailability is a defect.

**Shape (open — see OQ-S1):** the leading candidate is a derived classification
computed where cw already knows PR authorship — `pr_hydrate` — plus an explicit
field on the hold record for cases with no PR yet. For auto-dev-originated work
the counterparty is `self` by construction (`_is_candidate` already fences the
auto-dev PR set, `pr_hydrate.py:257-261`); `external` only arises for
review-of-teammate-PR work, which today lives outside the reactor.

Every downstream policy branches on this axis:

| Condition | `self` | `external` |
|-----------|--------|------------|
| Unavailability hold (Epic I) | batch, hold, resume on return | escalate with urgency; never silent indefinite hold |
| Idle watchdog (Epic II B1) | park per policy | **exempt from silent reap**; surface that a teammate waits |
| Outward write (Epic II B2) | unrestricted (own PR) | 2-party opt-in gated |

### Epic I — Availability-aware holding (inward)

#### A1 — Distinct `awaiting_operator` park class *(keystone)*

A hold caused by operator/dependency unavailability carries a **distinct
disposition** from a generic `blocked`. It still routes to `BLOCKED_ON_USER`
(reuse the existing lane-slot + concierge semantics) but is tagged so the
attention layer and the resume path can treat it as *waiting-on-you*, not
*broken*. Like `push_auth_failed`, it MUST stay out of
`FINALIZE_REGRESS_BLOCKER_REASONS` (`auto_dev_result.py:114-119`) — an unavailable
operator is not fixed by re-running IMPL.

**Shape (open — see OQ-A1):** a new `blocker.reason` value + a disposition tag,
vs a first-class dimension on the park record. Whatever the shape, `#1049`'s
`push_auth_failed` becomes the first *instance* of this class, not a parallel
one-off.

#### A2 — Generalize the unavailability detector

`#1049` detects auth failure at finalize's two push sites from a fixed
4-signature list (`Permission denied (publickey)`, `could not read Username`,
`Host key verification failed`, `Authentication failed`). Generalize to a shared
classifier over the full failure family — {network-unreachable, GitHub
5xx/secondary-rate-limit, auth-failure, MCP-github-unreachable, auto-mode
classifier-deny (`tool_denied`, #636)} — mapping all of them to the A1 class,
applied wherever a leg touches the remote or the `gh`/MCP-github surface: intake
fetch, PR-hygiene sweep, tracker read/write, CI poll, and the push sites.

#### A3 — Proactive stop-before-finalize hold

A per-run flag (`--hold-finalize`) and/or per-client config (`finalize_gate:
manual`) that, for an operator-away wave, runs intake→review to completion (none
of which needs the key) and holds **every** leg at the Stage 3→4 boundary as
`awaiting_operator`, emitting one digest instead of N noisy per-leg auth walls.
Morning = one `/auto-dev-finalize --resume` pass per held ticket under live auth.
(Whether "operator away" is *declared* by the flag or *detected* by A5 is OQ-A3.)

#### A4 — Auto-resume-on-return

`cw dev-queue resume --held` (name TBD): re-fire every `awaiting_operator` ticket
through the existing `--resume <ticket>` path (which jumps to
`detect_current_stage()` and picks up mid-pipeline). Closes the loop: hold
overnight → digest → one-command drain under live biometric auth.

#### A5 — Availability preflight probe

A cheap probe (`git ls-remote` dry-run / `gh auth status`) run at dispatch/tick
time. If the remote/auth is down, **do not spin up a full session that will just
die** — hold at the queue level. Detects "GitHub unavailable" at the front door
instead of at the wall; biggest efficiency win overnight. Cadence/caching is
OQ-A5 (per-tick network cost is real).

#### A6 — Digest / batch on the attention channel

Coalesce N `awaiting_operator` parks into **one** operator signal ("12 legs held
pending your auth, resume with `cw … resume --held`") rather than N
`session.needs_attention` pushes at 3am. Client-side of #1117; may live in
`attention_monitor` or as a cw-side coalescer (OQ-A6).

### Epic II — Counterparty-aware collaboration (outward)

#### B1 — Teammate-review idle-reap exemption

A session whose counterparty (S1) is `external` is **exempt from silent
idle-reap** (`reconcile/idle.py`, gated by `resolve_idle_watchdog_budget`,
`_shared.py:1143`). Instead of reaping a quiet review-of-a-teammate's-PR session,
surface that a teammate is waiting and escalate. Coordinates with the already-open
per-stage idle axis (#1061) — this adds the counterparty axis alongside the
stage/tier axes.

#### B2 — 2-party opt-in outward-signal gate

All outward writes to **others'** PRs (review submissions, thread replies,
`request_reviewer`-style actions) are gated by a **2-party opt-in**: enabled in
*our* cw settings **and** the counterparty has opted in. Default off; no
unilateral outward writes. **Our own authored PRs are unrestricted** — the gate
guards only acting on someone else's PR. How the counterparty's opt-in is
recorded/discovered is OQ-B2 (the crux of "2-party").

#### B3 — Request-gated re-review (anti-spam)

A re-review fires **only** on an explicit review *request*. New commits pushed to
a PR we've already reviewed do **not** auto-retrigger — a re-request is required.
This is the anti-spam boundary on the outward reactor; it deliberately diverges
from the inward reactor's "act on every transition" posture.

#### B4 — Thread-response contract

Outward thread responses are **succinct and answer-first**, lean toward *"here's
how you can validate this claim,"* and are **self-fact-checked before posting** —
each asserted claim verified against the diff/CI/source first (reuses the repo's
existing No-Unverified-Claims + completion-artifacts discipline, and the
adversarial-verify pattern). Applied to the `address-review` vendored skill and
any outward-reply path. Mechanism for the pre-post check is OQ-B4.

## Resolved constraints (operator, 2026-07-12)

- **Two epics, not one.** The inward (availability) and outward (counterparty)
  concerns have different blast radii and reviewers; #1120 stays discovery/audit
  shaped and is not the home for either build. This RFC is the shared spec; each
  epic is filed as a sibling issue.
- **Shared seam (S1) lands solo, first.** Both epics read the counterparty axis.
- **Outward signal is opt-in and 2-party** (B2). Default off.
- **Re-review only when requested; anti-spam on new commits** (B3).
- **Anything we author is fair game** — the opt-in gate guards only others' PRs.
- **Thread responses: succinct, validate-our-claims, self-fact-check** (B4).
- **Tracker of record is RFC + GitHub epic issues + milestone**, matching RFC
  0010. Notion is an optional roadmap layer above execution, not where cw sprints
  live.

## Explicitly out of scope

- **The full teammate-PR tracking store** (`register`/`discover`, the bulk of the
  legacy `review_monitor.py`) — same Option-A boundary RFC 0010 drew. Epic II
  acts on *review-requested* teammate PRs; it does not build a general PR
  registry.
- **Outbound nudge/DM queue** — B2/B4 write to the PR the request came from; they
  do not build a general outbound-message drain (RFC 0010 out-of-scope, retained).
- **Multi-operator / delegation** — "hand a teammate-blocking review to a
  *different* reviewer when I'm away" is a tempting A×B interaction but is
  deferred; Epic II escalates/signals, it does not reroute work to other humans.
- **Making unavailability *recoverable without the operator*** — we detect and
  hold; we do not, e.g., cache credentials or hold the key unlocked (that would
  defeat the compliance posture that motivates the whole RFC).

## Phasing

Two epics over the shared seam. After S1, the two tracks run largely in parallel;
within each track the keystone gates the rest.

| Wave | Track A (availability) | Track B (counterparty) |
|------|------------------------|------------------------|
| 0 (solo, blocking) | **S1 — counterparty axis** | — |
| 1 | A1 (park class, keystone) · A2 (detector) · A5 (probe) | B2 (opt-in gate, keystone) · B1 (idle exemption) |
| 2 | A3 (stop-before-finalize) · A4 (auto-resume) · A6 (digest) | B3 (request-gated re-review) · B4 (response contract) |

Independent, any lane, no dependency on the above: RFC 0010 P5 (#1100), the
RFC-0006 B-series legacy-script consolidation (#678, #686–#690), #1140.

## Open questions

- **OQ-S1 — Counterparty representation.** Derived at `pr_hydrate` from PR
  authorship + review-requested-of-us, a stored field on the hold record, or
  both? How is `external` determined for a hold that has no PR yet? (Auto-dev work
  is `self` by construction — is that assumption safe in every path?)
- **OQ-A1 — Park-class shape.** New `blocker.reason` + disposition tag, vs a
  first-class dimension on the park record? Must compose with the existing
  `push_auth_failed` handling and the `FINALIZE_REGRESS_BLOCKER_REASONS`
  exclusion without a parser bump if possible.
- **OQ-A2 — Detector reach in v1.** Ship the generalized classifier across all
  five surfaces at once, or start with finalize+intake and widen? Which surfaces
  can even *emit* a structured blocker today vs die?
- **OQ-A3 — Declared vs detected "away."** Is stop-before-finalize armed by an
  explicit flag/config, by the A5 probe, or both (flag forces; probe
  auto-detects)? Interaction with existing scope-tier auto-advance (Small tickets
  never park today — do they need to be force-held here?).
- **OQ-A5 — Probe cost/cadence.** Per-tick network probing is expensive. Per
  dispatch only? Cached with a short TTL? Where does the probe live relative to
  the reconcile tick?
- **OQ-A6 — Digest ownership.** cw-side coalescer vs client-side
  `attention_monitor` batching. How is a "batch window" defined without delaying a
  genuinely urgent (broken, not held) signal?
- **OQ-B1 — Does Epic II require the legacy port first?** Review-of-teammate-PR
  work lives on the legacy `/review-monitor` skill, outside the reactor. Can B1/B2
  ride the legacy path, or do they depend on the RFC-0006 B-series (`cw review`
  subcommands, #678/#686–690) landing first?
- **OQ-B2 — How is the counterparty's opt-in recorded?** A per-repo/per-org
  allowlist in cw settings, a label/marker the teammate adds, a shared registry?
  This is the crux of "2-party" and the highest-uncertainty item in the RFC.
- **OQ-B4 — Self-fact-check mechanism.** A pre-post verification pass (spawn a
  checker that validates each claim against diff/CI/source before the reply
  posts), reusing the adversarial-verify pattern? What's the failure mode when a
  claim can't be verified — hold the reply, or post with the unverified claim
  flagged?

## References

- `src/cw/reconcile/tasks.py:112` — `record_event(SESSION_NEEDS_ATTENTION)`, the
  attention emit broadened by #1117 (edge-triggered on the BLOCKED_ON_USER write)
- `src/cw/reconcile/_shared.py:104,1143` — `IDLE_WATCHDOG_SECONDS = 900`,
  `resolve_idle_watchdog_budget` (the idle budget B1 adds a counterparty axis to)
- `src/cw/reconcile/idle.py:311,352,610` — idle detect / route-by-policy / act
  (the reap path B1 exempts `external` from)
- `src/cw/auto_dev_result.py:114-119` — `FINALIZE_REGRESS_BLOCKER_REASONS`
  (the regress set A1/`push_auth_failed` must stay out of)
- `.claude/commands/auto-dev-finalize.md` — the `push_auth_failed` classifier &
  two push sites (#1049), the first instance A2 generalizes
- `src/cw/pr_hydrate.py:97-143,257-261` — `_compute_attention_state`,
  `_is_candidate` (where S1 counterparty derivation would live)
- `src/cw/reconcile/review_recipes.py` — RFC 0010 reactor; `request_reviewer` /
  `address_review` are the outward actors Epic II gates
- `src/cw/reconcile/gate_recipes.py` — the detect/act/resolve template (RFC 0009)
- `docs/rfcs/0010-native-review-monitor.md` — the reactor this builds on; its
  Option-A (auto-dev-PRs-only) boundary is what Epic II extends outward
- `#1117` (attention on all parks, shipped), `#1049` (push-auth park, shipped),
  `#1061`/`#1020` (idle/budget axes), `#1120` (orchestration epic; workstream-3
  attention-routing contract)

Issues: (to be filed — two epic issues + S1 seam ticket, then children per wave)
