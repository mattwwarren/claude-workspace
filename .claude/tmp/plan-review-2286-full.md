**Verdict: MUST_FIX**

Verified the plan's Touch-point Contract by reading all 17 cited files/line-ranges directly (not taking the plan's self-reported quotes on faith). 16 of 17 touch-points check out exactly — line numbers, verbatim quotes, and Match verdicts (including both correctly-flagged real mismatches: `LocalLivenessHandle` vs. ticket's phantom "PidSnapshot" name, and #2367's genuinely OPEN state confirmed via `gh issue view` + a real `grep -rn start_new_session src/` returning zero hits). Found one fabrication.

## Check 1 — Contract Specificity

**Finding.** Touch-point "`tests/test_codex_executor.py` thread-registry assertions" asserts a helper named `_inline_codex_executor` at `tests/test_codex_executor.py:75-84`, Match verdict **MATCH**. Read the actual file: the helper is named `_sync_codex_executor`, defined at `tests/test_codex_executor.py:72` (docstring 75-83), and used at 17 call sites throughout the file (`grep -n "_sync_codex_executor" tests/test_codex_executor.py` returns 17 hits at lines 72, 168, 191, 222, 244, 291, 321, 392, 447, 486, 516, 547, 572, 612, 674, 731, 775, 915). `_inline_codex_executor` does not exist anywhere in that file (confirmed via the same grep — zero hits for that string). This is a false confirmation — Check 1's explicit reject list: "A Touch-point Contract entry's `Match` is CONFIRMED but the Read body contradicts the Plan asserts." Everything else in that bullet (semantics — `background=lambda fn: fn()` seam, purpose, that it needs rewriting alongside `test_codex_background.py`) is accurate; only the identifier is wrong.

Blast radius is narrow: the fabricated name does not appear in the ADR document's own content (the `## Tickets` item #6 just says "rewrite `tests/test_codex_background.py` and the thread-registry assertions in `tests/test_codex_executor.py`" with no specific helper name cited) — so it won't propagate into the shipped deliverable. But it is exactly the class of error v2 enforcement exists to catch (the ADR's own cited incident: fabricated identifiers confirmed as true — four fabrications that went undetected until a spec reviewer independently disconfirmed them).

Fix: one-line correction — either rename to `_sync_codex_executor` in that bullet, or drop the specific identifier from the claim.

All other Touch-point Contract entries verified PASS against direct reads of:
- `src/cw/codex_background.py` — module docstring (1-23), `_start_daemon_thread` (93-114), `_outstanding`/`_outstanding_lock` (85-90), `_default_background` (215-217), `join_outstanding_codex_threads` (220-246), the lazy `cw.executor` import (682-690) and the `cw.dispatch.claim` deferred import (822-824). All verbatim quotes match exactly.
- `src/cw/dispatch/loop.py` — the `finally:` shutdown-drain block (888-922), including the exact `_codex_threads_still_running` field name and the `join_outstanding_codex_threads()` call at line 902. Matches.
- `src/cw/codex_runner.py` — `RealCodexRunner.run`'s `subprocess.Popen` call (83-91), no `start_new_session` kwarg present. Matches.
- `src/cw/reconcile/codex_boot.py` — the "blast-radius bound, not a liveness handle" docstring (62-65), `_gate_clean_requeue` (407-426) and `_resolve_orphan_action` (376-404), and the psutil cwd-scan `_codex_processes_in`/`_live_writer_park` (302-356). All verbatim quotes and line numbers match.
- `src/cw/models/session.py` — `LocalLivenessHandle` class (27-41: `pid: int`, `start_time_ns: int`, frozen via `ConfigDict(frozen=True)`) confirming "PidSnapshot" does not exist as a name; `Session.local_liveness` field comment (129-134) including the "None for every non-LOCAL session" line the plan flags as going stale. Matches.
- `src/cw/executor.py` — module-top import of `cw.codex_background` names (13-18) confirming the import-cycle direction; `StageExecutor` Protocol docstring's CodexExecutor "accepted, documented exception" paragraph (252-276); `LocalExecutor.spawn()` (425-593, launch at line 511, liveness stamp 512-523); `OpencodeExecutor.spawn()` (674-817, launch at 748, liveness stamp 749-760) including the `max_parallel=1 lanes` docstring line at 701. Matches.
- `src/cw/local_runner.py` — `RealAiderRunner.launch`'s `Popen` call (164-187), no `start_new_session`. Matches.
- `src/cw/opencode_runner.py` — `RealOpencodeRunner.launch`'s `Popen` call (126-150), no `start_new_session`. Matches.
- `src/cw/reconcile/local.py` — `_local_process_alive` (60-69), `_detect_local_harvest_candidates` (72-110), `_synthesize_harvest_sentinel`'s opencode-log-exists-vs-git-fallback binary branch (113-146), `_act_on_local_harvest_candidates` (149-259). Matches, including the exact "If ``.cw/opencode.log`` exists..." quote.
- `docs/adr/template.md` — full section order (Title, Status, Driven by, Decision, Invariant, What this means for callers, What this means for producers, Consequences, Alternatives considered, Referenced by) confirmed; plan's proposed ADR follows this order and appends `## Tickets` after Referenced by, consistent with template.
- `docs/adr/README.md` — Index table confirmed to stop at `[0017]` (line 62), immediately followed by the "ADR-0000 is the foundational record" paragraph (line 64) — matches plan's pre-flight verification claim and its stated insertion point for the new `[0018]` row.
- `tests/test_codex_background.py` — the whole `_outstanding`/`_default_background`/`join_outstanding_codex_threads` test block (82-248 range) and `test_module_docstring_names_the_threading_precedent` exactly at line 248. Matches.

Independent verification beyond the plan's own citations:
- `grep -rn "start_new_session" src/` → zero hits, confirming R4's premise that #2367 has not landed.
- `gh issue view 2367 --json state,title` → `{"state":"OPEN","title":"fix(executor): launch aider/opencode children with start_new_session so Ctrl-C on serve doesn't kill them"}` — confirms the plan's claim that #2367 is open, not merged.
- `grep -rn "max_parallel" src/cw/` → every hit is the general `LaneConfig.max_parallel` / dispatch-admission machinery (`dispatch/tick.py`, `models/orchestrator_config.py`, `board.py`, `dispatch_serve.py`, `config.py`) plus the one advisory docstring line in `executor.py:701` — nothing codex-specific enforces "=1". Confirms the plan's claim.

Pre-flight Resolution Conformance section is present, all 4 R-items (R1-R4) have conformance lines, each correctly grounded in the verified code reads above (R1: new CLI + driver module named; R2: LocalExecutor/OpencodeExecutor mirroring confirmed by direct read of both spawn() methods; R3: stage-agnostic contract, Phase 1 REVIEW-only, #1550 named as stage-2 consumer; R4: #2367 dependency correctly stated as open/unlanded, independently confirmed above).

## Check 2 — File Enumeration
PASS. Two files, both docs, both tied to the ticket's binding deliverable (new ADR + one README index row). No test/impl asymmetry issue — ticket is docs-only and the plan states this explicitly; verified accurate by reading `docs/adr/README.md` (index stops at `[0017]`) and confirming `docs/adr/0018-codex-runs-as-a-detached-local-liveness-job.md` does not yet exist. No cross-ticket scope creep — the `## Tickets` breakdown (7 items) is explicitly named as future/dispatchable work, not claimed as done in this ticket's own `## Files Modified` list.

## Check 3 — Test Helper Inventory
N/A-satisfied. Docs-only ticket; plan correctly states no test phase and no `src/`/`tests/` changes are proposed.

## Check 4 — Observability Call Inventory
N/A-satisfied. Docs-only ticket; no new log/audit calls introduced by this ticket's own deliverable. The ADR's Consequences section describes future removal of the `DISPATCH_LOOP_EXITED.codex_threads_still_running` field, but that is documentation of a future implementation ticket's effect, not a new call this ticket itself makes.

## Findings

**[MUST_FIX] Contract Specificity — fabricated identifier confirmed as MATCH in Touch-point Contract**
- What's missing: the plan's Touch-point Contract names a test helper `_inline_codex_executor` at `tests/test_codex_executor.py:75-84` and marks it MATCH; the real helper at that location is `_sync_codex_executor` (defined at line 72, used at 17 call sites in the file).
- Why it matters: a false MATCH confirmation on a fabricated identifier is the exact failure class v2 enforcement targets — the plan-reviewer agent spec's own cited real incident was four fabricated touch-point claims that went undetected until independently disconfirmed. Practical drift risk here is low since the fabricated name does not appear anywhere in the ADR's own shipped content (the `## Tickets` item naming this file only says "rewrite ... the thread-registry assertions in `tests/test_codex_executor.py`," with no specific helper name), but the Touch-point Contract's entire value proposition is that every entry is independently trustworthy — one silent fabrication erodes that guarantee for the whole section.
- How to fix: correct the identifier to `_sync_codex_executor` in that bullet (or remove the specific name and keep the claim about its semantics/purpose generic) before the `plan-reviewed` marker is appended.

## Friction Report
- **Level**: WARN
- **Scope**: 0 files changed by me — review only. Plan under review touches 2 files (~140 lines new ADR + ~1 line README).
- **Assumptions**: NONE — every claim checked was verified by reading the cited file/line directly, not inferred.
- **Deviations**: NONE.
- **Discoveries**: the one fabricated identifier described above. Also independently confirmed (not just trusting the plan's self-report) that #2367 is genuinely OPEN via `gh issue view`, and that `start_new_session` has zero hits anywhere in `src/` via grep — both of R4's grounding claims check out under direct verification, not just plan-author assertion.
- **Risks**: NONE — docs-only ticket, no shared code touched by this plan's own deliverable (the ADR document and one README row).

## Health Check
- **Context usage**: LOW
- **On-spec confidence**: HIGH
- **Shortcuts taken under pressure**: NONE — read all 17 touch-point files/ranges directly (module docstrings, function bodies, test files, template, README index) rather than sampling a subset, given the explicit instruction to verify high-blast-radius categories (call-graph, return types) plus the general v2 mandate to distrust self-reported quotes.
- **Could work be incomplete?**: NO — all 4 checks were run to completion; the one gap found is fully characterized with an exact fix.
- **Recommendation**: PROCEED — route back to plan revision for the single one-line Touch-point Contract fix (rename or de-specify the fabricated identifier), then either a quick re-check of that one line or accept the fix inline, before the `plan-reviewed` marker is appended.
