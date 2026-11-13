# Sweep checklist: historical cases and probe examples

The fixture set for the harden-ticket sweep checks in `SKILL.md` (M1-M4
mechanical checks, Q1-Q4 sweep questions, S1-S3 self-critique). Each row is a
real ticket and the check that would have caught its defect. If a case would
not be caught, say so here rather than adjusting the case: the checklist is
wrong, not the history.

## Cases

| ticket | check | caught? | why |
|---|---|---|---|
| #1392 | M1 | yes | The codex review module was already past the ~1000-line ceiling when the ticket added a fix-loop adapter; `wc -l` plus a growth estimate flags the projected breach as a split decision (it became #1462). |
| #1389 | M2 | yes | The ticket mandated a new helper where one existed; a `grep -rn` of `tests/` by capability keyword finds the existing one. |
| #1462 | M3 | yes | The ticket named paths that a recent refactor had moved; grepping each named path against the current tree flags the rename. |
| #1444 | M4 | yes | A mechanism claim carried no file:line or grep output; M4 makes an uncited claim a finding, not a premise. |
| #1303 | S1 | yes | The mandated `_lock = _bus._lock` aliasing trips `SLF001` under `src/`, which the repo ignores only under `tests/**`; a scratch `ruff check` reproduces it. |
| #1457 | S2 | yes | A fallback clause created a second write path outside the single result-publishing door, against ARCHITECTURE.md §8 anti-pattern 2 and §7 principle 2. |
| #1674 | Q2 | yes | The alternative mechanism, `_find_session_for_ticket` in `src/cw/reconcile/concierge/_common.py`, is status-blind and returns the old session; it sat in the same open file, so "what other mechanism" finds it. |
| #1689 | Q1 | yes | The observed 194ms flake exceeded the proposed 150ms threshold; both numbers were in the same document, and the comparison was never shown. |
| #1643 | M4 | yes | "The commit already has a CHANGELOG entry" had zero grep matches; one grep with a control (an entry that does exist) settles it. |
| #1638 | M4 + Q2 | yes | A transcript footer was read as a paused-sub-agent marker; it is universal boilerplate on every synchronous `Agent()` return. A control grep (M4) plus "what else produces this observation" (Q2) catches it. |
| #1618 | Q3 | yes | `model_dump(mode="json")` made the specified "add a plain field, drift guard goes RED" mutation unable to fire; the seam it probed no longer existed. |
| #1859 | Q4 | yes | A release-tag canary was hand-closed 31s after a dry-run whose closer step showed `skipped`. The account is #1693's; #1859 itself only shows the closed canary. |

## Probe examples (step 2b, #1238)

Facts that reading code could not settle, found by a cheap live probe:

- **R11, malformed-schema enforcement.** A live probe showed the external API
  reshapes output to the schema even when the prompt asks it to violate it, so
  a malformed-schema live fixture is unproducible. The resolution replaced it
  with an enforcement-conformance test; the parse-failure branch stays
  unit-covered.
- **R12, snap-sandbox `/tmp` blocking.** A snap-installed `codex` cannot reach
  `/tmp` via `-C`, and pytest's default `tmp_path` resolves there. The
  resolution put live-suite fixtures under a home-tree base dir.
- **R13, exit-code variance.** `-o` pointing into a nonexistent directory: the
  exit code is CLI-version behavior. The resolution recorded the observed
  branch and asserted on a tolerant set of outcomes, with an exit-code
  tolerance for other versions.

Record only exit codes and the error class or shape in the posted comment, and
redact credentials and identifiers first.
