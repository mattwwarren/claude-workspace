# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`cw doctor` now detects an ACTIVE daemon session stuck roster-present with no terminal sentinel (#2078):** `signal_stop()`'s `background_tasks` defer can permanently stall for a plain (non-headless) DAEMON spawn when the harness's "next Stop hook re-fires" contract doesn't hold for one turn, so nothing completes the session and it never leaves `roster.json` — a case `compute_drift`'s existing phantom check missed entirely, since that check is gated on the session being *absent* from the roster and it never goes absent here. The new `wedge/active-daemon-stale-no-sentinel` class is the roster-present mirror of that check, reusing the exact evidence `reconcile/liveness.py`'s distress computation already gathers (no terminal sentinel, a transcript stale past the top liveness bucket, no outstanding subagent spawn still within its await deadline) and the canonical `_classify_liveness_bucket` for its staleness threshold — so it honors per-stage floor overrides and degraded `liveness_buckets_minutes` configs the same way reconcile's own liveness sweep does, rather than re-deriving a threshold that could diverge and misfire against this class's destructive `--reap` path. `--reap`'s `SESSION_REAP_AUTHORIZED` audit event now threads the specific wedge class through as `proposed_action`, so a post-hoc investigator can tell which of the two reaped classes fired instead of seeing one bare session-id list.
- **`cw pr-channel serve`, `cw queue-channel serve`, and the operator channel no longer crash every SSE subscriber with `TypeError: 'typing.Union' object is not callable`, and the channel proxies no longer fail on `.root` when a relayed event arrives:** the `[mcp]` extra's floor was `mcp>=1.27.1` with no ceiling, so `uv tool install` resolved `mcp` 2.x while `uv.lock` (and therefore CI) stayed on 1.27.1. In 2.x `JSONRPCMessage` is a plain `Union` alias rather than a `RootModel`, so the `_drain` closures' `JSONRPCMessage(notification)` call raised on the first queued notification and `extract_payload`'s `session_msg.message.root` would have raised `AttributeError` on the proxy side. The four producers now hand the `JSONRPCNotification` to `SessionMessage` directly, `extract_payload` reads `session_msg.message` itself, the `Server` annotations drop the removed second type parameter, and the extra is constrained to `mcp>=2.1.1,<3` with `uv.lock` regenerated to match, so the version CI tests is the version the operator's tool install runs and the next major cannot slip in through an unbounded floor again. The three servers' hand-rolled `_drain` notification construction, previously `pragma: no cover` in triplicate, is consolidated into a covered `build_server_notification` helper in `_events_channel_base`, so the exact shape that crashed now has an executed test.

## [1.45.7] - 2026-09-04

### Fixed

- **The Codex review backend no longer loses a Linear-tracked ticket's review verdict to a `gh issue comment` that can only fail, and every verdict now leaves a durable copy in the worktree (#2095):** `_post_review_comment` posted the rendered Stage-3 verdict via `gh` unconditionally, so on a Linear client every post died with `invalid issue format` and was swallowed into a log line — combined with #2094 the review stage could leave zero record of what it concluded. The daemon cannot post to Linear at all (ADR-0013 keeps cw's only programmatic tracker client GitHub-only), so the verdict is now first written to `.claude/review-verdict.md` in the ticket's worktree, unconditionally and before any post is attempted; then, when the client's tracker is positively known to be non-GitHub, the `gh` call is skipped and a WARNING names the tracker and the artifact path instead of pretending to post. GitHub-tracked and unresolvable-tracker clients post exactly as before.
- **A clean, fully-pushed worktree is no longer parked `dirty_worktree` because its branch happens to track the default branch (#2114):** the unpushed-commit half of `worktree_has_unsaved_work` trusted whatever `@{u}` was configured, and when that was `origin/<default_branch>` the `git log @{u}..HEAD` it ran counted every commit the feature branch contains as unpushed — a static condition, so the stale-worktree guard in dispatch re-parked the same clean ticket on every claim (one ticket eight times in a row), each park burning an attempt and holding a lane slot until an operator hand-set the upstream. The base-ref ladder now starts with `origin/<checked-out branch>` whenever that ref exists (`rev-parse --verify` answers "is this branch pushed" exactly, independent of tracking configuration) and only falls back to `@{u}`, `origin/<default_branch>`, then the local default branch when the branch has no remote counterpart; `worktree_gc`'s mirror of the predicate gets the same level. The park breadcrumb now names which predicate fired, the base ref it measured against, and the count (`<path>: 14 commit(s) ahead of origin/main and no origin/<branch> ref ... (cannot prove they are pushed)` / `2 uncommitted path(s)`) via a new `unsaved_work_reason`, so `dirty_worktree` on a visibly clean tree points straight at the predicate instead of at `git status`. Two adjacent edges found while fixing it: the pre-spawn `dirty_worktree` park (and the codex capability gate's park, same shape) no longer charges `unproductive_attempts` — no session ever ran for that claim, and charging it ratcheted a re-derived park toward `attempt_cap_blocked` in ten cycles, the same reasoning `_park_stale_dispatch_gate` already applied; and `cw doctor --reap` no longer classifies a `dirty_worktree` park as a dead-session wedge and reverts it to PENDING, which just re-claimed the same tree and re-parked it.

## [1.45.6] - 2026-09-04

### Fixed

- **A review finding whose `evidence` quote does not match its declared diff window is now adjudicated on its merits instead of being discarded as `evidence_not_in_diff` (#2099):** over a single ticket that rejection threw away correct findings at least four times, twice hiding a live production bug — the reviewer's claim was right, only its quote was stale. The round-3 root cause was a `PostToolUse` formatter hook rewriting the file after the reviewer authored its quote, so the quote and the diff carried the *same* code, re-wrapped: a comparison that runs line for line matches nothing. Two changes, and both are additive. First, `_reconcile_evidence_window`'s phase 1 and `_content_rescue_anchor` gain a last-resort reflow-tolerant stage — whitespace runs (newlines included) collapse to a single space, spaces immediately inside a bracket are dropped, and black's magic trailing comma before a closing bracket is dropped — applied identically to both sides only after every existing check has already failed, so everything that matched before still matches and content genuinely absent from the diff still misses (whitespace collapses to one space, never to none, so two tokens can never fuse). `_content_rescue_anchor` additionally gains a content-sized window search, since its line-count-sized windows are blind to a quote the diff wraps across a different number of lines. Second, and this is what actually closes the ticket: `evidence_not_in_diff` joins `unanchored` (#1632) and `line_anchor_degraded` (#2081) in `_ADJUDICATION_ROUTED_REASONS`, so a finding that still misses reaches `.accepted` with `Finding.anchor_degraded` set and a new `anchor_degraded_reason` naming the reason — keeping its line endpoints, which unlike #2081's routing did verify. Such a MUST_FIX now blocks and is adjudicated rather than parking the run through #1714's `rejected_must_fix` signal, and `/auto-dev-review` tells the coordinating session to re-anchor it from the finding's text and bucket-sort it like any other; a MUST_FIX it genuinely cannot locate goes to REJECT with a rationale quoting the unmatched evidence, so the claim is recorded in `ADJUDICATIONS` rather than lost. #1714's `review_blocked` exit is unchanged for the reasons that stay mechanically rejected (`schema_invalid`, `unknown_file`, `line_reference_out_of_range`, ...), the verdict comment annotates the two routings distinctly, and reviewers are now told to quote the inlined diff and never the on-disk file — the #2087 shared-worktree hazard on the evidence axis.
- **`auto_fix_ci` no longer mints a duplicate dev-queue row when it re-dispatches a ticket whose row is already terminal, and the resulting stuck `terminal_sibling` parks now have a way out (#2100):** when the PR watcher's `auto_fix_ci` recipe fired on a ticket whose queue row had already reached COMPLETED (or CANCELLED/FAILED), it called `add_ticket` for a fresh PLAN-stage row — `add_ticket`'s dedup guard is stage-scoped (#876), so the add always sailed past the terminal row — and the collision with the running `cw dev-queue serve` lock left a duplicate `BLOCKED_ON_USER` row (`disposition: terminal_sibling`, `attempts: 0`, `session_id: null`) occupying a lane slot forever: `cw dev-queue remove` refused with two rows matching, `--all` would have deleted the legitimate row too, and `cw doctor --reap`'s existing dead-session collapse just reverted it to PENDING, which `park_terminal_sibling_tasks` immediately re-parked `terminal_sibling` again on the next reconcile pass. `_dispatch_auto_fix_ci` now resolves the ticket's own row and mutates it in place instead: `requeue_ticket(..., from_completed=...)` when it's terminal (same stage, no sibling minted), nothing beyond the follow-up dispatch tick when it's already active or parked — a `DispatchLoopLockedError` from that tick now says explicitly that the row is already requeued and the running loop will pick it up. A `terminal_sibling` `BLOCKED_ON_USER` row no longer counts toward its lane's occupancy cap (`occupies_lane_slot`, applied at every dispatch/board consumer), `cw dev-queue remove` gained `--status`/`--disposition` selectors so a stuck duplicate can be removed without `--all` catching the real row, and `cw doctor --reap` gained a dedicated wedge class that CANCELs a never-claimed `terminal_sibling` park (never reverts it to PENDING, which would just re-park it) while the existing dead-session collapse now leaves every `terminal_sibling` row alone rather than ping-ponging it.
- **Tracker comments this pipeline writes now carry a machine-readable `<!-- cw-agent-authored -->` marker, and a directive sourced from any comment that would destroy work is never actioned headlessly (#2097):** an automated session posts under the operator's own account, so an agent-written comment and one the operator typed were byte-indistinguishable — a pipeline-authored comment directing deletion of a remote branch holding ~48 files of work was read by a later Stage-3 session as binding operator instruction, and that session exited `blocked` on an *invented* `blocker.reason` (`stale_branch_restart_directed`) that exists nowhere in cw but read exactly like a documented routing code. `cw.gh.post_issue_comment` — the single choke point every Python-side comment goes through — now appends `AGENT_COMMENT_MARKER` unless the caller passes `operator_authored=True`, which only `cw dev-queue approve --post-marker` does (that comment records the operator's own invocation); `is_agent_authored` is the read side. A new **Comment provenance rule** section in `.claude/commands/auto-dev.md` states the trust rule once — a comment carrying the marker, a pipeline fixed header, or the plan-of-record post is agent analysis and can never be plan-approval evidence, a Step 1c.0 settlement answer, or a 4c send-back adjudication input; only an unmarked human comment or a `<!-- auto-dev-preflight-resolutions -->` comment carries operator authority — and the four reader sites now cross-reference it instead of each restating a hand-maintained header list (the #1650 list at Step 1a and its narrower duplicate in the Decision branch below it could, and did, drift apart). Every prompt-driven producer — the plan-of-record post, both blocking-findings rules, the consolidated park, the operator-actionable checklist, and the previously unmarked, header-less PR-link comment — is instructed to append the same marker line, GitHub and Linear alike. The same section adds the destructive-directive gate: a directive from ANY comment, marked or not, that would delete a remote branch, force-push or rewrite shared history, discard work, or close/reopen a ticket exits `blocked` with the newly registered `destructive_directive_requires_operator` and `retry_eligible: false` rather than acting — the same fail-closed reasoning as finalize's semantic auto-resolve, which now cross-references it. Separately, `blocker.reason` gains a warn-only `KNOWN_BLOCKER_REASONS` registry: the enum stays open (§4.2) and an unrecognised reason still parses and is surfaced verbatim, but it logs `blocker_reason_unknown`, reads ` (unrecognized)` in the `session.needs_attention` breadcrumb and `?reason` in `cw dev-queue tasks`' REASON column (a prefix, so the flag survives the 20-char truncation), with `x_` reserved as the explicit freeform namespace that opts out. A doc-conformance test parses `auto-dev.md`'s `blocker.reason` table and asserts every row is registered, so the prose and the registry cannot drift.
- **Reviewers now see the approved plan's declared file scope, and `cw review consolidate` cross-checks each finding's file against it before it can reach FIX NOW (#2101):** a plan's `## Decisions` explicitly excluding a file ("Drop the X client work, tracked as #NNNN") was prose the reviewer subagents never received and consolidation never checked a finding's file against — so a Stage-3 MUST_FIX targeting the excluded file reached the fix loop unchallenged, and a later round then blocked `plan_deviation` citing the very exclusion the reviewer was never told about, an oscillation observed on two more tickets on a typing-only ticket's fix loop. Every reviewer prompt now carries a `## Planned File Set` block inlining the plan's `## Files Modified` manifest and any excluding/deferring `## Decisions`/`## Adopted Assumptions` lines, with the instruction to cap an out-of-scope finding's severity at SHOULD_FIX rather than silently drop it. `cw review consolidate` gains a `--plan <path>` option (parsed with `cw.plan_files.parse_plan_files_modified`, the same mirror `.claude/scripts/check_plan_scope_conformance.py` already uses) that stamps a new `AcceptedFinding.in_plan_scope: bool | None` on every accepted finding — `None` when no plan was supplied or the finding has no diff anchor, `True` when the file is in the plan's manifest or the diff's own changed-file set (so an incomplete manifest can never manufacture a false exclusion), `False` otherwise. This is adjudication input only, never a rejection — the #1632 silent-drop regression this deliberately avoids repeating. `/auto-dev-review`'s Checkpoint 3a gains rule (4d), between the existing (4b) spec-citation and (4c) operator-send-back cross-checks: an out-of-scope MUST_FIX/SHOULD_FIX routes to DEFER (not FIX NOW) with a rationale naming the excluding plan clause, unless the plan's own `## Decisions` names the file back in scope, and a finding that is both NON_DEFERRABLE and out of scope is a self-contradictory plan resolved via the existing 4a `plan_deviation` exit. The rendered verdict comment tags an out-of-scope finding inline with `(outside planned file set)`.
- **`review.rejected_count`/`review.rejected_count_by_severity` now default to `null` ("not reported") instead of `0`, and `auto-dev-review.md` actually populates them, so a review with real mechanically-rejected findings can no longer read as clean (#2098):** the #2000 fields existed on the `Review` schema model but were never added to the Freeze rule or the Stage 3 Completion sentinel template, so every producer omitted them and the Pydantic default of `0`/`{}` silently stood in for "nothing rejected" — observed live on a round reporting `rejected_count: 0` in the terminal sentinel while the underlying review-verdict artifact recorded `rejected_count: 2` (two rejected MUST_FIX findings). The Freeze rule now names both fields, copied verbatim from the first `cw review consolidate` call, "never hand-computed and never omitted"; the sentinel template carries them as `null` placeholders. Separately, `auto-dev-plan.md`'s `resolution_consumed`/`resolution_evidence` (#1896) emission rule gains a clarifying clause: it is correctly scoped to Step 1c.0 settlement only, and a Step 1b `## Binding Pre-flight Resolutions` merge is not a settlement and must never emit these keys — widening it would defeat `cw.dispatch.productivity`'s anti-gaming crashloop ceiling.
- **`cw dev-queue approve` on a `plan_pending_approval` row now works on Linear-tracked tickets — the approval is recorded on the dev-queue row instead of only as a GitHub comment (#968/#1906 follow-up):** the approve command had two GitHub-only legs that together left every Linear ticket looping at the plan-approval gate. The only approval evidence `auto-dev-plan.md`'s Checkpoint 1 accepted for a resumed Large-scope draft was a tracker comment, and the only cw-side producer of one was `--post-marker`, which posts through `gh` and can only fail against a Linear id — so the re-dispatched plan stage never saw the approval and parked `plan_pending_approval` again, every round. Separately, `_plan_is_reviewed` (and the auto-adopt gate recipe's `_plan_of_record_body`) called the GitHub-only `fetch_approved_plan_comment` unconditionally, then fell back to `task.worktree_path / .cw/plan.md` — a field dispatch never stamps on the `TicketTask` — for signoff markers that Step 1f only writes after the gate; the fallback could never fire for a dispatch-driven row. A new `TicketTask.plan_approved_at` (dev-queue schema v35) is stamped whenever a PLAN-stage gate is released, on both the #968 same-stage re-park and the direct advance, threaded into the worker's `cw-context.json` `queue_metadata` (schema v7), and accepted by Checkpoint 1 alongside an approving tracker reply. The GitHub fetch is now gated on the resolved tracker (skipped when it is positively non-GitHub), the `.cw/plan.md` fallback resolves the real branch-derived worktree and trusts it only when the checked-out branch matches, and `--post-marker` says honestly on a non-GitHub tracker that no comment was posted and where the approval lives. A `cw dev-queue requeue --regress` into the plan stage clears the approval (a re-plan revokes it); #2102 tracks binding approval evidence to the specific draft that was read.

- **`scripts/install-skills.sh` now installs `.claude/scripts/` alongside the commands that invoke them, so `/prep-pr`'s backing script can no longer go stale under `~/.claude/scripts/` (follow-up to #2090):** cw owned `commands/prep-pr.md` but nothing installed `prep_pr_state.py` with it — the `~/.claude/scripts/` copy came from a separate `global-claude` checkout, sat three versions behind, and silently stripped Step 7's `gate-timeout` ladder. Every file this repo tracks under `.claude/scripts/` (including the `utils/` package the top-level scripts import via `Path(__file__).parent`) is now symlinked into `~/.claude/scripts/` **per file** — never per directory — so scripts that exist only in `global-claude` keep working, and the manifest-scoped prune never touches them. A regular file already at a destination is replaced with the symlink and named under `scripts replaced` in the summary, with the `git rm --cached` hand-over that finishes the move in `global-claude`; when its bytes differ from cw's source (hand-edits, a newer copy) they are kept beside the link as `<name>.pre-symlink.bak` first, so the replacement is reversible from a file rather than from git history. `check_imports.py`, this repo's CI smoke-import gate, is skipped via a new `scripts/excluded-scripts.txt`, the same single-source-of-truth mechanism as `excluded-commands.txt`; `cw doctor`'s `skills-commands-drift` check reads it too and now tracks `.claude/scripts/` as a third root, so the exact drift that caused #2090 is reported as `differ` instead of going unnoticed. `/prep-pr` Step 2's prose no longer claims nothing syncs the installed path.

## [1.45.5] - 2026-09-03

### Fixed

- **The SSH-key preflight gate now keys on the transport the client's push actually uses, so an HTTP(S) or local-path remote is never held PENDING on the state of an ssh-agent it will never touch (#1495):** `check_ssh_key_available` probes `ssh-add -l` and the gate fails closed on any probe error, so in an environment with no ssh-agent at all — a CCR container whose `origin` is an HTTP remote through a git proxy, where pushes work with zero SSH involvement — dispatch held the entire fleet PENDING with `skip_reason: ssh_key_gate` on a premise (pushes need an SSH key) that was simply false. A new `cw.ssh.push_remote_scheme` resolves the client repo's effective push URL via `git remote get-url --push origin` (so `insteadOf`/`pushInsteadOf` rewrites are honoured — the answer is the transport a push really uses) and classifies it `http` / `local` / `ssh` / `unknown`; the gate, now extracted into `_apply_ssh_key_gate` beside `_apply_disk_pressure_gate`, skips the probe entirely for `http` and `local` remotes and engages it unchanged for `ssh`. `unknown` (not a git dir, no `origin`, git missing, timeout, an unrecognised URL shape) deliberately engages the probe too, so a scheme-resolution failure keeps the gate fail-closed rather than silently disabling it — the helper can only ever narrow the set of clients the probe applies to, never widen a bypass. Both the `dispatch.tick` `ssh_key_gate` skip payload and `gate.ssh_key_bypassed` (#1437) now carry `remote_scheme` naming which transport engaged the probe, so a false gate is diagnosable from events alone. Composes with #1436 (IdentityAgent resolution when the remote *is* ssh) and #1437 (the operator escape hatch stays the last-resort override).
- **`/prep-pr` no longer hard-codes `~/.claude/scripts/prep_pr_state.py`, so a stale installed copy can no longer silently strip Step 7's gate-timeout ladder (#2090):** that path resolves to a separate `global-claude` checkout's `scripts/` directory that nothing syncs with this repo, and its copy predated #1432 — so every `gate-timeout` / `gate-elapsed` call failed with `invalid choice`, looked like a bad invocation rather than a stale install, and the agent fell back to picking its own timeouts (the exact judgment #1432 removed). Observed live on #2089's `/prep-pr` run, where the full-suite pytest gate ran on an ad-hoc timeout. Step 2 now resolves the script once — `.claude/scripts/prep_pr_state.py`, then `scripts/prep_pr_state.py`, then the installed `~/.claude/scripts/` copy as the fallback rather than the default — and refuses to proceed when the resolved copy cannot answer `gate-timeout`, printing a `STALE:` line instead of letting timeouts be improvised (headless: a `Step 2 script resolution` BLOCK). Every later call site in the file (`snapshot`, `check-scope`, `gate-timeout`, `gate-elapsed`, `clean`) goes through the resolved `"$PREP_PR_STATE"`, the prose says shell state does not persist between `Bash` calls so the path is substituted literally, and `/auto-dev-finalize`'s semantic-resolve gate run carries the identical resolver fence. Step 9's "prefer the checked-out repo script" fallback sentence, which pointed at a `scripts/` relative path that does not exist here, now names the real `.claude/scripts/` layout. Guard tests pin both fence copies identical (#1634's all-copies rule), assert no bare installed-path invocation remains, and execute the resolver against real command trees — repo copy wins over a stale install, stale-only stops loudly, none-anywhere stops loudly, and this repo's own tracked copy passes the check it ships.
- **`tests/test_disk.py` no longer compares two live free-space readings for exact equality (#2091):** `test_walks_up_to_nearest_existing_ancestor_for_missing_path` asserted `check_disk_usage(missing) == check_disk_usage(tmp_path)`, so any write between the two `shutil.disk_usage` calls — the suite's own tmpdirs and caches during a full run — shifted `free_gb` in the ninth decimal place and failed the `NamedTuple` equality, most likely under exactly the parallel dispatch load this project exists to run. The test now spies on `shutil.disk_usage` and asserts the claim it was actually making — the missing path resolves to its nearest existing ancestor — plus the tolerance-shaped sanity checks its siblings already use.

- **`/prep-pr` Step 8 now finds a project ship-it that ships as a skill, not only one that ships as a command:** the step probed a single path (`test -f .claude/commands/ship-it.md`), so a repo whose ship-it lives at `.claude/skills/ship-it/SKILL.md` — commonly a symlink into `.agents/skills/ship-it/` — read as having no ship-it at all. Interactively that meant a STOP telling the user to create the ship-it they already had; headless it meant a `no project /ship-it` BLOCK, which `/auto-dev` finalize routes to a human, for a branch that could have shipped unattended. Step 8 now probes all three supported layouts, prints each hit's resolved path so a `.claude/skills` → `.agents/skills` symlink reads as one ship-it rather than two, invokes a skill-layout ship-it through the `Skill` tool (falling back to reading its `SKILL.md` where no `Skill` tool exists), prefers the command form if a repo genuinely has both, and names every probed path in the STOP/BLOCK message. `/auto-dev-finalize-appendix`'s operator prompt for that BLOCK says the same. Step 8 also gains a tie-break for the case where both skill paths exist as *different* files (prefer `.claude/skills/`, name the shadowed `.agents/` copy) — the symlink dedup does not cover it. `/setup` Step 6 carried the identical single-path probe and would have bootstrapped a command stub beside a working skill, which Step 8's command-wins precedence then prefers over the real ship-it: it now probes the same three layouts and skips the bootstrap when any of them matches. `/setup --reset` no longer routes a skill-layout repo into the command-template copy (that path writes `.claude/commands/ship-it.md`, which would shadow the skill it was asked to reset): an existing command is overwritten in place as before, an existing skill is left alone or hand-edited — there is no skill-flavored template, and the command one would produce a `SKILL.md` with the wrong frontmatter — and a skill→command migration is an explicit request with a resolved, symlink-aware removal step rather than a `--reset` side effect. Guard tests pin every prose copy of the layout list — both probe fences, the STOP message, the Notes bullet, the appendix prompt, and `/setup`'s Step 6 intro and bootstrap question — following #1634's all-copies rule, and execute both extracted fences against real command-, skill-, symlinked-skill-, and both-present trees, so a narrowing regression fails loudly instead of resurfacing as a spurious BLOCK.

## [1.45.4] - 2026-09-01

### Fixed

- **Reviewers are told the session worktree is shared and read-only, and a MUST_FIX about session-transient tree state no longer hard-blocks a round the session has already proven clean (#2087):** in a live large-scope round, the Test Reviewer ran a revert-and-rerun verification directly against the orchestrating session's shared worktree; two sibling reviewers observed the transient uncommitted mutation and each filed a MUST_FIX about working-tree divergence, `cw review consolidate` rejected both (they set `no_diff_anchor: true` against a real file/line), and #1714 then exited the round `review_blocked` — even though the session had verified the tree clean before dispatch and after consolidation and the reviewed diff was never affected. Net cost: a wasted review round, a real MUST_FIX and two SHOULD_FIX left un-bucketed, and an operator round-trip that could only end in "dismiss". Every reviewer prompt now carries a verbatim shared-worktree rule (read-only; request a kill-check in `suggested_fix` instead of running it; never file transient tree state as a finding or smuggle it through `no_diff_anchor`), the Test Reviewer spec says the same, and Step 3a captures `git status --porcelain` / `git diff HEAD --stat` before dispatch and again after consolidation. Checkpoint 3a gains a single, narrow carve-out on the #1714 exit: a rejected MUST_FIX whose subject is transient tree state — not diff content — is dismissed with both clean-tree captures quoted in the tracker comment and a `transient_state_finding_dismissed` friction highlight, if and only if both captures exist and were empty. Anything else stays on the exit.

## [1.45.3] - 2026-09-01

### Fixed

- **A review finding whose cited line resolves against nothing in the diff but exists in the file on disk is now degraded to file-level and adjudicated on its text, instead of being dropped as `invalid_line_reference` (#2081):** a MUST_FIX about a branch ~1000 commits behind its base ("required base update was skipped before editing shared configuration") was correct and load-bearing, but its line citation had drifted, the content-based rescue (#2007) found nothing in the diff to re-anchor it to, and the finding was mechanically rejected before adjudication — parked for the operator as a one-line summary plus a rejection code, and caught only by an out-of-band manual check. Line numbers are the least stable part of a finding and drift in exactly the stale-base situation where such a finding matters most, so `invalid_line_reference` was filtering hardest precisely when the finding was most likely to be real. Under the worktree opt-in, `_classify_drifted_finding` now returns a new `line_anchor_degraded` verdict when every cited line is within the on-disk file (an out-of-range citation stays `line_reference_out_of_range`; an unreadable file stays `invalid_line_reference`), and `validate_reviewer_document` routes it to `accepted` as a file-level finding with a new validation-stamped `Finding.anchor_degraded` flag — the #1632 `unanchored` precedent, which already adjudicates a finding on nothing more than its path existing on disk. The flag is hidden from the reviewer-facing schema (a reviewer-sent value is reset), so it can only ever mean validation dropped the anchor. The verdict comment annotates such findings inline so the adjudicator weighs the text rather than mistaking them for reviewer-filed file-level findings, and `/auto-dev-review`'s bucket rules say the same. Separately, the operator-facing park for a MUST_FIX that *is* still mechanically rejected now renders the finding's full original text — consequence, suggested fix, and verbatim evidence — under the rejection line, so it reads as "a MUST_FIX went unevaluated" rather than "the reviewer made a citation error".

## [1.45.2] - 2026-09-01

### Fixed

- **`build_aiderignore` now escapes gitwildmatch metacharacters when writing literal tracked paths into the generated `--aiderignore` file (#2072):** the file is parsed as glob patterns, not literal paths, so any tracked path with a bracketed segment either crashed aider deterministically — a reversed character range like `[sort-panel]` (a natural Next.js route-dir convention) made `pathspec` raise `re.error` inside `sanity_check_repo`, before the model was ever contacted, killing every aider-backed impl run in the repo as `aider_no_output` — or, when the pattern happened to compile, silently fenced the wrong file: `a/[b]/c.py` matched `a/b/c.py` instead of itself, so the plan scope fence quietly stopped blocking the file it named and blocked an unrelated one, with no error at all. `\` `[` `]` `*` `?` are now escaped (backslash first) in cw's own block and negation lines; a pre-existing repo `.aiderignore` is still merged in verbatim, since its lines genuinely are glob patterns. Regression tests assert the generated file against `pathspec` itself — the matcher aider feeds it to — covering the crash mode, the wrong-file fencing mode, and the bracketed-manifest-path negation.
- **The review fix loop's `pending_fix_dispatch` handoff now either spawns its fix session or pages, and its unparks no longer charge the attempt ceiling (#2075):** three occurrences in one operating day showed handoff records going unconsumed. The silent variant's mechanism: the handoff row is meant to stay RUNNING, but any non-sentinel RUNNING→PENDING revert (crash/phantom/stall sweeps) could re-park it with the record untouched, and `_claim_next_pending` — which never looked at the field — then dispatched a fresh REVIEW session whose live worktree made every subsequent `dispatch_fix_agent` attempt raise `HookContextConflictError`, retried silently forever with no `fix_dispatch_failed` and no `session.needs_attention`. Claim now holds any row carrying `pending_fix_dispatch` or `fix_dispatch_session_id` (the fix-dispatch pass owns it and dispatches regardless of row status), and a conflict on a handoff older than 15 minutes escalates through the loud failure path instead of retrying — the writing REVIEW session went terminal long ago, so a persisting conflict means the spawn will never succeed. Separately, both fix-dispatch unparks now pass `unproductive=False`: the routine completion unpark is progress (a full review round ran AND its fix session went terminal), and the failure unpark follows a round that produced a real action list — charging both made every healthy fix cycle count double against the attempt ceiling, blocking fully-approved finalizes behind `attempt_cap_blocked` and reading as instability that never happened.
- **The `dispatch_loop_stale` watchdog no longer false-pages ~90s after a healthy loop's queue-draining claim tick (#2076):** two compounding defects. A `dispatch.tick`'s `pending` payload is the tick-*start* snapshot, so the tick that claims the queue's last row records `claimed=1 pending=1`; the shared `_stale_pending_clients` predicate read raw `pending`, so once that final claim tick aged past `TICK_STALE_SECONDS` it looked like abandoned work (observed twice in one day: `pending=1 age_s=91` right after a successful claim, with no row actually pending). The predicate now reads `pending - claimed`, which also applies to `cw doctor`'s loop-liveness check by shared construction. And the watchdog scanned *before* the iteration's `dispatch_tick`, so the newest recorded tick at scan time was always a full iteration old — sleep plus the guarded pre-tick passes (PR hydration's `gh` calls included) plus the tick's own spawn work routinely exceeds the 90s threshold; the scan now runs after `dispatch_tick`, so a healthy loop's newest tick is seconds old when scanned (`--once` still scans exactly once, after its single tick).

## [1.45.1] - 2026-08-31

### Fixed

- **A codex review finding carrying `null` in the two #1837 admission-rationale fields is no longer mechanically rejected as `schema_invalid` (#2070):** the OpenAI strict-mode schema transform (#1364) wraps every defaulted field nullable rather than omittable, so codex faithfully emits `transitive_impact_evidence: null` / `release_critical_exception: null` on every finding that doesn't invoke the out-of-delta exceptions — and `Finding` typed both as plain `str`, so every such finding failed `model_validate` and was rejected before adjudication. In the observed incident a genuine MUST_FIX and a SHOULD_FIX were both discarded (`must_fix_initial=0`), the session parked as `codex_must_fix_mechanically_rejected`, and the ticket burned dispatch attempts to the cap on a pipeline defect rather than anything wrong with the change under review. A `mode="before"` normalizer now maps `null` to `""` — blank and null both mean "the exception was not invoked" — mirroring `_null_no_diff_anchor_to_default`, the fix for the identical gap #1817 opened (and #1837 reopened) in this same producer/consumer drift family as #190/#191. A new contract test walks every nullable-wrapped `Finding` field in the strict output schema and asserts the consumer model tolerates `null` for each, so the next defaulted field added without a matching normalizer fails in CI instead of silently discarding real findings at review time.
- **The review fix loop's async dispatch now actually runs instead of dying on every attempt (#2064):** `dispatch_fix_agent`'s spawn reaches `spawn_create_impl`'s own `sessions_lock()` acquisition, so it could never run from inside `_run_terminal_backstops_and_sweeps` while `reconcile()` still held `sessions_lock` — every attempt died with `SessionsLockReentryError`, silently swallowed by a broad `except CwError`, and no fix session was ever spawned. `run_fix_dispatch` is hoisted to `core.reconcile()`'s own post-lock pass (unconditional, ahead of the `completed_ticket_ids` early return so it still runs every tick). Since this makes the previously-dead `_act_on_pending_fix_dispatches` loop execute for the first time, a per-tick spawn cap (`_MAX_FIX_DISPATCHES_PER_TICK = 3`, mirroring `dispatch/pr_gate.py`'s `_MAX_PROBES_PER_TICK`) now bounds its fan-out, since this loop bypasses host_capacity/lane admission by design.
- **`cw review consolidate` now rescues a review finding whose evidence text is genuinely present elsewhere in the file, instead of mechanically rejecting it on line-number mechanics alone (#2019):** `_evidence_in_claimed_lines` missing (the `evidence_not_in_diff` path) used to reject outright, even when the finding's declared line was a valid anchor and its evidence quote was verbatim, real diff content just outside the (already #1792-widened) claimed window — three real-world occurrences shipped or nearly shipped defects because of it. A new `_classify_mislocated_finding` gives the evidence-gate the same unbounded, whole-file content rescue the anchor-gate already had via `_content_rescue_anchor` (#2007): a hit accepts the finding, and `_resolved_finding` gains a matching persist-time rescue so the finding's *persisted* anchor is corrected too, when that correction is possible without ever pointing at a context line. `_evidence_window_discrepancy_detail`'s message now notes when the unbounded rescue also ran and found nothing, so a genuine `evidence_not_in_diff` rejection's `detail` tells an operator both tiers were tried.
- **`worktree_gc._has_unpushed_commits` now resolves the checked-out branch's real upstream via `@{u}` instead of assuming `origin/<branch>` (#2053):** it previously compared unpushed-commit counts against `origin/<branch>` by name, so a branch pushed under a different name or without that exact remote ref was misjudged. It now resolves the actual configured upstream and falls back only when none is set, matching the same fix already applied to `_has_unpushed_commits`'s sibling helper `worktree_has_unsaved_work` under #2050.

## [1.45.0] - 2026-08-27

### Added

- **`cw queue peek`'s idle detection now blends in a spawned subagent's own liveness (#2028):** `idle_m` previously resolved exactly one transcript per RUNNING task and never looked at a subagent's own transcript, so a session actively waiting on a working subagent could read as stuck (`idle_m > 15min`, no PR) and get a false STOP-OR-PEEK recommendation. A new `cw._transcript.subagent_transcript_paths(project_dir, csid)` locates a parent session's own `<csid>/subagents/*.jsonl` (scoped narrower than the existing project-dir-wide rglob so a sibling session's leftover subagents in a reused worktree are never picked up), and `queue_peek._newest_subagent_ts` finds the freshest child activity, inheriting `parse_transcript`'s `away_summary` immunity. `format_row` blends the child timestamp into `idle_min` — most-recent-activity-wins, so a working child rescues a parent that looks idle, and a dead/absent child never makes `idle_min` look worse than the parent-only value did — and appends a reason suffix naming the rescuing child. The `unproductive_attempts` STOP arm in `recommend()`/`_score_session()` is likewise suppressed when a child is demonstrably active, falling through to `_stall_check` so it consults the same blended `idle_min` instead of firing on attempt count alone.
- **`create_worktree` now reports a colliding foreign worktree by name instead of surfacing git's bare stderr (#2034):** a branch already checked out elsewhere — most often an orphaned harness `Agent(isolation="worktree")` workspace cw's own GC never scans, since it carries no `Session`/`TicketTask` row (#2017) — used to bubble git's raw "already used by worktree at '<path>'" fatal straight through. A new `BranchHeldByWorktreeError` names the holder path, reports whether it looks clean or dirty, and gives the exact `git worktree remove` command either way. The holder itself is never touched, since cw cannot verify it is finished with. Separately, `_record_lane_spawn_error` now collapses `last_error` to a single line before a `LANE_PAUSED` circuit-breaker event: some raise sites (e.g. a `WorktreeError` embedding git's multi-line stderr) legitimately contain newlines, which previously broke `cli.queues._format_event_line`'s one-event-per-line rendering contract.

- **`cw guide` documents the dropped-approval hazard and stage-keyed monitor emission (#2033):** three new Gotchas bullets close a gap where an orchestrator reading a burst of monitor events could narrate them instead of executing an operator's answer that arrived in the same turn. They cover: an operator answer and monitor events share one tool-result channel, so the answer must be executed as the next step, not summarized alongside the noise; `cw dev-queue approve` clears the queue-state gate but is not itself the Large-scope carve-out's required live ticket-comment evidence, and skipping the comment silently re-parks the ticket; and a watcher that emits on every status change (including the non-actionable `pending → running` half of all events) should key emission on stage transitions and `blocked_on_user` arrivals instead, using `cw queue peek` for periodic liveness and `cw event tail --type`/`--dedup-terminal` to narrow the stream.

- **`--base` verification is now mandatory (mutually exclusive with a new `--no-base-check` escape hatch) on `cw review consolidate`, and `cw review verify-fixes` gains the same pair plus `--worktree` and a required `reviewed_sha` field (#1988):** `--base` on `consolidate` was previously optional with a silent skip when omitted — a payload could reach adjudication with a diff nobody verified against real git history, and nothing said so. `--base` and `--no-base-check` are now mutually required: a `click.UsageError` (exit code 2) is raised unless exactly one is given, mirroring `dev_queue_prune`'s `--client`/`--all-clients` guard shape. `verify-fixes` gains the identical `--base`/`--no-base-check` pair, a new `--worktree` option (mirroring `consolidate`'s), and a required `reviewed_sha` field on its request envelope — wiring the existing `_check_diff_matches_base` (#1924) helper onto the fix-verification path for the first time, so a "fixed" disposition can no longer be adjudicated against a hand-typed or corrupted fix-cycle diff either. `.claude/commands/auto-dev-review.md` is updated to pass `--base` unconditionally at both call sites (Checkpoint 3a and Step 3c), never `--no-base-check` — which is reserved for tests and human post-hoc recovery debugging — threading a new `CHECKPOINT_3A_SHA` variable between the two checkpoints so Step 3c has a concrete base ref instead of a prose placeholder.
- **The auto-dev review fix-loop wedge — a parent that ends its turn awaiting a subagent completion notification that silently never arrives — is now closed on both sides (#2012):** a fix-loop dispatched asynchronously by `auto-dev-review.md` Step 3b, if the spawn itself failed, left the parent waiting forever with no sentinel, no error, and a queue row stuck `running`. `cw agent-spawn-verify` is a new command the orchestrator runs in the same turn as the spawn — a bounded, single-call check (not a poll loop) confirming a real subagent transcript appeared before the parent ends its turn to await one. On the watchdog side, the liveness sweep's subagent-await suppression (which previously silenced the `SESSION_NEEDS_ATTENTION` distress signal for the life of any outstanding spawn) is now bounded by a new `fix_loop_await_deadline_minutes` config field: past the deadline, suppression lifts and the signal fires under a discriminating `fix_loop_await_deadline_exceeded` reason. Still signal-only — the deadline stops suppressing a signal, it never dispositions or kills anything (ADR-0014).

- **`cw dev-queue clear` now previews by default and requires `--confirm` to delete (#2003):** `clear` was the only bulk-delete command with no gate -- `-c <client> [-s <status>]` deleted every matching row immediately, including RUNNING (live sessions) and BLOCKED_ON_USER (parked work) rows, and its `--help` text called this "queue hygiene." It now takes the same `--dry-run`/`--confirm` pair `prune` (#382) does: without `--confirm` (or with `--dry-run`) it only reports what would be deleted, and a confirmed run derives its candidate set exactly once under the dev-queue lock so it cannot delete more than it reports. With no `--status`, RUNNING, BLOCKED_ON_USER, and AWAITING_OPERATOR_SIGNOFF (the existing `OCCUPIED_LANE_STATUSES` set) are now excluded from the sweep; naming one of them via `--status` still deletes it, since `clear` (unlike `prune`) must remain able to force-clear a stuck or parked row when an operator asks for it by name.

  - **BREAKING -- `clear` no longer deletes anything without `--confirm`.** Any non-interactive caller of `cw dev-queue clear` that relied on the old immediate-delete behavior now gets a preview and a "nothing was deleted" message instead of a mutation; add `--confirm` to restore the previous effect. No in-repo caller under `docs/` or `scripts/` was found to invoke `clear`.
- **Every mechanically-rejected review finding below MUST_FIX now leaves a trace instead of vanishing silently (#2000):** #1714 gave a mechanically-rejected MUST_FIX finding a signal and a force-block, but a SHOULD_FIX/DEBT/NIT/PRINCIPLE rejection was deleted with no counter anywhere — a review that had thrown findings away rendered and reported byte-identically to one that genuinely found nothing. `ReviewVerdict` gains `rejected_count`/`rejected_count_by_severity` (a superset tally of `rejected_must_fix`, stamped in `consolidate_verdict` from the same `all_rejected` list) plus `downgraded_disposition_count`, the sibling counter for the other silent-deletion path — `fixed` dispositions `verify_fixed_dispositions` downgrades to `dropped` because the fix-cycle diff never touched the cited location. Both counters are additive/default-zero, threaded through to the `AUTO_DEV_RESULT` sentinel's `Review` model so an orchestrator reading only the terminal sentinel sees them without opening `.claude/review-verdict.json`, and logged and rendered in the CLI review comment. They deliberately do not feed `Health.recommendation` — counting and rendering satisfy "impossible to ship silently" without folding a matcher miss into a gate that today means "coverage degraded". No `schema_version` bump (docs/headless-contract.md §8, Note A13). Follow-ups tracked separately: #2008 (a stale discard instruction in `auto-dev-review.md`), #2009 (verify-fixes audit trail + `downgraded_disposition_count` sentinel/render parity), #2011 (`review_adjudication.py` module-size split).

### Fixed

- **Worktree dirty-detection now resolves the real upstream of the checked-out branch instead of assuming `origin/<branch>` (#2050):** `_has_unpushed_commits` previously verified `origin/<branch>` by name and, if absent, fell straight to comparing against `origin/<default_branch>` — so a worktree whose branch was pushed under a different name, or whose upstream pointed elsewhere, was misjudged as having no canonical remote. It now resolves the worktree's actual upstream via `@{u}` on the checked-out branch and only falls back to `origin/<default_branch>` when no upstream is configured at all. `reconcile._shared._worktree_dirty_by_path` — used for DAEMON sessions, which always have `session.branch == None` in production — now forwards its own `worktree_path` through to `worktree_has_unsaved_work` (previously it silently re-derived the branch but dropped the path), so dirty detection actually inspects the worktree it was given rather than the canonical `worktree_path_for(client, branch)` location.

- **`cw queue peek` no longer STOPs a demonstrably live session (#2044):** `_score_session`'s age arm (`age_min > 55`) read no idle signal at all, and its `unproductive_attempts` arm only checked `child_active` (a subagent-only liveness signal added by #2028/#2038) — neither consulted the main session's own `idle_min`, so a session actively writing to its own transcript could still get told to STOP. A new `IDLE_LIVE_MAX_MIN` constant and `_gate_stop_on_liveness` helper are applied once, in `recommend()`, after every `_score_session` arm has returned: any STOP-flavored verdict is downgraded to PEEK (with a caveat appended) when `idle_min ≤ 2.0`. `idle_min is None` (no signal) does not suppress STOP, mirroring `_reached_deep_stage`'s existing "unknown = no signal" convention. One verdict-level gate covers every arm, present and future, instead of each arm re-deriving its own liveness check the way #2038's per-arm `child_active` fix did.

- **One schema-invalid finding no longer deletes every sibling finding in the same reviewer's document (#2029):** `ReviewerFindingsDocument.findings` is a `list[Finding]`, and Pydantic's list validation is all-or-nothing — so a single item failing a field or model validator (a missing `evidence`, a severity outside the Literal) made the *entire* `model_validate` call raise, discarding every well-formed sibling before `validate_reviewer_document`, which owns per-finding mechanical rejection, could run at all. The Claude-native path surfaced this as a `DocumentsFromReadError` the pipeline treats as a hard error; the codex path converted it to `CODEX_REVIEW_UNPARSEABLE`. Either way a reviewer's whole pass was lost over one malformed finding. A new `parse_reviewer_document` replaces the strict `model_validate` at both call sites (`cli/review.py::_load_reviewer_document` and `codex_review/_context.py::_parse_reviewer_document`), validating each `findings[]` item independently first: the survivors build the document and each casualty becomes an ordinary `RejectedFinding` under a new eighth `RejectedFindingReason`, `"schema_invalid"` — the only one produced before `_classify_finding` ever runs.

  - **The casualties reuse the existing machinery wholesale.** `_select_rejected_must_fix` (#1714) and `_count_rejected_by_severity` (#2000) are already generic `.get()`-based readers of `RejectedFinding.raw`, so a schema-invalid MUST_FIX force-blocks for operator review, and every severity is counted and rendered, with no new gating code. `consolidate_verdict` gains one additive `pre_validation_rejected` kwarg that seeds `all_rejected` before the per-document loop; every existing call site is unaffected.
  - **The rescue is per-item only.** A `findings` key that is not a list, a payload that is not a dict, or a document whose surviving findings still cannot satisfy its own invariants remains a structural failure and raises exactly as before. `.claude/commands/auto-dev-review.md`'s Checkpoint 3a non-zero-exit contract is narrowed accordingly — "a schema violation" now reads as a *structural* one, since a per-finding violation is a normal adjudication path.
  - **The residual case is counted rather than silently lost.** When a document genuinely cannot be built, no per-finding record exists — so `ReviewerRunFailure` gains a best-effort `discarded_finding_count`/`discarded_finding_severities` tally read off the raw payload, and `ReviewVerdict` gains `run_failures_with_should_fix_discards` selecting the failures that were claiming a MUST_FIX **or SHOULD_FIX**. That threshold is one step stricter than the per-finding MUST_FIX-only gate on purpose: there the finding's own text survives in `.rejected` for an operator to read, here only a count remains. Those failures park the run under a new `codex_reviewer_failure_discarded_findings` blocker reason, ordered ahead of `codex_review_partial` ("findings we threw away unread" is more specific than "the roster was incomplete") and behind both MUST_FIX branches, and render their own section on the posted comment. Deliberately not retry-eligible — an identical pass reproduces the identical schema mismatch. The inline `_ConsolidateInput.documents` array path has the same all-or-nothing limitation and is tracked separately as #2042; `_verdict.py`'s module-size split is #2043.

- **`create_worktree` no longer discards a branch's pushed history when only its local ref is gone (#2032):** it previously checked `refs/heads/<branch>` alone before falling back to creating a brand-new branch from the client's default branch, so a branch whose local ref was deleted (e.g. `auto-dev-review.md`'s fix-loop `git branch -D` reset) but that still existed on `origin` was silently recreated from `origin/main`, discarding its real pushed history. Resolution is now three-way: local ref present uses it as before; local ref absent fetches and checks `origin/<branch>`, resuming the worktree from there if present; only when neither exists does it fall through to `_resolve_branch_start_point`'s `origin/<default-branch>` ladder to create a genuinely new branch.
- **A review finding whose cited line drifted beyond the ±3 tolerance is now re-anchored by its own evidence text instead of being thrown away (#2007):** #1715's `_LINE_ANCHOR_TOLERANCE` repairs a citation that missed its true line by one to three; past that the finding was rejected as `invalid_line_reference` — or, on the fix-verification path, had its `"fixed"` disposition walked back to `"dropped"` — even when the evidence it quoted was genuinely present in the diff, just further down because an earlier hunk grew. A scan of 3,318 transcripts found 52 such downgrades across ~31 already-shipped tickets, plus live review-time false-rejects. A new content-based rescue searches for the evidence text with no line bound at all, delegating the actual match to the existing `_reconcile_evidence_window` so #1714's no-gap-synthesis floor and #1976's normalization carry over unchanged rather than being reimplemented. Strictly additive at all three call sites: a rescue that finds nothing leaves the existing rejection or downgrade exactly as it was.

  - **Each of the three call sites keeps its own substrate on purpose.** Classification searches `file_window_text` (context and added lines), the same substrate the evidence-quote check already matches against. Anchor persistence searches the narrower `file_line_text` (added lines only), preserving #1738's invariant that a persisted anchor never points at a context line — a finding rescued on a context line at classification therefore keeps its declared anchor rather than being snapped onto that line. Fix substantiation searches `file_diffs` for a genuinely removed (`-`) line, which has no new-file line number and so exists in no other substrate; it matches removed lines exclusively rather than substring-matching the whole hunk, since a plain substring cannot tell deleted code from untouched context elsewhere in the same file, and reporting a fix as substantiated when the cited code was never removed is the exact false accept that bar exists to prevent.
  - **`line_reference_out_of_range` is a new `RejectedFindingReason`**, split off the generic `invalid_line_reference` for the citation naming a line the real file does not have at all — an invented position no re-anchoring could repair, versus one that merely drifted. Produced only when a caller opted into the `--worktree` fallback (there is otherwise nothing to measure the file's length against); without one, the pre-existing reason is byte-identical to before. Its `detail` names the file's actual length, so the distinction is diagnosable from the sentinel alone.
  - The pure text-matching primitives move to a new `cw.review_findings._text_match` leaf and the rescue itself to `_reanchor`, so the two consumers share one implementation without an import cycle; `_validation.py` drops from 910 to 853 lines as a result.

- **`cw review consolidate` no longer mechanically rejects a valid finding over formatting trivia (#1976):** three shapes were observed bouncing real MUST_FIX/SHOULD_FIX findings back to the operator as `evidence_not_in_diff`. Two were genuine matcher defects and are fixed here — a Unicode em dash (or en dash, curly quote, or non-breaking space) quoted where the diff carries the ASCII form, and a `-`/`+` diff-pair quote for a single rewritten line, whose removed half has no new-file line number and so exists only in `file_diffs`. The third (evidence missing its leading `+` marker) was already handled correctly by #1715's per-line marker strip and is now regression-locked. Both evidence-comparison paths — `_evidence_in_claimed_lines`'s primary finding-evidence path and `_substring_in_diff`'s escalation-`evidence_quote` path — now apply diff-marker stripping and Unicode-punctuation folding identically; the diff-pair rescue stays scoped to the primary path only, since an escalation quote carries no declared line range for it to resolve against. Strictly additive: evidence absent from the diff under every normalization and the rescue stays rejected (#1714's false-accept floor), and a rejection's `detail` now names which normalization and rescue stages ran, so the next false-reject is diagnosable from the sentinel alone.
- **The review fix loop's agent now launches as a `cw`-owned session instead of an invisible harness subagent (#2017):** `auto-dev-review.md` Step 3b spawned its fix agent with the harness `Agent` tool and `isolation: "worktree"` — a worker cw could not see, could not control the placement of (the harness picked the worktree, so a branch could end up checked out somewhere cw's GC never scans), and could not verify the launch of beyond #2012's transcript-appearance probe. The fix agent is now dispatched by a new `dispatch_fix_agent` recipe (`cw.reconcile.review_recipes.fix_agent`, modeled on `address_review`'s `_dispatch_address_review`) under a new `SessionPurpose.FIX`, excluded from `WORKER_PURPOSES` the same way `ORCHESTRATE` is. The recipe provisions the ticket's branch-keyed worktree via the same `create_worktree` every other pipeline stage reuses, verifies HEAD landed on the expected `origin/<branch>` commit (replacing an agent eyeballing `git log --oneline -1`), merges `origin/<default-branch>` so the fix lands on top of any sibling PR that merged mid-pipeline, and aborts the merge and raises naming the conflicting files rather than forcing or auto-resolving. The dispatch is **asynchronous, and issued by the orchestrator rather than by the review session**. It has to be: cw provisions one worktree per `(client, branch)` with no per-purpose dimension, so the fix session's worktree *is* the review session's, and `spawn_create_impl` refuses to overwrite a hook context naming a still-live session — with no exemption for "that session is my own parent". A review session dispatching its own fix agent is therefore refused on every cycle, structurally, forever. Instead the review session records the action list on its dev-queue row (`TicketTask.pending_fix_dispatch`, dev-queue schema v34, carrying the prompt text rather than a worktree path that would die with the worktree) and exits with `blocker.reason: "fix_loop_pending_dispatch"`; a new always-on reconcile module (`cw.reconcile.fix_dispatch`) picks it up on a later tick, from a process resident in no worktree, and spawns the fix agent then. It is sited alongside `gate_recipes`/`escalation` rather than inside `review_recipes` deliberately — that package's master switch defaults off, and inheriting it would silently disable the fix loop for every client that never opted in. `dispatch/routing` short-circuits the new blocker reason before the generic blocked handling, so the row stays RUNNING (invisible to `claim.py`'s PENDING-only reclaim, which is what prevents a second REVIEW session being dispatched mid-fix) and is unparked to PENDING automatically once the fix session goes terminal, resuming at `s3_fix_loop, cycle_{N+1}` off the existing `Auto-Dev-Fix-Cycle` trailers. Because no session is running when an asynchronous dispatch fails, failures surface as `stage.errored`/`session.needs_attention` events rather than a blocker reason — `fix_loop_dispatch_unverified` is retired, replaced by `fix_loop_pending_dispatch`. Two further fixes inside the recipe: a successful spawn now records `session.spawned`, mirroring `dispatch/claim.py`, so a fix session is as auditable as any other; and the live-session refusal moved ahead of the fetch/merge, so a dispatch that cannot succeed no longer mutates the worktree on its way to failing — with the `git merge --abort` exit status now checked, so a failed abort reports a possibly-mid-merge tree instead of claiming it was left clean. Step 3b no longer removes the worktree or deletes the local branch ref at all; `create_worktree` takes its idempotent-reuse path. `cw agent-spawn-verify` itself is unchanged and kept, redirected from a pipeline call site to a standalone operator diagnostic and the shared transcript-resolution leaf #2028's `cw queue peek` consumer builds on.

## [1.44.0] - 2026-08-25

### Added

- **`cw guide` now documents three contracts that previously failed silently (#2015):** the operator guide ships inside the package, so it is the one documentation surface present wherever the binary is — a worker dispatched against a client repo loads that repo's `.claude/`, never claude-workspace's. Three rules that lived only in source or pipeline prose are now in it. (1) Pre-flight resolutions are binding only when the comment or body carries the literal `<!-- auto-dev-preflight-resolutions -->` marker; `/harden-ticket` appends it, a hand-written comment does not, and omitting it fails silently — including making a reviewer's "missing `## Pre-flight Resolution Conformance` section" finding a false positive. (2) A session waiting on a subagent never raises `session_unresponsive` at any age, because that signal requires no pending subagent spawn — so `cw queue peek`'s `idle_m` vs `age_m` is the only positive liveness check. (3) `/ship-it` and `/prep-pr` push `git branch --show-current`, which is the session branch inside an orchestrator worktree; check out the feature branch before delegating. The guide's `cw dev-queue clear` entry, which described an unguarded bulk delete as "queue hygiene", now states that it removes RUNNING and BLOCKED_ON_USER rows with no dry-run and no confirmation, and points at the new `prune` as the safe alternative.

- **`cw session prune` archives old completed/failed sessions, and session lookup is now archive-aware (#1983):** a new `session_retention` module (`prune_sessions`, `find_session_by_id`) backs a new `cw session prune` command that moves terminal sessions older than a retention window out of the hot `sessions.json` into dated archive files, keeping the live state file small. `find_session_by_id` (replacing the old `_resolve_session`) now falls back to scanning those archives when a session isn't found in the live state, so `cw resume`/`cw status`/session-inspect lookups keep working for archived sessions, and warns when a truncated archive scan may have missed a match. The per-session migration walk in `migrate_cw_state` is now version-gated so it's skipped once state is already current.

- **`_poller_tick` skips its load/poll/save cycle when neither state file changed (#1981):** the queue-channel poll tick previously ran a full `load_state()` (up to 12 MB `sessions.json`) plus `load_dev_queue()` every 2s regardless of whether anything moved. A new `_stat_for_change_detection` probe ((`st_mtime_ns`, `st_size`), any `OSError` treated as "changed") backed by two module-level guard-state singletons now skips the load-compute-save cycle entirely when neither `sessions.json` nor the dev-queue store changed since the last tick that actually ran one. The operator-bridge call stays unconditional and outside the guard, preserving the #1002/RFC-0008-W3 ordering guarantee.

- **`cw dev-queue prune --older-than <days>` retires stale terminal queue rows in bulk (#382):** `cw dev-queue remove` deletes one ticket at a time, so a queue that has accumulated months of COMPLETED/FAILED/CANCELLED rows had no maintenance path short of `clear`, which is indiscriminate. `prune` deletes only rows strictly older than `--older-than` days (default 90), and **previews without deleting anything unless `--confirm` is passed** — `--dry-run` says the same thing explicitly and wins if both are given. Each removed row emits the existing `task.deleted` event with a new `reason=operator_prune`, so a bulk deletion is as auditable as a single `remove`. Unrelated to `cw event prune`, which trims the event log rather than the queue.

  - **A confirmed run cannot delete more than it reports.** `prune_tickets` derives its candidate set exactly once, inside a single dev-queue lock acquisition — the same lock the dispatch loop takes — and deletes precisely that set, then the CLI renders that call's return value. Previewing first and deleting second would re-derive the set under a later lock, leaving a window in which a concurrent dispatch tick could grow the deleted set past what the operator was shown; the `--confirm` path therefore makes exactly one library call and never consults the unlocked preview helper.
  - **Live and operator-parked work is never prunable at any age.** RUNNING, BLOCKED_ON_USER, and AWAITING_OPERATOR_SIGNOFF (the existing `OCCUPIED_LANE_STATUSES` set, reused rather than re-declared) are refused outright when named in `--status` — an error, not a silent skip. Release them via `drain --held` / `approve` / `requeue` instead.
  - **PENDING is a double-gated carve-out.** It is prunable only when `--status pending` names it explicitly *and* a single `--client` is given; it is absent from the default status set and refused outright in combination with `--all-clients`.
  - **`--client` is required unless `--all-clients` is given**, matching the tenant-boundary guarantee `clear`/`drain` get from click's `required=True` — expressed as a `UsageError` raised before any library call, since click cannot state "required, with one named override". The library layer re-checks the same condition for any non-CLI caller.
  - Age is measured from `completed_at` falling back to `created_at`. A CANCELLED row always has `completed_at` cleared to `None` by its lifecycle transition, so keying on `completed_at` alone would have made every CANCELLED row permanently unprunable.

- **Merged-PR checks route through persisted `pr_state` before falling back to a `gh` call (#975):** `reconcile()`'s pre-pass, `complete_timed_out_merged_tasks`'s `_filter_merged_candidates`, `cw doctor`'s `_check_timed_out_merged`, and `_load_monitored_prs`'s mergeable overlay now resolve a ticket's merged/mergeable verdict from the dev-queue's fresh hydrated `pr_state` (GitHub #929) first, via three new `cw.gh` helpers (`pr_state_is_fresh`, `pr_is_merged_from_state`, `resolve_merged_via_pr_state`), skipping a redundant `gh` subprocess call whenever that state is fresh under the same staleness window `cw.pr_hydrate._throttled` uses. Falls back to the existing `pr_is_merged_for_ticket` gh call when no fresh state is available; `ci_status` is left exactly as review-monitor reported it.

### Fixed

- **Event-bus followers read only appended bytes instead of the whole inbox on every poll (#1979):** `read_events()` read and fully parsed the entire `events/inbox.jsonl` on every call and applied the cursor as a *post-parse* filter, so a follower poll cost O(entire inbox) no matter how many events were actually new — including when the answer was none. Against a 17.0 MiB / 42,797-event inbox, a poll returning zero events cost 543 ms of CPU and a 17 MiB read; at the 50 ms follow interval a single follower demanded roughly twelve cores' worth of work and simply pegged one. Four daemons on one machine accumulated ~917 GiB of re-reads in ten hours, all served from page cache, which is why it presented as sustained CPU (and laptop fan) rather than disk load. `tail_events_follow` and `wait_for_event` now resolve their starting cursor to a byte offset with one full read at startup and read incrementally from there: the same zero-event poll is 3.7 us and reads nothing, and a poll carrying one new event is 12.3 us for 404 bytes. Operators who have never pruned should still run `cw event prune --keep <n>`; the read path no longer scales with history, but every other `read_events` caller still does (see #1980).

  - **Delivery is preserved across a prune, not skipped.** When the inbox is replaced or truncated the byte offset stops referring to anything, so position is re-resolved from the last delivered event id and the surviving events the follower has not seen are replayed. `prune_events` keeps a *suffix*, so a follower whose cursor was pruned away has necessarily not seen anything that survived — replaying is correct and cannot duplicate. Skipping to the new EOF would have silently dropped those events, which for `cw event wait` means a script blocked on `session.completed` hanging to its timeout and concluding the session never finished.
  - **Replacement is detected by inode, not by size.** `prune_events` rewrites via `atomic_write_text` (temp file + `Path.replace`), so a rewrite is a new inode. Size arithmetic alone cannot see a replace-then-regrow that lands above the old offset, and seeking there would land mid-line and raise `JSONDecodeError` out of a loop that only catches `KeyboardInterrupt`/`BrokenPipeError`. Bytes and identity now come from one `fstat` on the descriptor that produced them, so they cannot describe different files.
  - **A failed `stat` is not a replaced file.** Transient stat failures (permissions, a network filesystem hiccup) are reported distinctly from "absent" and leave follow state untouched, instead of being mistaken for a truncation and forcing repeated full re-resolves.
  - Scope: `read_events(since_ts=...)` callers (`board.py`'s event feed, the dispatch loop's usage-limit cohort scan) and `cw_queue_events_server.py`'s snapshot reader are unchanged and still pay a full read — tracked in #1981-#1984.

- **`load_offset_from_file` parsed the entire channel log on every call instead of reading backward from EOF (#1986):** `EventBus.load_offset_from_file` now walks backward from EOF in bounded 64 KiB chunks and stops as soon as one chunk yields a valid record, instead of reading and JSON-parsing the whole file to find `max(offset) + 1`. Because `file_lock` is a `threading.Lock` (thread-scoped, not process-scoped), two processes appending to the same channel log can interleave writes so the file is not strictly offset-monotonic in file position — the offset returned is the max over the bounded read window already pulled off disk, not the first valid record found walking backward, so a lower, stale offset can never be handed back to the next `append_event` and cause a replay. A malformed line or a line truncated mid multi-byte UTF-8 character (crash mid-write) is skipped with a warning rather than aborting the scan. Follow-ups: the sidecar offset index (#1990) and channel-log retention (#1991) are tracked separately.

## [1.43.0] - 2026-08-22

### Changed

- **auto-dev pipeline docs split into core + appendix under per-doc size caps (#1879):** all five stage docs (`.claude/commands/auto-dev-{intake,plan,impl,review,finalize}.md`) now pair a common-path core with a rare-path appendix (`auto-dev-<stage>-appendix.md`) loaded only on named trigger conditions, cutting the per-invocation context cost of every pipeline stage. Guard tests were re-pointed to follow relocated content — every previously pinned literal stays asserted at its new location (a shared `_appendix()` reader joins `_cmd()` in `tests/conftest.py`), with no assertion weakened or dropped. Common-path contract text stays in the core docs (operative rules restored where the first split cut too deep), and residual over-cap docs carry caps re-scoped to measured floor +10%, documented on the ticket. The only source change is a citation-comment refresh in `src/cw/unavailability.py` pointing the auth-failure signature mirror at its relocated appendix home; no behavior change.

## [1.42.0] - 2026-08-22

### Added

- **Dispatch loop staleness watchdog + scoped-serve starvation WARNING (#1875):** the dispatch loop now detects when a client's tick summary goes stale while pending work is stranded and pages the operator via a recurring `session.needs_attention(paused_status="dispatch_loop_stale")` signal, debounced on a fixed interval (`OrchestratorConfig.dispatch_stale_notify_interval_minutes`, default 15) rather than exponential backoff, so the page keeps recurring while the loop stays stuck instead of decaying into silence. The same interval also throttles the watchdog's own inbox scans, since each scan parses the entire events inbox. A scoped `cw dev-queue run --client <name>` serve that starves other clients of scan coverage now also logs a WARNING. `cw doctor`'s `_check_loop_liveness` was refactored to delegate to the same shared `_stale_pending_clients` predicate the watchdog uses, so the two staleness checks can no longer drift apart. See `docs/dispatch-runbook.md` for the recovery runbook.

### Fixed

- **Liveness distress reads `agent_spawn_stamp` instead of transcript pairing (#1969):** removed the generic "any pending `tool_use`" suppression from the reconcile liveness path (`_awaiting_subagent`). It was inert — under default config and every live client's `liveness_buckets_minutes`, the 30-minute suppression window and the 45-minute distress-eligibility bucket share the same clock, so distress is never evaluated while a pending `tool_use` is still inside the suppression window. Liveness distress now reads the `agent_spawn_stamp` written to `cw-context.json` directly rather than re-deriving subagent liveness from transcript `tool_use`/`tool_result` pairing.

## [1.41.0] - 2026-08-20

### Added

- **`stale_dispatch` parks now self-register a `WatchedPr` for their own blocking PR, so they can self-release (#1927):** a `stale_dispatch` park (blocked behind its own earlier, un-harvested-sentinel PR) is discovered by a live `gh pr list --head <branch>` self-check that never writes a `pr_url` onto the `TicketTask` row, so `release_stale_gated_tasks`'s Variant B cross-reference had nothing to match the park's `blocked_on_pr` against and the park could never self-release. A new `cw.reconcile.stale_dispatch_watch` pass closes the gap by registering the blocking PR as a `WatchedPr` — a store-level PR watch already hydrated every serve tick by `_hydrate_watched_prs` (RFC 0011 S2, #1154) — and wires the registration into the dispatch tick. Because the existing `register_watched_pr` dedup key (`repo`, `pr_number`, `status == "active"`) has no client dimension, registration goes through a new `register_or_adopt_watched_pr` that adopts a client-less existing watch for the same PR or, on a genuine cross-client collision, refuses to mutate and instead emits a new `WATCHED_PR_COLLISION` (`watched_pr.collision`) event naming the park's client, the PR, and the colliding watch — never a silent no-op that would leave the park permanently unreleasable.

## [1.40.0] - 2026-08-20

### Added

- **`cw guard-busy-wait` PreToolUse guard — the mechanical layer for #1944's never-busy-wait rule (#1946):** #1945 shipped the prose half (an explicit "never busy-wait" rule in `auto-dev-{plan,impl,review}.md`) after finding a headless review pass that spent 173 of its 234 `Bash` calls on `true` — a worker holding its turn open waiting on an async Agent spawn it had no blocking primitive to wait on. That pattern is worse than wasteful: ADR-0014 removed every kill timer, so the only automated stuck-worker signal left is the transcript-staleness liveness sweep, and a no-op poll loop keeps the transcript fresh enough that the spinning worker classifies as LIVE and `session.needs_attention` never fires. A new `cw guard-busy-wait` command rides the existing `"Bash"` `PreToolUse` matcher in `cw.spawn._HOOK_SETTINGS_TEMPLATE` alongside `cw guard-cwd` and blocks (exit 2, reason fed back on stderr) three shapes: a bare `true`/`:` no-op, a bare `sleep N` with no follow-on work (`sleep 5 && ./run_tests.sh` passes), and an identical command repeated past a configured threshold inside a rolling window. Fail-open throughout, mirroring `cw guard-cwd`: unreadable stdin, a missing/malformed context, a contended lock, or any unexpected error all yield a silent exit 0.

  - **Hashed, not raw, state.** The rolling window stored in `cw-context.json` holds a truncated SHA-256 of the whitespace-normalized command, never the command text — shell commands routinely embed secrets inline (`Authorization` headers, exported keys), and repeat detection needs only equality. Prior to this the file's only write precedent was a plain integer counter.
  - **Every block is observable.** A new `OrchestratorEventType.GUARD_BUSY_WAIT_BLOCKED` (`guard.busy_wait_blocked`, documented in `docs/events.md`) records the reason, the command hash, and the resolved threshold/window, following the `cw signal-stop` precedent of a hook subprocess calling `record_event`. The event write is isolated in its own `try`/`except`, separate from the classifier's fail-open wrapper: once a block is decided, a briefly-unwritable event bus must never turn it back into an allowed call.
  - **Per-lane with a global default.** `busy_wait_guard_enabled` / `_repeat_threshold` / `_window_seconds` on `OrchestratorConfig` (defaults `true` / `3` / `300`), each overridable bidirectionally per lane via the matching `LaneConfig` fields — the same shape `reap_policy` uses, not `codex_fix_loop_enabled`'s opt-in-only asymmetry. Config is re-read per hook subprocess, so an operator edit takes effect on the next Bash call with no worker restart. Documented in `config/CONFIG_REFERENCE.md`.
  - **`cw-context.json` schema v5 → v6:** adds a `lane` key. The hook runs as a bare subprocess with only that file for context — no cw session, no dev_queue row — so without the key the per-lane override would be unreachable. `null` for USER-origin sessions, which have no lane.
  - Reuses `cw.cli._hook_io._write_cw_context_locked` (extracted by #1947) as its state primitive rather than hand-rolling a parallel lock/read/write, becoming the third consumer of the discipline that module's own docstring anticipates. Also adds `tests/test_cli_hook_io.py`, closing a pre-existing gap: that module backed three hook commands with no direct test coverage of its primitives.
  - **Known limitation, deliberate:** the Bash tool's `tool_input` field names (`command`, `run_in_background`) are inferred, not captured — no Bash-tool `PreToolUse` payload exists in this repo, only the Agent-tool shape. Every read is `.get()`-based and type-checked, and an unexpected shape fails open with one loud stderr warning naming the mismatch, so a wrong inference degrades to "guard does not fire, and says so on every call" rather than "guard silently never fires". The `cw doctor` retrospective check and the progress-staleness liveness dimension the ticket also sketched stay deferred.

- **`cw review consolidate` gains envelope-integrity guards and two flags
  (#1924):** the hand-assembled consolidate envelope had no integrity check at
  all, so a duplicated diff hunk or a paraphrased evidence quote reached the
  evidence matcher as if it were the real thing. Two guards now screen every
  payload unconditionally, before any consolidation: a placeholder-diff check
  rejecting a `diff` that never carried a diff (an unresolved `<diff here>` /
  `<insert diff>` / `...` token at any length, or text under a 40-character
  floor that *also* carries no `diff --git` header — the conjunction, never
  length alone, so a real but truncated diff still passes), and a
  duplicated-hunk check rejecting the same hunk repeated for the same file,
  keyed on `(path, hunk text)` so byte-identical hunks under two different
  files stay legitimate. `--base <ref>` additionally verifies the payload's
  diff text is byte-identical to the real `git diff <ref>...<reviewed_sha>`,
  resolving the repo root from `--worktree` (falling back to the current
  directory) via a local of its own — not `resolved_worktree`, which
  `--no-tree-evidence` nulls, so the check cannot silently no-op when both
  flags are passed. `--documents-from <dir-or-glob>` reads each reviewer's
  findings document off disk instead of from an inline `documents` array (a
  path that exists and is a directory is read as `<path>/*.json`, anything
  else as a glob; matches consolidate in lexicographic filename order, and a
  `documents` field still present on the payload is ignored). Zero matches is
  valid — a round in which every reviewer failed writes no documents at all —
  but a source whose parent directory does not exist is not, and any
  unreadable, non-JSON, or schema-invalid file names itself in the error.
  `documents` is now optional on the envelope. `.claude/commands/auto-dev-review.md`,
  the sole producer, is rewired onto the new path: each `REVIEW_FINDINGS`
  block is Written verbatim to `.cw/review-findings/<role-slug>.json` rather
  than retyped into JSON, which is what made a one-word paraphrase of a
  verbatim-substring `evidence` field possible in the first place.

## [1.39.0] - 2026-08-20

### Fixed

- **#1646 agent-spawn stamp reconfirmed hollow under the async `Agent` tool; `PostToolUse:Agent` wiring removed (#1947):** replaying a live async `Agent(isolation="worktree")` spawn (session `ea2f3d42`/ticket #1902) showed `PostToolUse:Agent` fires at launch-return (`13:12:09.513Z`, paired with the `Async agent launched successfully.` tool_result) — ~3.9s after `PreToolUse:Agent` and ~3.5s *before* the harness's own `turn_duration` record still reported `pendingBackgroundAgentCount: 1`. The `agent_spawn_stamp.unresolved_count` pair balanced back to 0 while the subagent was, per the harness's own accounting, still running, so the phantom sweep's `unresolved_subagent_spawn` signal never fired for this incident (no `session.reap_proposed` event exists for it). `PostToolUse:Agent`/`cw agent-spawn-post` are removed; `cw signal-stop` (`cli/stop_hook.py`) now snapshots/clears the counter off the Stop hook payload's own `background_tasks` list instead — a signal that tracks the harness's live turn-accounting rather than a tool-call return that races ahead of it. The shared lock-then-read-then-write plumbing (`cw.cli.agent_spawn_stamp._context_lock` and friends) moved to `cw.cli._hook_io._write_cw_context_locked` so both `cw agent-spawn-pre` and `cw signal-stop` share one discipline against the same file. `cw.reconcile._shared`'s read side (`extract_unresolved_spawn_count`) is unchanged. See #1886.

- **auto-dev dispatch rules no longer assume a synchronous Agent tool (#1944):**
  the headless spawn rules in `auto-dev-{plan,impl,review,finalize}.md` were
  written against a subagent tool that ran synchronously unless given
  `run_in_background: true`. That parameter no longer exists — Agent spawns are
  asynchronous unconditionally — which left "block on each before dispatching the
  next, and do NOT end the parent turn between them" with no blocking primitive
  to use, and drove sessions to hold the turn open with no-op `Bash` calls
  instead. One observed headless review pass spent 173 of its 234 `Bash` calls on
  `true`. The rules now say to end the parent turn and resume on the spawn's
  completion notification, which is safe: in-flight subagents appear in the Stop
  hook payload's `background_tasks` as `{"type": "subagent", "status": "running",
  ...}` (verified empirically), so `cw signal-stop`'s existing deferral guard
  prevents the #175/#176 orphan. Adds an explicit never-busy-wait rule — after
  ADR-0014 removed every kill timer, the only automated stuck-worker signal is
  the transcript-staleness liveness sweep, and no-op polls keep the transcript
  fresh so a spinning worker classifies as LIVE and `SESSION_NEEDS_ATTENTION`
  never fires — and documents the parent/subagent asymmetry: a parent's
  turn-end is a pause, a subagent's is a return, so a subagent must not
  background work and then return. Also sweeps the dead Agent-tool
  `run_in_background: true` from `review.md`, `review-monitor.md`, and
  `review-sweep.md` (issue #1944 remaining-work item 2; Bash usages keep the
  parameter — it is only the Agent spawn that lost it), and marks the
  `auto-dev-finalize.md` capture-gate spawn as claude-native-only: opencode's
  FINALIZE stage consumes the same file (#1670) but has no Agent tool, Stop
  hook, or completion notifications, so it must run the capture inline.

### Added

- **Provider-overload (API 529) classifier and reconcile diagnostics (#1923):** `src/cw/unavailability.py` gains a sibling `FAMILY_PROVIDER_OVERLOAD` / `PROVIDER_UNAVAILABILITY_SIGNATURES` table and `classify_provider_unavailability()`, covering the Anthropic API 529-overload signature captured live in dev-1751. Kept separate from the existing `UNAVAILABILITY_SIGNATURES` table since that table's prose drift-guard test requires every signature appear verbatim in files scoped to git/gh subprocess failures, not API errors. The reconcile phantom sweep now surfaces this as a diagnostics-only `provider_overload_detected` field on `ReapCandidate` (DAEMON-only), reported in the `SESSION_PHANTOM_REVERTED` event payload and documented in `docs/events.md`. It carries zero routing weight — `resolve_reap_policy`, `_route_phantom_by_policy`, and `reap_policy: signal_only` are all untouched. Layer 1/3 of the #1923 SPLIT plan; retry-routing (Layer 2) split out to #1948.

- **Worker-declared provider_overload honored in Rule 5 routing (#1948):** Layer 2/3 of the #1923 SPLIT plan. `cw.dispatch.routing._route_stage_failure` (extracted from `_route_staged_decision`'s STAGE_FAILURE_STATUSES branch to stay under the PLR0912 branch ceiling) now special-cases a `blocked` status whose `blocker_reason` is `FAMILY_PROVIDER_OVERLOAD`: instead of routing to `SESSION_NEEDS_ATTENTION`/`BLOCKED_ON_USER`, the task same-stage retries — `task.stage` is left untouched (no `_stage_regress`, no pipeline boundary crossed), `task.session_id`/`task.stage_base_ref` are cleared, and the task transitions back to `PENDING` marked unproductive so it counts toward the global `unproductive_attempts` ceiling. The emitted `TICKET_REQUEUED` event carries `reason: "provider_overload_retry"` plus `session_id`/`unproductive_attempts` correlation fields, matching the sibling `finalize_regress`/`SESSION_NEEDS_ATTENTION` emissions.

## [1.38.0] - 2026-08-19

### Added

- **`resolution_consumed`/`resolution_evidence` wired into `AutoDevResult` (#1896):** the plan-stage settlement mechanism (`auto-dev-plan.md` Step 1c.0) now emits both fields on a plan-stage pause when the round settled at least one ambiguity/premise item, and `AutoDevResult` (`cw.auto_dev_result.schema`) validates them — `resolution_consumed` is a `StrictBool` rejecting coercible non-bool values, and `resolution_evidence` requires a non-empty `comment_id` and non-empty `items` list when present. `cw.dispatch.productivity`'s existing STRICT consumer, previously structurally correct but dormant per the #1750 plan's first Adopted Assumption, is now live against a real producer.

- **OpencodeExecutor extended to all auto-dev stages (#1669):** the
  opencode backend previously supported FINALIZE only; it now handles PLAN,
  IMPL, REVIEW, and FINALIZE. FINALIZE keeps its `auto-dev-finalize.md`
  command-file prompt (the one stage validated backend-neutral, #1670 R6),
  now resolved worktree-first (the git-tracked authoritative copy) with a
  `~/.claude/commands/` fallback for client worktrees. PLAN/IMPL/REVIEW get
  self-contained prompts carrying each stage's essential headless contract —
  their command files require Claude Code-only machinery (Agent/Skill tools,
  `$CW_SESSION`, hook-written `.claude/cw-context.json`) an opencode
  subprocess cannot execute. Every opencode failure sentinel now carries the
  dispatched stage's own `stage_reached` entry marker
  (`stage_entry_marker`), so a PLAN-stage failure no longer reads as a
  later-stage self-escalation that walks the task pointer past planning.
  Three headless-wiring fixes make opencode functional in fire-and-forget
  dispatch: `TMPDIR` added to `_ENV_ALLOWLIST` (tempfile calls in
  gh/git/python were failing silently), `--auto` flag added to `build_argv`
  (permission prompts were hanging with no TTY), and command-file resolution
  to absolute paths. `SLACK_MCP_CLIENT_ID` and `SLACK_MCP_CLIENT_SECRET`
  added to `_ENV_ALLOWLIST` so Slack MCP OAuth succeeds in the subprocess
  env.

## [1.37.0] - 2026-08-19

### Added

- **Claim-time disk-pressure preflight gate for dispatch (#1887):** dispatch
  now checks free space on a client's worktree-base mount (via
  `cw.disk.check_disk_usage`, a `shutil.disk_usage` probe that walks up to
  the nearest existing ancestor) as a third preflight gate, between the
  SSH-agent-key gate and the freshness gate. When free space drops below
  `OrchestratorConfig.disk_pressure_min_free_gb` (default 5.0 GB), the
  client is held `PENDING` for the tick with `skip_reason=disk_pressure_gate`
  instead of spawning a session onto a filling disk. The probe fails open on
  `OSError`, mirroring the freshness gate, and
  `disk_pressure_gate_enabled=False` is the operator escape hatch; each
  bypass records `gate.disk_pressure_bypassed`, forwarded to the operator
  channel by default.

- **Variant B gate-release predicate extended to `stale_dispatch` parks
  (#1902):** `release_stale_gated_tasks`'s cross-reference scan now also
  recognizes a `stale_dispatch`/`pr_already_open` park (alongside the
  existing `merge_gate_blocked`/`prior_pipeline_pr_open` case) as eligible
  for auto-release once its blocking PR merges. This is groundwork only: no
  production code path yet populates a store row's `pr_url` for the
  `stale_dispatch` producer, so the new branch is unit-test-reachable but
  production-unreachable until the independent PR-state source in #1927
  lands — the ticket's literal "self-releases once merged" acceptance
  criterion is not yet satisfied by this diff alone.

### Fixed

- **`--aiderignore` blocks non-manifest files from aider's reflection-loop
  echo re-adds (#1915):** aider's reflection loop rescans the model's own
  reply text on every round, independently of the `--message`/`--read`
  split #1905 closed, so an excluded path echoed in reasoning prose could
  still get auto-confirmed back into the chat under `--yes-always`. `cw`
  now materializes a `.cw/aiderignore` blocking every git-tracked file the
  plan's `## Files Modified` manifest doesn't name (folding in and negating
  against any pre-existing worktree `.aiderignore` so a client repo's own
  patterns can't shadow a manifest path), threads it through
  `_local_preflight`/`spawn` as `--aiderignore`, so an excluded file is
  never addable in the first place.

- **Fence-aware `## Files Modified` heading matcher in the scope-conformance
  gate (#1917):** `check_plan_scope_conformance.py`'s `_parse_files_modified`
  previously matched the first `## Files` heading line-by-line with no fence
  tracking, so a plan illustrating a fixture `## Files Modified` heading
  inside a fenced code block earlier in the document was parsed as the real
  enumeration — see #1905's impl-gate false positive. Both this script and
  its mirrored copy (`src/cw/plan_files.py::parse_plan_files_modified`) now
  skip lines inside fenced code blocks (``` and `~~~`, including indented
  fence markers) before searching for the heading.

## [1.36.0] - 2026-08-18

### Added

- **Per-lane attempt ceiling for dispatch and concierge (#1751):** dispatch's
  claim path and reconcile's concierge recovery recipes previously enforced
  only the single global `dispatch.attempt_ceiling`. Adds
  `LaneConfig.attempt_ceiling` and `resolve_attempt_ceiling`, which resolve a
  per-lane override before falling back to the global default, so individual
  lanes can carry their own attempt budget without disturbing the fleet-wide
  ceiling.

- **`cw spawn close --requeue` (#1889):** folds
  `cw dev-queue requeue ... --from-cancelled` into `cw spawn close`, so
  closing a confirmed-dead session whose `claude --bg` async-completion
  wakeup never arrived and requeuing its ticket back to PENDING at its
  current stage is one command instead of two separate ones run in sequence.

### Fixed

- **Headless aider no longer stalls silently on a missing-file free-text ask
  (#1905):** three related defects in `LocalExecutor`. (1) `build_argv` passed
  no `--file` flags at all, so which files entered aider's chat was decided
  entirely by aider's own path-mention heuristic; the plan's `## Files
  Modified` manifest is now parsed (`cw.plan_files.parse_plan_files_modified`,
  mirroring `.claude/scripts/check_plan_scope_conformance.py`) and threaded
  through pre-flight into one `--file` flag per planned path — a plan with no
  manifest section still falls back to the previous behaviour. (2) The full
  plan and ticket prose used to be embedded in `--message`, which is the exact
  text aider scans for path mentions, so a plan's own "EXPLICITLY OUT OF SCOPE"
  list and its touch-point citations force-added under `--yes` precisely the
  files they named as untouchable; that content is now materialised to
  `.cw/task_context.md` and handed to aider via `--read`, whose content bypasses
  the mention scan, while `--message` carries only a fixed, path-free
  instruction. (3) A run that ended with the model asking for a file to be
  added to the chat, instead of emitting edits, was reported as the generic
  retryable `aider_no_output`; it now gets the distinct, non-retryable
  `blocker.reason` `aider_file_request_unanswered` so the disposition is
  self-diagnosing and parks for a human instead of re-dispatching into the same
  stall.

- **A requeue-to-impl refusal message no longer claims a non-GitHub tracker
  was checked when it wasn't (#1906):** `requeue.py`'s impl-bypass plan-
  availability guard called `fetch_approved_plan_comment` (GitHub-only)
  unconditionally as its tracker fallback, so a Linear-tracked (or other
  non-GitHub) ticket's requeue-to-impl refusal wrongly told the operator "no
  reviewed plan comment ... was found on the tracker" even though the
  tracker was never actually queried, and directed them to re-run Stage 1
  when an approved plan may already be posted there. The guard now resolves
  the client's tracker first: it still fail-opens (attempts the GitHub fetch)
  when the tracker is unresolvable, but skips the call and returns an honest,
  tracker-aware refusal message when the tracker is positively known to be
  non-GitHub — pointing the operator at tracker-side plan recovery instead of
  a redundant Stage 1 rerun.

- **A usage-limit-only attempt history no longer reads as a legitimate
  first-stage ship (#1631):** reconcile's timed-out-merged backstop refused to
  auto-complete a PENDING row only when `attempts == spawn_error_count`, but
  the two counters move asymmetrically on the `UsageLimitError` revert path —
  `attempts` increments at claim time while `spawn_error_count` deliberately
  does not (`stamp_backoff=False`, so a usage limit is never charged as a spawn
  error against #868's fleet-wide backoff). A row whose every attempt died that
  way therefore looked byte-identical on the task record to one that genuinely
  ran, shipped and timed out inside its first pipeline stage (both carry
  `stage_high_water is None`), and was silently marked `shipped` off a PR it
  never opened. Adds `TicketTask.ever_spawned` (dev-queue schema v33), stamped
  `True` at the single spawn-success seam in `dispatch/claim.py`, seeded
  `False` at the single true-new-task constructor (`cw dev-queue add`), and
  OR'd into `_is_never_claimed`'s guard. The field is durable — no requeue,
  regress, revert or retry clears it — and both the model default and the
  migration fill are `True` (fail-open): a legacy row carries no record of its
  spawn history, and retroactively refusing a legitimate completion would be
  worse than the bug. The `SESSION_NEEDS_ATTENTION` breadcrumb now names which
  of the two refusal causes fired instead of unconditionally blaming the
  spawn-error path. The usage-limit back-off mechanism itself is unchanged.

## [1.35.0] - 2026-08-17

### Changed

- **The global dispatch attempt ceiling now counts unproductive claims, not
  every claim (#1750):** the ceiling compared raw `task.attempts`, so a ticket
  making genuine forward progress burned the same budget as a crashloop —
  #1727 reached 9 of 10 attempts mid-pipeline after a run of legitimate stage
  claims that each produced commits or real review findings, staying alive
  only via a temporary ceiling override. Adds `TicketTask.unproductive_attempts`
  (dev-queue schema v32) and moves the ceiling to it at all four call sites
  (`dispatch/claim.py`'s two claim paths, `reconcile/concierge.py`'s recipe 1
  and recipe 2 `refused_ceiling`). `transition_task_status` gains a
  keyword-only `unproductive: bool = True` and charges the counter on a
  genuine RUNNING exit only; the default means every crash/phantom/stalled/
  wedge revert still counts with no code change. A claim is productive when it
  pushed commits or surfaced real review findings — classified by the new
  schema-owned `cw.dispatch.productivity` module, which both the routing and
  reconcile paths share. Stage advances and regresses are productive by
  construction via hardcodes at `_advance_task_pointer`/`_stage_regress`, as
  are `no_op` completions, terminal-stage completions, merged-PR crash
  salvages, and the mechanically-rejected-MUST_FIX park (a reviewer
  malfunction the ticket is not charged for). Every charge decision is
  recorded on the existing `task.transition` event via new
  `unproductive_attempts` and `unproductive_charge` payload keys. The #756
  per-stage `validation_failed` cap deliberately still reads raw `attempts`.
  The #1653 crashloop the ceiling exists to stop is still caught at exactly
  the same rate. The "consumed an operator resolution" productivity signal is
  wired in the schema (`ClaimEvidence.resolution_consumed`) but its producer
  (sentinel emission of `resolution_consumed`/`resolution_evidence`) is not —
  a plan-stage clarification round-trip is still charged as unproductive until
  the producer ships in the #1896 fast-follow, so the N-round-trip guarantee
  is not yet general for that case.

### Added

- **Dispatch now gates on an already-open PR for PLAN/IMPL-stage tickets, with
  a new `stale_dispatch` terminal status (#1862):** a ticket could be
  re-dispatched into PLAN or IMPL while a PR from a prior attempt was still
  open, producing duplicate work and duplicate PRs. Adds a pre-dispatch
  open-PR probe (`src/cw/dispatch/pr_gate.py`) backed by an
  `OpenPrProbeCache` sidecar so repeated ticks don't re-query GitHub for the
  same ticket, gated by the new `OrchestratorConfig.pr_gate_enabled` escape
  hatch (default enabled, mirroring `ssh_key_gate_enabled`) and skipped
  outright when a client has no free dispatch slots. The probe loop caps
  itself at 20 open-PR checks per tick and batches its sidecar writes rather
  than persisting one at a time. Detecting an open PR routes the ticket to
  the new `stale_dispatch` terminal status instead of dispatching a
  duplicate; recovery is via `cw dev-queue requeue <ticket> -c <client>`,
  documented in `docs/dispatch-runbook.md` and `docs/session-disposition.md`.

- **`SESSION_NEEDS_ATTENTION` now re-fires on a debounced cadence for
  sessions latched at the top staleness bucket (#1858):** a session
  saturated at `STALE_45M` previously fired the operator distress signal
  exactly once at the bucket crossing and then went quiet forever, so a
  session stuck there for hours produced no further signal. The liveness
  detect pass gains a second candidate kind — a level-detector for a
  session still latched at `STALE_45M` whose renotify debounce window has
  elapsed — alongside the existing edge-detector for bucket crossings.
  Distress is re-evaluated fresh on every renotify check rather than
  latched from the initial fire, and `Session.liveness_attention_next_eligible_at`
  (schema v18, with the new `OrchestratorConfig.liveness_attention_renotify_interval_minutes`
  knob, default 60) is cleared on recovery below `STALE_45M`. Every re-fire
  carries a fresh `renotify_marker` in its `SESSION_NEEDS_ATTENTION` payload,
  fed through a `_terminal_dedup_key` widened to a renotify-marker-aware
  4-tuple, so `cw event tail --dedup-terminal` shows each re-fire as a
  distinct row instead of collapsing it with the original.

- **Cross-round adjudication memory for codex review (#1838):** an operator
  adjudication settled in one review round was forgotten by the next, so
  already-rejected findings kept coming back. Adds the durable
  `TicketTask.finding_dispositions` ledger (dev-queue schema v31), keyed by
  `cw.review_debt.fingerprint_v1`, fed from the ticket thread's
  `REVIEW-FINDING-DISPOSITIONS` operator marker and applied by the codex
  review context/verdict path so settled findings are suppressed on later
  rounds. The field deliberately has no clear site: unlike the per-arrival
  markers (`regressed_into_stage`, `pending_operator_comment`), a settled
  adjudication is a durable fact about the ticket.

- **Stale-gate detection for dev-queue tasks parked behind a cleared PR gate
  (#1713):** dev-queue rows blocked behind a merge/CI gate (their own PR, or
  a different ticket's PR ahead of them in the pipeline) never observed that
  gate clearing — `hydrate_pr_states` emitted `pr.merged` events every tick,
  but nothing consumed them against dev-queue task state. Adds
  `ReapReason.STALE_GATE`, a `stale_gate_detected_at` task field, and a new
  `release_stale_gated_tasks` reconcile pass wired into the dispatch loop
  that re-validates both blocking variants (own-PR "merge_pending"/
  "automerge_not_armed", and cross-ticket "merge_gate_blocked") and, under
  `ReapPolicy.AUTO`, releases the task (own-PR case completes it, blocking-PR
  case requeues it to PENDING). `SESSION_REAP_PROPOSED` fires on every
  detection regardless of policy, per ADR-0006 precedent; `SIGNAL_ONLY`
  (default) only stamps the detection timestamp.

- **Accepted-limitation decision record for the no-sentinel regress-marker
  gap (#1801):** `TicketTask.regressed_into_stage` (#1794) does not survive
  a spawn that dies before ever emitting a sentinel — the marker is
  consumed and cleared at spawn time (`dispatch/claim.py`), well before any
  reconcile reap could observe the death. Evaluated changing the clear to a
  sentinel-gated one and rejected it: it would fragment the shared
  `_stage_regress` seam this field co-stamps alongside
  `pending_operator_comment`/`finalize_regress_branch_head`, and it
  reintroduces the same false-negative shape at the impl guard's early
  `blocked`-exit path. No behavior changed — this documents the accepted
  gap (comments in `src/cw/models/tasks.py`, `dispatch/claim.py`,
  `.claude/commands/auto-dev-impl.md`, `docs/dispatch-runbook.md`) and adds
  characterization tests pinning that reconcile's reap path never touches
  the field and that the second post-death spawn still carries no regress
  signal.

- **Codex review findings, treadmill/convergence tracking, and review debt
  (#1837):** extends `cw.review_findings` and `cw.codex_fix_loop` with
  delta-per-fix-cycle review handling, and adds two new modules —
  `cw.review_debt` (review-debt tracking) and
  `cw.codex_fix_loop_convergence` (fix-loop convergence/treadmill
  detection) — plus supporting changes to the `cw.codex_review` package
  and `cw.models.enums`.

- **`empty_diff_blocked` sentinel status for zero-commit branches
  (#1870):** an `/auto-dev` branch that reaches IMPL/REVIEW exit — or the
  dispatch loop's own empty-diff gate — with zero commits ahead of
  `origin/<default_branch>` previously had no dedicated terminal status,
  so it was liable to be mislabeled `crashed` and re-dispatched onto the
  same empty branch. Adds the `empty_diff_blocked` status (schema v6,
  accepted under all supported versions during rollout) plus
  `EMPTY_DIFF_BLOCKER_REASON` (`empty_diff_no_commits`), wires it into
  `STAGE_FAILURE_STATUSES`, `SALVAGE_TERMINAL_STATUSES`, and
  `_TERMINAL_REJECT_STATUSES`, adds `cw.branch_ahead.commits_ahead_of_default`
  to measure the diff, adds a dispatch-level empty-diff gate, exits
  `empty_diff_blocked` from the clean review-synthesis path, and classifies
  it in `wait` exit codes and the monitor. Documented across the headless
  contract and the producer skills (`auto-dev-impl`, `auto-dev-review`,
  `auto-dev`).

### Fixed

- **`cw queue peek`'s STOP/WAIT/PEEK ladder read raw `attempts`, disagreeing
  with the dispatch admission gate's #1750 signal (#1768):** #1750 moved the
  admission gate's ceiling check to `TicketTask.unproductive_attempts`
  (charged only on a claim that exits RUNNING with no evidence of progress),
  but `queue_peek.py` was deliberately left on raw `attempts` at the time —
  so a ticket at 12 raw attempts with 0 unproductive attempts (the #1727
  all-productive shape) was admission-gate-healthy yet advised **STOP —
  systemic** by `cw queue peek`, exactly when an operator is deciding
  whether to kill a session. `_score_session`'s STOP-by-attempt-count branch,
  `recommend()`, and `format_row()` now read `unproductive_attempts` instead
  (renamed `STOP_ATTEMPTS_MIN` to `STOP_UNPRODUCTIVE_ATTEMPTS_MIN`, same
  threshold of 3); raw `attempts` remains on the row as display-only data.

- **`cw review adjudicate --deferred-findings-out` overwrote the prior
  round's record instead of merging into it (#1840):** every call rendered
  only its own round's applied adjudications and atomically full-replaced
  the target file, so a second call in one Stage 3 pass silently dropped
  the first call's entries. `review_adjudication.py` gains stamped
  `round`/`recorded_at` fields (both optional, so a legacy pre-#1840 entry
  parses identically to an unstamped one), a merge that dedupes new
  entries only against prior entries by a wider fingerprint (never against
  each other within the same round, which previously collapsed two
  distinct findings with the same templated text into one and silently
  dropped one from the audit trail), and a `parse_deferred_findings_md`
  that fails closed on its own durable output rather than swallowing a
  parse error. `cli/review.py` now reads and merges prior content before
  re-rendering. The reject-entry placeholder severity constant was also
  promoted to the public `REJECTED_ENTRY_SEVERITY` so `cli/review.py`
  stops reaching across the module's private-by-convention boundary.

- **`prior_attempts_summary` leaked prior-attempt failure summaries across
  clients sharing a ticket number (#1839):** `_collect_prior_attempts_summary`
  filtered the shared `sessions.json` by `ticket_id` alone, so two clients
  dispatching the same ticket number (e.g. ticket 47 in both `review-bingo`
  and `definitely-not-digimon`) had their prior-attempt failure summaries
  cross-contaminated in the next retry's `cw-context.json`. Added a required
  `client` kwarg and `s.client == client` to the filter, mirroring the
  existing `(client, ticket_id)` predicate already used by
  `concierge._find_session_for_ticket`.

- **Codex-subsystem `make_blocked()` call sites silently borrowed
  LocalExecutor's `next_actions` label instead of the Codex-review one
  (#1842):** `#1835` fixed this for the 4 call sites in
  `cw.codex_review._verdict`, but 6 more sites — 2 in `executor.py`'s
  `CodexExecutor.spawn`, 1 in `codex_background.py`, and 3 in
  `codex_fix_loop.py` — still called raw `local_runner.make_blocked()` and
  either explicitly passed `next_actions=_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS`
  or silently defaulted to the LocalExecutor-specific label, the same
  N-patched-sites shape #1835 left behind. Added `make_codex_blocked()` — a
  thin wrapper around `make_blocked()` that permanently bakes in the correct
  `next_actions` and takes no override — and migrated all 10 Codex-subsystem
  call sites onto it, so every future Codex call site inherits the right
  label by construction instead of remembering to pass it. LocalExecutor/
  aider and OpencodeExecutor call sites are unchanged.

- **`/prep-pr`'s default Python quality gates didn't include `ruff format`
  (#1867):** `ECOSYSTEM_GATES["pyproject.toml"]` in
  `.claude/scripts/prep_pr_state.py` ran `ruff check`, `mypy`, and `pytest`
  before PR creation but never checked formatting — a client repo with no
  `## Quality Gates` override in its own `CLAUDE.md` could land a PR with
  unformatted code that CI's separate `ruff format --check` gate would then
  reject (the review-bingo-hub incident, #45/#48, PRs #52/#57). Added a
  `ruff-format` `Gate` entry (`uv run ruff format --check .`, autofix `uv run
  ruff format .`) immediately after the existing `ruff` entry, so the
  fail-fast ruff-family checks run before `mypy`/`pytest`. Repos needing a
  scoped or custom format command already override via the CLAUDE.md
  `## Quality Gates` section — no change needed to that merge logic.

- **Reviewer `schema_mismatch` on `no_diff_anchor: null` (#1817 regression):
  `Finding.no_diff_anchor` was added as `bool = False` without a
  `mode="before"` None-normalizer, unlike `detail`/`findings` on
  `ReviewerFindingsDocument`. The OpenAI strict-schema transform
  (`to_openai_strict_schema`) makes it nullable+required, so `codex exec
  --output-schema` faithfully emits `"no_diff_anchor": null` on every
  finding that doesn't use the #1817 no-diff-anchor marker — and
  `ReviewerFindingsDocument.model_validate()` rejected `null` for a `bool`
  field, classifying the entire reviewer document as `schema_mismatch`.
  Every reviewer role with ≥1 finding failed. Added
  `_null_no_diff_anchor_to_default` mirroring the existing
  `_null_detail_to_default` / `_null_findings_to_default` pattern, plus a
  round-trip test that would have caught the gap at #1817 time.

## [1.34.0] - 2026-08-13

### Added

- **`cw dev-queue add --stage <plan|impl|review|finalize>` (#1682):** a
  removed row previously had no direct path back onto the queue at a
  specific stage — the only recovery was `add` → `cancel` → `requeue
  --from-cancelled --stage <x>`. `add --stage` closes that gap, reusing
  `requeue --stage`'s exact vocabulary (now a shared `_STAGE_CHOICES`
  constant) and its per-client pipeline-membership check (now a shared
  `_validate_stage_in_pipeline` helper in `cw.dev_queue.crud`, called by both
  `add_ticket` and `_apply_requeue_stage`). An unrecognized `--stage` value
  fails loudly at Click's parse step — exit code 2, no row inserted, no
  silent fallback to the default stage. Enqueuing off-`plan` also stamps
  `TicketTask.stage_high_water` (matching what the 3-step workaround already
  produced) and adds `"stage"` to the `TICKET_ENQUEUED` event payload.
  Omitting the flag preserves today's default behavior exactly. See
  `docs/dispatch-runbook.md` §2.

- **Unresolved sub-agent spawns are stamped, detected, and routed to a
  terminal disposition (#1646):** a worker that dies (or pauses forever)
  mid-spawn was invisible to every transcript-based signal — no terminal
  sentinel, no `tool_result`, just a surface that stops — and used to land
  under the generic `phantom_surface` disposition, indistinguishable from a
  clean, harmless crash. `spawn._write_hook_context` now seeds an
  `agent_spawn_stamp` counter into the worktree's `.claude/cw-context.json`
  (schema v5); new `cw agent-spawn-pre`/`cw agent-spawn-post` hooks
  increment/decrement it around each sub-agent spawn (matched on the
  empirically-captured `Agent`/`Task` tool names), and the phantom sweep
  reads the counter to stamp `_UNRESOLVED_SUBAGENT_SPAWN_REASON` instead —
  overriding `reap_policy: auto` and routing ticket-less crashes straight to
  a terminal state rather than an unresolvable park. See
  `docs/session-disposition.md` §6b.

- **`cw dev-queue status` distinguishes a dead dispatch loop from a busy one
  (#1742):** a stale tick alone could not tell "the loop crashed — restart it"
  from "the loop is alive and an executor is mid-review — leave it alone", and
  both rendered as `[STALE — no tick in Ns]`. The dispatch-side codex worker
  now records a transient `ExecutorBlockedMarker` in the `dispatch_state.json`
  sidecar for the duration of a review, so `cw dev-queue status` renders
  `[BLOCKED — codex review, ticket 1723, 449s]` instead and `cw doctor`
  suppresses its `loop-liveness` warning. Markers are wiped at dispatch-loop
  boot — a review's daemon thread never outlives its process, so any marker
  surviving a restart is orphaned by construction.

- **`codex_review` gets its own blocked `next_actions` label (#1835):** added
  an optional `next_actions` override to `local_runner.make_blocked`, and a
  new `_CODEX_REVIEW_BLOCKED_NEXT_ACTIONS` constant so `codex_review`'s four
  blocked-result call sites no longer inherit `local_runner`'s
  LocalExecutor-specific `user_resolve_local_executor_failure` default. A
  Codex CLI subprocess failure was previously mislabeled as a
  LocalExecutor/aider failure, costing a full transcript dig to diagnose.

### Fixed

- **Review health gate no longer parks clean work on Test Reviewer's
  read-only-sandbox degradation (#1856):** `_derive_health` treated any
  reviewer document with `status != "ok"` as reason to downgrade health to
  `EXIT_FOR_HUMAN_REVIEW`, including a Test Reviewer document that
  self-reports `status="degraded"` because it can never start pytest under
  codex review's unconditionally read-only sandbox (`_roles.py`'s
  `--sandbox read-only`, the MUST_FIX 4 remedy from #1236) — a structural
  limitation true on every ticket, not a signal about the diff under review.
  `_derive_health` now excludes exactly the `("Test Reviewer", "degraded")`
  pair from its confidence computation via the new
  `_is_environment_muted_degradation` predicate; a Test Reviewer
  `status="failed"` document, or a `"degraded"` document from any other
  role, still downgrades health and still parks. Real test verification is
  unaffected: the IMPL-stage worker and CI both run the actual test suite
  elsewhere in the pipeline — the codex Test Reviewer was never capable of
  running tests itself, on any ticket.

- **Codex fix-cycle retries its commit once after a pre-commit hook rewrites
  files (#1855):** a repo-local pre-commit hook that reformats staged files
  (ruff-format, black, prettier) exits non-zero on the run where it changes
  something — standard "hook modified files, nothing was committed" behavior.
  `_commit_fix_cycle` now re-stages and retries the commit exactly once before
  giving up, so a correct, fully-written fix is no longer stranded uncommitted
  in the worktree. A second failure still surfaces as the same
  `CalledProcessError`/`codex_error` as before — no regression.

- **Codex model-capacity errors are retried instead of parking the ticket
  (#1836):** a `codex exec` reviewer role that failed with `{"type":
  "turn.failed","error":{"message":"Selected model is at capacity. Please try
  a different model."}}` (exit 1) parked the ticket `blocked_on_user` — a
  definitionally transient provider blip needing an operator to un-stick it.
  Root cause: `_classify_codex_failure` maps every nonzero exit to
  `nonzero_exit` → `CODEX_ERROR`, and only `CODEX_TIMEOUT` /
  `CODEX_BUDGET_EXHAUSTED` were in `_TRANSIENT_FAILURE_REASONS`, the set that
  drives `Blocker.retry_eligible`. `_run_codex_role` now sniffs the terminal
  `turn.failed` event's `error.message` (via the new
  `_extract_terminal_error_message`) for the narrow, case-insensitive `at
  capacity` marker and overrides the coarse reason to the new, transient
  `CODEX_MODEL_CAPACITY`, so reconcile self-heals the role. Only the coarse
  reason changes: the fine-grained `ExecutorFailureCategory` stays
  `nonzero_exit`, so the persisted diagnostics bundle and every other
  consumer of the shared taxonomy are untouched, and any nonzero exit without
  that exact phrasing still parks as `CODEX_ERROR`. `retry_eligible` is now
  transient-derived on both the zero-documents *and* partial-review
  (`CODEX_REVIEW_PARTIAL`) dispositions — a capacity blip hitting one role
  while others still produced documents (the likelier shape for a
  concurrently-dispatched review) no longer falls back to non-retry-eligible.

- **`cw queue peek`'s STOP-by-age reason splits "approaches" vs "exceeded"
  wording (#1795):** the STOP-age message now says `age Nmin approaches
  60-min timeout` while `age_min` is still below the 60-min ceiling
  (`STOP_CEILING_MIN`), and `age Nmin exceeded 60-min timeout` once at or
  past it — previously the text always said "approaches" even after a
  session blew well past the ceiling. The `cw-queue-peek` and `cw-fanout`
  skill docs were also corrected: the liveness-signal wording now describes
  idle time since the last **parsed transcript record** (what `idle_min`
  reports), not file `mtime`, which can advance after a session has gone
  silent and falsely read as "live."

- **Infra-only CANCELLED/STARTUP_FAILURE checkruns no longer flip `ci_ok`
  to `False` (#1684):** `_summarize_status_checks` in `src/cw/pr_hydrate.py`
  now treats a CANCELLED or STARTUP_FAILURE conclusion as `ci_ok` when every
  other checkrun on the PR passed — a pure GitHub-infra outage (e.g. a runner
  cancellation) was previously indistinguishable from a real code failure,
  which wrongly fired the `ci_failing` attention state, dispatched an
  `auto_fix_ci` session, and paged the operator for an outage that needed no
  code change.

- **Release-tag commit-subject contract documented, and `scripts/release.sh`
  now guards it (#1707):** the two release-tagging paths —
  `.github/workflows/release-tag.yml` (automatic) and `scripts/release.sh`
  (manual) — shared a machine-parsed commit-subject contract that only the
  workflow enforced; `docs/release-playbook.md` never mentioned the workflow
  existed. `scripts/release.sh` gains a pre-flight guard (byte-identical
  regex, pinned by an anti-drift test; opt-out via
  `RELEASE_SH_SKIP_SUBJECT_GUARD=1`) that catches a mis-shaped subject before
  the manual path can silently compensate for a failing automated run, and
  the playbook now documents both paths and the contract explicitly.

- **`release-tag.yml`'s automated release path never closed
  `dispatch-guard.yml`'s `dispatch-drift` issues — the closer step only
  existed in `release.yml`'s manual-tag path (#1799):** `release.yml`'s
  `release` job carries a "Close dispatch-drift issues" step gated on
  `github.ref_name`, which is only meaningful for its `push: tags: ['v*']`
  trigger. `release-tag.yml` — the workflow that actually creates the tag and
  release on every `chore(release):` merge to `main` — had no equivalent, so
  issues `dispatch-guard.yml` opened sat open indefinitely on the automated
  path. `tag-release` gains its own closer step (same `gh issue list`/`gh
  issue close` body, `RELEASE_TAG` built as `v${{ steps.guard.outputs.version
  }}` per this file's own tag-naming convention) plus `issues: write`
  permission, and "Dry-run summary" now reports how many `dispatch-drift`
  issues a real run would close. `release.yml`'s copy is left in place —
  idempotent and still correct for the manual-tag path.

## [1.33.0] - 2026-08-11

### Fixed

- **A MUST_FIX finding whose remedy is outside the diff now has an
  operator-actionable route instead of being silently dropped (#1817):** a
  reviewer finding whose fix is not a code change at all — an acceptance
  criterion demanding a follow-up ticket that was never filed (#1764) — had no
  honest way to be expressed. Reviewers invented a fake `file` value, which
  `_classify_finding` then mechanically rejected as `unknown_file`, routing a
  genuine MUST_FIX into #1714's operator park behind an anchor nobody could
  trust. `Finding` gains an explicit `no_diff_anchor` marker (with `file`
  pinned to the literal `"N/A"` and line anchors rejected outright), and
  `_classify_finding` short-circuits to acceptance on it before any anchoring
  check runs. `Disposition` gains `"operator_actionable"` and
  `AdjudicationOutcome` gains `"operator_action"` mapped to it — a genuine
  recorded decision that stops blocking the verdict, scoped to `MUST_FIX`
  severity at the model so a SHOULD_FIX cannot regain the route through a
  prose-only edit. `.claude/commands/auto-dev-review.md` declares a fourth
  adjudication bucket, a `## Operator-Actionable Review Findings` tracker
  checklist rule triggered by the adjudication entry (not by the exit reason),
  and a `blocker.reason: "review_operator_actionable"` override wired at
  Step 3c. A finding that is *also* NON_DEFERRABLE never reaches the new
  bucket — it keeps exiting `plan_deviation` — so that exit is folded into the
  #1815 blocking-findings comment rule as a third trigger site, closing the
  one blocked-exit door that rule did not cover. `auto-dev.md`,
  `auto-dev-plan.md` and `docs/headless-contract.md` are updated to match.
- **`plan_unreviewable`/`plan_unsound`/`review_blocked` blocked exits now post
  their MUST_FIX finding(s) to the tracker (#1815):** these three headless
  exits carried the blocking finding(s) only inside the `blocked` sentinel's
  structured payload — no tracker comment was ever posted, so the next round
  (or a human triaging the ticket) had no visibility into *why* the plan or
  review was rejected without digging through the session transcript.
  `.claude/commands/auto-dev-plan.md` and `.claude/commands/auto-dev-review.md`
  now declare a shared `## Blocking Review Findings` header — the same
  greppable-fixed-header idiom `## Pending Verification Scan` already uses
  for the Step 1c park exits (#1650) — and post the persisting station's
  finding(s) verbatim before exiting. `auto-dev.md`'s decision tables and
  `docs/headless-contract.md` are updated to note the new comment.
- **A persisted cycle verdict snapshot no longer disagrees silently with the
  reported blocker (#1763):** the fix loop writes one
  `cycleN-review-verdict.json` per cycle (#1739), but nothing on disk said
  which one actually backed the returned `Blocker.details` — so an operator
  opening `cycle0-review-verdict.json` "by habit" read a legitimately-empty
  `rejected_must_fix` while the blocker cited a MUST_FIX that a later cycle's
  re-review mechanically rejected (#1729). `ReviewVerdict` gains an in-band
  `is_terminal_snapshot` marker: each per-cycle persist stamps it `false`, and
  each true fix-loop exit path re-writes exactly the one file it rendered its
  details from with `true`, so at most one snapshot per session ever reads
  terminal. The `friction_highlights` pointer now also names the specific
  snapshot filename instead of only the diagnostics bundle directory.
  `docs/session-disposition.md` records the reading rule as Gotcha 4.
- **`session_inspect` emits the full `Session` field set instead of a
  hand-maintained subset (#1624):** the human-readable session detail view
  now derives its displayed fields from `Session.model_dump()` rather than a
  drift-prone, hand-maintained field list — new `Session` fields
  automatically appear in `session_inspect` output without a matching code
  change.

- **Degraded/failed reviewer verdict without a stated reason is now a
  contract violation (#1806):** `ReviewerFindingsDocument` rejects
  `status="degraded"`/`status="failed"` with an empty or missing
  `detail` at parse time — closing the gap #1775 could not reach (it can
  only persist a reason that exists). A reviewer that self-reports
  degraded with no reason is now treated the same as any other
  malformed output: rejected at the parse boundary and surfaced as a
  blocked review (`CODEX_REVIEW_PARTIAL`/`CODEX_REVIEW_UNPARSEABLE` on
  the codex path, a hard `cw review consolidate` failure on the
  Claude-native path) rather than a spuriously clean `status="ok"`-
  looking pass. The reviewer output contract in
  `.claude/commands/auto-dev-review.md` and the codex adapter's
  `_OUTPUT_SCHEMA_RULES` now state the reason requirement explicitly.

### Added

- **Structured, content-anchored voided-findings suppression seam
  (#1814):** review findings can now be explicitly voided (rather than
  silently dropped) via a new `VoidedFinding` model and
  `review.finding_voided` event, and the codex review path consults voided
  findings so a previously-voided finding is suppressed on re-review by
  content fingerprint rather than by line anchor (which shifts across
  diffs). `cw review check-voided` exposes the suppression decision as a
  CLI command, and ADR-0015 records why the fingerprint is
  content-anchored. Fixes a latent bug where diff markers (`+`/`-`) were
  stripped from void-fingerprint evidence, weakening the fingerprint match.

- **REVIEW-stage requeues deliver live operator comments to codex-backend
  reviewers (#1730):** when a fresh operator comment triggers a REVIEW-stage
  requeue, the codex backend now receives that comment's content directly
  instead of re-reviewing a stale snapshot. If delivery to the backend can't
  be completed, `TicketTask.pending_operator_comment` marks the task so the
  gap degrades loudly (a `requeue.review_delivery_degraded` event) rather than
  silently reviewing without the operator's input. `gh.fetch_issue_comments`
  is now public to support the fetch path.

- **`branch_freshness` ticket-branch staleness probe closes both REVIEW approve
  paths against a stale branch (#1823):** REVIEW-scoped approvals no longer
  advance a task whose ticket branch has drifted behind its tracked base
  without first routing through the branch-staleness park — both approve paths
  (the direct approve and the walk-path approve) are now closed against it, not
  just one. The REVIEW-scoped gate table was extracted out of
  `src/cw/dispatch/routing.py` into a new `src/cw/dispatch/review_gates.py`
  module as part of adding the probe, shrinking `routing.py` toward the
  ~1000-line module ceiling instead of growing it further.

- **Claude-native review adjudication seam with `cw review adjudicate`/`verify-fixes` subcommands (#1805):**
  review findings can now carry an `accepted`/`rejected`/`deferred`/`dropped`
  disposition and be adjudicated without shelling out to the codex CLI — `cw
  review adjudicate` records FIX/REJECT/DEFER decisions per finding, `cw
  review verify-fixes` re-checks `fixed` dispositions against the fix-cycle
  diff and downgrades unsubstantiated claims to `dropped`, and
  `.cw/deferred-findings.md` no longer renders adjudication entries that
  don't match a real finding (and no longer double-counts them in the
  deferred-count filter).

- **`finalize_regress_repeat` companion signal for FINALIZE self-heal
  round-trips with no new commit (#1717):** a FINALIZE self-heal regress
  (#770) that reverts a ticket to `Stage.IMPL` but produces no new commit
  lands back at REVIEW with an unchanged branch head, re-parking the ticket
  with an identical disposition and burning an attempt each cycle with no
  operator-visible signal that this is a *repeat*, not a fresh park (the
  #1644/#1702/#1710 incidents). `src/cw/dispatch/regress_repeat.py` now
  fires `"finalize_regress_repeat"` alongside the ordinary park whenever a
  pass genuinely re-parks the task with the branch head unchanged since the
  regress, and the monitor surfaces it. Also fixes a latent bug in the
  multi-hop stage-walk path where `_advance_task_pointer` cleared
  `task.stage_base_ref` before the walk's REVIEW-rung repeat check could
  read it, meaning the walk-path route could never have detected a genuine
  repeat despite looking identically wired to the directly-tested routing
  functions; the REVIEW-stage guard is also deduplicated so it fires once
  per pass instead of once per call site.

- **`cw doctor` preflight check for missing reviewer agent specs (#1776):**
  new `agent-spec-drift` check runs per configured client, resolving every
  reviewer role in `_REVIEWER_ROLE_AGENT_FILES` the same way #1773's review
  pipeline does (repo-local `.claude/agents/<role>.md` first, then
  `~/.claude/agents/<role>.md` when the repo's `agent_spec_global_fallback`
  gate allows it) and warning when a role has no usable spec — distinguishing
  a blank tracked file from a missing one. Detection only, advisory
  (`ok=True`/`warn=True`, never a hard failure) — surfaces drift before a
  reviewer silently runs unspecified mid-review.

- **OpenCode executor tests, safety, and observability (#1671):**
  `queue_peek` now detects opencode sessions and parses the `.cw/opencode.log`
  JSONL log (backend-aware transcript reader) instead of searching for a
  claude-jsonl transcript — the one rendering surface that is not cleanly
  backend-neutral. New `test_reconcile_opencode.py` covers the live-process
  / dead-process / recycled-PID / cancellation-retains-liveness harvest
  scenarios (no process-tree kill tests — scope removed per #1669 R2).
  Result-door collision tests cover opencode's two write sources
  (EXECUTOR_DIRECT + GIT_SYNTHESIS) racing each other and external writers.
  Lane serialization tests pin `max_parallel=1` for opencode-configured lanes.
  A live smoke test (`test_opencode_contract_live.py`, gated behind
  `INTEGRATION_OPENCODE_LIVE`) and nightly workflow (`nightly-opencode.yml`)
  pin the JSONL event shape against a real `opencode` CLI. Part of #1668.

- **Per-arrival regress marker for the impl-guard staleness gate (#1794):**
  `TicketTask.regressed_into_stage` records a fresh, per-arrival marker
  whenever an operator sends a ticket back to Stage 2 with `requeue
  --regress --stage impl`, so the Pre-Stage Detector Guard in
  `auto-dev-impl.md` no longer short-circuits on HEAD's stale
  impl-complete trailer and silently no-ops the send-back. The new
  `check_impl_guard_staleness.py` deterministic gate script decides
  whether the guard should honor newer tracker comments or an operator
  regress, and a regress-marker verdict is now decoupled from a
  comments-file load failure so a fetch error can no longer mask a real
  regress.

- **Producer-side evidence/line-range window reconciliation (#1792):**
  `_reconcile_evidence_window` repairs a codex-review finding's declared
  line window when it is a few lines short/long of its own evidence's true
  span — first via the exact pre-#1792 gap-tolerant join (byte-for-byte
  compatible with existing #1236/#1715/#1738 behavior), then, only on
  failure, by widening the window within `_LINE_ANCHOR_TOLERANCE` lines and
  requiring an exact (not substring) match so widening can never absorb an
  unrelated adjacent line. Applied both to evidence-quote matching (wide
  `file_window_text` substrate) and to persisted-anchor repair (narrow
  `file_line_text` substrate), reducing false `evidence_not_in_diff`
  rejections without weakening the #1714 false-accept guard.

## [1.32.0] - 2026-08-10

### Added

- **Post-impl scope-conformance gate script (#1779):**
  `.claude/scripts/check_plan_scope_conformance.py` compares the delivered
  diff's file set against the approved plan's `## Files Modified` list and,
  on exceeding a proportionality threshold, emits `status: "blocked"` with
  `blocker.reason: "plan_scope_drift"` and the offending paths enumerated.
  Thresholds are read from a `[tool.cw.scope_conformance]` table in the
  repo's own `pyproject.toml`, fail-safe to module defaults. Deliberately
  ships without any operator-authorized-additions mechanism — see #1786.
  **Known limitation:** the file-list parser recognizes only bullet lists
  under one exact heading, so it is inert on table-formatted plans and fails
  open. Until #1796 lands, a passing result means "no file list could be
  parsed," not "the diff matched the plan."
- **Plan-draft checkpointing during Stage-1 plan generation (#1778):**
  the plan draft is now persisted at checkpoints rather than only on exit,
  so a crashed plan stage no longer loses the entire draft. #1649 persisted
  on exit, and a crash is not an exit.

### Fixed

- **Degraded-reviewer reason no longer dropped before persistence (#1775):**
  `ReviewerFindingsDocument.detail` is now copied onto `ReviewerRunRecord`,
  and the verdict comment renders a `DEGRADED COVERAGE` note naming each
  degraded role, instead of silently persisting an empty reason when a
  reviewer's findings were downgraded.

### Documentation

- **`[tool.cw.codex_review].agent_spec_global_fallback` is now documented
  (#1782):** the key that gates #1773's global agent-spec fallback was
  undiscoverable for the adoption case it exists to serve. Adds a
  `CONFIG_REFERENCE.md` drift-guard test so the documentation cannot silently
  fall out of sync with the code again.

## [1.31.0] - 2026-08-09

### Added

- **`clients.yaml` gains a per-client `quality_gate_commands` field (#1703):**
  the impl/debt worker prompts previously hardcoded `ruff check, mypy, pytest`
  for every client, which was wrong for non-Python clients. When set, the
  configured commands are threaded into `start_session`'s generated prompt in
  place of the hardcoded sentence; clients that don't set it keep the
  existing default wording.
- **`cw event tail` gains `--collapse-repeats` to merge consecutive same-type
  repeats into a summary line (#1754):** consecutive events sharing the same
  type and compact payload collapse into a single `TYPE xN over Mm` line; a
  run broken by an unrelated event re-opens instead of merging across the
  gap. Rejected together with `--follow` (collapsing requires buffering a run
  until it closes, which conflicts with the immediate-flush follow
  contract). Composes with `--dedup-terminal` and `--limit`, applied last in
  the pipeline. `--json` is unaffected — passing `--collapse-repeats` with
  `--json` is a no-op, one JSON line per original event.
- **Reviewer agent specs now fall back to the operator's global copy when the
  repo's own is missing or blank (#1773):** `.claude/agents/<role>.md` is still
  read repo-first, but a worktree that carries no usable copy falls through to
  `~/.claude/agents/<role>.md` instead of silently running the reviewer with an
  empty `## Agent Specification` section. Gateable per-repo via
  `[tool.cw.codex_review].agent_spec_global_fallback` in `pyproject.toml`
  (default enabled). Every outcome — repo, global, or none — is recorded as an
  `AgentSpecStatus` on the review verdict and rendered into the posted review
  comment, so a silently-empty spec is now visible instead of swallowed.

### Fixed

- **`cw lane rm`/`ls`/`add`/`pause`/`resume` now accept `-c`/`--client` (#1607):**
  every other client-scoped subcommand accepts `-c`/`--client` as an
  alternative to a positional `CLIENT`, but the `lane` subcommands only
  accepted `CLIENT` positionally, contradicting the documented `README.md`
  usage. `lane ls` now takes `CLIENT` positionally (optional) or via
  `-c`/`--client` (exactly one required); `add`/`rm`/`pause`/`resume` keep
  the legacy `CLIENT NAME` two-positional form and additionally accept
  `NAME -c CLIENT`.

## [1.30.0] - 2026-08-09

### Added

- **`cw event tail` gains `--limit`/`-n` to bound output to the most recent N
  matching events (#1694):** the flag composes with the existing `--type`/
  `--client`/`--lane`/`--since` filters (filter-then-limit) and is rejected
  together with `--follow`, which streams unboundedly. The default
  (non-`--json`) output format also changed to compact: nested dict/
  list-of-dict payload fields — e.g. `dispatch.tick`'s `lanes` and
  `lane_occupants` — are now omitted, keeping only scalar fields and
  scalar-lists (no length limit — the filter is shape-based, not size-based).
  `--json` output is untouched and remains full-fidelity.

### Fixed

- **`read_events(limit=...)` returned the OLDEST N matching events instead of
  the most recent N (#1694):** the function already accepted a `limit`
  parameter but head-sliced (`events[:limit]`) a list that's in ascending
  chronological order, so `limit=N` silently returned the N *earliest*
  events. No production caller passed `limit=` yet, so this was latent —
  surfaced while wiring up `cw event tail --limit`. Fixed to tail-slice
  (`events[-limit:]`), with `limit=0` special-cased to `[]` (`list[-0:]` is
  a Python trap that returns the whole list, not an empty one).
- **A structural finding anchored on an enclosing def/class is no longer
  dropped as unverifiable (#1743):** `_line_reference_valid`'s existing
  tolerance check only accepts a finding whose cited line sits within a
  small distance of a changed diff line — but a structural finding (e.g. a
  missing type hint, a naming issue) is legitimately anchored on the
  enclosing `def`/`class` line, which is itself rarely a changed line and so
  was silently rejected. When a `worktree` is supplied, validation now falls
  back to `_anchor_in_enclosing_def`, which parses the file's AST and accepts
  the finding if its cited line falls anywhere inside the enclosing
  def/class span (innermost span wins for nested defs). A missing/unreadable
  file or a line with no enclosing definition still returns `False`, so the
  fallback only rescues genuinely structural findings.

- **A converged fix loop is distinguished from a no-op one (#1723):** the loop
  reported convergence whenever no MUST_FIX findings survived, without regard
  for whether any fix cycle had actually changed a file. A run in which every
  cycle's codex fix invocation was a tolerated no-op was therefore
  indistinguishable from one that genuinely resolved the cycle-0 blockers — the
  two render identically while meaning opposite things. `Review` gains
  `had_real_commit`, OR'd across cycles, and the verdict headline now reads
  **UNVERIFIED** for a loop that converged without committing anything rather
  than claiming the findings were resolved. The field defaults to `None` so
  payloads from producers predating it stay explicitly unknown instead of being
  coerced into a false negative; finalized fix-loop results always populate a
  concrete bool.

- **Codex reviewers are grounded in the repo's actual ruff opt-outs and
  complexity thresholds (#1744):** reviewers were raising MUST_FIX findings
  against ruff rules the repo has explicitly ignored, and misreading
  `PLR0915` (too-many-statements) as a line-count metric rather than the
  statement-count metric it actually gates — the exact #1729 failure mode.
  The review prompt now injects a lint-grounding block built from the repo's
  `[tool.ruff.lint]` ignores and pylint-threshold overrides, instructing
  reviewers to downgrade or drop findings based solely on an opted-out rule
  or a misread default, while still treating a concrete security or
  correctness failure as MUST_FIX even when a related rule is ignored.

### Fixed

- **`test_worktree_path_finds_transcript_via_csid` and 5 sibling tests wrote
  into the real `~/.claude/projects/` (#1736):** `claude_project_dir()`
  resolves via `Path.home()` directly, bypassing the `patched_peek` fixture's
  redirection of `CLAUDE_PROJECTS`/`CW_STATE`. `patched_peek` now also
  redirects `HOME`; a new session-scoped `conftest.py` guard fails on any
  leaked `tmp-pytest`/`pytest-of`-named directory under the real path and
  warns (without failing) on any other unexpected new entry, so unrelated
  concurrent Claude Code usage can't flake the suite.

- **A codex review no longer freezes the shared dispatch tick (#1727):**
  `CodexExecutor.spawn()` ran the whole review — per-role `codex exec`
  subprocesses plus the bounded fix loop, up to the full REVIEW budget —
  inside `dispatch_tick`'s own call stack, so one client's review stalled
  spawns for every other client and lane. Pre-flight (session creation and
  the REVIEW-stage/binary-presence checks) still runs on the caller's thread,
  but once it passes the review is handed to a `cw.codex_background` daemon
  thread and `spawn()` returns the session id immediately. The session's
  `session_id` is stamped onto the still-RUNNING dev-queue row *before* the
  handoff, so a crash in the window before dispatch's own post-spawn stamp
  cannot leave a live codex session with no row attributing it. Backgrounding
  costs the two recovery paths a synchronous caller, so both are replaced:
  `run_dispatch_loop`'s shutdown path bounded-joins outstanding review threads
  against one shared deadline and reports the still-running count on
  `DISPATCH_LOOP_EXITED`, and a boot pass parks any codex session left ACTIVE
  by a crash or SIGKILL, which no join can reach. The boot pass keys its
  dev-queue lookup on `(ticket_id, client)`, matching
  `_park_running_task_blocked_on_user`: ticket numbering is per-client, so a
  ticket 21 in two clients would otherwise collide. It additionally requires
  the matched row's recorded `session_id` to *be* the orphaned session, so a
  zombie ACTIVE record from an earlier crashed boot cannot park the healthy
  review that has since claimed the same ticket.

- **Gate parks that do populate breadcrumbs are no longer filtered out of the
  attention stream (#1729):** `BREADCRUMB_ELIGIBLE_PAUSED_STATUSES` omitted
  `codex_must_fix_mechanically_rejected`, the park disposition #1714 introduced.
  That park is the one gate-class park whose breadcrumbs are genuinely populated
  from `blocker.reason` rather than a hardcoded `""` literal, so excluding it
  meant an operator-review park emitted an empty breadcrumb — the single case
  where the operator most needs to know *why* the branch stopped. The constant
  now includes it, and the surrounding comment records the property that makes
  the rest of the exclusions correct rather than accidental: membership here
  does not by itself cause a breadcrumb to be emitted, since the producing
  `_park_*` helper must independently stamp non-empty content at its own call
  site. Every other gate-class park hardcodes `breadcrumbs=""`, so adding it
  here would be cosmetic. The composition test is pinned against the imported
  constants instead of hand-written string literals, closing the transcription
  gap that produced this bug.

- **A valid, fully-verbatim evidence quote spanning hunk context lines was
  wrongly rejected as `evidence_not_in_diff` (#1738):** the real #1729
  diagnostics artifact still on disk showed a `SysAdmin Reviewer` SHOULD_FIX
  finding on `tests/test_dispatch.py` claiming lines 9522-9527 rejected, even
  though its 6-line evidence quote is a genuine, byte-exact copy of the
  post-change source at those exact lines — a fourth mechanical rejection
  mode distinct from #1715's near-line-anchor/marker-normalization fixes and
  from the ticket's own (unverifiable, likely fabricated) "no quotable
  evidence span" hypothesis. Two compounding bugs: `_parse_unified_diff`
  only ever recorded content for `+`-prefixed added lines, so the 5 of 6
  claimed lines that were unchanged context had no entry at all in the
  line-content map; and `_resolve_line_window` snapped *every* claimed
  endpoint onto the nearest *added* line even when the endpoint was already
  exactly correct, silently shrinking `(9522, 9527)` down to `(9521, 9526)`
  and discarding the tail of the quote. `CapturedDiff` gains a second map,
  `file_window_text`, alongside (not replacing) `file_line_text` — it also
  captures context-line content at its real new-file line number (a removed
  line still has no new-file position and stays excluded). A new sibling
  resolver pair, `_nearest_hunk_line`/`_resolve_hunk_window`, mirrors the
  existing `_nearest_added_line`/`_resolve_line_window` but draws candidates
  from `file_window_text`; `_evidence_in_claimed_lines`'s windowed branch
  routes through the new pair, while `_line_reference_valid`'s
  anchor-*validity* gate and `_resolved_finding`'s persisted-anchor snap both
  keep calling the original added-line-only functions, unchanged — an
  accepted finding's `line_start`/`line_end` still snap onto the nearest
  genuine added line (`9521`/`9526` for this fixture), not the reviewer's raw
  claimed endpoints, so downstream consumers keep pointing at real changed
  source. In a representative before/after aggregate check combining this
  fixture, its negative control, and the two existing #1715 fixtures, 1 of 4
  findings that was incorrectly rejected pre-fix is now correctly retained
  post-fix (retained/raw: 2/4 → 3/4).

  Mutation-proof seam check (#1628 convention): with the fix applied,
  `test_hunk_context_window_evidence_retained` passes —

  ```
  tests/test_review_findings.py::TestValidateReviewerDocument::test_hunk_context_window_evidence_retained PASSED [100%]
  1 passed in 0.12s
  ```

  Reverting the full production side of this fix — `CapturedDiff.file_window_text`,
  `_nearest_hunk_line`/`_resolve_hunk_window`, `_evidence_in_claimed_lines`'s
  rewiring onto them, and the `_parse_unified_diff` 3-tuple→4-tuple signature
  change and its call sites (test file untouched) — reproduces the original
  rejection —

  ```
  E       ValueError: not enough values to unpack (expected 4, got 3)
  tests/test_review_findings.py::TestValidateReviewerDocument::test_hunk_context_window_evidence_retained FAILED [100%]
  1 failed in 0.22s
  ```

  The failure surfaces at the `_parse_unified_diff` unpack (the test's own
  fixture builder calls it directly and always unpacks 4 values) rather than
  at the evidence-containment assertion the fix is actually about — expected,
  since the signature change is part of the same seam being proven, not a
  separate one.

- **The fix loop only persisted cycle 0's review-verdict snapshot, not the
  cycle that actually produced the park (#1739):** `_persist_cycle0_snapshot`
  generalizes to `_persist_cycle_snapshot`, called once per fix cycle (0
  through the terminal cycle) instead of only before the loop starts. Every
  exit path now threads the latest cycle's pointer, so whichever cycle's
  `ReviewVerdict` actually produced a park (e.g.
  `codex_must_fix_mechanically_rejected` on a later cycle, as in #1729) is
  the one an operator finds referenced in `friction_highlights`, instead of a
  stale cycle-0 snapshot that may not even contain the offending finding.

## [1.29.0] - 2026-08-08

### Fixed

- **A mechanically-rejected MUST_FIX no longer reads as a clean review
  (#1714):** `consolidate_verdict` computed `blocking` from accepted findings
  only, so a MUST_FIX rejected for a mechanical reason (bad line anchor,
  evidence not found in the diff, unknown file) landed in `rejected`, never
  reached `must_fix`, and the codex path fell through to `stage_complete`. The
  posted comment then read "Non-blocking — no MUST_FIX findings", and
  `_render_findings` iterates only `accepted`, so the discarded findings
  appeared nowhere at all. In a 9-pass fleet sample, 4 of 4 MUST_FIX findings
  were rejected this way and the review reported clean. `ReviewVerdict` gains
  `rejected_must_fix` — the MUST_FIX-severity subset of `rejected`, selected
  by severity rather than by rejection reason, so any future
  `RejectedFindingReason` is covered by construction — and the codex path now
  exits `blocked` with its own reason and its own park disposition rather than
  reporting success. Deliberately a *second* signal rather than a widening of
  `blocking`: the fix loop gates on `verdict.blocking`, and a finding whose
  anchor could not be located is precisely what must never be handed to an
  autofix loop. The Claude-native coordinator's instruction to discard
  `.rejected` from adjudication is narrowed to non-MUST_FIX severities, since
  the same `consolidate_verdict` serves both backends.

- **The #1709 capability probe reported every host incapable, and cached it
  (#1732):** the probe deliberately runs in its own scratch dir under
  `state_dir()` rather than the review worktree — so it is never inside a git
  repo, and `codex exec` refuses to start outside one ("Not inside a trusted
  directory and `--skip-git-repo-check` was not specified", exit 1) before any
  sandbox work happens. `_is_probe_error` classified that as neither a timeout
  nor a spawn error, so it fell through to `_classify_capability_failure`,
  matched no known marker, and returned a determinate `unknown` — which was
  then written to a cache that has no TTL. Every codex review would have been
  marked degraded, and #1702 hard-parks the REVIEW stage on degraded health:
  the exact failure #1709 was filed to eliminate, reproduced through a new
  mechanism. The probe now passes `--skip-git-repo-check`, and
  `_is_probe_error` additionally treats "codex exited non-zero having written
  nothing to stdout" as a probe error — degrading this one run, logged, and
  never cached — per R7's requirement that a transient failure must not become
  silently permanent. Keyed on empty stdout rather than on any particular
  message, so a new refusal reason inherits the safe behavior; a genuinely
  incapable sandbox still *replies*, so it is still classified and cached as a
  real verdict. Operators who ran a codex review between #1709 and this fix
  must clear the poisoned cache at
  `~/.local/share/cw/codex-review/capability-cache.json`. Found by running the
  shipped probe end-to-end against merged main while the unit suite was fully
  green — every probe test used a fake runner, so nothing exercised a real
  `codex exec` invocation.

### Added

- **Codex filesystem capability is now probed, not assumed (#1709):** the
  codex-review path used to tell every reviewer "do not rely on filesystem
  access" as a hardcoded constant. That was true of exactly one runtime. The
  discriminator turns out to be the *install method*, not the OS: a
  snap-confined codex gets its own PATH/mount namespace, cannot see the host's
  `bwrap`, and fails closed — while a non-snap install on the same machine
  reads the worktree fine. A host-side `which bwrap` check answers CAPABLE for
  the snap install and is wrong, so `cw` now asks codex itself, via a real
  `codex exec --sandbox read-only` read of a sentinel file whose value never
  appears in the prompt.

  The verdict is cached on disk at
  `~/.local/share/cw/codex-review/capability-cache.json`, keyed by a runtime
  fingerprint (cli version, platform, install type, sandbox mode) and with **no
  TTL** — the fact only changes when the codex install does, so re-probing on a
  timer would spend a model round-trip per dispatch tick to re-learn it. Delete
  the file to force a re-probe. A probe that never *completed* (timeout, spawn
  failure) degrades that one run and is deliberately not cached, so a transient
  failure cannot become silently permanent.

  Reviewers on a capable runtime now get a prompt variant that permits reading
  beyond the inlined diff (consumer search, prior-art search, repo-wide
  regression verification); write access is neither offered nor possible, and
  the schema/degraded/escalation rules are shared verbatim between the two
  variants so capability can never quietly alter the output contract. Failures
  are classified — `sandbox_incapable`, `install_incomplete`, `unknown` — rather
  than collapsed into one boolean, because a broken install and a confined
  sandbox produce the same "cannot read" answer and want opposite remedies. The
  selected mode is recorded on `ReviewVerdict.capability_mode` /
  `capability_reason` and in a per-session `codex-capability.json` diagnostics
  artifact. Rendering it in the verdict comment is deferred to #1725.

- **The probed capability mode now renders in the verdict comment (#1725):**
  closes the loop #1709 opened — `render_verdict_comment` gains
  `_render_capability_note`, which prints a one-line `capable`/`degraded`
  (with reason) annotation when `ReviewVerdict.capability_mode` is set, and
  renders nothing for a verdict that never probed (`capability_mode is
  None`), so an unprobed run is never mistaken for a probed-and-unknown one.

- **OpenCode executor backend foundation (#1669):** `opencode` is now a
  first-class executor backend, selectable via `backend: opencode` in
  `StageExecutorConfig`. Spawn is fire-and-forget (mirroring `LocalExecutor`):
  `opencode run --format json --pure --dir <worktree>` is launched as a
  detached subprocess, PID + start-time are captured on `LocalLivenessHandle`,
  and the session is left ACTIVE until `reconcile/local` harvest detects the
  dead process and parses the JSONL log for the `<<<AUTO_DEV_RESULT>>>`
  sentinel. No `--output-schema` (opencode has none); the sentinel pattern is
  used instead, like the `claude-native` and `local` backends. `--pure` is the
  mechanical permission profile (built-in tools only, never `--auto`).
  Cancellation does NOT kill the process tree — cw stops tracking + parks the
  task; the orchestrator session kills strays. Stage-specific adapters
  (finalize/plan/impl) are follow-on tickets (#1670, #1671). Part of #1668.

- **OpenCode executor finalize adapter (#1670):** `OpencodeExecutor` is now
  FINALIZE-only: `spawn()` returns a typed `BLOCKED`
  (`reason=opencode_<stage>_not_implemented`) for any non-FINALIZE stage,
  mirroring `CodexExecutor`'s REVIEW-only pattern. For FINALIZE, the adapter
  materializes a prompt that instructs opencode to read and follow the
  existing `auto-dev-finalize.md` skill (no new skill file) and emit the
  `<<<AUTO_DEV_RESULT>>>` sentinel with the correct `stage_reached` marker
  (`stage4a_merge_gate`, `stage4b_pr_create`, or `stage5_post_create`). The
  plan-fetch pre-flight is removed for FINALIZE (the finalize flow reads
  `.cw/context.json`, not `.cw/plan.md`). Part of #1668.

- **Per-role codex reviewer metrics, and one-shot reviewer runs are now
  ephemeral (#1710):** reviewer invocations pass `--json` and `--ephemeral`,
  and the JSONL audit stream is consumed into per-role metrics on the existing
  `ReviewerRunRecord` — token usage, tool-call counts, and an
  `unexpected_tool_attempts` list recording any MCP, web, or write attempt made
  during a nominally read-only review. `duration_seconds` is now recorded on
  the success path as well as the failure path, where it was previously
  computed and discarded. `--ephemeral` suppresses Codex's own rollout store,
  which cw never read; cw's own diagnostics are unaffected.

  Two safety properties are deliberate and tested. Telemetry can never
  influence a review's outcome — metrics reach `ReviewerRunRecord` only, never
  `ReviewerFindingsDocument.status`, so a malformed or truncated audit stream
  degrades to "no metrics" rather than to a degraded verdict. This matters
  because #1702 now parks the REVIEW stage on degraded health. And a codex
  build that rejects the new flags is detected and retried once without them,
  so a runtime lacking `--json`/`--ephemeral` records no metrics instead of
  failing every reviewer invocation.

### Fixed

- **Unanchored reviewer findings now reach adjudication instead of being
  silently discarded (#1632):** a finding whose `file` wasn't a key of the
  diff's changed-file set was rejected outright as `unknown_file` before it
  ever reached adjudication, even when the file genuinely exists in the repo
  tree (e.g. a reviewer citing a doc or config file the diff didn't touch).
  `consolidate_verdict`/`validate_reviewer_document` now accept an optional
  `worktree`; when a finding's file resolves to a real path under it, the
  finding is classified `"unanchored"` and routed into `accepted` rather than
  `rejected` — tree-existence proves the path is real, not the evidence
  quote, so an unanchored finding's escalation still goes through the
  ordinary diff-based check. `cw review consolidate` gains `--worktree`
  (defaults to the current directory) and a `--no-tree-evidence` flag to
  disable the relaxation entirely. Omitting `worktree` everywhere keeps
  today's `unknown_file` behavior byte-identical.

- **Degraded review health now actually gates the stage (#1702):** a REVIEW
  result could carry `health.recommendation="EXIT_FOR_HUMAN_REVIEW"` — meaning
  the review itself was incomplete — and still advance to FINALIZE, because
  dispatch routed on `status="stage_complete"` alone and never consulted
  health. In one nine-pass sample 8 of 9 passes derived
  `EXIT_FOR_HUMAN_REVIEW` and all 9 were eligible to advance; whether a lane
  stopped depended on it happening to carry an unrelated signoff rule, so
  safety rested on configuration coincidence. A REVIEW-stage advance now parks
  `BLOCKED_ON_USER` with the distinct `review_health_gate` disposition, which
  is batch-releasable via `dev-queue drain` (re-running review clears it,
  unlike a deliberate force-hold) and escalation-eligible so it pages rather
  than sits. `recommendation="PROCEED"` routing is byte-identical to before,
  and a missing or malformed `health` payload degrades to today's behavior
  rather than to a park. Deliberately scoped to REVIEW: the local/aider IMPL
  path hardcodes `EXIT_FOR_HUMAN_REVIEW` as a pessimistic default (#1580)
  rather than deriving it, so gating IMPL would have permanently stalled
  unattended `IMPL → REVIEW` auto-advance on LOCAL-backend clients.

- **The posted review comment could not distinguish "found nothing" from
  "found and fixed" (#1705):** `render_verdict_comment` only inspected
  `verdict.blocking`/`must_fix`/`accepted`, never `verdict.review` — so a
  fix-loop cycle that converged on a real MUST_FIX finding and a genuinely
  clean first pass both rendered the same "Non-blocking — no MUST_FIX
  findings" string. Root cause was two bugs: `_clean_exit` and
  `_park_scope_violation` (`codex_fix_loop.py`) reconstructed the correct
  cross-cycle `Review` and patched it onto the returned `AutoDevResult`, but
  not onto the returned `ReviewVerdict` — the object `render_verdict_comment`
  actually reads — so the posted comment kept reading the terminal
  re-review's own `fix_cycles_used=0`. Both now stamp the finalized `Review`
  onto the verdict they return, mirroring `_survivors_only_verdict`'s
  existing pattern. Separately, `Review.fix_cycles_used == 0` is produced
  identically by a fix-loop-disabled single pass and a fix-loop-enabled pass
  whose cycle-0 review was already clean — no `Review`-only signal can tell
  these apart, so `render_verdict_comment` (and
  `synthesize_codex_review_result`/`run_review`, which now forward it) takes
  a new required `fix_loop_enabled: bool` parameter to render each history as
  its own state rather than lumping fix-loop-off in with the others. The
  comment also now surfaces a "PARTIAL COVERAGE" note when a reviewer role
  failed to run, reusing `verdict.agents_run` (#1710) — previously that
  signal only reached `Blocker.details` on the zero-documents path, never the
  posted GitHub comment.
- **Reviewer findings with a near-line anchor or a stray diff-marker in
  evidence were wrongly rejected (#1715):** two false-positive rejections on
  top of #1632's diff-anchoring. First, `invalid_line_reference` used exact
  line-number membership, so a finding whose anchor was off by even one line
  from the real added line was rejected even with correct evidence text —
  fleet evidence shows reviewer anchors commonly drift by one to three lines
  (stale line numbers, off-by-one miscounts). Second, `evidence_not_in_diff`
  compared evidence against raw per-file hunk text that still carries
  `+`/`-`/context markers on every line, so a reviewer's genuine multiline
  quote (no markers) couldn't match past the first line, and the windowed
  path had the same exposure in reverse when the reviewer's own quote
  carried diff-style markers (plausible if copied from a rendered diff
  view). Line anchors now resolve via a fixed `±3` line tolerance
  (`_nearest_added_line`: exact match first, else nearest candidate within
  bound), and every evidence-vs-diff substring comparison is routed through
  a marker/whitespace normalization (`_normalize_diff_text`) on both sides.
  The bound is a fixed module constant, not derived from hunk/file size, and
  stays enforced: an anchor farther than 3 lines away, a file not in the
  diff, or evidence that still isn't a genuine substring after normalization
  are all still rejected. In a representative before/after aggregate check,
  2 of 3 findings that were incorrectly rejected pre-fix are now correctly
  retained post-fix (retained/raw: 0/3 → 2/3). An accepted finding's
  `line_start`/`line_end` are also snapped onto the resolved anchor before
  the finding is returned, not left at the reviewer's raw off-by-up-to-3
  claim — otherwise the verdict comment and fix-loop prompt would keep
  pointing at the wrong line even after this fix retains the finding.

## [1.28.0] - 2026-08-07

### Fixed

- **`requeue --stage impl` could bypass to an unrunnable impl session without
  a plan (#1681):** requeuing a task at `plan`/`harden` directly to
  `Stage.IMPL` previously advanced the stage pointer unconditionally, even
  when no `.cw/plan.md` was ever written for the bypassed worktree — the
  common case right after a fresh dispatch tick, since dispatch stamps
  `session_id` but never `worktree_path` on `TicketTask`. The requeue guard
  now checks local-first (via the same read-only primitives
  `create_worktree` uses to decide reuse-vs-rebuild), falling back to the
  tracker's approved-plan comment only when the local read can't prove the
  plan is there, and refuses the bypass with a clear error naming the
  missing plan path when neither source proves sign-off. The `review` and
  `finalize` bypass targets are intentionally left unguarded — both degrade
  gracefully on a missing plan per their own command specs.

- **Earlier-stage terminal sentinel wrongly discarded (#1676):** a headless
  auto-dev session that reports a terminal sentinel from an earlier stage
  than the one it was dispatched at (e.g. a `plan_pending_approval` or
  `review_pending_approval` report surfacing while the task record still
  shows a later stage) is no longer misread by dispatch routing as if the
  work had completed at the later stage. A new `_is_earlier_stage_report`
  predicate now gates Rule 5a's FINALIZE self-heal regress and Rule 1's
  small-tier auto-advance, both of which previously performed an additional
  `task.stage` mutation on the assumption that any sentinel reflected work
  done at the task's current stage — that assumption broke for
  non-advance-claim, earlier-stage reports, silently regressing or
  over-advancing `task.stage`. Both rules now park the task
  `BLOCKED_ON_USER` instead when an earlier-stage report is detected.

- **`cw queue peek` false STOP verdicts from attempt-count churn and stale
  reused-worktree transcripts (#1678):** `_score_session`'s attempts-based STOP
  branch no longer fires on fleet-wide usage-limit outage churn — a high
  attempt counter deliberately does not decrement on a usage-limit death
  (#786), so `parse_transcript` now scans assistant text for the documented
  usage-limit phrasing and `recommend()` downgrades the STOP to a PEEK when
  present. Separately, `format_row` now anchors `age_min`/`idle_min` on
  `Session.started_at` (claim time) instead of a transcript that a reused
  worktree may have inherited from a prior dead session, and nulls
  `idle_min` when it would be logically inconsistent with that anchor.

### Added

- **`HookContextConflictError` carries the conflicting session id, closing
  the phantom-locked-worktree recovery loop (#1674):** a requeue attempt
  that discovers an active session already holds the target worktree now
  records that session's id on the task, and concierge recipe 1 refuses to
  requeue when it cannot spawn into a conflicting context — instead of
  retrying indefinitely and burning the attempt counter. A shared
  terminal-session-status constant also stops an unrelated revert from
  wiping the recorded hook-context-conflict evidence.

## [1.27.0] - 2026-08-07

### Added

- **`cw focus` and `cw statusline render` (#1644):** a session can now say what
  it is working on, and the statusline can show it. `cw focus set
  <client>[/<lane>]` records a pointer keyed by `$CLAUDE_CODE_SESSION_ID` in
  `~/.local/share/cw/focus.json` (own file lock, validated against
  `clients.yaml`, cleared only by `cw focus clear` — no TTL, no pruning), with
  `cw focus show` to read it back. `cw statusline render` turns that into one
  terse segment — `client/lane 2▶ 1⧗ !1`, or `client/lane PAUSED 0▶ 1⧗` for a
  circuit-paused lane — via a three-step ladder: the session's focus, else the
  client whose workspace/worktree tree contains the cwd (aggregated across its
  lanes), else nothing at all. `render` is machine-invoked on every assistant
  message, so it reads only local files (no `gh`, `git`, network, or
  subprocess) and always exits 0: a missing, malformed, or config-drifted
  `focus.json` degrades to the next rung instead of failing. The `!N` count
  reuses `dev-queue status`'s existing `NEEDS_ATTN` predicate — relocated to
  `cw.dev_queue.attention.task_attention_state` so both surfaces share one
  definition — and therefore reflects last-hydrated PR state.

- **Consolidated park — Stage 1 finishes all plan-phase analysis before any
  human-gated exit (#1650):** the plan stage's human gates fired serially
  (ambiguity/premise parks before the plan-quality stations ever ran; the
  large-scope approval exit posted the plan without station findings), so a
  single ticket paid 2–4 operator rounds — one observed ticket bounced
  between `ambiguities_pending_resolution` and `plan_pending_approval` five
  times before being cancelled. When Stage 1 must exit headless with a
  draft in hand it now runs scope classification and both Step 1f stations
  in advisory mode (findings collected only — no markers, no revision
  cycle, a MUST_FIX never converts the park to `blocked`), posts ONE
  `## Pending Verification Scan` comment (parked items, advisory findings,
  `### Approval requested` when Large, and the full draft), and persists
  the draft. Exit statuses and result-payload rules are unchanged; a
  `consolidated park: …` line lands in `friction_highlights`. Step 1a
  explicitly never treats the parked comment's embedded draft as an
  existing plan, and a resumed Large draft still requires approval
  evidence before the approval gate auto-skips.

- **`Verified: DEFER` for runtime-only premises (#1651):** a premise
  checkable only against a live system used to park the run
  (`premises_pending_verification`) even though the human at a desk cannot
  check it statically either — one observed ticket burned six plan
  dispatches re-litigating a single runtime-only premise until the operator
  hand-wrote the "confirm during implementation, halt on mismatch" framing.
  That framing is now a first-class contract: the Product Manager Reviewer
  may tag a premise `DEFER` when it is (a) runtime-only, (b) checkable by a
  cheap bounded probe at implementation start, and (c) safe if false
  (halt-and-report, never silent fallback), carrying mandatory
  `In-implementation check:` / `On mismatch:` fields — either missing
  fails closed to `NO`. Stage 1 partitions premises three ways
  (`self_verified` / `deferred` / `unverified`); `deferred` never parks,
  lands in a `## Deferred Premises` plan section plus a leading
  implementation-phase step that runs the checks before dependent work,
  and leaves one `friction_highlights` audit line per premise. Exit
  gating keys on `parked` + `unverified` only.

- **Stage 1 persists `.cw/plan-draft.md` on every human-gated headless exit
  (#1649):** draft persistence previously fired only on the rare Step 1f.3
  `plan_unreviewable`/`plan_unsound` exits, so the dominant park exits
  (`ambiguities_pending_resolution`, `premises_pending_verification`,
  `plan_pending_approval` — 130 firings in a 3-week window) threw the
  generated plan away and every re-dispatch regenerated from scratch,
  making fresh interpretive choices that surfaced fresh ambiguities and
  cost fresh operator rounds. `auto-dev-plan.md` now defines a single
  draft-persistence rule covering all Stage-1 human-gated headless exits
  with a plan in hand; the existing Step 1a.0 resume check, supersession
  guard, and `no_op`/Step-1g cleanup paths consume it unchanged.
- **`TicketTask.salvage_no_sentinel_at` marks the LOW-path salvage park
  (#1638):** dev-queue schema bumps to v25, adding a durable timestamp field
  stamped by `transition_task_status` when a task transitions to
  `BLOCKED_ON_USER` with disposition `needs_salvage` — the LOW-path salvage
  outcome (`salvage.py`'s single call site, funneled through
  `_notify_needs_salvage`). `task.stage`/`stage_high_water` are deliberately
  left untouched so `unblock_ticket` respawns the row at the stage that
  actually stalled, instead of restarting from scratch. Migration backfills
  the field as the 25th per-task filler.

- **Recurring attention signal for starved circuit-paused lanes (#1630):**
  a lane paused by the circuit breaker while a task still waits in it no
  longer sits silently — `dev-queue status` now surfaces the paused-with-
  pending lane, and dispatch emits a recurring `SESSION_NEEDS_ATTENTION`
  (not just a one-shot) for as long as the lane stays starved, so an
  operator watching the event stream doesn't miss it after the first
  notification scrolls by. Lane resume clears the notify debounce stamp,
  and each firing timestamps its `session_id` so `dedup-terminal` treats
  every recurrence as distinct instead of collapsing them into one.

- **`stalled_retry_cap_parked` payloads carry correction-signal fields
  (#1625):** `ReapCandidate` gains `regress_attempts`/`spawn_error_count`
  fields (mirroring the existing `attempts` field), stamped at the
  stalled-retry-cap park emit site. Both `SESSION_NEEDS_ATTENTION` and
  `SESSION_REAP_PROPOSED` payloads now carry `crashed`/`regress_attempts`/
  `spawn_error_count` when the disposition is a stalled-retry-cap park —
  scoped strictly to that reason via a shared `_apply_correction_signal_fields`
  helper in `cw.reconcile._shared`, so a consumer no longer has to
  cross-reference the two events by hand to see retry/regression state.

- **`_stamp_salvage_stage` forces `stage=FINALIZE` at reconcile's four
  salvage backstops (#1629):** `tasks.py`'s timed-out-merged completion, and
  the merged branches of `idle`/`phantom`/`stalled`'s reconcile mutations
  each stamped a terminal `disposition="shipped"` while leaving `task.stage`
  advertising a stage the ticket never finished. Each now calls the new
  `_stamp_salvage_stage` helper (`cw.dev_queue.lifecycle`, re-exported from
  `cw.dev_queue`) to force `stage` to `FINALIZE`, emitting
  `TASK_STAGE_CHANGED` via the existing `_emit_stage_change` chokepoint.
  `stage_high_water` is deliberately left untouched, so a completed row
  where `stage_high_water != stage` now identifies a salvaged completion.

- **Anchor breadcrumb-eligible paused statuses, the sentinel health-field
  check, and the operator-event doc example against silent drift (#1597):**
  a new `BREADCRUMB_ELIGIBLE_PAUSED_STATUSES` constant in
  `cw.dispatch.routing` (composed from `STAGE_FAILURE_STATUSES` minus
  `scope_exceeded`/`forbidden_area`, which never carry a blocker per the
  #777 exception, plus the `_AWAITING_OPERATOR_REASON` substitute) anchors
  `attention_monitor.sh`'s hand-transcribed `_BLOCKER_REASON_PAUSED_STATUSES`
  set (outside `src/cw`, so it can't import the constant) to a
  `routing.py:131` file:line comment; `cw-validate-result`'s
  `validate_sentinel.py` now derives its `health_present` check from an
  `_EXPECTED_HEALTH_FIELDS` allowlist guarded at import time against
  `cw.auto_dev_result.Health.model_fields` instead of an inline
  string-literal set; and a new drift-guard test asserts
  `CONFIG_REFERENCE.md`'s documented `operator_channel_forward.event_types`
  example matches `_DEFAULT_OPERATOR_EVENT_TYPES` exactly. No behavior
  change in any of the three — doc and code already agreed; only the guards
  are new.

### Changed

- **harden-ticket repositioned as targeted pre-flight (#1655):**
  `/harden-ticket` front-loaded an operator round for every non-trivial
  ticket to compensate for the pipeline surfacing findings one exit class
  at a time — duplicating the sweep the plan stage runs anyway, missing
  plan-drafting-precision findings, and rotting within hours when a
  dependency PR merged. With consolidated park (#1650) and draft
  persistence (#1649) landed, the default flips to dispatch-first: round 1
  of the pipeline IS the hardening sweep, grounded in dispatch-time code
  with the draft attached. The skill keeps four mandatory cases —
  multi-task plan docs with verbatim code blocks (the pipeline cannot
  sweep the plan doc itself), public-contract tickets, tickets already
  bouncing (reactive trigger unchanged), and zero-interrupt batch waves.
  `orchestrate-sprint` Phase 2 gets the same default flip, keeping its
  read-fresh-plan-comments rule.

- **Headless re-dispatch enters via stage detection — implicit `--resume`
  (#1652):** the durable-signal stage detector was informational without an
  explicit `--resume`, so a headless queue re-dispatch always re-entered at
  Stage 1 — observed re-verifying a branch whose implementation had already
  shipped and re-posting an already-open question. Headless invocations now
  always run `detect_current_stage()` first and, when the durable signals
  are unambiguous and internally consistent, enter at the latest detected
  stage exactly as explicit `--resume` does (emitting `resumed_from_stage`).
  Signal conflict or ambiguity (branch without plan markers, commits
  without trailers) falls back to Stage 1; stage-entry gates and per-stage
  detector guards are unchanged; interactive mode without `--resume` keeps
  the detector informational.

- **Multi-marker gate resolves newest-wins instead of hard-EXITing
  (#1654):** when Step 1b's pre-flight-resolutions extraction found more
  than one marker-bearing comment it EXITed `ambiguities_pending_resolution`
  and demanded a manual `/harden-ticket` consolidation — a full operator
  round (~13h mean park latency) spent on a mechanical slip, and a contract
  mismatch with `harden-ticket/SKILL.md`, which already declares the newest
  superseding comment the single source of truth. The pipeline now uses the
  marker-bearing comment with the latest created timestamp, appends a
  `multi-marker` `friction_highlights` warning, and proceeds; body-over-
  comment precedence and the #967 `## Multi-Marker Gate Blocked` tally
  exclusion are unchanged, and harden-ticket's marker-stripping guidance
  relaxes to recommended hygiene.

### Fixed

- **Human-gated parks are no longer mechanically re-dispatched or
  duplicated (#1653):** a ticket parked on a human gate
  (`ambiguities_pending_resolution` / `premises_pending_verification` /
  `plan_pending_approval` / `review_pending_approval`) could be fed back
  into dispatch with zero new information — observed as 10 mechanical
  retries on a fixed ~2h39m cadence (~20.5h) that never left the plan
  stage. Two loopholes closed: `add_ticket` now refuses to insert when a
  `BLOCKED_ON_USER` / `AWAITING_OPERATOR_SIGNOFF` row already owns the
  ticket (previously a silent sibling row was minted, later surfacing as
  `terminal_sibling` reconcile noise; the CLI now points at
  `requeue`/`approve`), and `cw doctor --reap`'s class-5 wedge path
  (dead-session `BLOCKED_ON_USER` collapse — the one revert path with no
  disposition gate) now excludes human-gated dispositions at both the
  detector and the shared `_collapse_blocked_on_user_tasks` chokepoint,
  sourced from `PAUSED_FOR_USER_INPUT_STATUSES` so the sets cannot drift.
  `orchestrate-sprint` codifies the operator rule: a human-gated park is
  retry-eligible only after a tracker-state delta (new comment, body edit,
  or approval reply).

- **`approve` can release a `scope_hint`-gated park (#1640):** the approval
  gate only ever checked `session.last_result.status` against
  `SCOPE_GATED_APPROVAL_STATUSES`, so a ticket parked by the `scope_hint`
  escalation gate (`task.disposition == "approval_gate"`, with no matching
  `last_result` status) could never be released via `approve` — the operator
  had to fall back to `requeue` instead. `approve` now also checks
  `task.disposition`, extracted into a new `_not_at_approval_gate` helper,
  and either release condition is sufficient. The gate's error message now
  reports both `disposition` and `last_result` status so a future mismatch
  is easier to diagnose.

- **`complete_timed_out_merged_tasks` no longer over-trusts a
  spawn-error-only claim history (#1623):** `_is_never_claimed` narrows from
  `attempts == 0 and session_id is None` to `session_id is None and
  attempts == task.spawn_error_count` — this reconcile backstop previously
  matched only the exact #1387 shape, so a task where every attempt died on
  the generic spawn-error path (`attempts == spawn_error_count`, observed
  twice in the wild) could be marked `shipped` by borrowing an unrelated
  session's merged PR despite never having attached to a worker at all. The
  usage-limit-only gap (`attempts > spawn_error_count`, never attached) is a
  known accepted gap tracked in #1631, not closed here.

- **`scope_hint` escalation now binds at all three routing park sites, not
  just one (#1617):** only Rule 1 (`_route_scope_gated_approval`) honored a
  `scope_hint="large"` ticket's REVIEW-boundary park; Rule 3
  (`_route_stage_success`) and the stage-walk's REVIEW rung never consulted
  `task.scope_hint`/`_resolve_scope_tier`, so a worker that skipped the
  `review_pending_approval` sentinel (a Checkpoint 3a auto-continue, or a
  literal `stage_complete`) could sail straight to `FINALIZE` unattended. A
  shared `_should_gate_for_scope_hint` predicate and `_park_scope_hint_gate`
  helper now wire the escalation into both bypass sites, checked ahead of
  the finalize-hold and signoff gates. A new
  `dispatch.scope_routing_decision` audit event (diagnostic trail, not an
  operator alert — excluded from `_DEFAULT_OPERATOR_EVENT_TYPES`) records
  the sentinel tier, `scope_hint`, resolved tier, which rule fired, and the
  resulting disposition at all three `routing.py` park-decision sites plus
  the `approve` gate-release site.

- **BREAKING — `tasks --json` emits the full `TicketTask` field set
  (#1618):** `_task_to_dict` hand-listed 16 of `TicketTask`'s 43 fields,
  silently dropping `scope_hint`, `computed_scope_tier`, `stage`,
  `hold_finalize`, and 23 others from `cw dev-queue tasks --json`. It now
  calls `task.model_dump(mode="json")` directly. `created_at`'s JSON shape
  moves from a `+00:00` UTC offset suffix to a `Z` suffix under this dump
  mode; `status` and `worktree_path` shapes are unchanged. No in-repo
  consumer parses the suffix (`tests/test_dev_queue.py`,
  `.claude/skills/cw-fanout`, `src/cw/data/GUIDE.md`, and `CHANGELOG.md`
  itself all checked clean) — the population actually at risk is an
  external `jq`/script consumer of `cw dev-queue tasks --json`. The human
  table also gains `SCOPE_HINT`/`COMPUTED_SCOPE_TIER`/`STAGE` columns.

- **`review_recipes/_shared.py`'s four attention-state constants are now
  type-annotated (#1613):** `_ATTENTION_CHANGES_REQUESTED`,
  `_ATTENTION_CI_FAILING`, `_ATTENTION_NO_REVIEWER`, and
  `_ATTENTION_MERGE_BLOCKED` were bare, unannotated `str` values with no
  type link to `cw.pr_hydrate.PrAttentionState`, the canonical `Literal` for
  this vocabulary — a typo in any of them would type-check cleanly and
  surface only as a silently-never-matching runtime comparison. Each is now
  annotated `PrAttentionState` so mypy catches drift; a comment notes why
  only four of the `Literal`'s five members have a recipe here
  (`ready_to_approve` has nothing to auto-fix).

- **Salvaged sessions no longer mark failed tickets as done (#1566):**
  reconcile's salvage path and live dispatch classified the same terminal
  `AutoDevResult` status oppositely — dispatch held `scope_exceeded`,
  `forbidden_area`, `merge_gate_blocked`, and `merge_pending` at
  `BLOCKED_ON_USER`, while salvage routed all four to `COMPLETED`, so a ticket
  whose worker had actually stopped short was silently closed out. Both paths
  now read a single classifier, `queue_status_for_terminal_sentinel` (with its
  `SALVAGE_HOLD_STATUSES` set) in `cw.auto_dev_result.schema`, and a drift-guard
  test pins dispatch's own status sets against it.

### Changed

- **Plan signoff markers have one canonical home (#1567):** `_marker_version`
  moved from `cw.reconcile.gate_recipes` to `cw.dev_queue.lifecycle`, joined
  there by a new `_plan_body_signoff_ok` that composes it over both signoff
  markers. `_plan_is_reviewed` now delegates to that shared predicate instead of
  a bare `marker in body` substring check, so an unclosed marker comment is no
  longer accepted as a reviewed plan. `gate_recipes` imports the marker helpers
  and constants from `cw.dev_queue` rather than keeping its own copies.

## [1.26.0] - 2026-08-02

### Added

- **Cycle-0 MUST_FIX findings are persisted for every codex fix-loop exit
  (#1485):** a fix loop that converged cleanly previously reported
  "Non-blocking — no MUST_FIX findings," discarding the content of the
  findings it had just fixed and leaving no record that anything was wrong.
  The cycle-0 `ReviewVerdict` is now captured once before the loop begins and
  written via `write_review_verdict` into
  `diagnostics_bundle_dir(<session_id>)/cycle0-findings.json`, so the snapshot
  survives every exit path — clean convergence, cap or budget exhaustion,
  scope violation, fix-invocation failure, and an unparseable mid-loop
  re-review. A pointer to the file is appended to the sentinel's
  `friction_highlights` on each of those paths, since a clean exit carries no
  `Blocker` for the usual diagnostics pointer to attach to and the artifact
  would otherwise be undiscoverable without knowing the session id. Retention
  follows the existing `diagnostics_retention_hours` sweep (default 24h); a
  write failure logs a WARNING and never interrupts the loop. This is the
  auditable record named as a precondition for re-enabling
  `codex_fix_loop_enabled` on a supervised lane.

### Fixed

- **Self-reported `scope.files` / `scope.lines_actual` are now verified against
  real git facts (#1487):** a worker computing its diff against a stale
  merge-base could report 18 files / 1567 lines for a branch that actually
  changed 3 files / 152 lines (#1393), and nothing downstream noticed. A new
  `cw.worktree.compute_branch_diff_scope` resolves the merge-base against
  `origin/<default_branch>` fresh and measures the branch's own diff;
  `reconcile_result_scope` compares that measurement against the self-report and
  overwrites both fields when they disagree, logging a WARNING on a gross
  divergence (zero-vs-non-zero either way, or a ratio past 2x). It is wired into
  all three producer families: reconcile-sweep salvage
  (`_parse_any_sentinel_from_transcript`, covering all eight call sites across
  idle / phantom / stalled / concierge), the headless Stop-hook parse
  (`_parse_headless_sentinel`), and codex-review synthesis — where
  `synthesize_codex_review_result`'s hardcoded `files=0, lines_actual=0`
  placeholder is replaced by a real measurement. Every path fails open: a
  pre-impl exit, a missing worktree, or an unverifiable git state leaves the
  result untouched and never raises. `local_runner._git_facts` now shares the
  same numstat parser, so the two scope producers cannot drift.

## [1.25.0] - 2026-08-02

### Added

- **`operator_github_login_by_repo` config field (#1171):** a repo-keyed
  operator-login override on `OrchestratorConfig`, consulted at the
  client-less entry points that have no `ClientConfig` to read the existing
  `operator_github_login` override from — `cw review register`, the
  `review_requested` webhook handler, and `hydrate_pr_states`. The new
  `resolve_operator_login_for_repo` resolver never calls `cached_gh_login()`
  itself (per-repo resolution runs many times per hydrate tick; an inline
  call would reintroduce the #1195 subprocess retry storm) — callers thread
  a caller-resolved fallback through instead.
- **`review_requested` events now relay through `pr-events.yml` (#1169):**
  the workflow adds `review_requested` to its `pull_request` trigger types
  and builds a payload from `requested_reviewer`/`requested_team` +
  `sender.login`, closing the producer side of RFC 0011 S2 (the server-side
  consumer, `_handle_review_requested_sync`, already existed). The
  `pull_request` case now dispatches on `github.event.action` before the
  merged-gate, since a `review_requested` delivery always has `merged=false`
  and would otherwise be silently swallowed as "PR closed without merge."
- **`cw dev-queue drain --held` (RFC 0011 A4, #1161):** batch-requeues every
  operator-availability-park (`disposition=awaiting_operator`)
  `BLOCKED_ON_USER` ticket for a client back to its own current stage in one
  command, continuing past a per-ticket failure instead of aborting the
  batch (`--client` required, `--lane`/`--dry-run` optional). Emits
  `TICKET_REQUEUED` per drained ticket and exits nonzero iff any selected
  ticket failed to drain. A3 force-holds (finalize-gate holds, #1160) are
  explicitly excluded — only Rule-5 availability parks are batch-drainable.
- **Proactive finalize hold — `hold_finalize` field, `finalize_gate` config,
  `gate.auto_approve_held` event (RFC 0011 A3, #1160):** schema v23 adds
  `TicketTask.hold_finalize` (`Literal["manual"] | None`) plus
  `LaneConfig.finalize_gate` / `OrchestratorConfig.default_finalize_gate`,
  letting an operator force a ticket to park at
  `BLOCKED_ON_USER`/`finalize_gate_held` ahead of an otherwise-unattended
  ship instead of finalizing automatically. Set per-ticket via
  `cw dev-queue add --hold-finalize`; released only by an
  operator-initiated `cw dev-queue approve` (any automatic caller that
  hits an armed hold now emits `GATE_AUTO_APPROVE_HELD` as a correction
  instead of silently shipping). Not batch-drainable via `dev-queue drain
  --held` (see above).
- **Attention-digest coalescing (RFC 0011 A6, #1162):** schema v24 adds
  `TicketTask.attention_digest_buffered_at`; three new `OrchestratorConfig`
  fields — `attention_digest_window_tz` (default `America/New_York`),
  `attention_digest_window_start_hour`/`_end_hour` (default `8`/`20`), and
  `attention_digest_idle_floor_seconds` (default `60`) — govern when a held
  (`HOLD_DISPOSITIONS`) `session.needs_attention` park gets buffered into a
  single digest push instead of forwarded immediately. The digest flushes
  once the local-timezone delivery window is open AND the idle-drain floor
  has elapsed since the most recent buffered arrival; every non-held event
  (genuine blocked/broken parks, ticketless fleet-wide events) still
  forwards unbatched, exactly as before. **Operator-visible by default:**
  a held attention park that arrives outside 8am–8pm America/New_York no
  longer pushes immediately — it waits for the window to open.
- **`ssh-key-loaded` diagnostic in `cw doctor` (#1400):** surfaces whether
  an ED25519/RSA key is loaded in the ssh-agent — the same condition the
  dev-queue dispatch SSH-key preflight gate (#927) already blocks on, now
  visible as a non-blocking warn-only check in `cw doctor` output too.
- **Advisory CHANGELOG check on pull requests (#1532):** a new
  `changelog-advisory.yml` workflow emits a non-blocking `::warning::`
  annotation when a PR touches `src/cw/**` without touching `CHANGELOG.md`.
  Advisory only — it never fails the job, since docs-only, test-only, and
  default-off "dark release" plumbing PRs legitimately have nothing to
  document. Evaluated per-PR rather than accumulated since the last tag, so
  a single existing `[Unreleased]` entry cannot mask any number of PRs that
  added none. The file list is read via `gh api ... --paginate`, not
  `gh pr view --json files`, which silently caps at 100 entries.
- **`dispatch-guard.yml` watches the real dispatch/reconcile paths for the
  first time (#1565):** the workflow's push-path filter and in-job
  `git diff` both targeted `src/cw/dispatch.py` and `src/cw/reconcile.py` —
  flat modules that have never existed in this repo's history; both were
  born as packages. The unreleased-changes safety net has therefore been
  inert since the workflow was authored. Repointed at `src/cw/dispatch/**`
  and `src/cw/reconcile/`; confirmed live by issue #1583, opened
  automatically the same day once real dispatch/reconcile changes landed
  post-release.

### Changed

- **BREAKING — `codex_fix_loop_enabled` moves from `ClientConfig` to
  lane/global scope (#1553):** `ClientConfig.codex_fix_loop_enabled` is
  removed outright. `ClientConfig` is `extra="forbid"`, so any
  `~/.config/cw/clients.yaml` that still sets `codex_fix_loop_enabled` on a
  client block — `true` or `false` — now fails config validation and
  breaks every `cw` command until fixed. Replaced by the new
  `LaneConfig.codex_fix_loop_enabled` (`Literal[True] | None` — a lane can
  only opt IN, never opt a `True` global default back out) with
  `OrchestratorConfig.default_codex_fix_loop_enabled` (default `false`) as
  the global fallback. **Migration:** delete the client-level
  `codex_fix_loop_enabled` line. If it was `false`, deleting it is a no-op
  (matches the new default) and no replacement is needed. If it was `true`,
  add `codex_fix_loop_enabled: true` to the relevant lane block instead (or
  set `default_codex_fix_loop_enabled: true` in `orchestrator.yaml` to
  restore it fleet-wide).
- **`TicketTask.disposition` distinguishes hold-class parks from other
  terminal parks (RFC 0011 A1, #1254):** a park caused by operator/
  dependency unavailability now stamps the shared `awaiting_operator`
  disposition (visible in `cw dev-queue list` / JSON output) instead of the
  verbatim status it previously carried, via the new
  `_hold_aware_disposition` helper and `HOLD_DISPOSITIONS` namespace
  threaded through both the dispatch staged-decision table and all six
  reconcile salvage/foreign-result call sites. Building block for the new
  `dev-queue drain --held` command (above); `_derive_disposition` itself is
  unchanged for every other case.
- **`install-skills.sh` symlinks commands and skill dirs instead of copying
  them (#1535):** `~/.claude/commands/<name>` and `~/.claude/skills/<name>`
  now point directly into the checkout's `.claude/` tree — one copy on
  disk, so drift between the tracked repo and the global install is
  structurally impossible (the exact class of bug `skills-commands-drift`
  in `cw doctor`, added in 1.24.0, exists to catch). The installer
  self-migrates a prior copy-based install and repoints a stale symlink on
  the next run; the excluded-commands list also moves from a hardcoded
  array to `scripts/excluded-commands.txt`, shared with `cw doctor`'s drift
  check.
- **Release gate strictness matched to CI (#1565):** `release.yml` and
  `scripts/release.sh` ran `mypy src/` while CI and CLAUDE.md gate 4 require
  `mypy --strict src/` — a weaker duplicate of the quality-gate list that
  could let a `--strict`-failing change through a release build.
  `release.sh` also gained the missing `ruff format --check` step. Both
  release surfaces' version-mismatch guidance was corrected: it told the
  operator to edit a version literal in `src/cw/__init__.py` that no longer
  exists (`cw.__version__` resolves dynamically from installed distribution
  metadata) — now points at `pyproject.toml` + `uv sync`, matching
  `docs/release-playbook.md`'s corrected release-mechanics section.
- **`cw-fanout`/`cw-followup` monitoring scripts stop re-deriving cw-owned
  paths (#1565):** `wave_status.py` hardcoded
  `~/.local/share/cw/dev_queue.json` without `cw.config.STATE_DIR`'s
  `XDG_DATA_HOME` branch — an operator with `XDG_DATA_HOME` set silently got
  an empty wave status, since `_load_tasks` treats the missing hardcoded
  path as "no tasks." The script stays import-free by design and now
  mirrors the 3-line XDG branch. `parse_sentinel.py` inlined the transcript
  project-dir path encoding (`replace('/','-').replace('.','-')`) instead of
  calling `cw._util.claude_project_dir` — the exact single-replace encoding
  bug #463 fixed there once would not have propagated to this copy; it now
  imports and calls the canonical helper.
- **`_KNOWN_STATUSES`, `SUPPORTED_SCHEMA_VERSIONS`, and `DRAIN_DISPOSITIONS`
  now derive from their schema home instead of re-enumerating it (#1565):**
  three hand-typed vocabularies that could silently drift from
  `schema.py`'s `Status`/`SchemaVersion` Literals. `parse.py`'s
  `_KNOWN_STATUSES` is now `frozenset(get_args(Status))` — a divergence
  previously could have short-circuited a schema-valid status into
  `BlockedResult` before Pydantic ever saw it. `SUPPORTED_SCHEMA_VERSIONS`
  is now derived from a new single-source `schema.SchemaVersion` Literal.
  `drain.py`'s `DRAIN_DISPOSITIONS` is now
  `HOLD_DISPOSITIONS - {FINALIZE_GATE_HELD_DISPOSITION}` instead of a
  hand-pinned set whose own comment admitted the excluded constant "does
  not exist on main yet" — stale within a day of being written, since
  #1160 had already landed. No behavior change: all derived values are
  byte-identical to what they replace.
- **Dead `_assistant_text_from_transcript` removed from `cw.reconcile`
  (#1565):** zero call sites in `src/` or `tests/`, superseded by
  `_iter_assistant_records`, which reimplements the same per-record content
  guard with timestamps. Removed the function and its stale re-export from
  `cw.reconcile.__init__`'s `__all__`.

### Fixed

- **Codex review prompts no longer send dangling doc references (#1548):**
  6 of the 9 large-tier reviewer specs told codex to consult
  `output-formats.md`, `review-tone-guide.md`, or `testing-philosophy.md` —
  files codex has no filesystem access to read. The severity taxonomy,
  tone guide, and (for Test Reviewer) the 13-item testing checklist are now
  inlined directly into the prompt instead, ahead of `_OUTPUT_INSTRUCTIONS`.
- **Codex reviewer tool preconditions no longer suppress diff-groundable
  findings (#1543):** the inlined Agent Specification section was authored
  for a tool-using Claude subagent; codex was silently dropping any finding
  whose spec-defined verification step it had no tool access to perform.
  `_OUTPUT_INSTRUCTIONS` now overrides those preconditions explicitly — an
  unperformed verification step gets reported as a `LOW`-confidence finding
  (naming the unperformed check in `consequence`) instead of being
  suppressed.
- **Reviewer documents must justify a clean `status="ok"` verdict (#1544):**
  `{"status": "ok", "detail": "", "findings": []}` — indistinguishable from
  a reviewer that never actually looked — is now rejected. A clean pass
  with no findings must state what was checked in `detail`;
  `status="degraded"` is the correct signal when a rubric-mandated check
  could not actually be performed. Applies to both the native Claude
  subagent and codex review paths.
- **Review verdict comments surface finding confidence (#1555):** a
  non-`HIGH`-confidence finding now renders an inline `_(LOW confidence)_`-
  style annotation on its line in the rendered PR verdict comment.
  Display-only — confidence still never affects dedup, blocking, or
  partition logic.
- **Codex review health is no longer hardcoded to HIGH/PROCEED on a clean
  pass (#1551):** `synthesize_codex_review_result`'s clean-review branch
  previously reported `Health(HIGH, False, PROCEED)` regardless of what the
  reviewer documents actually said. It now derives health from document
  status — any `degraded` or self-reported `failed` document (no MUST_FIX
  finding, no run failure, but reduced coverage) downgrades health to
  `MEDIUM`/`EXIT_FOR_HUMAN_REVIEW` instead of reporting a spuriously clean
  signal.
- **Local-runner health is no longer hardcoded to HIGH/PROCEED on a clean
  git result (#1580):** `synthesize_git_result`'s clean `stage_complete`
  path claimed `Health(HIGH, False, PROCEED)` on nothing more than "commits
  exist" — no review, no test run, no vetting of any kind. Now reports the
  honest pessimistic default (`MEDIUM`/`True`/`EXIT_FOR_HUMAN_REVIEW`),
  mirroring the fixed-review path's posture. Sibling of #1551 above.
- **`signoff_gate` parks now emit `session.needs_attention` immediately
  (#1552):** all four signoff-gate park sites (three in
  `dispatch.routing`'s staged-decision table, one in `dev_queue.approval`'s
  `approve` CLI path) previously transitioned a task to
  `AWAITING_OPERATOR_SIGNOFF` with no attention push, so an operator only
  learned about the park up to 45 minutes later via the escalation sweep.
  Each now emits `SESSION_NEEDS_ATTENTION` (`paused_status=signoff_gate`)
  before the transition.
- **`cw dev-queue wait` no longer misreports `stage_complete`/
  `merge_pending` sentinels as FAILED (#1565):** `_WAIT_STATUS_EXIT` was a
  hand-typed re-enumeration of `schema.Status` that had drifted — a
  `stage_complete` sentinel (a successful intermediate stage hand-off) fell
  through to the FAILED default instead of being treated as non-terminal,
  and `merge_pending` misreported FAILED instead of the `BLOCKED_ON_USER`
  exit code dispatch already used for the same sentinel. A drift-guard test
  now pins every `Status` value to an explicit mapping or an
  intermediate-advance classification so a future addition fails loudly
  instead of silently exiting FAILED.
- **Release PRs no longer get titled after a `docs(...)` fixup commit
  (#1531):** `/ship-it`'s title ladder skipped `chore` outright, so on a
  release branch the sibling `docs(release): ...` commit won the PR title and
  the version bump became invisible in the merge history. A new tier ahead of
  the ladder matches `^chore\(release\):` and wins outright regardless of
  commit order; ordinary non-release `chore:` commits are still excluded, so
  the substantive-commit tier is unaffected. Relatedly,
  `release-tag.yml` now emits a `::warning::` when a push to `main` whose
  subject was *not* recognized as a release commit nevertheless carries a
  `pyproject.toml` version that differs from the latest `vX.Y.Z` tag on
  `origin` — the silent-skip arm of the tagging guard is now observable
  instead of failing quietly.
- **`dev-queue wait` no longer fires false ATTENTION during a normal
  inter-stage handoff (#1557):** mid-wait-reap detection previously fired
  exit `3` (`reaped_awaiting_redispatch`) on the bare observation that
  `session_id` went non-None→None — exactly what a normal stage-boundary
  advance also produces. Detection now requires `reap_proposed_at` evidence
  on the prior session, matching the existing `BLOCKED_ON_USER` reap-detection
  pattern (#542).

### Documentation

- **Codex fix-loop cost posture for metered plans (#1549):** documents the
  worst-case codex-invocation count with the fix loop enabled — up to
  `6 × review_pass_size + 5` calls (29 for a small-tier ticket, 59 for
  large-tier) — versus `1 × review_pass_size` with it off, and recommends
  operators on a metered codex/GPT plan leave the fix loop disabled
  (already the default) until that worst case is checked against remaining
  billing-period quota.
- **CLAUDE.md's quality-gate list was missing CI's smoke-import step
  (#1565):** the list claimed to mirror "the first five" CI gates, but CI's
  `.claude/scripts/check_imports.py` smoke-import step wasn't listed at
  all — a contributor following the list verbatim could go green locally
  and red in CI with no documented reproduction command. Added as gate 5
  (CI order, gates renumbered 6-9), with a new note on the separate
  `package-smoke` CI job that has no local equivalent. Also corrected: the
  pre-commit-hooks claim said hooks enforce "gates 1-5"; it's actually
  gates 1-4 (gate 6, the hook suite itself, enforces those) — gate 5 has no
  hook and runs only in CI and this list.
- **`CONFIG_REFERENCE.md` corrected against the code it documents (#1565):**
  `orchestrator.default_ceiling` was shown as `2` inside a block explicitly
  framed "created with defaults" — the actual code default is `1`. The
  `operator_channel_forward` default event list — also framed as showing
  defaults — was missing `gate.auto_approve_held` (#1160) and
  `gate.ssh_key_bypassed` (#1437), telling operators those events do *not*
  forward by default when they do. `default_finalize_gate` (#1160) and
  `diagnostics_retention_hours` (#1239) existed in `OrchestratorConfig` but
  were entirely undocumented; both are now included with their defaults and
  semantics.
- **Stale flat-module citations retargeted across commands, skills, and
  CLAUDE.md (#1565):** nine citations in
  `.claude/commands/auto-dev-finalize.md`, `auto-dev-intake.md`,
  `auto-dev-review.md`, plus `.claude/skills/cw-session-watch/SKILL.md`,
  `sprint-buildout/SKILL.md`, and CLAUDE.md still pointed at flat modules
  that no longer exist (`auto_dev_result.py`, `dispatch.py`,
  `reconcile.py`) — retargeted to the real package paths
  (`auto_dev_result/schema.py`, `dispatch/routing.py`,
  `dispatch/gating.py`, `reconcile/`), with brittle line-number citations
  replaced by symbol/section anchors.

## [1.24.0] - 2026-07-27

### Added

- **Codex fix-loop scope-violation gate — `CODEX_FIX_SCOPE_VIOLATION` +
  sensitive-files manifest (#1464).** The first enforcement gate on the
  autonomous commits the codex fix loop (#1392) makes: a fix-cycle change parks
  only when it both (a) touches a path outside the cycle-0 reviewed diff's file
  set and (b) matches the new `.claude/sensitive-files.yml` manifest — an
  in-scope sensitive edit or an out-of-scope non-sensitive addition still
  passes. `_porcelain_changed_paths` is a rename-aware `git status --porcelain`
  parser using `--untracked-files=all`, so a wholly-new directory reports its
  individual files instead of collapsing to `?? dir/` and blinding the match.
  The park reason is deliberately kept out of `_CATEGORY_TO_REASON` — a
  successful-but-out-of-policy fix is a distinct axis from a review finding.
  Manifest entries are dual-form (glob + bare prefix) so both the small
  (fnmatch) and large (substring) tier match contracts are covered by one file.
- **`skills-commands-drift` doctor check (#1514):** `cw doctor` now compares
  this repo's git-tracked `.claude/skills` / `.claude/commands` — the
  authoritative copy a dispatched worker loads from its worktree — against the
  `~/.claude` global install those paths symlink to. Because both are single
  top-level symlinks, `readlink -f` on a child path proved nothing about
  whether the trees agreed, and the silent divergence had already produced
  three wrong-repo verdicts. New leaf module `src/cw/doctor/skills_drift.py`.
- **`ssh_key_gate_enabled` operator escape hatch (#1437):** new
  `OrchestratorConfig` field (default `true`, gate stays enforced) plus a
  `SSH_KEY_GATE_BYPASSED` event emitted when dispatch proceeds with the gate
  disabled, so a bypass is never silent.
- **`cw dev-queue approve --post-marker` (#1419):** `approve` can now post the
  `<!-- auto-dev-plan-approved -->` human-signoff marker it previously only
  assumed, with dedup, a warn path, and fail-closed behavior. Without it, LARGE
  plans re-parked in an approve↔replan loop because the marker the next attempt
  looked for was never written.
- **`ClientConfig.codex_fix_loop_enabled` (#1465):** per-client gate for the
  codex fix loop, forwarded by `CodexExecutor.spawn`. **Defaults to `False`** —
  the autonomous fix loop shipped in 1.23.0 (#1392) is opt-in per client, not
  on by default. When disabled, a blocking cycle-0 review returns
  `run_review`'s result unchanged with zero fix invocations — the checkpoint-2
  prerequisite.
- **Release-notes checkpoint step in `release-tag.yml` (#1513):** an
  observation-only step after "Create GitHub Release" records, in the job
  summary and as an annotation, which notes source actually shipped —
  CHANGELOG-derived, autogenerated fallback, or no release-creation action
  because the release already existed. It adds no outputs, changes no existing
  step, and never asserts a cause for a mismatch.

### Fixed

- **Release tagging no longer silently no-ops (#1483):** the `release-tag.yml`
  guard regex only accepted `chore(release): vX.Y.Z` subjects, but the actual
  release process authors `chore(release): bump version to X.Y.Z`. The regex
  failed to match, emitted `match=false`, and every downstream step — tag
  creation, release creation — was skipped with no error. This bit both v1.22.3
  and v1.23.0, each of which needed a manual tag push after the fact. The
  alternation now accepts both conventions, and a non-matching release-shaped
  subject fails loudly instead of skipping.
- **SSH preflight no longer stalls the whole fleet on a false negative
  (#1436):** the probe read `SSH_AUTH_SOCK` only and ignored an `ssh_config`
  `IdentityAgent` directive, so an operator with a working agent configured
  that way tripped `ssh_key_gate` for every dispatch. The probe now resolves
  `IdentityAgent` via `ssh -G` before falling back to the `ssh-add -l` check.
- **`session.needs_attention` now fires on approval-gate parks (#1257):** the
  contract documented at `docs/headless-contract.md:499` was false — four park
  sites in `claim.py` (codex-capability gate, stale-worktree dirty-park
  pre-spawn guard, stalled-headless dirty-worktree revert, attempt-cap block)
  transitioned to `BLOCKED_ON_USER` silently. `_park_running_task_blocked_on_user`
  now takes a breadcrumbs param and folds the `SESSION_NEEDS_ATTENTION` emit
  into the shared helper, so every park signals through one path.
- **Finalize no longer strands work in a local-only worktree (#1414):**
  GEN-5343. Step 4c.2 merged `origin/main` into the feature branch and then
  delegated to `/prep-pr`, whose quality gates can run up to 5400s — a timeout
  anywhere in that window left the merge commit unpushed and the branch
  reachable only from the worktree. The branch is now pushed immediately after
  the merge, before delegation.
- **`_route_blocked_result_to_task`'s catch-all now respects transcript
  liveness (#1406):** the FAILED/abandoned branch applied its routing without a
  liveness check, so an actively-working session could be routed terminal off a
  stale read. Adds the liveness veto the sibling paths already had (alternate
  path from #1281).
- **`signal_stop` no longer leaks a live daemon worker on a terminal-FAILED
  landing (#1273):** `SentinelRouteOutcome.routed` conflated a genuine terminal
  landing with the #986 stage-mismatch refusal, so the daemon stop never fired
  for the former. A `landed_terminal` discriminator — set only in the
  `BlockedResult`-on-`RUNNING` arm of `_apply_sentinel_to_task`, derived from
  `_route_blocked_result_to_task`'s own return value — separates the two.
- **Live sessions bearing a door-refused foreign result now complete (#1470):**
  a headless DAEMON session whose `last_result` already carried a terminal
  sentinel from another authority (e.g. an out-of-band `cw result emit`, which
  by contract never flips `session.status`) sat ACTIVE/IDLE forever, falling
  through the budget/salvage/retry-cap chain every tick and re-offering itself
  to a first-writer-wins door that silently refused, with no bounded cap. A new
  `COMPLETE_FOREIGN_RESULT` detect-phase guard — after `SKIP_PARKED`, before the
  budget gate — completes the session the same tick directly from
  `session.last_result`, with no new door write, so RFC 0012 first-writer-wins
  is preserved. An unroutable foreign result still short-circuits the wasted
  transcript re-parse but is not completed.
- **`blocker.reason` now propagates to queue state and the attention formatter
  (#1511):** new `TicketTask.blocked_reason`, stamped and cleared by
  `transition_task_status`, carrying the reason off a well-formed
  `blocked`/`merge_gate_blocked` sentinel — distinct from
  `last_blocked_result`'s malformed-sentinel diagnostic dump.
  `DEV_QUEUE_SCHEMA_VERSION` 21 → 22 with a default-filling migration.
- **Plan drafts persist across blocked headless attempts (#1510):** complex
  plans thrashed against the 1-revision headless cap — nothing survived the
  session, so each round regenerated from scratch and surfaced a fresh,
  legitimate MUST_FIX. Stage 1 now writes `.cw/plan-draft.md` when Step 1f.3
  exits blocked with `plan_unreviewable` or `plan_unsound`, and a retry resumes
  from Step 1a instead of starting over. `.cw/plan.md` (the Stage-2
  implementation contract) is untouched: still written only by Step 1g, which
  best-effort clears the draft on success, as does the Step 1e `no_op` exit; a
  supersession guard in Step 1a ignores the draft whenever `.cw/plan.md`
  already exists, regardless of timestamps. A draft resume is recorded in
  `friction_highlights` — the only sentinel-visible signal, since `plan_source`
  stays `generated`.
- **Step 1a merges a later operator comment into an already-posted plan
  (#1515):** the existing-plan-plus-later-resolution case had no merge branch,
  so the posted plan was discarded, regenerated, and forced through a second
  approval round. Step 1g's `## Decisions` fold — previously scoped to the
  interactive path only — was broadened to cover the new headless re-entry, so
  the resolved answer can't silently fail to land.
- **Behind-only origin drift no longer blocks the next ticket at pre-flight
  (#1434):** when a wave PR merged, the base checkout was not auto-fast-
  forwarded, and the following ticket failed pre-flight with
  `local_main_diverged_from_origin` until an operator ran a manual refresh-all.
  Step P3 now does a bounded wait-and-recheck for the behind-only case.

### Changed

- `reconcile/stalled.py` (1601 lines) split into the `cw.reconcile.stalled`
  package per `ARCHITECTURE.md` §7 principle 6 (module-size / package-split:
  modules stay under ~1000 lines) — `_detect` / `_mutations` / `_events` /
  `core` submodules behind a re-exporting `__init__` that preserves the
  historical import surface plus `_deps`, so existing patch targets keep
  resolving (#1484). Pure move: all 29 top-level definitions carried over
  byte-for-byte, verified by AST source-segment comparison against the fork
  point. Fast-follow #1512 strengthens the gh-prepass test to prove its
  `_detect_stalled_candidates` monkeypatch actually fired — it previously
  passed green even if the target regressed to the pre-#1484 bare path, which
  patches a decoy binding.
- `_validate_existing_result_for_routing` and the isinstance/status routing
  ladder (now `_foreign_result_target_queue_status`) promoted from
  `concierge.py` to `reconcile/_shared.py` so `stalled.py` can reuse them
  without a cross-module private import (#1470); behavior unchanged.
- `tests/test_codex_review.py` (1456 lines) split into per-submodule files
  mirroring the `cw.codex_review` package — `_const` / `_diff` / `_context` /
  `_roles` / `_verdict` / `core` — with shared doubles extracted to
  `tests/_codex_review_helpers.py`, mirroring `tests/_reconcile_helpers.py`
  (#1473). Pure move, no assertion changes.

## [1.23.0] - 2026-07-23

### Added

- **Unified result publishing — one door, per-backend harvest authorities
  (RFC 0012, #1446, milestone v1.23.0).** Every `Session.last_result` write in
  `src/cw/` now routes through a single importable door in `cw.result`:
  - `emit_result()` / `emit_result_locked()` extracted from the `cw result
    emit` CLI, with domain exceptions (`EmitValidationError`,
    `EmitSessionNotFoundError`) and a typed `EmitOutcome` (#1455).
  - `Session.last_result_source` provenance enum (`emit_cli`,
    `stop_hook_harvest`, `executor_direct`, `git_synthesis`,
    `salvage_transcript`; `None` = pre-migration) and first-writer-wins
    arbitration: the door refuses to overwrite a terminal result, logs the
    collision with both sources, and returns the existing result (#1456).
  - Writer migrations: the Stop-hook transcript harvest (#1457, including a
    discriminated `AutoDevResult | BlockedResult` union so parser-synthesized
    blocked results pass the door), CodexExecutor/LocalExecutor direct writes
    (#1458), and reconcile git-synthesis + transcript salvage (#1459, via a
    pure `emit_result_on()` that batched reconcile sweeps call without
    double-save clobbering; door refusals short-circuit salvage completions).
  - Retirement of `persist_last_result` and the dead `stdout` event-payload
    branch in dispatch (#1460), and a source-scan guard test that fails on
    any `last_result` assignment outside the door module, with a
    self-documenting allowlist for park markers, routed-sentinel advances,
    rescue bookkeeping, and the requeue reset (#1461). The harvest-authority
    model and provenance enum are documented in `docs/headless-contract.md`.
- **Codex review fix-loop** (#1392): the codex review backend now drives a
  real fix loop — accepted MUST_FIX findings feed a second write-capable
  `codex exec --sandbox workspace-write` invocation plus an in-process
  commit, followed by a full re-review against a freshly captured diff;
  shared 7200s deadline with floor-gated cycles, cap 5 with escalation at
  3+, disposition tracking by dedup key, and cycle-0-snapshot
  `must_fix_initial` semantics per the headless contract. Review roles stay
  read-only.
- **Bounded reconcile vetoes:** consecutive park-veto counter +
  `park_veto_cap` for the stalled-session liveness veto (#1445), and the
  sibling `consecutive_sentinel_mismatch_vetoes` counter +
  `sentinel_mismatch_veto_cap` for the phantom sentinel-stage-mismatch veto
  (#1449) — cap-exhaustion escalates via `SESSION_NEEDS_ATTENTION` instead
  of looping silently, including under `signal_only`.
- **`ARCHITECTURE.md`** (#1451): repo-root architecture document with
  numbered `§7 Principles` / `§8 Anti-patterns` sections, making the Plan
  Soundness Reviewer's Tier-1 codified-violation check enforceable (it was
  previously vacuous).
- **Per-stage idle-watchdog budgets** (#1061): `idle_watchdog_by_stage`
  config field + resolver.
- **`host_session_budget`** config field and dispatch skip-reason (#1444).

### Fixed

- **Wall-clock reaps no longer kill actively-working sessions** (#1471): a
  transcript written within the liveness window earns a hard grace veto that
  is not charged against `park_veto_cap`, ending same-night token loss from
  liveness-blind budget kills.
- `Blocker.details` now carries the rendered verdict on MUST_FIX blocks
  (#1390) and a failure summary on `CODEX_REVIEW_PARTIAL` (#1439);
  `_post_review_comment` gh failures are logged instead of swallowed (#1391).
- Review-gate worktree path keyed on `$CW_SESSION` with INT/TERM traps and
  exit-status checks (#1443).

### Changed

- `codex_review.py` (1042 lines) split into the `cw.codex_review` package —
  `_const` / `_diff` / `_context` / `_roles` / `_verdict` / `core` submodules
  behind a pure re-export `__init__` (#1462); move-only, consumer imports
  unchanged.

## [1.22.3] - 2026-07-21

### Fixed

- **`cw worktree gc`'s default all-clients run no longer silently truncates
  on a per-client failure** (#1389): the per-client loop had no failure
  isolation — an exception while processing one client aborted the whole
  sweep, silently skipping every client ordered after it (e.g. 16
  git-registered worktrees on one client going completely unexamined with no
  error, skip line, or summary). Each client's GC pass is now wrapped so a
  failure prints `[<client>] ERROR — <reason>` and the sweep continues to the
  next client; a `[<client>] N examined / M skipped` summary line makes a
  short/incomplete per-client pass visible instead of reading as a complete
  sweep. The same isolation applies uniformly to an explicit `--client X`
  invocation — a single-client run that fails now prints `[X] ERROR — ...`
  and exits 0 instead of raising an unhandled traceback. Exit code
  intentionally stays 0 on partial failure this round (see #1399 for the
  deferred exit-code-contract decision).
- **`_claim_next_pending` no longer starves younger pending tasks behind an
  attempt-capped task** (#1248): the plain pending-scan used to `return None`
  the instant it hit a task at the global attempt ceiling, abandoning the
  rest of the sorted pending list for that tick. On a `max_parallel: 1` lane
  this was indefinite head-of-line starvation — the capped task parks
  `BLOCKED_ON_USER` (occupying the lane's only slot) while a claimable task
  sorted behind it is never reached. The attempt-cap branch now `continue`s
  the scan instead of returning, matching the backoff branch immediately
  above it.
- **Diagnostics bundle filenames no longer overwrite on repeat failures**
  (#1330): a diagnostics bundle filename used to be
  `<role-slug>-<reason>[.json|-schema.json|-output.json]` with no
  disambiguator, so a second same-role/same-category failure within one
  session silently clobbered the first's bundle files. Filenames now include
  the failure's `occurred_at` microsecond timestamp
  (`<role-slug>-<category>-<timestamp>...`), so every failure gets its own
  set of files.
- **`cleanup_expired_diagnostics` no longer walks the filesystem on every
  dispatch tick** (#1330): the sweep is now internally throttled to at most
  once per hour via a sentinel file under `state_dir()`, independent of the
  configured retention window.

### Documentation

- **Aider `edit-format` guidance for the local backend** (#1204): documents
  that aider's `whole`-mode default for model ids it doesn't recognize (i.e.
  most non-Claude models) can regenerate the entire target file on every
  edit and blow a stage timeout — a symptom that looks like a model/timeout
  failure rather than the real cause. Operators should set
  `edit-format: diff` in `~/.aider.conf.yml` (or `AIDER_EDIT_FORMAT=diff` in
  the dispatcher's own environment before it starts) to force `diff` mode.
  Doc-only — no `src/cw` changes; edit-format policy stays with aider's own
  config/env precedence rather than a new `cw` passthrough flag.

## [1.21.0] — 2026-07-17

Dispatch-latch hardening plus installer ownership of subagents. The two
`*_fired_at` latches stop recipe re-dispatch storms, `disallowed_mcp_tools`
moves worker tool policy from a tracker heuristic to explicit operator config
(**see MIGRATION below**), and cw's installer now owns agent definitions
outright.

### Added

- **Subagents install globally alongside commands and skills** (#1278):
  `scripts/install-skills.sh` now installs `.claude/agents/*.md` into
  `~/.claude/agents/`, tracked in the same manifest and covered by the same
  manifest-scoped prune invariant (an agent present only in the destination is
  never pruned). `EXCLUDED_AGENTS` withholds experiment-scoped agents from the
  global install, mirroring the existing `EXCLUDED_COMMANDS` precedent. cw is
  now the single source of truth for agent definitions, which previously lived
  as a drifting duplicate set in the `global-claude` checkout.
- **`auto_fix_ci_fired_at` latch + schema v16→17** (#1205): the `auto_fix_ci`
  recipe now re-dispatches once per ci-failing episode instead of spawning a
  worker session on every reconcile tick until hydration catches up.
- **`address_review_fired_at` latch + schema v17→v18** (#1206): the
  `address_review` recipe now dispatches once per changes-requested episode
  instead of re-dispatching an `/address-review` session on every reconcile
  tick until a human re-reviews.

### Changed

- **Daemon worker MCP-tool restrictions are now operator-configurable.** The
  hard-coded, tracker-gated block that stripped Linear MCP tools from
  `github-issues`-client DAEMON workers (added in #726 to avoid a headless
  Linear-OAuth stall) is replaced by a global `disallowed_mcp_tools` list on
  `OrchestratorConfig` (`~/.claude-workspace/orchestrator.yaml`), default empty
  and applied uniformly at both DAEMON spawn chokepoints. cw no longer decides
  tool availability from a tracker heuristic; the operator declares the exact
  deny patterns. **MIGRATION:** any `github-issues` client that relied on the
  old automatic block must now set
  `disallowed_mcp_tools: ["mcp__plugin_linear_linear__*"]` explicitly, or its
  workers will have Linear MCP tools available (a Linear-tracked ticket
  mis-routed to such a client previously blocked at pre-flight with zero Linear
  tools). The single `--disallowed-tools=` token form (#733) is preserved.
- **Premise gate gains a self-verified-→-proceed path** (#1192): the auto-dev
  plan stage's premise contract now distinguishes evidence quality instead of
  treating every unverified factual claim identically. A premise the
  plan-stage agent settled itself with authoritative evidence (official docs,
  `<tool> --help` output, source, or a quoted command + its verbatim output
  from this session) is recorded under a new `## Self-Verified Premises` plan
  section and `friction_highlights`, and the run proceeds; a premise with
  absent, ambiguous, self-contradictory evidence, or one turning on operator
  intent still parks with `premises_pending_verification` exactly as before.
  Headless-only, mirrors the existing ambiguity `Recommendation: ADOPT | PARK`
  fast path. Skill-markdown only — no `src/cw` or schema changes.

## [1.20.0] — 2026-07-15

RFC 0011 availability- & counterparty-aware holding — wave-0 seams plus the
first two epic tickets, all shipped dark or naturally inert (the counterparty
axis has no live "external" producer yet, so the new park class and idle-reap
exemption only fire once a later ticket wires real PR-author comparison).
Alongside it: groundwork for a native `/sprint-buildout` RFC→ticket pipeline
(`cw.sprint` parser, config/plan builder, gh creation helpers — no `cw sprint`
command yet, that lands with the remaining tasks), two webhook/config security
hardenings, and a batch of dispatch/reconcile correctness fixes surfaced while
dogfooding the sprint's own dispatch.

### Added

- **RFC 0011 S1 — counterparty axis + operator self-identity** (#1153):
  `derive_counterparty` (self|external), `ClientConfig.operator_github_login`
  override, and `cw.gh.current_gh_login` promoted to a public contract. Always
  resolves "self" today — no candidate-selection path can reach a PR authored
  by anyone but the operator yet.
- **RFC 0011 S2 — native review-requested register** (#1154): a `WatchedPr`
  model (schema v14→15) for externally-requested PRs, `register_watched_pr`,
  watched-PR hydration wired into `hydrate_pr_states`, a `review_requested`
  webhook event, and a new `cw review register` CLI command.
- **RFC 0011 A1 — `awaiting_operator` park class** (#1155):
  `OPERATOR_UNAVAILABLE_BLOCKER_REASONS`, a distinct axis from
  `FINALIZE_REGRESS_BLOCKER_REASONS` meaning "operator/dependency unreachable"
  rather than "leg is broken." `push_auth_failed` (#1049) is retro-classified
  as the first instance.
- **RFC 0011 B1 — external-counterparty idle-reap exemption** (#1158): a
  DAEMON session idling while reviewing a teammate's PR now escalates to the
  operator via `SESSION_NEEDS_ATTENTION` instead of being silently
  parked/reaped.
- **`request_reviewer_fired_at` latch + schema v15→16** (#1197): the
  `request_reviewer` recipe now fires once per no-reviewer episode instead of
  re-requesting on every reconcile tick.
- **`cw-deps` dependency drift check** (#1077): `cw doctor` now warns when the
  running `cw` interpreter is missing a distribution declared in the source
  `pyproject.toml` — the drift class that crash-looped `cw dev-queue serve`
  after #1075 added `psutil` without a venv resync.
- **`cw dev-queue requeue --from-failed`** (#1190): a FAILED row whose
  underlying session actually completed clean now has a CLI path back to
  PENDING, mirroring the existing `--from-cancelled` escape hatch.
- **Sprint-buildout groundwork** (#1174, #1193, #1209): `cw.sprint`'s
  RFC→ticket parser (`docs/rfcs/TEMPLATE.md` as the strict input contract),
  `BuildoutConfig`/`build_plan`, and gh issue/milestone creation helpers —
  foundation tasks for an upcoming `cw sprint plan|apply` command; the CLI,
  idempotent apply, and acceptance fixture are not in this release.
- **`ConfigValidationError` + `extra="forbid"` on config-facing models**
  (#1200): a typo'd `clients.yaml`/`orchestrator.yaml` key (e.g.
  `review_recipies`) now surfaces as an actionable error at load time instead
  of silently resolving to a hardcoded default.
- **`CodexExecutor` parses real findings** (#1203): codex review counts now
  come from structured `--output-schema` JSON output instead of a hardcoded
  clean review on every exit.
- **ADR-0012 — cw never grants a GitHub review approval** (#1199): documents
  the invariant and adds a guard test scanning for the GraphQL/REST call
  shapes that would violate it.

### Fixed

- **`/pr-event` default-denies when the HMAC secret is unset** (#1127):
  previously fell through unauthenticated; now returns 401 unless
  `--allow-unsigned` is explicitly passed to `cw pr-channel serve`.
- **Plan-review marker comments now require an authorship match** (#1128):
  `fetch_approved_plan_comment` no longer trusts a "plan reviewed" marker
  comment posted by anyone other than the authenticated `gh` identity.
- **`ticket_id` charset widened to permit `#`** (#1184): the #1129 validator
  rejected `repo#N`-shaped ids already live in production (e.g.
  `redact-api#1`), which would have taken down the dev queue for every client
  on the next upgrade since one bad row fails the whole store's validation.
  `#` is now percent-encoded only at the one sink where it's actually
  dangerous (a `gh` API URL path segment).
- **Later-stage sentinel self-escalation walks the stage pointer forward**
  (#1149): a sentinel mapped to a later pipeline stage now advances
  `task.stage` one rung at a time instead of silently no-op-ing — the fix for
  the 76-event Stop-hook storm on session `cbfdc122`. Refusal loops now latch
  instead of re-proposing the same doomed candidate every tick.
- **`SESSION_NEEDS_ATTENTION` now emits on every `blocked_on_user` park**
  (#1117): previously only Rule 2 emitted attention; Rule 3b
  (`merge_pending`), the Rule 5 fallthrough, and Rule 6 (unparseable
  sentinel) parked silently.
- **Transcript liveness derives from the last content-bearing record, not
  mtime** (#1076): Claude Code's metadata-only transcript writes (ai-title,
  permission-mode, etc.) no longer reset a stalled session's idle counter and
  mask a real stranding.
- **Salvage LOW path merges into `last_result` instead of replacing it**
  (#1105): a wholesale replace was destroying pre-existing keys (`status`,
  `review`) that `cw dev-queue approve` depends on to detect an approval gate.
- **`events._parse_lines` tolerates an unknown event type** (#1210): a line
  written by a newer producer no longer crashes `read_events`/`prune_events`;
  only skipped and summarized in one warning per call.
- **`approve` on an unreviewed plan re-parks at PLAN, not IMPL** (#968): a
  Large-scope plan approved before its quality review ran previously advanced
  straight to Stage 2 against an empty `.cw/plan.md`.
- **Forbidden-area classification inspects diff content, not just path**
  (#1104): a `.github/workflows/**` change with no actual pipeline-logic diff
  no longer permanently sticks a ticket at large-tier; Stage 3 can one-time
  downgrade a Stage-1 false positive.
- **Push-path PR hydration recomputes `attention_state`** (#1196): a
  webhook-driven update previously carried the prior `attention_state`
  forward unchanged instead of re-deriving it from the overlaid facts.
- **`aider` stdout/stderr captured to a per-run log file** (#958): a
  died-without-committing failure previously surfaced with no diagnostic
  text at all.
- **`ship-it` PR title no longer wins from TDD scaffolding commits** (#1208):
  title derivation now prefers a substantive `feat`/`fix`/`refactor` commit
  over a `test`/`docs`-only one.
- **Real state-write guard + dev-queue backup rotation** (#1017): writes to
  the real (pre-monkeypatch) state/config dirs are refused under pytest, and
  `save_dev_queue()` now rotates a timestamped backup before every write.
- **`AutoDevResult` empty-item filtering extended** (#1130): the existing
  blank-item guard now also covers `commits`, `friction_highlights`,
  `next_actions`, `Health.shortcuts`, and `AgentHealthEntry.agent_id`.

## [1.19.0] — 2026-07-12

RFC 0010 native review-monitor — a native, event-driven port of the global
`/review-monitor` skill into cw's `reconcile()` loop as config-gated **review
recipes**. Four recipes (`address_review`, `auto_fix_ci`, `request_reviewer`,
`escalate_merge_block`) detect a PR's attention-state and act on it — dispatch a
fix, request a reviewer, escalate a merge block — each emitting auditable
`PR_ACTION_TAKEN`/`PR_ACTION_FAILED` events. Ships **dark** behind a master
`review_recipes_enabled=False` switch with a per-lane enablement map, so a fresh
install auto-does nothing. Includes two finalize-pipeline hang fixes discovered
while dogfooding the sprint's own dispatch.

### Added

- **RFC 0010 P1 — detect-only `address_review` recipe + `review_recipes.py`
  skeleton** (#1096, #1107): the detect→act module (modeled on `concierge.py` /
  `gate_recipes.py`) that reads a PR's `attention_state` from `pr_hydrate.py`'s
  `_compute_attention_state`. Detect-only first; inert behind the master switch.
- **RFC 0010 P2 — `PR_ACTION_TAKEN` / `PR_ACTION_FAILED` events + `address_review`
  act-phase** (#1097, #1121): the act phase re-dispatches a `changes_requested`
  PR, emitting the durable event *before* mutating and a `_FAILED` correction on
  error (the concierge fail-safe pattern).
- **RFC 0010 P3 — per-lane `review_recipes` config gate** (#1098, #1115):
  `LaneConfig.review_recipes` + `TicketTask.review_recipes` +
  `resolve_review_recipe_enabled()` 3-tier precedence (ticket > lane >
  global default-off), mirroring `resolve_gate_recipe_enabled`.
- **RFC 0010 P4 — remaining recipes** (#1099, #1123): `auto_fix_ci` (re-dispatch
  on CI failure), `request_reviewer` (driven by a repo-committed
  `review_strategy` key in `.claude/project-config.yaml` — modes
  `ci | repo_owner | reviewer_team`, default `ci`), and `escalate_merge_block`
  (one-shot-per-episode via a new `escalate_merge_block_fired_at` latch). Adds
  `resolve_review_strategy` (`review_strategy.py`), an `add_pr_reviewer` gh
  helper, a `cw doctor` WARNING on a misconfigured `review_strategy`, and retires
  the coarse `pr.ci_failed` / `pr.review_received` orchestrator rows. Dev-queue
  schema v13→14 (additive).
- **RFC 0010 P5 — ported review-monitor operational lessons** (#1100, #1125): the
  47 dated lessons from the global `review-monitor` skill audited against the
  native port — `# Why:` comments at the guards they justify, regression tests
  for the reproducible failure modes, and a durable `docs/review-recipes-lessons.md`
  index tagging every lesson applies / N-A with where it landed.

### Fixed

- **Finalize isolation gate made non-skippable** (#1122): the `/auto-dev-finalize`
  `#766` dispatch-detection was skippable prose after the spawn instruction; a
  headless finalize defaulted to `isolation:"worktree"`, collided with the live
  dispatch worktree, and hung ~40 min. Folded into mandatory numbered Steps
  4c.1/4c.2.
- **Finalize spawn moved out of the Step 4c intro** (#1124): #1122 numbered the
  gate but left an actionable spawn instruction *leading* Step 4c, so a headless
  finalize (even on Sonnet) still executed the spawn before reaching the gate and
  hung on the same collision. Step 4c's first actionable line is now the gate
  check; the spawn moves to 4c.2. Validated end-to-end on P5's own finalize.
- **`uv.lock` re-locked as part of the cut** (#1101): the v1.18.0 release left
  `uv.lock` lagging, tripping `main_checkout_drift`; this cut re-locks.

## [1.18.0] — 2026-07-09

RFC 0009 gate-recipe automation — the first event-driven auto-actor. Two
config-gated "gate recipes" auto-clear the two operator-approval gates that an
operator was clearing *mechanically* — a clean Stage-3 review and a clean
Stage-1 plan — whenever the pipeline's own structured evidence already says
"clean." Ships **opt-in** behind a master `gate_recipes_enabled=False` switch
with per-lane enablement, so a fresh install auto-approves nothing. Plus two
model-cost fixes (the impl and plan stages no longer default to Opus) and
assorted finalize/install hardening.

### Added

- **RFC 0009 gate recipes — `auto_approve_clean_review` + `auto_adopt_clean_plan`**
  (#1065, #1078): a detect→act reactor (modeled on `concierge.py`) that
  auto-clears a `review_pending_approval` gate when the review is clean
  (`must_fix_initial=0`, `deferred=0`, `recommendation=PROCEED`,
  `forbidden_touched=false`) and a `plan_pending_approval` gate when both plan
  signoff markers are present. Emits an auditable `gate.auto_approved` event
  (carrying a `predicate_snapshot`) *before* mutating, forwarded to the operator
  channel; fails safe via `gate.auto_approve_failed` + a self-clearing latch.
  Behind the master `gate_recipes_enabled=False` opt-in.
- **Per-lane gate-recipe enablement** (#1067): `LaneConfig.gate_recipes` +
  `TicketTask.gate_recipes` + `resolve_gate_recipe_enabled()` 3-tier precedence
  (ticket > lane > global default-off), mirroring `resolve_signoff`. Dev-queue
  schema v12→13 (additive, non-destructive).
- **Behavioral `reconcile()`-tick integration test for the gate recipes** (#1088):
  drives a full reconcile tick with the switch enabled end-to-end (clean review
  auto-approved, clean plan auto-adopted, `GATE_AUTO_APPROVED` emitted). Plus
  shared `_newest_by_created_at` (dev_queue) and `post_issue_comment` (gh)
  primitives extracted from duplicated call sites.
- **Event catalog entries for `gate.auto_approved` / `gate.auto_approve_failed`**
  (#1087).

### Fixed

- **Impl agent no longer hardcodes Opus** (#1079): the Stage-2 implementation
  agent's model is resolved from the ticket's scope tier (Small→Sonnet,
  Large→Opus) instead of unconditionally Opus — matching the documented model
  matrices and cutting Opus spend on every small-scope ticket.
- **Plan agent runs Sonnet, not Opus** (#1084): the Stage-1 plan agent is Sonnet
  so the plan review is calibrated to the weaker model's output (the
  plan-revision agent was already Sonnet).
- **Gate recipe could clear an `AWAITING_OPERATOR_SIGNOFF` gate it never
  validated** (#1083): the act phase now threads the resolved row identity
  through `_approve_ticket_locked` (plus a status assertion) instead of
  re-resolving by `(ticket_id, client)` string key, so a newer duplicate
  signoff row can no longer be cleared by a review recipe.
- **`--headless` now propagates into finalize's `/prep-pr`** (#1074, #1071).
- **Cross-platform LocalExecutor liveness** (#921): a single psutil code path
  replaces the Linux-only `/proc` read, with a schema migration clearing stale
  liveness handles.
- **`mypy .` scoped to the CI gate** (#1064).
- **Project-scoped `/ship-it` no longer installed globally** (#1090).

### Changed

- RFC 0009 design doc + errata: lock-free approve helper, marker-read reality
  (#1068).

## [1.17.0] — 2026-07-08

Finalize-reliability wave: eliminates the `needs_salvage` false-block class
that made successful ships look like failures, closes the primary-checkout
leak and the push-auth silent-death in finalize, and adds per-stage timeout
axes — plus the accumulated recovery-gap wave.

### Added

- **`headless_timeout_by_stage` resolution axis** (#1020): per-stage
  wall-clock budgets (plan 3600 / impl 4200 / review 7200 / finalize 5400s)
  so long review/finalize passes are not reaped on the flat global budget.
- **`cw dev-queue requeue --from-cancelled`** (#1018): escape hatch to
  recover a CANCELLED ticket (forward/same-stage only).

### Fixed

- **Finalize `needs_salvage` false-block / salvage deadlock** (#1054):
  `idle.py`'s stage-blind `SALVAGE_GIT` classification raced ahead of the
  finalize-aware `stalled.py` path (900s vs 5400s), parking FINALIZE-stage
  sessions `needs_salvage` and tripping the `park_marker_blocks_salvage`
  deadlock — so a session whose PR had already merged showed
  `blocked_on_user` despite shipping. Idle now defers FINALIZE-stage
  git-branch sessions and consults merged-PR ground truth; the same
  merge-aware check is applied to `rescue_finalize_blocked_sessions`, with
  cross-client `(client, ticket_id)` scoping throughout.
- **Finalize leaked branches into the primary checkout** (#1047): finalize
  Steps 4c / 4d.1 now apply the #766 dispatch-worktree detection (omit
  `isolation: "worktree"` when already in a cw worktree), preventing the
  `git checkout` leak that freshness-gated the whole client.
- **Stale `.cw/context.json` on requeue** (#1046): dispatch invalidates the
  per-worktree context before re-spawn so a requeue's newly-added operator
  resolutions reach the planner (LocalExecutor lane excluded).
- **Push-auth silent death in finalize** (#1049): a git-push auth failure
  (locked SSH key / expired credentials) now emits a `push_auth_failed`
  `blocked_on_user` blocker with a recovery hint instead of dying silently.
- **Rescued-finalize carried context** (#1050): the computed scope tier and
  `plan_source` are now written back onto the task after each stage, so a
  respawned finalize synthesizes a full PR body — and the large-tier idle
  budget (3600s) is finally fed instead of always falling back to 900s.
- **Concierge false-park recovery backoff** (#1030): dead-on-arrival
  detection plus backoff so an instant-death retry loop recovers
  mechanically without operator action.
- **Events inbox prune/rotate** (#856): `cw event prune` plus a doctor
  inbox-size check bound unbounded inbox growth.
- **`cw orchestrate status` recent-events flood + stale `last_stage`
  placeholder** (#854): the human renderer now aggregates consecutive
  `dispatch.tick` events in the "Recent events:" section (a new
  `--raw-events` flag restores the unaggregated stream), and the stale
  `last_stage` placeholder is replaced with a neutral, producer-agnostic
  message.
- **Stage-mismatch sentinel refusal** (#1031): the idle/local/signal_stop
  paths refuse a sentinel whose `stage_reached` disagrees with `task.stage`.
- **Adopt-assumption fast path** (#1032): self-answerable plan ambiguities
  are adopted inline instead of parking for the operator.

### Removed

- **Dead local task-queue (`cw queue`) and stale ROADMAP** (#1051).

## [1.16.0] — 2026-07-07

Post-RFC-0008 reliability wave: false-park elimination, a wrong-stage
sentinel guard, and headless hang mitigations — the operational gaps the
2026-07-03/05 sprint incidents exposed.

### Added

- **Stage-mismatch sentinel guard** (#1019): `_route_staged_decision` now
  validates the sentinel's `stage_reached` against `task.stage` via a new
  `_STAGE_REACHED_TO_STAGE` mapping table before routing; a late or
  replayed sentinel from a previous leg (the #986 incident class) is
  refused with a new `sentinel.stage_mismatch` event and a true no-op on
  the row. Missing `stage_reached` (legacy/`BlockedResult` payloads)
  bypasses the guard. A new `SentinelRouteOutcome` return contract is
  threaded through `_apply_sentinel_to_task` so `reconcile/phantom.py` no
  longer unconditionally completes the session on a refused route
  (orphaned-row hazard); the same gating for `idle.py`/`cli/sessions.py`/
  `local.py` is tracked in #1031.
- **Wall-clock liveness veto** (#976): the stalled sweep re-classifies
  transcript staleness fresh (via `_classify_liveness_bucket`, per-stage
  floors included) before proposing a wall-clock-budget park; a
  demonstrably-alive session is never parked or killed — a new
  side-effect-only `ProposedAction.PARK_VETOED` candidate emits
  `session.park_vetoed` instead. Applies to both AUTO and SIGNAL_ONLY
  lanes; the cap-exceeded park branch is unaffected.

### Fixed

- **Null-disposition BLOCKED_ON_USER parks** (#976): every operator-facing
  park now carries a disposition — `ReapCandidate.paused_status` is stamped
  in the idle/stalled `PARK_BLOCKED_ON_USER` branches, `_apply_queue_mutations`
  threads a disposition at all three call sites, and six bare park sites
  (gh-blocked, salvage, config-error fallbacks, terminal-sibling) are
  normalized. The concierge and escalation eligibility frozensets are
  extended to the new disposition strings so live auto-recovery and the
  45-minute operator-escalation latch keep their coverage.
- **Watchdog systemd unit fails 203/EXEC** (#1027): `cw watchdog install`
  now resolves the absolute path of the running `cw` executable
  (argv[0]-derived with a `shutil.which` fallback; hard error if neither
  resolves) into `ExecStart`/`ProgramArguments` instead of writing bare
  `cw`, which systemd user managers cannot resolve.
- **Body-folded pre-flight resolutions invisible to the plan scanner**
  (#980): `auto-dev-plan` now live-fetches the ticket body on every
  invocation (the #952 rule extended beyond comments) and treats a
  marker-bearing body resolutions section as an authoritative operator
  response channel; body markers are excluded from the multi-marker gate
  tally, and the Step 1c scan-exit comment gets a pinned
  `## Pending Verification Scan` header.
- **Dispatched workers can hang on interactive gh/git prompts** (#979):
  `native_daemon.py:_spawn_clean_env` now unconditionally sets
  `GH_PROMPT_DISABLED=1`, `GH_PAGER=cat`, `GH_NO_UPDATE_NOTIFIER=1`, and
  `GIT_TERMINAL_PROMPT=0` on every `claude --bg` spawn, and worker
  prose (`auto-dev.md`) now requires `timeout 120` on gh/git/curl calls
  and prohibits WebFetch of external docs in headless runs.

## [1.15.0] — 2026-07-07

RFC 0008 (orchestrator push channel) lands in full: queue-event producers,
the operator attention channel, subscribe-first monitoring docs, and the
gate-concierge capstone.

### Added

- **Gate concierge + durable escalation + `cw watchdog`** (#1015, RFC 0008
  capstone): `cw.reconcile.concierge` — a mechanical recovery reactor with
  three recipes (wall-clock false-park requeue, park-marker-poison clear,
  cancelled-row restore), gated behind a new global opt-in
  `OrchestratorConfig.concierge_enabled` (**default `false`**, per
  ADR-0006) with per-recipe `concierge_recoveries` flags
  (merge-with-defaults semantics — omitting a key does not disable it).
  `cw.reconcile.escalation` — a durable escalation latch
  (`escalation_parked_at`/`escalation_fired_at`, flat 45-minute threshold)
  over six judgment gates, emitting a latched `operator.escalation` event
  on the operator channel; `concierge.recovered` is audit-trail only. New
  `cw watchdog` CLI group (`tick`/`install`/`uninstall`/`status`) installs
  a session-independent systemd user timer (Linux) / launchd plist (macOS)
  dead-man's switch. Dev-queue schema v9 → v10.
- **Operator attention channel** (#1002, RFC 0008 W3): operator-relevant
  event filtering over the queue-events SSE channel, configured via
  `OrchestratorConfig.operator_channel_forward` and
  `_DEFAULT_OPERATOR_EVENT_TYPES`.
- **`task.transition` / `task.stage_changed` / `task.deleted` producers**
  (#1000, RFC 0008 W1): queue-row lifecycle events with disposition
  payloads, emitted from `transition_task_status` and friends.
- **`session.liveness_changed` producer** (#1001, RFC 0008 W2): a new
  `cw.reconcile.liveness` sweep classifies each live DAEMON session's
  transcript-mtime staleness into a latched `Session.liveness_bucket`
  (`live` / `stale_15m` / `stale_30m` / `stale_45m`), edge-triggering
  `session.liveness_changed` only on a bucket crossing. Per-stage floor
  overrides (`OrchestratorConfig.liveness_first_bucket_by_stage`) can raise
  the entry-point threshold for a pipeline stage without renaming or
  reassigning the global bucket labels — see `docs/events.md` for the
  floor-suppression semantics. Pure observation: no disposition, no queue
  mutation. `CW_STATE_SCHEMA_VERSION` bumped 12 → 13.

### Fixed

- **Detached-HEAD main checkout misclassified** (#964): dispatch's freshness
  gate now surfaces `freshness_detail=main_detached_head` (with accurate
  checkout advice in the WARN and `dev-queue status` subline) instead of
  falling through to the generic `main_behind_origin` label.
- **Release-tag workflow skipped on squash merges** (#1009): the
  `chore(release):` commit-subject guard now tolerates the PR-number suffix
  GitHub appends on squash merge; v1.14.0 had to be tagged by hand because
  of this.

### Documentation

- **Subscribe-first monitoring** (#1003, RFC 0008 W4): the dispatch runbook's
  monitoring section now leads with operator-channel subscription
  (`task.transition` disposition payloads); the poll ladder is demoted to an
  explicitly labeled fallback. `cw-session-watch` Mode B and `cw-fanout`
  gate-closure consume the pushed events.

## [1.14.0] — 2026-07-06

**Observability sprint Phase 4 (RFC 0007 W2 push + deprecation) — sprint
close.** Zero-latency PR signal via GitHub-webhook push, latch-style
escalation for the silent repeated-skip classes, release tagging automated,
`cw orchestrate watch` formally deprecated. RFC 0008 (orchestrator push
channel) accepted as the follow-on design.

### Added

- **GitHub webhook push producer** (#930, #1005): `.github/workflows/pr-events.yml`
  POSTs `merged` / `ci_failed` / `review_received` wire events to
  `POST /pr-event` through an operator-run relay tunnel; the server verifies
  an HMAC signature (`CW_PR_EVENTS_HMAC_SECRET` env, `X-Cw-Signature:
  sha256=…`, fail-open with a startup warning when unset) and routes pushed
  observations through the same persist/diff/emit path as the poll layer
  (shared `apply_pr_state_observation` extracted from `pr_hydrate`) — push and
  poll dedupe against the same persisted `pr_state`, with a TOCTOU re-read
  fix from review. `COMMENTED` reviews emit without mutating
  `review_decision`; `mergeable` stays poll-only; unmatched PRs no-op.
  Runbook §10 documents the tunnel as operator infrastructure and the
  poll-covers-push-down degradation contract.
- **Consecutive-skip escalation counters** (#996, #1006, closes #974):
  per-client `consecutive_freshness_blocks` (override store) and per-session
  `consecutive_salvage_skips` (state schema v12) — latch semantics (one
  `session.needs_attention` per streak at threshold 5, reset on recovery),
  new `paused_status` values `freshness_gate_blocked` /
  `salvage_skip_escalated`, and a client-header attention badge on `cw board`
  so client-scoped signals are actually visible (the #940 silent freeze now
  pages the board within 5 ticks).
- **Tag-on-release-merge workflow** (#997, #1004): `chore(release): vX.Y.Z`
  commits landing on main are tagged and get a GitHub Release with the
  matching CHANGELOG section automatically — idempotent, version
  cross-checked against pyproject.toml, never force-moves a tag. This release
  is the first to use it.

### Deprecated

- **`cw orchestrate watch`** (#995, #998): prints a deprecation notice to
  stderr on every invocation before launching the board. Board-backed since
  #986 (v1.12.0); removal targeted for the release after v1.14.0. Use
  `cw board`. `cw watch` (the flat table) is unaffected.

### Documentation

- **RFC 0008 — orchestrator push channel** (#999): transition/liveness
  producers + server-side operator attention filter; implementation tickets
  #1000-#1003.

## [1.13.0] — 2026-07-05

**Observability sprint Phase 3 (RFC 0007 W3 — operator signoff gates)**:
`--signoff operator` reliably parks a ticket before ship, code-enforced in
dispatch regardless of worker scope classification (the #926 lesson).

### Added

- **Operator signoff gates** (#990, #991): `TicketTask.signoff` /
  `LaneConfig.signoff` (`Literal["operator"] | None`) and
  `OrchestratorConfig.default_signoff`, resolved ticket > lane > global at
  gate-check time by the new 3-tier `resolve_signoff` (extends the 2-tier
  `resolve_reap_policy` shape). New `QueueItemStatus.AWAITING_OPERATOR_SIGNOFF`
  routed from both REVIEW-exit branches of `_route_staged_decision`
  (`stage_complete` small tier and `review_pending_approval` small-downgrade),
  parked with disposition `signoff_gate`. `cw dev-queue approve` gains the
  clearance arm (advance to FINALIZE, no `gh` call — the ready-flip arrives
  with RFC 0005 C3/C2); large+signoff tickets take two approvals by design
  (scope gate, then ship gate — the first approve says so on stdout).
  `cw dev-queue add --signoff operator`; `signoff` in the tasks `--json`
  contract; dev-queue schema v9 with migration fill. Invalid signoff config
  raises loudly (no fail-safe coercion — coercing would silently disable the
  gate).
- **Occupancy consolidation**: new `OCCUPIED_LANE_STATUSES` constant replaces
  four inline `(RUNNING, BLOCKED_ON_USER)` literals (dispatch lane stats,
  board lane panel, reconcile rescue lookup, `lane rm` guard) — signoff-parked
  tickets hold their lane slot and count as occupying, never as "running";
  the lane breakdown line gains a `signoff=N` field. `cw dev-queue wait`
  treats the signoff park as terminal with a new distinct exit code;
  `requeue` accepts signoff-parked tickets (reject-via-regress lever);
  `move` refuses them; default `status` view shows them.

## [1.12.0] — 2026-07-05

**Observability sprint Phase 2 (RFC 0007 W1 — board consolidation)**: `cw board`
becomes the primary interactive read surface; tui.py shrinks to the `cw watch`
stack per the operator-decided usefulness bar.

### Added

- **Board PR/CI column, session age, attention badges, bounded event feed**
  (#985, #987): per-ticket PR/CI cell rendered from persisted
  `TicketTask.pr_state` (no subprocess in the render path); session-age column
  (session `started_at` → `created_at` fallback); attention/park badges from
  the event bus with first-match precedence `reap_proposed` >
  `needs_attention` > `pr_state.attention_state`, joined on `ticket_id` with
  the #857 bounded-window pattern; new global event-feed panel that aggregates
  consecutive `dispatch.tick` runs (`dispatch.tick ×N over Xm`) *before*
  tailing to the display limit so tick bursts cannot evict real signal
  (absorbs the #854 tick-flood slice); `cw board --raw-events` restores the
  raw stream; `--client` scopes the feed.
- **`cw board --detail`** (#986, #988): session-grouped toggle panel with
  worktree-contention column (`⚠×N` when ≥2 sessions share a path), built on
  the new non-display `cw.session_groups` module (extracted from tui.py per
  RFC 0007 resolved Q4); orchestrate summarizers promoted to public.

### Changed

- **`cw orchestrate watch` repoints to the board** (#986): the client-grouped
  dashboard renderer and its helpers are deleted from tui.py (837 → ~420
  lines; git history is the archive); `cw watch` keeps the flat session table;
  `cw orchestrate status --json` contract unchanged; `--compact`/`--verbose`
  options removed with the DetailLevel machinery. Formal deprecation notice for
  `cw orchestrate watch` lands in Phase 4.
- **`docs/dispatch-runbook.md` routine status reads point at `cw board`**
  (#986); `cw dev-queue status` remains documented for scripting/parseable
  reads (RFC 0007 resolved Q3).

### Documentation

- 2026-07 sprint operational lessons encoded into `cw-fanout` Step 4 and the
  dispatch runbook §7 — liveness-first monitoring, gate workarounds, recovery
  recipes (#984).

## [1.11.0] — 2026-07-05

**Observability sprint Phase 1 (RFC 0007 W2 poll layer + W4 push completion)**:
the orchestrator stops re-deriving PR state every prompt, and workers can push
validated completion results instead of relying solely on transcript parsing.

### Added

- **PR-state hydration in the serve tick — first `pr.*` event producer**
  (#929, #981): throttled (default 150s) `gh pr view` hydration for dev-queue
  tasks with open PRs; persisted `TicketTask.pr_state` (dev-queue schema v8);
  attention-state decision table (ported `_summarize_status_checks`, rows
  5a-5c distinguishing BLOCKED-waiting-on-CI from genuinely approvable);
  `pr.merged` / `pr.ci_failed` / `pr.review_received` / `pr.mergeable` emitted
  on transitions diffed against persisted state (`pr.merged` also fires on
  first observation so late-enqueued tasks still retire). `retire_merged_prs`
  begins actually firing. `cw dev-queue tasks` gains `pr_state` (JSON) and an
  ATTENTION column (human); `status` gains per-client NEEDS_ATTN counts.
- **`cw result emit` — push-based completion** (#536 Phase 1, #977): workers
  push the full `AutoDevResult` through a strictly-validated CLI (reusing
  `cw result validate`, inheriting the A6 empty-ambiguity invariant); on
  success `session.last_result` is written synchronously under the sessions
  lock. The Stop hook remains the completion-event source but now defers to
  an emitted terminal result instead of overwriting it; the phantom-reconcile
  path gains the same gate so an emit-then-crash session is never re-salvaged
  over its authoritative result. Transcript sentinels are demoted to forensic
  fallback. `_seed_daemon_session` promoted to conftest for shared test use.

## [1.10.0] — 2026-07-04

**Observability sprint Wave 0**: worker-context correctness (the RFC 0007 W4
data-integrity floor), the per-lane circuit breaker, and defense-in-depth
against worker escapes onto the operator's main checkout.

### Added

- **Per-lane circuit breaker on consecutive spawn errors** (#875, #969): a
  lane that accumulates consecutive `spawn_error`s pauses instead of grinding
  a ticket to the attempt cap. Manual `cw lane resume` resets the
  consecutive-error counter (operator decision: resuming asserts the
  underlying problem is fixed). No auto-resume.
- **Main-checkout escape defenses** (#940, #972): new `cw guard-cwd`
  subcommand wired as a `PreToolUse` hook that blocks worker Bash calls whose
  cwd resolves to the client `workspace_path`; `resume_session` respawn guard
  (DAEMON-origin with no worktree hard-fails; USER worktree-purpose sessions
  get `check_not_main_checkout`); new `main_drift` reconcile sweep emitting
  `session.needs_attention` per tick while a live session's client checkout
  is dirty/ahead with the session homed elsewhere; freshness-gate divergence
  message now gives inspect-first advice instead of `pull --rebase`.
- **`cw event record session.needs_attention`** (#952, #965): the attention
  event type workers are instructed to emit on comments-fetch failure is now
  accepted by the record CLI.

### Fixed

- **Workers no longer plan against stale ticket context** (#952, #965): the
  plan stage live-fetches issue comments on every invocation (including
  re-dispatch rounds) and pins the resolutions-marker grep to that live fetch;
  intake's single-ticket fetch now requests comments explicitly. Root cause of
  the #949 three-round resolution-blindness incident; RFC 0007 W4's original
  suspect corrected in the RFC.
- **Empty ambiguity items can no longer park a ticket with nothing to answer**
  (#953, #966): strict model-layer rejection of question-less ambiguity items
  (the future `cw result emit` in-turn error), with the parse boundary
  filtering empty items and coercing all-empty arrays to a labeled synthetic
  placeholder that parks visibly. Contract invariant A6; no schema bump.
- **Event-follow staleness guard deduplicated and hardened** (#954, #971):
  `tail_events_follow` and `wait_for_event` share one offset-tracking guard;
  the unreachable size-decrease case logs and continues instead of silently
  misbehaving. Delivery contract unchanged.

## [1.9.1] — 2026-07-03

### Fixed

- **Deterministic `cw event tail --follow` test** (#948, #959): the flaky
  follow-mode test's fixed 2-iteration sleep budget is replaced with a
  poll-until-observed loop (spy on the streaming seam), tolerating the
  `tail_events_follow` st_size guard lag tracked as #954. Test-only change.

## [1.9.0] — 2026-07-03

The **reliability bug sprint, waves 2–3 (P0 correctness)**: every P0 from the
109-issue triage is now shipped. The pipeline can no longer emit a
false-positive review PROCEED for plan-contradicting work, silently bypass the
operator's scope gate, park still-working sessions without rescue, or lose an
approved plan between stages.

### Added

- **Late-sentinel rescue for parked sessions** (#918, #946): a terminal
  `AutoDevResult` recorded by the Stop hook on an already-parked task now
  rescues it through a shared routing helper that mirrors live
  `apply_staged_decision` semantics (including FINALIZE self-heal regress and
  terminal-stage `pr_url` preservation). Large-tier idle budget default
  raised to 3600s. `BlockedResult` arms stay RUNNING-gated — an operator park
  is never reversed by an unparseable late signal.
- **Non-deferrable review findings + `requeue --regress`** (#917, #955): a
  finding that the implementation contradicts the approved plan (or an
  operator mandate) cannot be adjudicated away — review exits `blocked` with
  `blocker.reason: "plan_deviation"`. Spec-citation claims ("required by
  spec") are cross-checked against `.cw/plan.md` verbatim. New optional
  `review.deferred` sentinel field surfaces deferral counts. New
  `cw dev-queue requeue --regress` permits a backward stage target on a
  blocked task — the operator's bounce-to-impl path.
- **`cw init --no-onboarding` runnability warning** (#922, #945): init now
  warns the client is not runnable until `--onboard-only`, and the Next-steps
  block points at onboarding first.

### Changed

- **Operator scope gate is escalate-only** (#926, #950): the gate fires when
  EITHER the operator's `--scope large` hint OR the worker's sentinel tier
  says large — a worker reclassifying to small can no longer bypass the
  operator's primary safety gate. Budget resolution unchanged.
- **Pre-flight Resolutions are binding plan constraints** (#828, #951): the
  plan stage extracts the single authoritative resolution set (body or
  comment marker), refuses multi-marker accretion, and emits a per-item
  `## Pre-flight Resolution Conformance` section the Plan Reviewer enforces.

### Fixed

- **Plan stage persists `.cw/plan.md`** (#943, #944): Stage 1 writes the
  reviewed plan to the worktree before posting/sentinel; Stage 2 falls back
  to the newest reviewed tracker comment before exiting `plan_missing`. The
  latent gap became fatal with instruction-literal worker models.

## [1.8.0] — 2026-07-02

The **reliability bug sprint, waves 0–1**: stabilized the CI gate, hardened
the review stage's sentinel contract, and closed a cluster of dispatch /
reconcile / doctor correctness gaps found by a full 109-issue triage. Also
lands the RFC 0005 F3 async LocalExecutor spawn.

### Added

- **Async `LocalExecutor.spawn()` + local-harvest crash recovery** (#888,
  #920): aider launches fire-and-forget via `Popen`; reconcile's local
  harvest completes the session when the process exits, including after a
  cw crash.
- **`cw spawn close --confirmed-dead`** (#928, #938): flag-distinguished
  close for provably-dead cross-session workers, so an auto-mode allow rule
  can authorize cleanup without weakening bare `spawn close`.
- **argv regression guard for `claude --bg` spawns** (#736, #933): asserts
  the prompt stays the trailing positional at both spawn chokepoints
  (`spawn_create_impl`, `resume_session`) — the #716/#731/#733 silent-idle
  regression class now fails tests instead of burning 30-minute sessions.
- **`wedge/active-no-daemon-entry` doctor class** (#925, #939): `doctor
  --reap` detects and clears ACTIVE/IDLE sessions whose daemon entry is gone
  (crash without sentinel), releasing the hook-context lock.

### Changed

- **Review-stage sentinel hardening** (#916, #932): `scope.tier` carries
  forward from the plan sentinel (no more small→large flip-flop loops) and a
  pre-exit dirty-tree invariant stops the review session exiting with
  uncommitted fix-loop work.
- **`_local_preflight` returns a discriminated union** (#919, #934):
  `_PreflightOK` NamedTuple replaces the tuple-with-dead-`or ""`-guards
  shape; callers narrow via `isinstance`.

### Fixed

- **Flaky `test_cli_event_tail_client_filter_comma_separated`** (#913,
  #931): hex-compatible session ids in negative assertions made the test
  order-dependent; the CI gate is deterministic again.
- **`session.completed` without a PR now emits `session.needs_attention`**
  (#923, #935): plan-stage parks are no longer invisible to operators.
- **Stale PENDING dedup** (#876, #937): reconcile parks (auto: cancels) a
  PENDING task whose ticket already has a globally-terminal sibling row —
  scoped strictly to `{COMPLETED, CANCELLED}` so multi-stage tickets and
  legitimate retries are never false-reaped.
- **Finalize double-fire** (#912, #941): doctor's BLOCKED_ON_USER collapse
  skips tasks that already carry a `pr_url`, killing the re-dispatch path
  that produced redundant empty PRs.

## [1.7.0] — 2026-06-30

The **local-model validation** sprint (RFC 0005 executors): the exit bar was
met — a debt-tier ticket (#889) walked `plan(claude) → impl(local/qwen) →
review(claude) → merged PR` on a lane pinned to the local backend. Alongside
the validation, this release hardens dispatch reliability (merge_pending,
review-gate harvest), adds a Codex review-only runner seam, tightens the aider
subprocess environment, and makes a silently dispatch-blocked client visible in
`cw status`.

### Added

- **Codex review-only runner seam** (#627, #904): `CodexRunResult` +
  `CodexRunner` + `FakeCodexRunner` — the foundation for a review-stage
  executor backed by the hosted Codex CLI, parallel to `LocalExecutor`.
- **`merge_pending` status** (#899, #901, #903): a PR-created-but-awaiting-CI
  run is coerced at the parse boundary (`status=blocked` + non-null `pr` →
  `merge_pending`) and routed to `BLOCKED_ON_USER` with its PR URL preserved,
  instead of being recorded `failed`.
- **Freshness-gate surfacing in `cw status`** (#908, #910): the headline
  `cw status` now shows a "Freshness gates (action required):" section listing
  each dispatch-blocked client with the reconcile hint, so a silently gated
  client is visible without parsing `cw dev-queue status`.

### Changed

- **Constructor-inject `StageExecutorConfig` into `ClaudeNativeExecutor`**
  (#887, #905): closes the E1 double-resolution — the executor receives its
  resolved config instead of re-resolving it.

### Fixed

- **Review-gate sentinel harvest** (#892, #907): `_parse_any_sentinel_from_transcript`
  now falls back to the surface_ref transcript when the csid transcript yields
  no sentinel, so a `review_pending_approval` emitted by a still-alive worker is
  harvested instead of being recorded `needs_salvage` (which had blocked
  `cw dev-queue approve`).
- **Pipeline model defaults + recommended-defaults docs** (#900).

### Security

- **aider subprocess environment allowlist** (#891, #906): `build_env()` now
  forwards only an allowlisted set of environment variables to the aider
  subprocess instead of the entire parent environment (which included secrets);
  adds an `aider_available()` seam.

### Documentation

- **Dispatch runbook — freshness-gate diagnosis** (#908, #909): §9.1 gains a
  symptom-first rule ("dispatcher up but nothing dispatching → check
  `skip_reason` before suspecting the monitor"), and §9.3b documents
  diverged-main caused by release/merge artifacts rather than a worker leak.

## [1.6.0] — 2026-06-29

The **executor infrastructure** sprint (RFC 0005): `cw` gains a pluggable
`Executor` abstraction so stages can run via aider + a local OpenAI-compatible
endpoint instead of Claude Code, with per-lane override support. Alongside it:
spawn-error exponential backoff, an in-process `cw dev-queue serve` supervisor,
prior-attempt context on retry, and a version-drift self-check exception.

### Added

- **LocalExecutor backend** (RFC 0005 F3, #866): aider-based executor that
  targets a local OpenAI-compatible endpoint, fully integrated with the
  per-stage resolution path.
- **Per-stage executor resolution + lane override** (E1, #874): each dispatch
  stage resolves its executor independently; lanes can pin a non-default
  executor via config.
- **`cw dev-queue serve`** (#871): in-process supervisor that runs the dispatch
  loop with configurable backoff restart on failure — replaces the bare
  `dispatch run` call for long-lived operator sessions.
- **`prior_attempts_summary` on retry** (#872): workers spawned for a retry
  receive a structured summary of all previous attempts, giving the model
  context on what was tried before.
- **`VersionDriftExit` exception** (#880): dispatch loop self-check raises a
  typed exception when it detects it is running a stale version, allowing the
  supervisor to restart cleanly.
- **spawn_error exponential backoff** (#879): failed spawn attempts back off
  with jitter before retry, preventing tight error loops on model/API outages.

### Changed

- **`merge_gate` blocker reason is now a static constant** (#881): removes the
  last inline string literal from the finalize path; finding is now
  grep-able across the codebase.
- **Subagent models pinned explicitly in `/auto-dev` pipeline** (#877, #220):
  each stage's spawned subagent declares its model tier rather than inheriting
  the operator's default, cutting unnecessary Opus fan-out.

## [1.5.0] — 2026-06-26

A **dispatch-hardening** release: a global attempt ceiling kills dead-requeue
churn, the finalize path gains a regress route for gate failures, and the
attention digest is scoped to live sessions only. Plus file-overlap merge gate
and several targeted fixes from the v1.4.0 stability wave.

### Added

- **Global attempt ceiling** (#850, #786): dispatch stops requeueing a ticket
  once it has hit the configured max-attempts threshold — prevents dead tasks
  from churning indefinitely.
- **finalize → IMPL regress path** (#858, #770): when a gate check fails during
  finalize, the task reverts to IMPL rather than wedging in an unrecoverable
  state.

### Changed

- **Attention digest scoped to live sessions** (#857): `orchestrate watch`
  attention column is now bounded to the recent window and live sessions only,
  eliminating noise from stale/completed sessions.
- **heartbeat + sentinel promoted to `SessionSummary`** (#845): moved from
  inline columns to the shared summary model so both `cw status` and
  `orchestrate watch` render them consistently.

### Fixed

- **File-overlap merge gate** (#849, #777): finalize now blocks on overlapping
  file edits between the worktree and main, with a null-safe blocker field for
  the `merge_gate_blocked` case.
- **cw-fanout blocked_on_user gate loop** (#860): the wave-monitoring loop now
  closes correctly when a task enters `blocked_on_user` — previously it could
  spin indefinitely.
- **gh_blocked disposition** (#846): the gh-blocked idle-park transition now
  passes its disposition through `transition_task_status` like all other
  terminal branches.

## [1.4.0] — 2026-06-23

The **live work dashboard** sprint: `cw orchestrate watch` gains the signals it
was missing, on top of a single audited status-transition seam (ADR-0010,
ADR-0011). Plus a dev-queue queue-view cleanup, the reaper's branch-absence
diagnostic (ADR-0009), and a fix to requeue context staleness.

### Added

- **Storm-deduped attention indicator** on `orchestrate watch` (#537): repeated
  `session.needs_attention` collapse into one actionable row per
  `(session, condition)` with an affected-session count — readable during a
  reconcile storm.
- **Heartbeat + sentinel columns** on the sessions table (#833):
  transcript-freshness age and the session's paused/sentinel status.
- **CI / mergeable column** on the monitored-PRs table (#834).
- **Terminal disposition column** on `cw dev-queue tasks` (#310): `shipped` /
  `no_op` / blocker reason per terminal row, via new `TicketTask.disposition` /
  `pr_url` / `completed_at` fields (dev-queue schema v4 → v5).
- **`transition_task_status` seam** (#835): the single authority for every
  `TicketTask` status change (ADR-0011).
- **Branch-absence diagnostic** on `SESSION_TIMED_OUT` (#808): a nullable
  `branch_state` annotation; never inferred as completion (ADR-0009).

### Changed

- **`cw dev-queue status` defaults to active-only** (#308): the TICKETS column
  shows PENDING/RUNNING/BLOCKED_ON_USER by default; `--all` restores the full
  list. Counts are unchanged.

### Fixed

- **Requeue context staleness** (#837): `requeue` reused the worktree's
  `.cw/context.json` materialized at first dispatch, so operator resolutions
  added between requeues never reached the worker. Intake now stamps
  `materialized_by_session` and re-fetches the ticket on a new session.
- **gh-blocked park disposition** (#842): the gh-blocked idle-park branch now
  stamps its disposition like its siblings, plus added stamping test coverage.

## [1.3.2] — 2026-06-20

A **hotfix** release closing the root cause of the dispatch worktree-leak
class of bugs (#766) that the v1.3.1 round only mitigated downstream.

### Fixed

- **GIT_* env leak into spawned workers** (#766, #790): `RealNativeDaemonClient`
  spawned `claude --bg` workers with no `env=`, so workers inherited the
  orchestrator's `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE`. Git honors
  those over `cwd`, redirecting every worker git operation to the
  orchestrator's `.git` / index — producing both a FILE leak (worker edits
  appearing uncommitted in the main checkout) and a COMMIT leak (worker
  commits landing on local `main`, diverging it from `origin` and wedging the
  dispatch loop's `--ff-only` auto-fast-forward). Workers now spawn with a
  GIT_*-stripped environment via a new `_spawn_clean_env()` helper.

### Documentation

- **#766 leak-recovery + #774 manual-finalize runbook procedures** (#788):
  added operator procedures to `docs/dispatch-runbook.md` for recovering from
  a leak and manually finalizing a stuck session.

## [1.3.1] — 2026-06-20

A **dispatch-reliability + observability/inspection CLI** release. This patch
round closes the remaining rough edges from the v1.3.0 reliability sprint and
ships the first wave of operator-facing inspection commands (`cw event
tail/wait`, `cw session show/list/wait/result`, `cw queue peek`) that make
it possible to observe a running dispatch loop without reaching into state
files by hand.

### Added

- **`cw event tail`** (#769): stream events from the history log; supports
  `--follow` for live streaming, `--since`, `--json`, and `init_cursor_at_end`
  for efficient tailing without replaying history.
- **`cw event wait`** (#275, #773): block until a matching event arrives,
  enabling scripted polling for CI integration and operator runbooks.
- **`cw session show / list / wait / result`** (#779): four inspection
  subcommands for a running or completed session — show full session detail,
  list all sessions with filter/sort, wait until a session reaches a terminal
  state, and print the final result sentinel.
- **`cw queue peek`** (#778): promote the ad-hoc `cw_queue_peek.py` script to
  a proper `cw queue peek` subcommand for inspecting the dispatch queue.
- **`LANE_CAP_BLOCKED` skip reason** (#588, #775): dispatch now records a
  dedicated `LANE_CAP_BLOCKED` skip reason when a lane is full but not
  otherwise blocked, giving operators a precise signal vs. the generic skip.
- **`SESSION_STAGE_TIMED_OUT_RETRIED` event** (#724, #785): reconcile emits
  this event before applying route policy on a genuine stage timeout, providing
  an auditable record of every retry decision.
- **`HeadNotOnDefaultBranchError`** (#761): new `WorktreeError` subclass for
  the "HEAD not on default branch" condition, enabling callers to handle it
  specifically without string-matching the exception message.
- **`is_documented_example` sentinel field** (#771): `auto_dev_result` parser
  rejects placeholder sentinels that set `is_documented_example: true`,
  preventing no-op completions from being accepted as real results.

### Fixed

- **Dirty-worktree push notification storm** (#763, #767): edge-trigger the
  dirty-worktree notification instead of level-triggering it — the notification
  is now emitted once on transition to dirty, not on every reconcile tick while
  the worktree stays dirty.
- **`dev_queue_lock` deadlock on `record_event`** (#765): `record_event` was
  called inside `dev_queue_lock` in `_collapse_blocked_on_user_tasks`, creating
  a potential deadlock when the event writer also acquires the lock; the call is
  now outside the lock.
- **Lane validation in `add_ticket`** (#760): `add_ticket` now validates the
  lane name and raises a clear error on unknown lanes, preventing silent ticket
  starvation when a caller supplies a misspelled lane.
- **World-state timeout check** (#315, #776): the session-timed-out check now
  consults the world state (last-seen timestamp) before declaring a timeout,
  reducing false positives for sessions that are alive but quiet.

### Chore / Docs

- Gitignore local artifacts: `.claude/worktrees/`, `scheduled_tasks.lock`,
  `prep-pr-state.json`, `docs/handoffs/` (#759).
- README and RFC/ADR status fields updated to reflect current reality (#755).

## [1.3.0] — 2026-06-18

A **pipeline-reliability hardening** release. The v1.2.0 staged engine was
dogfooded hard; this release fixes the silent-failure classes that surfaced —
each of which could waste a worker, lose completed work, or mislabel a ticket —
and adds `cw worktree gc` for squash-merge-aware worktree cleanup. Several of
these fixes were found and shipped by the staged pipeline dogfooding itself.

### Added

- **`cw worktree gc`** (#630): prune cw-managed worktrees whose branch PR is
  MERGED, determined via **PR state** (`gh pr list --state all`) rather than
  `git branch --merged` — which misses every squash-merged branch and let
  worktrees accumulate unbounded. Dry-run by default; `--apply` to act;
  `--include-closed` opt-in. Skips locked/bare worktrees and never force-removes
  a worktree with unsaved/unpushed work.

### Fixed

- **Stage-advance sentinels from exited workers were dropped** (#716): a staged
  worker emits `stage_complete` and exits, so the phantom path handled it with
  terminal-only salvage and never advanced the stage — every dispatch paid a
  ~21–26 min/stage wall-clock-timeout tax. The phantom path now routes the
  emitted advance sentinel through `apply_staged_decision`.
- **Tool-emitted sentinels were invisible to the transcript scan** (#731): a
  worker that emits the `AUTO_DEV_RESULT` block via a Bash `cat` lands it in a
  `tool_result` block, which neither `signal_stop` nor reconcile scanned (both
  read assistant-text only) → no routing → stall. Both paths now scan
  `tool_result` content.
- **`--disallowed-tools` swallowed the worker prompt** (#733): the variadic flag
  was passed as two tokens immediately before the positional prompt, consuming
  it — workers launched promptless and did nothing. Now passed as a single
  `--disallowed-tools=<pattern>` token.
- **A near-miss `stage_reached` failed the whole sentinel** (#748): an
  off-contract stage label (e.g. `stage4_pr_creation`) hard-failed validation
  and discarded completed work; it now coerces to the canonical stage by
  stage-number prefix (informational field), while genuine garbage still rejects.
- **An unknown-status sentinel was marked COMPLETED** (#750): an unparseable /
  unknown-status `BlockedResult` fell through to `COMPLETED`, silently retiring
  unshipped work as "shipped". It now routes to `FAILED` — terminal, but never
  false success.
- **Spawn flake went undetected for ~30 min** (#520): a `claude --bg` spawn that
  returned a short id but never registered in the daemon roster was marked
  RUNNING and only caught by the idle watchdog. Spawn now verifies roster
  registration and fails fast.
- **github-issues workers could stall on Linear OAuth** (#726): the Linear MCP
  is withheld (`--disallowed-tools`) from headless workers when the tracker is
  github-issues, so a Linear-flavored ticket can't trigger an unanswerable
  headless OAuth prompt.
- **`prep-pr` titled the PR from the trailing chore commit** (#722): on
  squash-merge this became a misleading permanent `main` subject. Title now uses
  tiered selection (first substantive commit / ticket title), skipping trailing
  lockfile/chore commits.
- **`dev-queue wait` rode to exit 124 on a mid-wait reap** (#542): a session
  reaped mid-wait (task → PENDING, `session_id` cleared) was indistinguishable
  from the spawn window and never surfaced; it now returns ATTENTION.
- **Branch-key merged check hardcoded `dev/`** (#728): the merged-PR check used a
  literal `dev/` prefix, breaking for any client with a custom
  `feature_branch_prefix`; it now resolves the prefix per-session from the SSOT.
- **Supervisor session continuity was never verified** (#519): reconcile now
  compares `Session.claude_session_id` against the supervisor's
  `resumeSessionId` and clears stale continuity on mismatch.

## [1.2.0] — 2026-06-16

The RFC 0005 **staged pipeline engine** goes live (milestone #8, Phase 1). The
forward-compat seams from v1.1.3 are now wired: `dispatch_tick` spawns one
session per stage (`/auto-dev-{plan,impl,review,finalize}`) and a stage-advance
machine drives the ticket PLAN→IMPL→REVIEW→FINALIZE→COMPLETED. Validated
end-to-end by dogfooding: the engine autonomously planned, implemented,
reviewed, shipped, and auto-merged a real ticket (#475 via PR #706).

### Added

- **Staged dispatch engine — RFC 0005 B2** (#617): `dispatch_tick` spawns
  per-stage via `ClaudeNativeExecutor` (no monolith prompt); `_stage_advance` +
  the scope-gated advance decision in `_apply_events_to_store`; `stage_base_ref`
  stamped at spawn and cleared on advance.
- **`stage_complete` sentinel status** (#699): a PR-less intermediate
  stage-success status so IMPL can advance to REVIEW. (PLAN/REVIEW advance via
  the scope-gated `*_pending_approval` statuses; IMPL previously had no valid
  success status and mis-emitted `shipped`, which the schema rejects without a
  PR.)
- **`cw dev-queue run --client/-c`** (#663): scope a dispatch tick to a single
  client's queue instead of ticking all clients.
- **Skill distribution on install** (#473 follow-on): `install.sh` now syncs
  `.claude/commands/` *and* `.claude/skills/` into `~/.claude/` on every
  install/upgrade, with a manifest-scoped prune that removes only paths cw
  itself installed — foreign skills are never touched. cw-coupled commands and
  skills that previously lived only in `~/.claude` (queue-issues, graduate-plan,
  review-sweep, setup, cw-session-watch) are migrated into this repo as the
  single source of truth.

### Fixed

- **Staged advance machine was unreachable in production** (#698): reconcile's
  emitted-sentinel router (`_apply_sentinel_to_task`) routed completed-stage
  tasks with the stale monolith mapping *before* the advance machine ran,
  blocking every staged ticket at PLAN. The advance decision is extracted into
  `apply_staged_decision` and shared by both the consume and reconcile paths.
- **Scope-gated advance ignored a null `scope.tier`** (#696): a real PLAN
  sentinel can emit `scope.tier=null` pre-impl; the advance machine now falls
  back to `task.scope_hint` (mirroring reconcile's tier resolution) instead of
  blocking small tickets.
- **Sentinel persisted after the advance decision** (#694): `last_result` is now
  written before the advance decision in `consume_completed_sessions` (forward-
  compat for stdout-carrying completion events).
- **Install served a stale cached wheel** (#473): `install.sh` now passes
  `--reinstall --no-cache`, so a code-changed/version-unchanged rebuild is never
  skipped.
- **Headless worker isolation guard** (#402): `auto-dev-impl.md` codifies that a
  headless worker's git operations target only its worktree
  (`cw-context.json:worktree_path`), never the operator's main checkout; the
  cw-side `worktree_path` injection landed earlier in `spawn.py`.

### Changed

- **`cli.py` split into a `cli/` package** (#705): the 3568-line module is broken
  into focused submodules; no behavior change.
- **Docs**: tracker-descriptor seam (ADR 0008), review-system-in-cw design
  (RFC 0006), and the stage ledger (#692).

## [1.1.3] — 2026-06-14

RFC 0005 forward-compat seams (milestone #7). Additive, dormant data-model and
executor scaffolding so a later staged-pipeline engine (v1.2.0) can be wired in
without another schema migration. No dispatch behavior change.

### Added

- **Stage data-model + schema bump** (#612): `Stage` enum, `StageExecutorConfig`,
  `StagePipelineConfig` (with a `LaneConfig.pipeline` lane override and
  `ClientConfig.pipeline`), and dormant `TicketTask.stage`/`stage_base_ref` +
  `Session.stage` fields. Bumps `CW_STATE_SCHEMA_VERSION` 9→10 and
  `DEV_QUEUE_SCHEMA_VERSION` 3→4 with store-level migrations that default the new
  fields on load (verified against real on-disk v9/v3 state).
- **`StageExecutor` seam** (#613): a `@runtime_checkable` Protocol plus
  `ClaudeNativeExecutor` wrapping `spawn_create_impl` + the native daemon
  (forwards `--model`). Unwired — not yet called by dispatch.
- **`cw schema stage-output <stage>`** (#614): exposes the per-stage sentinel JSON
  schema (the validation contract foreign executors will consume).
- **`.cw/` worktree exclude** (#615): worktree creation idempotently registers
  `.cw/` in `$GIT_COMMON_DIR/info/exclude`, never touching the consumer's
  committed `.gitignore`.

## [1.1.2] — 2026-06-14

Dispatch-reliability cluster — worker liveness, re-dispatch safety, and
disposition visibility (the 1.1.x reliability backlog; all share the
on-demand-reconcile staleness root captured in ADR-0007).

### Fixed

- **`claude_session_id` backfilled at spawn-return** (#635): `spawn_create_impl`
  now attempts `_csid_from_transcript` before `save_state`, so a live worker's
  session id is populated immediately instead of staying null until the next
  operator-triggered `reconcile()` (hours later). Makes liveness detection
  (csid→transcript→silence) actually work for fresh workers.
- **Shipped tickets are no longer re-dispatched** (#637): before reverting a
  RUNNING task to PENDING, reconcile consults world state — if the ticket's PR
  is merged, the task is marked completed instead of reverted+re-dispatched. The
  PR-merge pre-pass runs outside `sessions_lock` and the guard covers all three
  revert sites (`_act_on_phantom_candidates`, `_act_on_stalled_candidates`,
  `_act_on_idle_candidates`). Closes the data-safety hazard that re-ran an
  already-merged ticket under `reap_policy: auto`.
- **Approval-pending sessions are surfaced, not hidden** (#633):
  `plan_pending_approval` / `review_pending_approval` now map to
  `BLOCKED_ON_USER` (were silently recorded as `completed` with null
  disposition); `cw dev-queue status` gains a `BLOCKED` column.

### Docs

- **ADR-0007 — reconcile cadence and ownership** (#639): on-demand vs
  background-ticker vs daemon-primary-runner; recommends an opt-in periodic
  ticker (Option B2) with a cron fallback, daemon promotion deferred.
- **#636 known limitation** documented in `auto-dev.md` Step 4c: headless
  `gh pr create` is blocked by the `auto` permission classifier inside the
  worktree-isolated `/prep-pr` subagent; the effective fix (non-`auto` worker
  `permission_mode`) is deferred to the RFC 0005 FINALIZE/REVIEW stages
  (#621/#622), which carry the requirement.

## [1.1.1] — 2026-06-13

Dispatch/reconcile reliability and packaging-gate hardening (Sprint 0 follow-ons to RFC 0004 Phase 4).

### Added

- **CI `package-smoke` job** (#611): builds the wheel (`uv build`), installs it
  into a clean env (`uv tool install --no-cache`), and smoke-tests the installed
  `cw` — including `cw guide` to assert the `GUIDE.md` data file is packaged.
  Closes the gap where a packaging break (e.g. #609) passed CI and only failed
  at `uv tool install`.
- **`SESSION_REAP_AUTHORIZED` audit event** (#603): `_reap_session_by_selector`
  now emits `session.reap_authorized` after the destructive reap, recording
  authority (`operator` vs `orchestrate-run`), lane, and the proposed action —
  closing the auditability gap under the automated 4c consumer (ADR-0006).

### Changed

- **`cw dev-queue status` footer** (#598): the "Last dispatch tick per client"
  block is now labelled as a historical snapshot, not live state; added a
  "Reading the status output" section to the dispatch runbook.

### Fixed

- **Worktree fetch-fail log spam** (#597): an unreachable client `origin` no
  longer floods every dispatch tick with a multi-line git `fatal:` block —
  stderr is collapsed to one line and de-duplicated per client per run
  (caller-owned warn set, mirroring `warned_stale`).
- **Graceful `Ctrl-C` on `cw orchestrate run`** (#604): the poll loop now exits
  130 with a one-line "stopped" message instead of a raw `KeyboardInterrupt`
  traceback; cursor state stays clean (idempotent replay, no cursor flush).
- **Version single-sourced from package metadata**: `cw.__version__` is now
  derived via `importlib.metadata` instead of a hardcoded literal, so
  `cw --version` / `cw doctor` can never diverge from `pyproject.toml` again.
  (The first `v1.1.1` tag's release build correctly failed its version-match
  guard because the literal was missed — this removes that failure class.)

### Docs

- **RFC 0005 — staged pipeline & heterogeneous executors** (#629): the
  pipeline-axis design (stage as a first-class concept; `StageExecutor` seam).
  v1.1.x ships only the forward-compat seams; the engine lands in v1.2.0.

## [1.1.0] — 2026-06-12

Lane-aware dispatch, gated reaping, and state-integrity hardening.

### Added

- **RFC 0004 lane system — Phases 1–4** (#575/#576/#577/#581): First-class
  `lane` routing throughout the dispatch pipeline.
  - **Phase 1** (#575): `TicketTask.lane` field (default `"default"`);
    DEV_QUEUE_SCHEMA_VERSION v3.
  - **Phase 2** (#576): Two-tier lane-aware scheduler — per-lane concurrency
    caps, pause/resume, priority ordering.
  - **Phase 3** (#581): `cw lane` CLI (`create`, `pause`, `resume`, `list`,
    `show`, `set-concurrency`); `--lane` flag on `cw dev-queue add`; runtime
    concurrency overrides persisted in `ConcurrencyOverrides` outside
    `orchestrator.yaml`.
  - **Phase 4** (#577): Lane-aware `cw status` / dashboard grouping; consumer
    audit.
- **`SESSION_REAP_PROPOSED` event + gated-act authorization** (#573): reconciler
  emits `session.reap_proposed` before any destructive reap; the owning task is
  routed to `BLOCKED_ON_USER` under the default `signal_only` policy.
- **`ReapPolicy` enum** (#572): `signal_only` (default) keeps surfaces intact
  and waits for operator action; `auto` enables self-healing (stop daemon, revert
  task to PENDING, clean worktree).
- **Per-lane `reap_policy` resolution** (#582): each lane can override the
  client-level reap policy; the reconciler resolves the effective policy per
  session by checking lane → client → global in order.
- **Auto fast-forward local main** (#569): `cw dev-queue` fast-forwards the
  local main branch when it is behind origin before dispatching, preventing
  stale-base merges.
- **CI status + mergeable in `MonitoredPR`** (#571): `cw status` surfaces
  `ci_status` and `mergeable` for each open PR alongside the existing fields.
- **Agent onboarding wiring** (#562): `cw install` now registers the MCP server,
  adds the `cw` allowlist entry, wires the SessionStart hook, and appends the
  `CLAUDE.md` snippet in a single command.
- **Route emitted-but-unrouted sentinels** (#584): reconciler now routes
  `AUTO_DEV_RESULT` sentinels that were emitted before the idle watchdog fires,
  preventing orphaned successful results from blocking further dispatch.

### Fixed

- **dev-queue wait resolves oldest terminal duplicate** (#585): `cw dev-queue
  wait` now resolves the oldest terminal-state duplicate task rather than a live
  one, preventing premature wait-exit when duplicate entries exist.
- **Inbox cursor-not-found wedge + torn-read** (#565): event-history reader no
  longer wedges when the inbox cursor points past the end of a rotated file; torn
  reads under concurrent writers are also closed.
- **Config re-resolved each tick** (#566): `run_watcher_tick` re-reads
  `clients.yaml` on every dispatch tick so hot-edited config takes effect without
  restarting the watcher.

### Refactored

- **Migrate remaining `sessions.json` writers to `mutate_state()`** (#567): all
  state mutations now go through the single-lock path introduced in v0.14.2,
  closing the last direct-write windows.

### Schema

- **v8** (#573): added `Session.reap_proposed_at`. Purely additive; defaults to
  `None`. Auto-migrates on first load.
- **DEV_QUEUE v3** (#575): added `TicketTask.lane`. Purely additive; defaults to
  `"default"`. Auto-migrates on first load.

### Documentation

- **ADR-0006**: reaping is gated by an authority, not automatic.
- **State-integrity audit** (#38042be): ADR-0005, RFC 0004 §State integrity,
  implementation plans.
- **Headless post-sentinel hardening** (#580): salvage-ship recipe and
  turn-end hardening notes for headless sessions.

## [1.0.0] — 2026-06-11

First stable release: the multiplexer layer (cmux/tmux) is deleted entirely,
workers spawn via `claude --bg` and are tracked by the native daemon roster,
and the reap path has been bulletproofed end-to-end (#543).

### Architecture

- **Native supervisor replaces multiplexer layer** (#119/#521): cmux/tmux
  adapters, the `MultiplexerAdapter` abstraction, and all wrapper shims are
  deleted. Workers spawn via `claude --bg`; liveness tracked through
  `~/.claude/daemon/roster.json`. No external multiplexer required.

### Added

- **Observable reaps: `queue.session_reaped` + `ReapReason` taxonomy**
  (#380): the reconciler emits a structured `queue.session_reaped` event for
  every reap decision, carrying one of eight `ReapReason` values
  (`phantom_surface`, `idle_stall`, `usage_limit_cutoff`, `retry_cap_parked`,
  `wall_clock_budget`, `completed_backstop`, `salvage_completed`,
  `salvage_parked`). `Session.reap_reason` (schema v7) records the reason on
  the session object.
- **Confirm-before-reap** (#545): idle watchdog waits for
  `idle_confirm_observations` (default: 2) consecutive idle observations before
  triggering an idle-stall reap. `Session.idle_observation_count` (schema v6)
  tracks the accumulating count.
- **Widened subagent-liveness window** (#544): `SUBAGENT_LIVENESS_WINDOW_SECONDS`
  raised 900 s → 1800 s so a single long quiet tool call or in-flight subagent
  is not reaped as idle. The transcript-mtime window and elapsed budgets are
  unchanged, and roster presence is deliberately NOT treated as proof-of-life
  (a dead worker can linger in the roster).
- **Unified transcript locator via `surface_ref`-prefix glob** (#541): precise
  liveness check resolves the transcript path from the daemon session id prefix
  rather than scanning all transcripts.
- **Sentinel-aware `cw dev-queue wait`** (#535): `wait` detects
  `AUTO_DEV_RESULT` sentinels in the transcript directly, eliminating
  false-timeout (exit 124) for long-running workers whose reconcile cycle
  hasn't fired yet.
- **`cw result validate`** (#482): pre-emit gate validates a candidate
  `AutoDevResult` JSON object against the authoritative schema.
- **`cw schema`**: inspect Pydantic model schemas for `AutoDevResult`,
  `TicketTask`, and `Session` directly from the CLI.

### Schema

- **v5** (#119/#521): cleared legacy multiplexer `surface_ref` values on
  upgrade. Auto-migrates on first load.
- **v6** (#545): added `Session.idle_observation_count`. Purely additive;
  defaults to `0`.
- **v7** (#380): added `Session.reap_reason`. Purely additive; defaults to
  `None`.

### Documentation

- **Operator runbooks** (#538/#539): `docs/dispatch-runbook.md` (end-to-end
  `cw dev-queue` dispatch procedure) and `docs/session-disposition.md` (how to
  read a session's outcome from the transcript sentinel).
- **`docs/MIGRATION-0.x-to-1.0.md`**: updated to cover the v5→v7 schema
  chain, the new 1.0 contract surface, and the `cw daemon` deprecation shim.

### Known issues

- **#542**: `cw dev-queue wait` can ride to the hard timeout (exit 124) instead
  of returning exit 3 (ATTENTION) when a session is reaped mid-wait. The
  sentinel-aware path only catches successful sentinels; a reap-during-wait
  is not yet signalled early.

## [0.14.2] — 2026-06-03

Reliability-hardening release: a wave of concurrency, atomicity, and
crash-safety fixes across the state store, config writes, worktree lifecycle,
event history, and the reconcile/daemon loops — closing torn-read, TOCTOU, and
lost-update windows that surface under parallel dispatch.

### Fixed

- **`sessions.json` read-modify-write locking** (#424 → PR #437): the state
  file is now locked across the full read-modify-write, closing a lost-update
  window when concurrent session operations raced.
- **Atomic + locked `clients.yaml` write** (#429 → PR #442): config writes go
  through an atomic, locked replace so a crash or concurrent writer can't leave
  a truncated `clients.yaml`.
- **Atomic hook-context writes + live-session guard** (#427 → PR #440): spawn
  writes hook context atomically and refuses to overwrite a live session's
  context.
- **Event-history hardening** (#433 → PR #449): torn-read, TOCTOU, fsync, and
  poll-lock fixes in `history.py` and the channel servers.
- **Worktree dirty-state guards** (#425 → PR #439, #426 → PR #441): dirty-check
  before force-removing a stale/timed-out worktree, and refuse dirty-worktree
  reuse in `create_worktree`.
- **Interactive-start isolation guard** (#428 → PR #448): guard interactive
  `start` isolation and `fast_forward_main` so an interactive session can't
  clobber in-flight work.
- **Reconcile salvage + parked-session skip** (#431 → PR #446): reconcile
  salvages all terminal statuses and skips parked sessions instead of
  re-flagging them.
- **Malformed-roster tolerance + `idle_watchdog=0`** (#432 → PR #443):
  reconcile tolerates malformed roster JSON and honors `idle_watchdog=0` as
  "disabled".
- **Daemon tick guards** (#390 → PR #444): per-client and whole-tick guards in
  `run_watcher_tick` so one client's failure can't abort the tick.
- **Sparse sentinel coercion** (#430 → PR #445): legitimate-but-sparse
  `AutoDevResult` sentinels are coerced rather than rejected.
- **Dispatch stdout visibility** (#420 → PR #435): operator-visible stdout for
  `cw dev-queue run`.
- **Logging handler at entrypoint** (#423 → PR #434): the logging handler is
  configured once at the CLI entrypoint.
- **dispatch-guard workflow YAML** (PR #450): repair invalid YAML in the
  dispatch-guard workflow.

### Documentation

- **Full CI gate set documented** (#436 → PR #447): the quality-gate docs now
  cover format, `mypy --strict`, pre-commit, coverage thresholds, and
  diff-cover.
- **Two-branch model + stale comment** (PR #414): correct the two-branch model
  in `headless-contract.md` and a stale cmux comment in `worktree.py`.

### Operations

- **Dispatch-critical drift guard + release closer** (PR #415): CI guard for
  dispatch-critical drift, plus auto-close of `dispatch-drift` issues on
  release.

## [0.14.1] — 2026-05-30

Bug-fix release closing dispatch-reliability gaps surfaced by the 2026-05-30
fanout-cascade RCA: a re-dispatched ticket can no longer inherit a prior run's
worktree state, and the idle-watchdog budget is now operator-tunable so workers
mid-plan/review aren't reaped at 15 minutes.

### Fixed

- **Stale-worktree reuse** (#404 → PR #410): `create_worktree` verifies the
  existing worktree is checked out on the requested branch before reusing it,
  raising `StaleWorktreeError` on a mismatch (wrong branch, detached HEAD, or not
  a registered worktree). The dispatch loop catches it, force-removes the stale
  tree, and reverts the task to `PENDING` so the retry rebuilds clean — closing
  an infinite-respawn window. `reconcile` also reaps a timed-out session's
  worktree on both the wall-clock-timeout and idle-stall-recover paths.

### Added

- **Tunable idle-watchdog budget** (#412): new `idle_watchdog_seconds` key in
  `orchestrator.yaml` overrides the hardcoded 900s (15 min) global default —
  15 min was reaping headless workers still mid-plan/mid-review. Falls back to
  the constant when unset; per-ticket and per-tier overrides still take
  precedence. Also adds `.claude/project-config.yaml` pinning
  `tracking.primary.system: github-issues` for this repo.

## [0.14.0] — 2026-05-30

Dispatch-reliability release: the idle watchdog now **auto-recovers** stalled
headless workers (bounded retries, then parks) and **salvages** a terminal
sentinel before flagging, so a worker that already shipped is never mis-parked
as `blocked_on_user`. Plus deterministic sentinel routing, parse-boundary
coercions, a cross-client `cw queue list`, and watchdog/doctor robustness fixes.

### Added

- **Idle-stall auto-recovery** (#384 → PR #394): the idle watchdog now reverts a
  provably-idle headless worker to `PENDING` for re-dispatch (capped per tier via
  `idle_retry_cap_by_tier`), only parking it `BLOCKED_ON_USER` once retries are
  exhausted — instead of parking on the first stall.
- **Cross-client `cw queue list`** (#201 → PR #397): `cw queue list` with no
  CLIENT arg now shows tasks grouped across all configured clients.

### Fixed

- **Idle watchdog salvages terminal sentinels** (#398 → PR #400): a shipped/no_op
  session idle past the budget is salvaged to `COMPLETED` rather than flagged
  `BLOCKED_ON_USER`.
- **Deterministic parse-failure routing** (#263 → PR #396): `schema_version_unsupported`
  and other deterministic parse failures route to `FAILED` (not an infinite
  PENDING retry); unknown blocker reasons route to `COMPLETED`.
- **no_op pre-impl `scope.lines_actual` coercion** (#399 → PR #401): a `no_op` at
  `stage1_pre_flight`/`stage1_plan` with a stray non-null `lines_actual` is
  coerced clean at the parse boundary instead of failing `validation_failed`.
- **blocked + stray `next_actions` coercion** (#371 → PR #376).
- **Transcript-mtime liveness check** (#340 → PR #383): prevents false-positive
  watchdog fires on workers that are actively writing their transcript.
- **`cw doctor` wedge-detection robustness** (#354 → PRs #378/#379): BACKGROUNDED
  exclusion + subprocess timeouts.
- **Reviewer stale-ref helper** (#381 → PR #385): `fetch_feature_branch` resolves
  stale local refs before review.
- **`_accumulate_task_cost` docstring + is-None guard** (#352 → PR #377).

## [0.13.0] — 2026-05-29

Minor release hardening the dispatch-reliability path: terminal-sentinel
salvage before timeout/crash disposition, cleaner no_op contract parsing, and a
`cw doctor` worktree-path check — plus the dev-queue missing-dir guard and a
type-suppression cleanup batch.

### Added

- **`cw doctor` worktree path existence check** (#143 → PR #365): `cw doctor`
  now flags sessions whose recorded worktree path no longer exists on disk,
  catching the drift where a worktree is removed out from under a tracked
  session.

### Fixed

- **Sentinel salvage on timeout/crash** (#372 → PR #374): the `TIMED_OUT` and
  crashed-phantom sweeps in `reconcile` now recover a terminal-success
  `AUTO_DEV_RESULT` (`shipped`/`no_op`) from the session's transcript before
  finalizing disposition. A headless worker that emitted a valid sentinel and
  then stalled (e.g. waiting on CI) or whose surface died is now recorded
  COMPLETED with its real result — and its ticket is **not** reverted to
  PENDING — instead of being mislabeled `timed_out`/`crash` and re-dispatched
  (dup-PR risk). Guards the reused-worktree stale-transcript case (#358) by
  only trusting a transcript modified after the session started.
- **`no_op` + stray-PR coerced at parse boundary** (#367 → PR #370): a sentinel
  reporting `no_op` while also carrying a stray `pr_url` is now coerced to a
  clean `no_op` at the parse boundary, so the disposition isn't ambiguous
  downstream.
- **dev-queue `refresh-all` skips missing client dirs** (#356 → PR #369):
  `refresh-all` now gracefully skips clients whose workspace directory is
  missing (e.g. a dead `sigma` entry) instead of crashing the whole refresh.

### Removed / chore

- **`get_item` queue helper** (#360 → PR #368): adds a `get_item` accessor,
  dropping 7 `type: ignore[union-attr]` suppressions.
- **Test annotation fixes** (#361 → PR #373): removes 32 `type: ignore`
  suppressions across the test suite via proper annotations.

## [0.12.0] — 2026-05-29

Minor release covering the cw 1.0-march observability and orchestration
substrate: live work board, read-only session peek, atomic terminal
transitions, the queue-events MCP channel, cost tracking, and `cw doctor`
wedge detection. Also enables the native nightly soak clock toward 1.0.

### Added

- **`cw watch` live work board** (#126 → PR #347): full-screen TUI streaming
  cross-client session + queue state, refreshed from the event bus.
- **`cw peek`** (#122 → PR #346): read-only tail of a running session's output
  without attaching to or disturbing the surface.
- **`cw spawn complete`** (#121 → PR #344): atomic session terminal-state
  transition, closing the race between session-flip and queue-flip.
- **cw-queue-events MCP channel** (#125 → PR #355): pushes queue-state deltas
  with persist-on-emit + cursor replay, mirroring the PR-events channel. Track C
  complete (7/7).
- **`cost_usd` persistence, schema v4** (#124 → PR #351): per-session and
  per-ticket USD cost recorded on `Session` + `TicketTask`.
- **`cw doctor` wedge detection** (#123 → PR #353): drift checks for wedged
  sessions, `--reap` recipes, and `--json` output.
- **AUTO_DEV_RESULT schema Phase C+D** (#174 → PR #343): expanded contract for
  queue-orchestrator observability.

### Changed

- **CI: native nightly scheduled, cmux nightly de-scheduled** (PR #363):
  `nightly-native.yml` gains a daily 09:00 UTC `schedule:` trigger, starting the
  2-week native-soak clock toward 1.0 (gates #242/#119/#120). `nightly.yml`
  (cmux integration) is de-scheduled to `workflow_dispatch`-only ahead of cmux
  removal (#119).

### Fixed

- **Silently-idle watchdog → flag-only** (#348 → PR #349): the `silently_idle`
  watchdog no longer reaps the worker; it flags only and lets the run reach the
  60-min ceiling, avoiding false kills of active workers.
- **`cw_queue_peek` stale-transcript false STOP** (#358 → PR #359):
  `find_transcript_for_ticket` no longer picks the oldest stale transcript in a
  reused worktree (which produced bogus age + a false STOP recommendation).

### Removed / chore

- **Delete `pr_responder.py`** (#245 → PR #357): superseded by the event-driven
  review-monitor path.
- **Suppression audit** (PR #362): `noqa` / `type: ignore` count reduced 110 → 52.
- **Skill audit + `/cw-fanout`** (PR #350): cw skills re-aligned to current
  workflows; new `/cw-fanout` multi-ticket dispatch skill added.

## [0.11.2] — 2026-05-28

Patch release with two reliability fixes for the dev-queue dispatch path,
surfaced during the 2026-05-28 dogfood wave.

- **Code-fenced sentinel parsing** (#337 → PR #339): `parse_stdout` now
  tolerates AUTO_DEV_RESULT JSON wrapped in a Markdown code fence (```` ```json ````)
  when the explicit `<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>>` markers are
  absent. Previously the dispatcher treated such sessions as no-sentinel and
  spawned wasteful att2/att3 retries on already-shipped work. Closes #336
  (downstream consequence — silently_idle hangs after the parser returned None).
- **Watchdog default bump** (#340 stopgap → PR #341): `IDLE_WATCHDOG_SECONDS`
  raised from `300` → `900` (15 min), `idle_watchdog_by_tier['large']` from
  `600` → `1800`. The previous 300s budget false-positively flagged active
  small-tier workers (#337 itself took 14 min wall time and tripped the
  watchdog at 5 min). The deeper fix — transcript-mtime liveness detection
  — remains open under #340.

## [0.11.1] — 2026-05-27

Patch release covering #129's BLOCKED_ON_USER producer + watchdog and the
SHOULD_FIX follow-up batch from PR #323's review, plus the new
`cw-queue-peek` skill for in-flight session inspection.

- **`BLOCKED_ON_USER` producer + watchdog** (#129/#322 → PR #323):
  `QueueItemStatus.BLOCKED_ON_USER` + `OrchestratorEventType.SESSION_NEEDS_ATTENTION`
  enum additions; `signal_needs_attention` path in `wrapper.py` for paused-for-input
  sentinels; `flag_silently_idle_daemon_sessions` watchdog in `reconcile.py` for
  silently-stalled DAEMON sessions; `notify.py` peon-ping + `notify-send` push
  helper; `docs/headless-contract.md` updated with the BLOCKED_ON_USER section.
- **Watchdog hardening** (#324/#332): reorder writes — `save_state` (session →
  COMPLETED + `last_result`) fires before queue mutation; crash between
  session-flip and queue-flip recovers cleanly on next reconcile tick.
- **Per-ticket / per-tier IDLE_WATCHDOG_SECONDS override** (#326/#331): mirrors
  the `HEADLESS_TIMEOUT_SECONDS` override pattern from #265.
- **`notify.py` debug logging** (#327/#330): each fail-quiet exception path now
  logs at debug level so `CW_LOG_LEVEL=DEBUG` surfaces misconfigured peon.sh.
- **Test rigor** (#328/#333): `_is_paused_for_user_input` tests construct real
  `AutoDevResult` instances instead of `MagicMock`, so future schema changes
  fail loudly.
- **`cw-queue-peek` skill + script** (PR #335): in-flight inspection of
  RUNNING dev-queue sessions. Computes age, idle gap, last sentinel status,
  and PR state per session; recommends WAIT / PEEK / STOP via a 10-rule
  peek-stop ladder. Reports only — operator runs `cw spawn close <id>`
  after reviewing. Closes the gap between `cw-session-watch` (post-mortem
  exit status) and `cw-validate-result` (post-mortem sentinel inspection).

## [0.11.0] — 2026-05-27

Pre-1.0 substrate release covering multiplexer-removal Phase D, the PR
event channel architecture, orchestrator subagent + `cw orchestrator-start`,
dispatcher routing hardening, and freshness-gate guardrails.

Major themes since 0.10.0:

- **Multiplexer Phase D complete**: `MultiplexerAdapter` removed from
  `reconcile`, `dispatch`, `doctor`, `orchestrate`, and `cli`. Liveness
  checks switched to `claude agents --json` + `roster.json`. Net -252 lines
  across the substrate. (#167/#269)
- **PR event channel architecture**: new `cw_pr_events_server` MCP channel
  pushes review-monitor deltas with durable persist-on-emit and cursor
  replay (#138/#282, #139/#284, #114/#288). Daemon's pr-watcher loop
  retired in favor of event-driven routing (#299). Stdio MCP channel
  proxy added for capability declaration (#291). Channel server lazy-imports
  starlette so the module loads without the `[mcp]` extra (#306); 307
  redirect on bare `/sse` path fixed (#309).
- **Orchestrator subagent + `cw orchestrator-start`**: new `cw-orchestrator`
  subagent routes channel events (#115/#296/#301); new `cw orchestrator-start`
  CLI command spawns the orchestrator session (#295/#302). Frees the
  daemon from PR-response logic and centralizes routing.
- **Dispatcher routing hardening**: v4 statuses (`ambiguities_pending_resolution`,
  `premises_pending_verification`) now recognized as terminal at
  `schema_version=2` and route to BLOCKED (no more retry-on-paused-sentinel
  bug) (#316/#319, e538638). New `QueueItemStatus.CANCELLED` prevents the
  dispatcher race when `cw spawn close` runs against an in-flight tick
  (#317/#320). Short-form `stage_reached` aliases mapped to canonical
  values (#292/#293). Code-fence wrapped sentinels parsed correctly
  (#307). `dispatch_tick` guards against worktree == main checkout
  (#300/#311). Permission_mode uses explicit None check (#298).
- **Reconcile resilience**: 30s spawn grace window prevents same-tick
  phantom reaping (#271/#272). Short-id `surface_ref` matches against
  UUID-prefix `sessionId` (#273). Stale Stop hook dropped on worktree
  reuse for retry (#285/#287).
- **Freshness gate**: pre-dispatch `dispatch_tick` checks each client's
  local `main` against `origin/main`; stale clients emit
  `OrchestratorEventType.TICKET_NEEDS_SYNC` and skip the tick without
  burning a dispatch slot (#215/#268). Misconfigured clients no longer
  dump tracebacks (#278). New `cw dev-queue refresh-all` subcommand
  fast-forwards every configured client.
- **Headless config**: default `HEADLESS_TIMEOUT_SECONDS` bumped 30→60
  minutes (#266). Per-ticket / per-`scope.tier` override (#265/#279)
  allows large-scope work room to finish.
- **CI scaffolding**: nightly integration workflow at
  `.github/workflows/nightly-integration.yml` exercises real `claude --bg`
  / `claude agents --json` / `claude stop` via `pytest -m integration`;
  `workflow_dispatch`-only until API budget is allocated (#110/#318).
  `[mcp]` extra installed in CI so pr-events tests + coverage run
  (#283).
- **Cleanup**: legacy `handoff.py` deleted (#246/#277); transcript handoff
  is now covered by `claude --resume`.

Full per-PR detail in the GitHub release notes (auto-generated by
`release.yml`).

## [0.10.0] — 2026-05-25

Pre-1.0 substrate release covering native-daemon dispatch hardening,
sentinel-capture observability, queue retry-cap, AUTO_DEV_RESULT
schema v4, and ruff/lint discipline.

Major themes since 0.9.0:

- **Native-daemon path**: `cw start` / `cw resume` migrated to
  `claude --bg + attach`; origin-aware Stop hook; settings.local.json
  write hardened.
- **Sentinel observability**: headless DAEMON sessions now persist
  `last_result` via `signal_stop` parsing (#225/#226); contract-level
  Blocker carries explicit retry policy (#193); `/cw-validate-result`
  and `/cw-followup` skills surface sentinel-aware post-run actions.
- **Queue resilience**: dedupe + remove + clear + reconcile-sweep
  (#177/#221); validation_failed retry-cap + sentinel-to-task routing
  in `signal_stop` (#251/#261); reconcile sweeps stalled headless
  sessions to TIMED_OUT (#185/#260).
- **AUTO_DEV_RESULT v4**: `ambiguities_pending_resolution` and
  `premises_pending_verification` promoted to canonical statuses
  with closed `next_actions` vocabulary (#191/#262).
- **Pipeline guardrails**: `_validate_worktree` pre-flight gate (#186);
  ANSI-strip on `claude --bg` stdout parsing (#203/#204); `cw doctor`
  setup checks (#142/#219); diff-cover pre-push hook (#250).
- **Cost guardrails**: per-client `worker_model` field pins spawned
  workers to Sonnet by default (#248/#254).
- **Ruff hardening**: rule selection expanded across exception
  handling, logging, pathlib, async, and defensive bundles (#230 family
  → #240, #241, #252, #253, #256, #258). Diff-cover gate mirrored
  locally via pre-push hook (#250).

Full per-PR detail in the GitHub release notes (auto-generated by
`release.yml`).

## [0.8.3] — 2026-05-21

Patch release — closes the wedged-`RUNNING` loop called out as a known
issue in 0.8.2. Dispatched `/auto-dev` workers that exit cleanly now
produce a `SESSION_COMPLETED` event with `crashed: false`, so the queue
task transitions `RUNNING → COMPLETED` instead of sitting on a consumed
concurrency slot.

### Fixed
- **Clean-completion signal for dispatched workers** (#99, #101).
  - `cw.spawn` now routes daemon spawns through
    `cw run-claude -- --print '<prompt>'` (instead of raw
    `claude --print`), passing `CW_CLIENT` / `CW_PURPOSE` /
    `CW_SESSION_ID` env so the wrapper can target the specific session
    even when concurrent daemon sessions share `(client, purpose=impl)`.
  - `cw.wrapper` detects headless mode (`--print` in args), tees
    claude's stdout to fd 1 while capturing the last 1 MiB into a
    bounded buffer, parses for the `<<<AUTO_DEV_RESULT…>>>` sentinel
    on clean exit, and emits `SESSION_COMPLETED` via the new
    `signal_completed()`. Idempotent against reconcile racing ahead.
    Falls back to `signal_idle` on parse failure or non-zero exit so
    reconcile's phantom-pane path still catches real crashes.
  - `CompletionReason.NORMAL` added for the wrapper-signaled terminal
    path (distinct from `CRASHED` written by reconcile).
- 0.8.2's consume-side `crashed: true` skip remains intact — no
  regression on the genuinely-crashed path.

### Known issues
- Full per-status routing (`shipped` / `no_op` → COMPLETED,
  `blocked` / `plan_pending_approval` → PAUSED_NEEDS_HUMAN) is still
  scoped to #58. This release ships the terminal-vs-respawn distinction
  only; all non-crashed completions currently land as COMPLETED.
- #59 resume-detection on re-invoke depends on `last_result` being
  populated, which this release enables — but the dispatch-side
  resume-injection is separate work.

## [0.8.2] — 2026-05-06

Patch release — fixes a queue-accounting bug discovered while validating
0.8.1 end-to-end. With 0.8.1's producer fix in place, `SESSION_COMPLETED`
events now carry `ticket_id` — but `consume_completed_sessions` was
matching on `ticket_id` alone, so a `crashed: true` event from a prior
(reconcile-reverted) session could falsely COMPLETE a freshly-respawned
task for the same ticket.

### Fixed
- **`consume_completed_sessions` now skips `crashed: true` events** and
  matches non-crashed events on `session_id` when both sides have one
  (#97, #98). Reconcile is the authoritative path for crashed sessions
  (RUNNING → PENDING revert); the consumer no longer shadows that with a
  spurious COMPLETED transition. Stale events from older sessions for
  the same `ticket_id` are rejected via `session_id` disagreement.
  - `TicketTask` gains a nullable `session_id` field stamped by
    `dispatch_tick` after spawn returns and cleared by `reconcile` on
    revert. Legacy tasks/events without the field fall back to
    `ticket_id`-only matching for backward compatibility.

### Known issues
- Reconcile still has no clean-completion signal — workers that exit
  successfully (`/auto-dev` returns to a bash prompt inside the tmux
  pane) sit `RUNNING` indefinitely, and tmux-session kills are still
  observed as `crashed: true` (now correctly skipped, but the queue
  task ping-pongs RUNNING ↔ PENDING via dispatch respawn). Tracked in
  #99 — not blocking this release; #98's fix prevents the false
  COMPLETED transitions that previously masked the issue.

## [0.8.1] — 2026-05-04

Patch release — fixes a queue-accounting bug introduced in 0.8.0 that
caused every `cw dev-queue` task to stay `RUNNING` forever.

### Fixed
- **`session.completed` events now carry `ticket_id`** (#94, #95). The
  reconciler emitted `SESSION_COMPLETED` without the `ticket_id` field,
  so `consume_completed_sessions` skipped every event and dev-queue
  tasks never transitioned to `COMPLETED`. Concurrency-cap accounting
  treated stranded `RUNNING` tasks as live, progressively starving
  available slots until the queue would refuse to dispatch anything new.
  - **Producer side:** the reconciler now includes `ticket_id` in the
    emitted payload whenever the session name parses as auto-dev,
    mirroring what `session.spawned` already does.
  - **Consumer side:** `consume_completed_sessions` falls back to
    parsing `ticket_id` from `session_name` when the payload lacks it.
    Drains historical `RUNNING` tasks whose completion events predate
    the producer-side fix — no manual queue-file surgery needed.
- `ticket_id_for_session` is now public (renamed from
  `_ticket_id_for_session`) so dispatch and reconcile share the parsing
  helper rather than duplicating the prefix logic.

## [0.8.0] — 2026-05-04

First release of the **0.8.0 milestone** — substrate for autonomous
`/auto-dev` dispatch via `cw`. This release ships parent/worker session
linkage, the headless contract parser, the structured `AutoDevResult`
sentinel, and the dispatch path that wires it all together.

Skipping 0.7.0 — the work landed under a 0.8.0 milestone tag (cw#52–#55
substrate, cw#56–#59 dispatch arc) and a single-digit-up bump matches
the surface-area change.

### Added
- **Spawn API + bidirectional parent-child persistence** (#53, #65). New
  `cw.spawn` module owns session creation; parent and worker sessions
  reference each other via `parent_session_id` / `worker_session_ids`
  fields on `Session`. Replaces the ad-hoc state writes in `start_session`.
- **State schema v2** (#62). `Session` gains linkage fields. Migration
  is automatic on read; old states are upgraded in place.
- **Doctor linkage drift detection** (#55, #68). `cw doctor` reports
  stale `worker_session_ids`, mismatched `parent_session_id`, and
  asymmetric references as failed checks contributing to the exit code.
  Each `linkage/*` check carries a remediation hint. `cw doctor --reap`
  additionally reconciles phantom sessions via the surface liveness
  check.
- **Dispatch headless mode** (#78). `dispatch_tick` spawns daemon
  workers with `--headless` and threads `parent_session_id` through so
  the worker's `AutoDevResult` lands on the parent.
- **AutoDevResult sentinel parser** (#57, #80). `cw.auto_dev_result`
  parses the `<<<AUTO_DEV_RESULT … AUTO_DEV_RESULT>>>` block emitted by
  headless `/auto-dev`, validates §3-§5 invariants, and persists the
  result on the worker `Session`. Six failure modes return synthetic
  `BlockedResult` rather than raising.
- **AutoDevResult `schema_version: 2`** (#82). Adds the `no_op` status
  (skill detected the ticket already satisfied; no plan, no branch, no
  PR) and `close_issue_as_completed` advisory `next_actions` value.
  v1-tagged payloads with v2-only statuses are rejected as
  `validation_failed`. Parser accepts both v1 and v2 during the rollout
  window.
- **Headless `/auto-dev` contract spec** (#60, #69). New
  `docs/headless-contract.md` documents the producer/consumer surface
  the skill emits and `cw` consumes. Source of truth for cross-repo
  drift checks.
- **Project `/ship-it` command** (#61). `.claude/commands/ship-it.md`
  used by the auto-dev pipeline's `/prep-pr` integration.

### Changed
- **CI hardening**:
  - `mypy --strict` enforced project-wide (#73).
  - 88% baseline coverage gate (#73).
  - 90% **patch** coverage enforced via `diff-cover` on PRs (#79).
  - Nightly cmux smoke run scaffolded (#73), launching cmux via the
    macOS `.app` bundle (#74).
- **Worktree slug charset** (#84). `slugify_branch` now matches
  `claude -w`'s validator (`[A-Za-z0-9._-]+`) instead of just collapsing
  path separators. Unblocks GitHub-issue ticket ids like `#7` from
  `cw dev-queue` dispatch.

### Fixed
- **`start_session` parent edge cases** (#64, #72). Idempotent on
  re-spawn; rejects parent IDs that don't exist; rejects self-parent.
- **`start_session` dual `save_state`** (#63, #70). Single atomic
  write — earlier flow wrote twice and could leave linkage half-applied
  on crash.
- **Dead `WorkerEntry.missing` field** (#66). Removed; tests tightened.

### Removed
- `WorkerEntry.missing` (#66) — never read by any consumer.

## [0.6.4] — 2026-05-01

Completes the dispatch-spawn fix started in 0.6.3. The earlier release
addressed the surface-level `claude -w` validator error, but two
deeper problems still made `cw dev-queue run` non-functional end-to-end:

1. `dispatch_tick` only ran `worktree_path.mkdir(...)` — it never
   created a real git worktree. Claude would have started in a plain
   (non-repo) directory.
2. The dispatch / pr_responder / plan call sites all wrote a prompt to
   a temporary file and immediately read it back into the spawn
   command string — pointless file roundtripping that obscured the
   real call.

### Fixed
- `cw.dispatch.dispatch_tick` now calls `create_worktree(client, branch)`
  (idempotent — returns the existing path if already created) instead
  of `mkdir`-ing an empty directory. `cw.pr_responder` uses the same
  pattern for PR-event branches.
- `cw.worktree._run_git` strips `GIT_*` from the subprocess env so
  cw's git operations always target the client repo at *cwd*. Without
  this, running cw from inside a git hook (e.g. a pre-commit pytest
  run that exercises dispatch) would leak `GIT_DIR` / `GIT_INDEX_FILE`
  and produce confusing "Not a directory" errors.

### Changed
- `cw.spawn.spawn_create_impl` and `cw.pr_responder._spawn_session`
  now take a `prompt: str` rather than a `prompt_file: Path`. Callers
  inline the prompt directly. The `cw spawn` CLI reads the file at the
  user-facing boundary and passes the contents through.
- `cw.plan` still persists the planner prompt to disk for audit /
  debugging but no longer reads it back to inline into the spawn.
- `tests/conftest.py::make_git_repo` now initialises with an empty
  `main` commit (and a per-repo `user.email` / `user.name`) so callers
  that exercise `git worktree add` have something to branch from.

## [0.6.3] — 2026-04-19

Surface-level fix for a `cw dev-queue run` regression: dispatch was
running `claude -w <absolute-worktree-path>`, but `claude -w` takes a
worktree *name*. The absolute path's leading `/` made the first segment
empty and failed claude's name validator, so no DAEMON session ever
started. This release dropped the `-w` flag and `cd`s into the
worktree path. (See 0.6.4 for the rest of the fix — the path itself
was still just an empty directory rather than a real git worktree.)

### Fixed
- `cw.spawn.spawn_create_impl` and `cw.pr_responder._spawn_session`
  no longer pass `-w` to claude.

## [0.6.2] — 2026-04-19

Phantom-session reconciliation. When tmux dies (machine sleep/restart)
or cmux surfaces close, `sessions.json` used to drift from reality —
dead sessions stayed ACTIVE/IDLE, blocking new dispatch and lying in
`cw status`. This release adds multiplexer/state reconciliation with
a transient-outage safety guard so a short cmux/tmux hiccup cannot
mass-reap live sessions.

### Added
- Multiplexer/state reconciliation. Phantom sessions (tmux/cmux surfaces
  that no longer exist) are detected and reaped automatically on `cw status`,
  `cw list`, `cw start`, and at the top of each `dispatch_tick`. Explicit
  reconciliation is available via `cw doctor --reap`.
- `MultiplexerAdapter.list_surfaces()` on the adapter protocol; implemented
  for tmux, cmux (macOS), and fake backends.
- Public `dev_queue_lock` context manager in `cw.dev_queue` for callers
  that need load → mutate → save around the queue.

### Changed
- `start_session`'s "Launching ... surfaces" message now names the active
  backend (tmux/cmux/fake).
- Dev-queue `TicketTask`s associated with reaped DAEMON sessions revert
  from RUNNING to PENDING so the dispatch loop retries them.
- `RealCmuxAdapter._call` now normalises socket `OSError` and
  `json.JSONDecodeError` into `CwError`, giving callers a single
  exception type to guard against backend failures.
- `reconcile()` refuses to mass-reap when the adapter reports zero live
  surfaces but the persisted state still has ACTIVE/IDLE sessions with
  surface refs — a transient cmux/tmux outage no longer marks every
  session COMPLETED/CRASHED. `compute_drift` stays pure; the guard
  lives only in the side-effecting path.
- `RealCmuxAdapter.list_surfaces` returns an empty set on any enumeration
  failure (including per-workspace `surface.list` errors) rather than a
  partial set, matching the all-or-nothing protocol contract expected by
  the reconciler.
- `ReconcileReport` carries `phantom_session_names` alongside IDs so
  callers no longer need to reload state to resolve names.
- `dispatch_tick` guards the reconcile call and logs failures instead
  of halting the dispatch loop on a reconcile error.
- `doctor._check_reconcile` narrowed its `except Exception` to
  `except CwError` and wraps the reconcile call itself so that an
  unexpected failure is reported as a check result rather than
  crashing `cw doctor --reap`.

## [0.6.1] — 2026-04-19

Small correctness fixes for state durability on Linux and worktree path
sizing for cmux.

### Fixed
- `fix(state)`: all JSON state files (sessions, dev queue, plan, cursors)
  now write via atomic rename so a crash mid-write cannot leave a
  truncated file behind (#46 / #48).
- `fix(worktree)`: the default worktree path now stays under cmux's
  64-character workspace-name cap, avoiding spawn failures when branch
  names push the computed path over that limit (#47 / #49).

## [0.6.0] — 2026-04-18

The multi-platform bridge. `cw` now runs natively on Linux via tmux,
while keeping the macOS-native cmux path unchanged. Backend choice is
driven by a three-tier selector so CI, power users, and single-user
preferences all have a way in.

### Added
- `cw.tmux.TmuxAdapter`: a tmux backend that wraps the `tmux` CLI via
  `subprocess`. A workspace maps to a tmux session, a surface to a
  pane. Raises `CwError` at instantiation time if `tmux` is not on
  PATH (#38).
- Three-tier backend selector in `cw.cmux.get_backend_adapter()`:
  `CW_BACKEND` env var → `orchestrator.yaml` `backend:` field →
  platform default (`darwin` → cmux, everything else → tmux). Setting
  `CW_BACKEND=fake` returns a `FakeCmuxAdapter` for CI and local
  smoke tests (#37).
- `BackendName` enum in `cw.models`; optional `backend` field on
  `OrchestratorConfig`.
- `MultiplexerAdapter` protocol — the backend-neutral name the
  protocol carries going forward. `CmuxAdapter` is a type alias kept
  for one release (#36).
- `cw doctor` subcommand and module — reports resolved backend,
  binary/daemon availability, config and state file validity, and
  version. Exits non-zero when any check fails (#41).
- Parametrized protocol-conformance suite covering every adapter
  class (`tests/test_adapter_protocol.py`) (#39).
- `integration` pytest marker for end-to-end tests that shell out to
  a real multiplexer.

### Changed
- CI matrix is now `[ubuntu-latest, macos-latest]`; tmux is installed
  via apt/brew on the matching runner, and the tmux integration test
  runs on both OSes (#40). Release workflow mirrors the matrix.

### Migration notes
- `from cw.cmux import CmuxAdapter, get_cmux_adapter` keeps working
  in this release. Switch to `MultiplexerAdapter` and
  `get_backend_adapter` before 0.7.
- On Linux, `cw` now defaults to tmux — install `tmux` or set
  `CW_BACKEND=cmux` (if you really want the macOS-only cmux path).

## [0.5.0] — 2026-04-18

Foundations for the multi-platform bridge landing in 0.6.0. No new user
features in this release — the focus is de-risking the tmux backend by
paying down debt in state isolation, error handling, schema migration,
and docs.

### Added
- `schema_version` field on `CwState` and `DevQueueStore`, with a
  `migrate_cw_state` pass that handles field renames and coerces unknown
  `SessionOrigin` values to `user` with a warning instead of crashing
  (#31).
- Accessor functions in `cw.config` (`state_dir()`, `events_dir()`,
  `queues_dir()`, …) so path-consuming modules read the current value at
  call time rather than caching the import-time global (#29).
- `tests/test_exceptions.py` exercising the exception hierarchy, and a
  regression test locking in the Linux-safe retirement path (#34).

### Fixed
- `cw orchestrate retire` no longer crashes on Linux when no session is
  correlated to the merged PR — adapter resolution is deferred until a
  surface actually needs to be closed (#30).
- Test runs no longer leak state into `~/.local/share/cw` or
  `~/.config/cw`. The autouse `tmp_config_dir` fixture covers every
  consumer via the new accessors (#29).

### Changed
- Documentation no longer references the retired Zellij multiplexer.
  README, CLAUDE.md, install guides, and in-source docstrings now speak
  of the pluggable multiplexer backend (cmux today; tmux in 0.6.0) (#32).

### Removed
- `ZellijError` (unused) and the `zellij-plugin/` Rust/WASM scaffold
  (#33). The `zellij_pane → surface_ref` migration in `load_state` is
  preserved as migration armor for older state files on disk.
