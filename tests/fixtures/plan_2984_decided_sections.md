## Non-Goals

- Do not add a second constraint store, a new `TicketTask` field, or a `DEV_QUEUE_SCHEMA_VERSION` bump. `tasks.py` is already over the module-size ceiling and must not grow.
- Do not add a retraction, supersede or edit path for plan-derived entries. That stays #2968, and entries are append-only.
- Do not feed plan-derived entries to the fix-loop hard-park guard (`constraint_breach` and token extraction). Plan entries reach the fix loop as prompt text only, until retraction exists.
- Do not deliver operator constraints and settles themselves. That is #2964, already merged.
- Do not change how the plan stage enumerates scope or add a plan-quality checklist (a separate ticket). The only plan-prompt change is requiring the `## Non-Goals` heading.
- Do not solve settled-finding identity across rewording (#2635). A reworded contest of an already-settled finding re-parks, which is safe-direction.
- Do not add a new `blocker.reason`, a new `Disposition`, a new event type, or a sentinel change. The plan reuses `review_operator_actionable`, `operator_actionable`, `deferred` and `ticket.constraint_recorded`. The prose in `docs/headless-contract.md` and `.claude/commands/auto-dev.md` that defines `review_operator_actionable` is not edited.
- Do not relax the `Adjudication` model validators for `operator_action`. A contested located finding is recorded as `defer` and re-stamped mechanically.
- Do not claim mechanical coverage of an unmarked conflicting finding. That case is covered only by the prompts and the contest marker.
- Do not record plan decisions from the review or impl stages. There is no review-Orientation self-heal.
- Do not add a dispatcher-side recorder at `cw dev-queue approve` / `promote_plan_draft`. A plan promoted that way after a regress into PLAN is not re-recorded. This needs a follow-up ticket.
- Do not change the OpenCode clean-review lane prompt (`src/cw/opencode_clean_review.py`). It is neither a Claude-native nor a codex lane.
- Do not mirror `plan_decisions` into `.claude/scripts/`. Only `cw` consumes it.

## Scope tier inputs

- Estimated files: 47 (22 `src`, 7 markdown/docs, 18 tests).
- Estimated lines: about 2,900 (about 1,060 source, about 120 markdown/docs, about 1,740 tests).

## Adopted Assumptions

- **`## Decisions` is recorded as its own kind (`plan_decision`)**, not only Adopted Assumptions and Non-Goals. Rationale: additive, deduped and capped; the owner's comment requires behavioural decisions to reach reviewers and a plan's `## Decisions` is where operator-resolved ambiguities land.
- **One marker field, `contests_decision`.** MUST_FIX parks (`operator_actionable`), SHOULD_FIX is a deferred note; re-litigation and a real contest both exit the fix loop. Rationale: the ticket delegates the (a)/(b) choice to the plan; a single field keeps the finding schema small; if park volume proves too high, narrowing is a follow-up.
- **Contest precedence parks the whole verdict (replaces the earlier mixed-case behaviour).** A marked MUST_FIX beside an unmarked MUST_FIX parks as `review_operator_actionable`, checked before `verdict.blocking`. No fix cycle runs until the operator has seen the contest. Rationale: the operator sees everything at once, and fixing an unmarked finding first could build code on top of the unsafe decision being contested. The single blocker reason keeps `_verdict_block_reason`, the renderer headline and Phase 1 items 9 and 12 aligned. After the operator settles or requeues, the unmarked MUST_FIX is fixed normally.
- **Claude-lane ordering difference.** The Claude-native lane keeps the existing #1817 ordering: Step 3b fixes of unmarked findings can run before the Step 3c park. Rationale: the park is a last-check gate by design, and the contested finding is surfaced either way.
- **Claude-lane outcome handling.** The marker overrides every outcome in `_stamp`: `fix`, `reject`, `defer` and `operator_action` all give `operator_actionable` for a MUST_FIX and `deferred` for a SHOULD_FIX. The session records `defer`, because the `Adjudication` validators make a located `operator_action` entry unrepresentable. Step 3c gets a second trigger keyed on the stamped verdict. A marked MUST_FIX with no adjudication entry stays `dropped`, which blocks and so is safe.
- **Plan-entry size-cap overflow:** a 32 KiB plan sub-budget under the shared 64 KiB total; overflowing items are not recorded (never truncated), the command exits 3 and lists them, and the stage continues with a friction note. Rationale: consistent with #2964's refuse-don't-truncate rule and cannot crowd out operator entries.
- **Re-plan accumulation:** a revised or reworded plan bullet is appended as a new entry (exact-text dedupe, append-only); the superseded entry stays until #2968. Rationale: supersession is the retraction work explicitly deferred to #2968. Because entries now inform prompts only, a stale entry can mislead a prompt but cannot hard-park the fix loop; the runbook documents the limit.
- **Plan-derived entries inform prompts only (replaces the earlier guard-coverage assumption).** They populate the Decided Scope block (`decided_body`) and the Claude `## Decided Constraints` block, and they do NOT create `ConstraintRule`s or feed `constraint_breach`. Rationale: an auto-derived, append-only entry with no retraction must not be able to hard-park the pipeline (`codex_fix_constraint_violation`) on code a later plan requires. Residual: the defence against an unmarked finding that conflicts with a decision is the prompts plus the contest marker only. Operator `cw review constrain` entries keep today's guard behaviour byte-identically.
- **Trust boundary (replaces the earlier recorder-placement assumption).** The recorder is invoked only from the plan stage (Step 1g.1), and its input is the plan text being persisted. The library refuses unless the dispatcher-owned row field `stage` is `plan`. Adopted rows are created at `Stage.REVIEW` (or `IMPL` with feedback, `adopt_pr.py:442`), so the recorder always refuses them and they only ever carry operator-kind entries; `cw review decided` prints "None recorded" plan entries for them.
  - Why not a fingerprint check: `plan_approved_fingerprint` is stamped by `approve` after Step 1g.1; a revoked re-plan clears it; and `plan.md` is rewritten in place by the review stage and can be a promoted draft, so it is not comparable to the draft fingerprint.
  - Why not `purpose`: `cw-context.json` `purpose` is the session purpose and is `impl` in every pipeline stage.
  - Rationale: a headless review or impl worker cannot mint binding input for the lane that reviews it, which is the same principle as #2964's refusal of `constrain` inside a worker. `cw review decided` is read-only and may print the "not recorded" notice, but the review stage never records.
- **Dispatch-loop refusal.** `require_dispatch_loop_safe()` refusing (a stale or unknown loop) makes the command exit non-zero. Step 1g.1 records a `plan_decisions_not_recorded` friction note and the stage continues, so delivery is lost for that run. Its wording is generalised via a defaulted `subject` kwarg, and the existing `constrain` text is unchanged.
- **Plans promoted by `cw dev-queue approve` after a regress** do not re-run Step 1g.1, so the reconciled plan's decisions are not recorded (entries from the earlier plan remain). Rationale: recording only from the plan stage is the chosen trust boundary. A dispatcher-side hook at `promote_plan_draft`, which knows the approved fingerprint, is the sound follow-up and needs its own ticket.
- **Settle payloads cover contested findings.** `_settleable_findings` includes them. A `REJECTED` settle suppresses a re-raise before the contest stamp, so the operator's dismissal sticks. Upholding a contest means changing course by hand, since there is no retraction (#2968).
- **Claude confirm round.** A contested confirm round parks via the existing healthy-failure path, which leaves the ledger `awaiting_confirm` and surfaces the finding. No code change.
- **Private imports.** `plan_decisions.py` imports the two private helpers from `plan_files.py` directly. Rationale: it matches existing practice and the ruff config does not forbid it; no public alias is added.
- **Contract docs.** `docs/headless-contract.md` and `.claude/commands/auto-dev.md` describe `review_operator_actionable` as the `no_diff_anchor` trigger. They are left unchanged under the no-contract-change Non-Goal. The runbook section and the `cw-orchestration` skill sentence document the second trigger.

## Ambiguities

NO_AMBIGUITIES
