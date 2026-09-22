# Ledger claim matching ships gated and measured, never on by default

**Status:** Accepted
**Driven by:** #2210 (building on #1838, #1814; see ADR-0015)

## Decision

The cross-round finding ledger (#1838) gains a second, **fuzzy claim-matching
tier** beside its exact tier (keyed on `fingerprint_v1` extended with a digest
of the verbatim summary — invariant 12), plus a typed contest signal
(`Finding.contests_adjudication`) and its first production **producer**
(`cw review settle`, fed by a ready-to-paste payload in every blocking review
comment). The claim tier ships **gated per lane and off**, and while it is off
it records every suppression it *would* have made as a
`review.finding_claim_shadowed` event — so the matcher is measured against real
rewordings before anyone arms it.

ADR-0015's invariants for the `VoidedFinding` seam are unchanged. That ADR is
not amended or superseded; the two seams stay independent.

## Invariant

1. **The exact tier is independent of the gate.** A byte-identical match —
   same file, same VERBATIM summary (invariant 12) — suppresses whichever way
   `claim_tier_enabled` is set. Round 3 made the tier stricter than it was
   before #2210 (see Consequences): it used to match on the lossy normalized
   summary alone.
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
   anywhere it cannot prove is an operator's own interactive session**. There
   is no bypass flag: a control an agent can switch off is not a control. The
   refusal is checked before any output, any file write and any event, and it
   **fails CLOSED** (round 4): the only state that proceeds is a discovered
   nearest `.claude/cw-context.json` whose `headless` is the JSON boolean
   `false`. A worker (`headless: true`) refuses, and so does every
   indeterminate answer — no context file above cwd, an unreadable or
   malformed one, one with no `headless` key, one whose `headless` is not a
   bool. `find_cw_context` cannot distinguish "there is no dispatch context
   here" from "the dispatch context could not be read", and the earlier
   fail-open posture read that ambiguity as "operator's own machine", so a
   worker with a missing or truncated context could settle its own reviewer's
   findings. Operationally this means `cw review settle` must be run from an
   interactive `cw` session worktree (or any directory beneath one); a plain
   checkout of the repo carries no context file and is refused. This is the
   mirror of
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
11. **An invalid record is never written and never evicts a valid entry —
    validate first, write second** (round 3). Ignoring an under-provenanced
    record when *applying* the ledger (invariant 9) is not enough if the same
    record can still *replace* a valid entry when the ledger is *written*: a
    malformed or hand-pasted block would destroy the provenance of a
    legitimately settled finding on the very ledger that decides what stays
    suppressed. `merge_finding_dispositions` — the one chokepoint every write
    passes through: the ticket-thread merge, the dev-queue row sync and
    `cw review settle` — therefore skips any incoming entry that fails
    `_provenance_gaps`. Only a valid entry may add a key or replace an entry
    for the same key, and between two valid ones the newest `recorded_at` still
    wins. A valid entry also replaces an invalid one already in the ledger (a
    heal); entries already in the ledger pass through untouched, so legacy rows
    stay for the reader to keep refusing and reporting. A refused record is
    logged once at WARNING (ticket and key) and reported on the review comment;
    it is not persisted. `build_finding_disposition_ledger` raises on an invalid
    entry rather than dropping it.
12. **The key binds the verbatim summary; the exact tier is byte-identical
    only** (round 3). A ledger key is
    `file::normalized_summary::<sha256 of the exact summary>` — the first two
    parts are `fingerprint_v1`, and the digest is the full 64-hex SHA-256 of the
    verbatim text (no normalisation, no strip, no case folding). Round 2 keyed
    on the lossy normalized half alone, so every rewording that normalised alike
    shared one record and a finding could drift onto a **different** finding's
    suppression, which nobody adjudicated: the silent-suppression direction
    again. `_provenance_gaps` recomputes the key from the key's file and the
    record's own `summary` and requires equality, so a record whose payload
    summary is not the one its key was minted from — or a digest-less legacy
    key — reports an `identity` gap and is refused. `split_disposition_key`
    still returns `(file, normalized_summary)` for the claim tier and the
    renderers, recognising the digest by its fixed anchored `::[0-9a-f]{64}$`
    shape rather than by position, because a normalized summary can itself
    contain `::`. The claim tier stays the **only** path that may match
    non-identical text, and it stays gated off with its shadow event.
13. **The reviewed sha is a required field of the record, not part of the key**
    (round 3). The reviewer re-raises a settled finding on a LATER commit; a key
    that included the sha it was settled at would stop matching after any fix
    commit and the whole ledger would go dead, which is the memory loss #1814
    and #2210 exist to remove. The sha still answers "against what code was
    this silenced" — it is a mandatory provenance field (invariant 9) and rides
    on `review.finding_settled` and `review.finding_claim_shadowed`. This is a
    deliberate reading of "the key includes the file, the summary and the
    reviewed sha": the ledger key carries what identifies the *finding*, the
    record carries what identifies the *decision*.
14. **Review text is untrusted input, and a record is made by POSITION as well
    as shape** (round 4). The pipeline renders model-authored text — a finding
    summary, a file path, quoted evidence, a contest claim, reviewer prose —
    into the very ticket comments the disposition reader parses on the next
    round. A finding whose summary carried a well-formed, fully-provenanced
    sentinel block therefore minted a durable suppression no operator
    authored: the reviewer silencing itself, straight past invariants 8–13,
    because every one of those asks *what the record says* and none asked
    *who put it there*. Two independent layers now sit in front of them, and
    either alone defeats the injection:
    - **Escape on render.** Every untrusted span
      `codex_review._verdict._render` interpolates goes through
      `review_markers.neutralise_marker_syntax`, which escapes
      `REVIEW-FINDING-DISPOSITIONS`, `VOIDED-REVIEW-FINDINGS` and the `<!--` /
      `-->` delimiters (which is also what makes `AGENT_COMMENT_MARKER`
      unforgeable, and so protects invariant 7's elision). The escape is a
      visible backslash inside the token, never a silent strip and never a
      zero-width character: the comment must still report what the reviewer
      actually said. The one exception is the `### Settle a finding` payload,
      whose `file` and `summary` ARE the ledger key and must survive
      `json.loads` byte-identically; it is made inert losslessly instead, by
      rewriting `<` and `>` as their `\uXXXX` JSON escapes.
    - **Parse by position.** `_DISPOSITION_BLOCK_RE` honours a block only
      where `render_finding_disposition_block` emits it: opening the comment
      body, under the marker's own `## Review Finding Dispositions` title,
      with nothing but whitespace between. Rendered finding text never sits
      there — it is many lines inside a `## Codex Review Verdict` body — and
      `post_issue_comment` appends its provenance marker, so a posted marker
      still parses. A block found anywhere else is **not a record and not a
      refusal**: nothing tried to settle anything, so there is nothing to
      report. Consequence for operators: post the marker `cw review settle
      --out` renders as its own comment, unedited.

## What this means for callers

- `suppress_adjudicated_findings` takes `claim_tier_enabled` and `reviewed_sha`
  as defaulted keywords. Both default to the safe value; nothing has to change
  to stay on today's behaviour.
- It also takes `refused` (round 3): the records the write path already refused
  and logged. They never enter the ledger, so partitioning it cannot find them;
  they ride `_ReviewPassInputs.refused_dispositions` →
  `synthesize_codex_review_result` → here and are merged (by key, sorted) into
  `ReviewVerdict.refused_dispositions` with the refusals derived from legacy
  rows, without a second WARNING.
- `parse_finding_disposition_block` returns `(enforceable, refused)`, not a
  bare ledger, for the same reason.
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
  finding — per distinct **verbatim** `(file, summary)`, since that is what the
  key binds — carrying that `file` and `summary` plus the verdict's
  `reviewed_sha`, so pasting it needs no editing and reproduces exactly the key
  a byte-identical re-raise will hit. An entry with no
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
  path that records provenance and refuses to run outside an operator's own
  interactive session. A hand-written block posted as its own comment is still
  *parsed* — the fields stay optional so history
  loads — but under invariant 9 it is not *applied* unless it happens to carry
  the whole provenance set, and the review comment reports the refusal.

## Consequences

- **The ledger is severity-blind.** `FindingDisposition` stores an outcome, a
  rationale, a date and (as of #2210) provenance — never a severity. The exact
  tier already suppresses any severity on a byte-identical
  `(file, summary)`. Once armed, the claim tier can let a
  REJECTED entry recorded for a nit shield a same-file MUST_FIX that clears the
  matcher. Bounded by the tier being MUST_FIX-only, same-file and default-off;
  measured by shadow events that carry the candidate's severity; fixed durably
  by follow-up F7 (a schema bump recording severity and an expiry).
- **Entries never expire, and there is no per-record rollback command.**
  ~~Unlike voids, no evidence anchor lapses them.~~ **Closed by #2232**, which
  was the follow-up ticket this paragraph anticipated. What #2210 did cheaply
  was make the rollback *possible later*: every record carries enough identity
  — the ledger key, the verbatim `summary`, `reviewed_sha`, `actor` and
  `recorded_at` — to target exactly one entry rather than a key's worth of
  them. #2232 spent that, in three parts:

  - **Rollback** is a third `Outcome` value, `REVERSED`, produced by
    `cw review settle` itself. No new command and no second write path: the
    marker's newest-`recorded_at`-wins merge makes the withdrawal durable
    through `merge_finding_dispositions`, the one chokepoint invariant 11
    names. A reversed record matches neither tier, is not rendered into the
    reviewer's binding "previously adjudicated" block (a withdrawal is the
    absence of a decision, so asserting one would be backwards), and emits
    `review.finding_disposition_reverted` rather than `review.finding_settled`
    so an operator can query withdrawals by event type.
  - **Staleness is surfaced, never expired.** `disposition_drifted()` compares
    the record's `reviewed_sha` against the pass's for that finding's file; on
    drift the record is NOT applied for that pass, the finding keeps blocking,
    a `StaleDisposition` reaches the posted comment via
    `ReviewVerdict.stale_dispositions`, and `review.finding_disposition_stale`
    records it. The entry stays in the ledger and still applies to any pass
    where its file has not moved — expiry would be the silent act this ADR
    exists to refuse. The check fails toward surfacing: an unresolvable ref or
    an unreadable repository reads as drift.
  - **Inspection** is `cw review dispositions <ticket>`, read-only, listing
    every record with its outcome and (given `--worktree`) its staleness.

  The arming precondition is now **enforced, not merely documented**:
  `disposition_drift_check_enabled` (global default `true`, per-lane override)
  gates the automatic check, and resolving it `False` on a lane whose claim
  tier resolves `True` raises `ClaimTierArmingError` rather than running a
  fuzzy suppression with its drift protection removed.
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
- **The exact tier is now stricter, and a two-part key is refused** (round 3).
  A reviewer's reworded re-raise no longer exact-matches a record it merely
  normalises like — matching non-identical text is the claim tier's job, and
  the claim tier is off by default, so until a lane is armed a reworded
  re-raise of a settled finding **blocks again** (and, when it clears the claim
  matcher's thresholds, is recorded as a `review.finding_claim_shadowed` event). That is the intended trade: a
  non-match the operator can re-settle over a silent match against a finding
  nobody adjudicated. Operator-visible: a record minted before this change
  (two-part `file::normalized summary` key) fails the identity binding, is
  refused and reported like any other unbound record, and must be re-settled
  with `cw review settle`. It stays in the durable ledger (entries already
  there pass through the writer untouched), so it is re-reported on every pass
  until the ticket ends; the fresh three-part record is a separate key and
  applies normally. Every `matched_key` on a suppression or shadow event now
  carries the digest.
- **Refusal is per record, not per ledger.** A well-formed entry alongside a
  refused one still applies. The refused section is bounded (20 rows, then a
  counted residue line) for the same comment-budget reason the settle section
  is.
- **The marker vocabulary lives in a leaf module** (round 4).
  `cw.review_markers` owns `DISPOSITION_SENTINEL`, `VOIDED_SENTINEL`,
  `SETTLE_SECTION_HEADING`, `RefusedDisposition` and the neutraliser, and
  imports nothing from `cw` at all. Two modules were pulling the whole ledger
  implementation in behind one name apiece: `codex_review._context._prompt_text`
  (static prompt text, which must stay dependency-free) and
  `review_findings._models` (the **executor-neutral** finding contract, whose
  dependency on one executor's ledger inverted the direction that package split
  exists to keep). Same shape #1409's import cycle was fixed with. A test
  parses the module's AST rather than trusting runtime behaviour, which is
  identical either way and is precisely why the direction needs its own lock.
- **`review_finding_dispositions.py` is a known seam.** It is ~1,100 lines,
  over this repo's ~1,000-line module ceiling, and it holds six
  responsibilities: the ledger record model, the marker renderer/parser, the
  key/identity arithmetic, the provenance gate, the fuzzy claim tier, and the
  suppression backstop with its event emission. Round 4 deliberately did **not**
  split it — a restructure of the module every one of this ticket's invariants
  lives in, in the same change that hardens them, would make the diff
  unreviewable. Recorded here as the seam the next change to this area should
  take: one submodule per responsibility behind a re-exporting `__init__`, the
  shape `cw.cli` and `cw.reconcile` already use.
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
  the one passing the flag. Round 4 closed the softer version of the same hole:
  an unreadable context file was itself an escape, and it needed no flag.
- **Stripping marker syntax out of a finding instead of escaping it.**
  Rejected: an operator adjudicating a finding needs to read what the reviewer
  said, and a finding about this very ledger will legitimately quote the
  sentinel. Escaping keeps the text and removes the grammar.
- **Relying on the provenance gate alone to stop an injected block.** Rejected:
  an injected record lacks provenance today and would already be refused, but
  that makes the whole defence one layer deep and couples it to a check whose
  purpose is different. The injection must fail at parse.
- **Keying on the reviewed sha as well.** Rejected: the reviewer re-raises on a
  later commit, so the key would never match again after a fix commit and the
  ledger would go dead (invariant 13). The sha is a required record field and
  rides on the event payloads instead.
- **Keying on the verbatim summary alone (no normalized half).** Rejected: the
  normalized half is what the claim tier compares, what `split_disposition_key`
  hands the renderers, and what keeps a marker human-readable; the digest is
  appended, not substituted.
- **Enforcing the write invariant only in the caller that parses the thread.**
  Rejected: the durable dev-queue sync and `cw review settle` also reach
  `merge_finding_dispositions`, and a guard a caller can forget is the
  writer-only contract round 2 already refused for the reader.
- **Deriving the reviewed sha from `git rev-parse HEAD` when the payload has
  none.** Rejected: it records the operator's current checkout, not the commit
  the finding was raised against, and a confidently wrong provenance field is
  worse than a refusal.
- **Extending `_admit_new_must_fix` to read contests.** Rejected deliberately —
  the fix loop's convergence logic is out of scope here (F4/F6).

## Referenced by

- #2232, #2210, #1838, #1814, ADR-0015
