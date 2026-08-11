# Voided-finding suppression is content-anchored, never positional

**Status:** Accepted
**Driven by:** #1814 (building on #1730, #1805)

## Decision

A review finding an operator has rejected is recorded as a durable
`VoidedFinding` on the ticket thread, and a later pass suppresses a re-derived
finding **only** when its content anchor — severity, file, summary, evidence —
matches that record exactly. Line position is not part of the identity, in
either direction. Suppression stamps `disposition="rejected"` and emits a
`review.finding_voided` event; it never introduces a new disposition, a new
adjudication outcome, or a new bucket.

## Invariant

1. **Identity is content, not position.** A void's fingerprint is
   `(severity, file, normalized(summary), normalized(evidence))`. No
   suppression path may add `line_start`/`line_end` to it, and none may drop
   `file` or `severity` from it.
2. **Expiry is anchor-based, never wall-clock.** A void lapses when the code
   it describes is rewritten, because the re-derived `evidence` then differs
   and the fingerprint stops matching. There is deliberately no TTL, no
   `expires_at`, and no pass counter — a second expiry mechanism could
   disagree with this one, and the disagreement would be invisible.
3. **Suppression and its audit record are not separable.** Every suppression
   emits exactly one `review.finding_voided` event, from inside the function
   that performs it. No caller may suppress without emitting, and emission is
   never a step a call site can be expected to remember.
4. **Only the Claude coordinating session mints voids.** Correlating free-text
   operator prose to a specific finding needs LLM judgment. A codex-only lane
   consults existing voids and never creates new ones.
5. **A suppressed finding is still rendered, and is visibly marked.** It stays
   in `verdict.accepted` and appears in the posted comment carrying its
   disposition. Removing it from the record would trade one silent
   disappearance for another.

## What this means for callers

- `cw.codex_review._verdict.synthesize_codex_review_result` applies
  suppression between consolidation and the disposition table, so every exit
  branch sees the suppressed state. Both its call sites (`core.run_review`,
  `codex_fix_loop._rereview`) reach the blocking check through it; suppressing
  at only one would let the same void be honored on one path and ignored on
  the other.
- `cw.codex_fix_loop._track_open_findings` treats only `disposition == "fixed"`
  as still open. Severity alone cannot exclude a voided finding — it keeps its
  MUST_FIX severity — so a severity-only filter hands the fix agent a decision
  the operator already made.
- `.claude/commands/auto-dev-review.md` Checkpoint 3a consults the record at
  step 3.5 and presents a suppressed finding informationally only. It is never
  a bucket-sort candidate: re-sorting it would append a second, conflicting
  `Adjudication` for one identity.
- Any renderer reading `AcceptedFinding` must consult `disposition`, not
  severity alone (`_render_findings`).

## What this means for producers

- The record lives on the tracker comment thread, not in `.cw/` or
  `.claude/cw-context.json`. Worktrees are torn down and `.cw/context.json` is
  deleted outright on the rescued-respawn path (`dispatch/gating.py`) — which
  is precisely the cycle this record exists to survive.
- The sentinel is JSON inside an HTML comment (`VOIDED-REVIEW-FINDINGS`) and
  carries `schema_version`. It is machine-parsed, unlike its
  `DEFERRED-REVIEW-FINDINGS` predecessor, because the codex backend has no LLM
  to interpret prose.
- Reading the record fails open: a malformed, truncated, or unreachable
  sentinel yields no voids and never raises. A missed suppression surfaces as
  the finding re-appearing, which an operator can see and act on; a false
  suppression is silent, which is the outcome this ADR is built to avoid.

## Consequences

- **Zero false positives, at the cost of some false negatives.** A reviewer
  who re-words the same defect's summary produces a different fingerprint, so
  the void does not apply and the operator must re-reject. That is the
  intended trade: a spurious re-park costs one operator comment, a spurious
  suppression silently ships a real defect.
- Voids accumulate on the ticket thread with no pruning pass. The record is
  small (one JSON object per rejected finding) and the parse is fail-open, so
  the cost is a longer comment, not a failure mode.
- The suppression seam lives in `cw.review_adjudication` alongside a module
  docstring that otherwise declares itself Claude-native-only. That is the
  carve-out the docstring already anticipated: this function shares only the
  already-shared `Disposition`/`AcceptedFinding` types, never
  `apply_adjudication` or its inverted `"deferred"` semantics.
- `cw.review_adjudication` now imports `cw.events`, so the module is no longer
  purely functional. Invariant 3 is the reason, and it is a deliberate
  exchange of purity for a guarantee that cannot be forgotten at a call site.

## Alternatives considered

- **`file` + `line` + a summary hash** (the shape the originating ticket
  sketched). Rejected on the operator's explicit instruction: line anchors
  drift by design (#1715's tolerance exists because reviewers miscount), so a
  positional key both misses real matches and, worse, matches unrelated new
  findings that happen to land on the same line.
- **A wall-clock TTL on each void.** Rejected: it would need to agree with the
  content anchor about when a void stops applying, and any disagreement is
  invisible. Anchor-based expiry gives the same outcome with nothing to keep
  in sync.
- **A new `AdjudicationOutcome` / `Disposition` value for "voided".** Rejected:
  the outcome genuinely *is* a rejection — the operator rejected the finding.
  A parallel value would fork every downstream consumer's disposition handling
  to distinguish two things that behave identically.
- **A `.cw/voided-findings.json` file.** Rejected: it would not survive the
  regress/redispatch worktree teardown that motivates the ticket.

## Referenced by

- #1814, #1730, #1805
