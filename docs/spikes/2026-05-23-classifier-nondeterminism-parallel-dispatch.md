# Spike: Auto-mode Classifier Non-Determinism Across Parallel Sessions

**Date:** 2026-05-23
**Provenance:** promoted from issue #183 (a research/observation ticket, closed during the 2026-07-07 ticket audit — patching was explicitly out of scope). Preserved here because the observation and its open questions inform how headless `/auto-dev` should treat classifier denials.
**Status:** Observation only. Not a clean bug; the classifier is opaque by design.

## Observation

Three parallel `/auto-dev 171 --headless` sessions (N=3 meta-test variants a/b/c) spawned
via `cw spawn --headless` at ~15:10 UTC all reached the same Stage 1c exit point and tried
to post the `AUTO_DEV_RESULT` ambiguity comment to issue #171 via `gh issue comment`.

- **Variant a** — comment posted, sentinel emitted, clean exit (~15:17).
- **Variant b** — comment posted, sentinel emitted, clean exit (~15:18).
- **Variant c** — the **same** operation was **denied** by the auto-mode classifier:
  *"Posting a comment to GitHub issue #171 is an External System Write under the agent's
  identity."* Variant c then stalled with no recovery path (see the then-sibling #182).

All three ran under the same user identity, carried identical `cw-context.json` (modulo
session id / label / worktree), ran identical prompts, and hit the gh-comment step within
~1 minute of each other. One of three was blocked on an operation identical in shape —
non-deterministic from the caller's vantage point.

## Hypotheses (not investigated)

- **Rate-limit / burst heuristics** — three concurrent gh-comment ops in a short window
  per identity may have read as a burst.
- **Context divergence** — the three variants generated different surrounding prose
  (ambiguity counts 5/7/2); the classifier may match on context, not just the canonical
  command.
- **Session salting** — policy may incorporate a per-session salt.
- **Timing race** — a mid-window policy update could hit sessions at different versions.

## Open questions this raises for the pipeline

1. Is classifier output a deterministic function of (operation, identity, context), or is
   there genuine non-determinism?
2. If non-deterministic, is `retry_eligible: true` (per #174 Phase E) the right semantic on
   a classifier denial?
3. Is there documentation of denial patterns that skill authors can target to
   pre-emptively structure operations to avoid deny paths in headless mode?

## Out of scope

- Reverse-engineering the classifier (opaque by design).
- Patching anything in claude-workspace — this was observation, not action.
