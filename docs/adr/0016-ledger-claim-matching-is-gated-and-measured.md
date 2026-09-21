# Ledger claim matching ships gated and measured, never on by default

**Status:** Accepted
**Driven by:** #2210 (building on #1838, #1814; see ADR-0015)

## Decision

The cross-round finding ledger (#1838) gains a second, **fuzzy claim-matching
tier** beside its exact `fingerprint_v1` tier, plus a typed contest signal
(`Finding.contests_adjudication`) and its first production **producer**
(`cw review settle`, fed by a ready-to-paste payload in every blocking review
comment). The claim tier ships **gated per lane and off**, and while it is off
it records every suppression it *would* have made as a
`review.finding_claim_shadowed` event — so the matcher is measured against real
rewordings before anyone arms it.

ADR-0015's invariants for the `VoidedFinding` seam are unchanged. That ADR is
not amended or superseded; the two seams stay independent.

## Invariant

1. **The exact tier is independent of the gate.** An identical
   `(file, normalized summary)` match suppresses exactly as it did before
   #2210, whichever way `claim_tier_enabled` is set.
2. **The claim tier suppresses only when both switches are true.**
   `OrchestratorConfig.codex_claim_suppression_enabled` (master) **and**
   `LaneConfig.codex_review_tiers["claim_suppression"]` (per-lane), resolved
   most-specific-wins down to a hardcoded-off floor. The flag is a keyword
   defaulting to `False` through every hop, so any call path that never threads
   it is off.
3. **Every claim match leaves a record.** An uncontested claim match either
   suppresses (emitting `review.finding_disposition_suppressed`) or, while the
   gate is off, is recorded with `review.finding_claim_shadowed` — both from
   inside `suppress_adjudicated_findings`, inline, for the reason ADR-0015
   invariant 3 gives. A **contested** claim match emits neither event: contest
   admission is INFO-log only.
4. **The claim tier is deliberately narrow.** Same file only; only MUST_FIX
   findings still at `disposition == "fixed"`; a disjoint symbol set vetoes;
   a nearer `ACCEPTED` entry vetoes.
5. **A contest fails toward blocking.** A non-blank `contests_adjudication`
   removes the finding from the match set entirely, on both tiers, regardless
   of the gate. It can never cause a suppression, only prevent one.
6. **Ledger entries are minted by operators — by mechanism at the reader, and
   by convention above it.** The dispositions reader still has no *author*
   filter (a comment's `login` proves little), but as of round 2 it has a
   **provenance** filter: see invariant 9. The `cw-followup` paragraph has an
   *agent* post the marker, so the skill still requires an explicit
   per-finding operator rejection first and is not run under
   `--auto-accept-defaults`. ADR-0015 invariant 4 stands; mechanical free-text
   ingestion is follow-up F1.
7. **A pipeline-authored convenience payload must not re-enter the pipeline's
   own prompt as evidence through the ticket-comments route.**
   `_load_operator_comments` elides the `### Settle a finding` section — and
   nothing else — from comments carrying `AGENT_COMMENT_MARKER`. The invariant
   is deliberately scoped to that route; the persisted `.claude/review-verdict.md`
   route is an accepted consequence below.
8. **Minting a suppression is an audited operator act.** `cw review settle`
   requires a non-blank `--reason`, records provenance on the entry itself
   (`actor`, a CLI-stamped UTC `recorded_at`, the verbatim `summary`, the
   `reviewed_sha` the finding was raised against), emits one
   `review.finding_settled` event per settled finding, and **refuses to run
   inside a dispatch worker** (nearest `.claude/cw-context.json` reporting
   `headless`). There is no bypass flag: a control an agent can switch off is
   not a control. The refusal is checked before any output, any file write and
   any event, and fails open when no context file is found — the same
   fail-open posture every other cw context guard takes. This is the mirror of
   invariant 3: #2210 stops a settled finding being wrongly **re-raised**, and
   an unaudited settle path would let one be wrongly **silenced**, which is the
   direction that loses information permanently and invisibly.
9. **The reader enforces the contract; the writer alone cannot** (round 2).
   Every guard in invariant 8 lives in `cw review settle`, and a marker pasted
   by hand — or one a worker writes into a ticket comment itself — never
   passes through any of them. So `partition_enforceable_dispositions` gates
   every consumption path: a record is **applied** only when it carries the
   full provenance set (finding identity, `actor`, a `recorded_at` that parses
   as a UTC instant, `reviewed_sha`, and a non-empty `rationale`). Anything
   short of that is ignored, logged once per pass at WARNING naming the ticket
   and the offending record, and reported on the posted comment under
   "Disposition records refused (no provenance)". This covers the reviewer
   prompt's `## Previously Adjudicated Findings` block as well as the
   mechanical backstop: that block tells the model the decision is BINDING, so
   an unaudited entry reaching it would suppress the finding one layer up. An
   `ACCEPTED` entry is gated on the same terms — it changes no gate, but it
   reaches the reviewer as a decided finding.
10. **The audit record is written before the effect it audits** (round 2).
    `cw review settle` emits every `review.finding_settled` event first and
    writes the marker (stdout and `--out`) only once all of them have
    recorded; a failed emit aborts with no marker and a non-zero exit. The two
    failure directions are not symmetric — an audit record with no effect is
    noise, a durable suppression with no audit record is invisible — which is
    why this deliberately does **not** follow #1617's save-then-emit
    precedent, whose subject is a state mutation whose event must not claim
    something that did not land.

## What this means for callers

- `suppress_adjudicated_findings` takes `claim_tier_enabled` and `reviewed_sha`
  as defaulted keywords. Both default to the safe value; nothing has to change
  to stay on today's behaviour.
- The gate is resolved once, in `cw.codex_background._resolve_claim_tier_enabled`,
  where the lane, the client's lanes and the loaded `OrchestratorConfig` are
  already in hand, and threaded as one keyword:
  `_run_codex_review_and_complete` → `run_review_with_fix_loop` →
  {`run_review` (cycle 0 and the whole fix-loop-disabled lane), `_rereview`
  (cycles 1+)} → `synthesize_codex_review_result` →
  `suppress_adjudicated_findings`.
- The Claude-native lane never calls the ledger, so the gate is codex-only by
  construction.
- `codex_fix_loop.py` carries the keyword and takes no decision with it;
  `_admit_new_must_fix` and `codex_fix_loop_convergence.py` are untouched.

## What this means for producers

- `cw review settle <payload> --reason '<why>'` renders the postable
  `REVIEW-FINDING-DISPOSITIONS` marker from a list of
  `(file, summary, outcome, rationale, reviewed_sha)` entries. `--reason` is
  mandatory and must be non-blank; an entry's own `rationale` overrides it, so
  several findings can be settled for different reasons in one call.
  `recorded_at` is stamped by the command's own UTC clock and is **rejected**
  as a payload key — it is audit data, not input.
- Every blocking review comment prints one payload per keyable MUST_FIX
  finding, carrying the **verbatim** `file` and `summary` — the record's whole
  identity — plus the verdict's `reviewed_sha`, so pasting it needs no editing
  and reproduces exactly the key the next re-raise will hit. An entry with no
  resolvable sha is refused; `--reviewed-sha` supplies one for a hand-written
  payload. There is deliberately no fallback to `git rev-parse HEAD`: the sha
  of whatever directory the operator happened to be standing in is not
  evidence.
- The section is **bounded**: at most 10 payloads and 12,000 characters of
  them. Past either cap the remaining findings are listed compactly (file plus
  a trimmed summary, no JSON) and the operator is pointed at `cw review
  settle`. A payload block is size-tested whole before it is kept, so the
  section can never end on a half-written JSON object — a truncated payload
  would paste into something that half-parses. GitHub rejects a comment body
  over 65,536 characters and the rest of the comment needs the remainder.
- The payload is deliberately **not** the postable marker itself. The
  dispositions reader ingests every comment body on the ticket, including the
  pipeline's own, so a `REVIEW-FINDING-DISPOSITIONS` block inside the blocking
  comment would auto-settle every finding as REJECTED on the next pass. A test
  pins the sentinel's absence from `render_verdict_comment`'s output.
- Payloads are per finding, not one combined block, so an operator cannot
  settle the genuinely actionable finding by pasting everything at once.
- **Hand-authoring a `REVIEW-FINDING-DISPOSITIONS` block is unsupported.**
  `cw review settle` is the only supported producer, because it is the only
  path that records provenance and refuses to run inside a dispatch worker.
  A hand-written block is still *parsed* — the fields stay optional so history
  loads — but under invariant 9 it is not *applied* unless it happens to carry
  the whole provenance set, and the review comment reports the refusal.

## Consequences

- **The ledger is severity-blind.** `FindingDisposition` stores an outcome, a
  rationale, a date and (as of #2210) provenance — never a severity. The exact
  tier already suppresses any severity on an
  identical `(file, normalized summary)`. Once armed, the claim tier can let a
  REJECTED entry recorded for a nit shield a same-file MUST_FIX that clears the
  matcher. Bounded by the tier being MUST_FIX-only, same-file and default-off;
  measured by shadow events that carry the candidate's severity; fixed durably
  by follow-up F7 (a schema bump recording severity and an expiry).
- **Entries never expire, and there is no per-record rollback command.** Unlike
  voids, no evidence anchor lapses them. The only mitigations today are
  visibility (the suppression annotation, `review.finding_settled` and the
  shadow events) and the marker's newest-wins merge, which lets an operator
  re-post the same key as `ACCEPTED` to reverse a settle by hand. Expiry and a
  real per-record rollback are a **precondition for ever arming the claim
  tier**, not for landing it, and are tracked in a separate follow-up ticket
  the operator filed. What #2210 does now, cheaply, is make that rollback
  *possible later*: every record carries enough identity — the ledger key,
  the verbatim `summary`, `reviewed_sha`, `actor` and `recorded_at` — to target
  exactly one entry rather than a key's worth of them.
- **ADR-0015's rationale is reversed for this seam.** "A spurious re-park costs
  one operator comment; a spurious suppression silently ships a real defect"
  applies with *more* force here, which is exactly why the tier is default-off
  and measure-first rather than suppress-by-default.
- **Pre-#2210 ledger records stop being applied.** A marker posted, or a queue
  row persisted, before the provenance fields existed carries no `actor`, no
  `reviewed_sha` and no verbatim `summary`, so invariant 9 refuses it. It is
  not deleted and not silently dropped: it still loads, the reader reports it
  on the comment, and the finding it used to suppress starts blocking again
  until an operator re-settles it with `cw review settle`. That is the
  intended direction — the alternative is honouring a record that cannot say
  who created it — but it means the first review round after this change can
  re-park a ticket whose finding was settled under the old shape.
- **Refusal is per record, not per ledger.** A well-formed entry alongside a
  refused one still applies. The refused section is bounded (20 rows, then a
  counted residue line) for the same comment-budget reason the settle section
  is.
- **Known false-match class, accepted:** same symbol, same verb phrase,
  different condition — "`foo` returns none when list is empty" versus
  "…contains duplicates" scores 0.73 and matches.
- **Thresholds are judgement calls**, not derived from a corpus of real
  rewordings. The shadow events *are* that corpus.
- **The contest field is unverified and gameable.** Non-blank means contest;
  nothing checks that the quoted code actually changed. It can only fail toward
  blocking. It is honoured on **both** tiers regardless of the gate, so with
  the gate off exact-tier behaviour equals pre-#2210 behaviour only for
  findings whose contest is blank, and there is no kill switch short of
  reverting the field. Abuse is not measurable from events, because admission
  is INFO-log only. Considered and deliberately not changed: an
  admitted-contest audit event, or scoping the bypass to the claim tier (which
  would gut the ticket's acceptance criterion). Recorded as follow-up **F9**.
- **The hatch is single-pass only** (plus fix-loop cycle 0).
  `_admit_new_must_fix` does not read `contests_adjudication`, so an in-loop
  out-of-delta contest is diverted to the debt ledger and the contest text is
  dropped. Deliberate; follow-up F6 covers splitting that file.
- **`consolidate_verdict`'s dedup can drop a contest.** When two reviewer roles
  raise the same finding and only the losing role set the field, the surviving
  representative carries no contest. Rare, and it fails toward suppressing a
  *bare* finding rather than toward a wrong block.
- **Three identity notions now coexist** on the codex path: exact
  `fingerprint_v1` (ledger, treadmill, debt), the void's content anchor, and
  the fuzzy claim tier. A finding can be "the same" for suppression and "new"
  for the treadmill tracker.
- **The tier is not the only suppression channel.** The prompt already asks the
  model to self-suppress, unmeasured. That instruction is limited to
  operator-authored ledger entries: an inline `# Why:` comment is offered to
  the reviewer only as *evidence to weigh* and cite in `consequence`, never as
  a decision, so code-author-controlled text cannot instruct suppression.
- **The settle payload is elided from the next reviewer's prompt, by
  provenance.** Only comments carrying `AGENT_COMMENT_MARKER` lose their
  `### Settle a finding` section; an operator's own pasted payload stays
  visible by design, and a bare `gh issue comment` posted without the marker is
  treated as operator-authored. The alternatives were leaving the echo
  (rejected: the reviewer would see its own findings restated as
  operator-attributed `"outcome": "REJECTED"` JSON and could self-suppress a
  real finding, bypassing the per-lane gate) and blanking `outcome` in the
  visible payload (rejected: conflicts with the "no hand-editing" requirement).
- **The elision covers the comments route only.** The same rendered review
  text, settle payloads included, is written to the git-tracked
  `.claude/review-verdict.md`, which reaches later reviewers as added lines in
  the next prompt's `## Diff` section. That route stays open; it is
  lower-likelihood (a diff hunk reads less like a binding adjudication than a
  comment does). Fix is follow-up **F10** — strip the section from the
  persisted artifact, or stop tracking the file.
- **Convention drift from `docs/release-playbook.md`.** The playbook says a
  `False` master short-circuits the entire module. Shadow recording runs with
  the master **off**, for any ticket that has a ledger — that is the whole
  point. A fresh install with no ledger still does nothing.
- **Suggested arming criterion (a recommendation, not enforced):** arm a lane
  only after at least 20 shadow events on that lane have been read and judged
  same-defect versus distinct-defect, with no distinct-defect pair among them.
  One distinct-defect pair is a reason to tighten the thresholds, not to arm.
- **Shadow events are not lost to auto-prune, but they move.** Once the inbox
  passes 5 MB it is trimmed to the newest 2000 events and the rest are archived
  to `events/inbox.<YYYY-MM-DD>.jsonl`. `cw event tail` reads only the live
  inbox, so judging a lane means tailing the live inbox for recent events and
  reading the dated archive files for older ones — or raising
  `event_inbox_retention_count`. The shadow payload carries `reviewed_sha` so a
  finding that fires on every fix-loop cycle can be grouped by ticket, file and
  summary and its distinct reviewed commits counted.

## Alternatives considered

- **Suppress by default.** Rejected: a false suppression is invisible, while
  the ticket's own complaint (a re-parked run) is visible and correctable.
- **Annotate only, with no gate.** Its durable form *is* the shadow event.
- **A severity-keyed ledger.** Needs a `FindingDisposition` schema bump and a
  dev-queue migration; deferred as F7.
- **Amending ADR-0015.** Rejected: it is a dated record of a different seam's
  decision.
- **A `--force`/`--i-am-an-operator` escape from the dispatch-worker refusal.**
  Rejected: the worker is the party the refusal exists to stop, and it would be
  the one passing the flag.
- **Deriving the reviewed sha from `git rev-parse HEAD` when the payload has
  none.** Rejected: it records the operator's current checkout, not the commit
  the finding was raised against, and a confidently wrong provenance field is
  worse than a refusal.
- **Extending `_admit_new_must_fix` to read contests.** Rejected deliberately —
  the fix loop's convergence logic is out of scope here (F4/F6).

## Referenced by

- #2210, #1838, #1814, ADR-0015
