# Hardening a multi-task plan before dispatch

Lessons from building out RFC 0011 / the `/sprint-buildout` plan (7 TDD tasks,
#1174–#1180). The plan doc's literal code blocks are transcribed **verbatim**
by the headless `/auto-dev` worker, so a defect in the plan is a defect in the
implementation — and the plan reviewer allows only **one** revision cycle, so
each defect it finds costs a full ~10-minute dispatch.

## The core rule

**Before dispatching a task whose plan contains literal code, run an exhaustive
Plan-Reviewer sweep of that task — grounded in the real code on `main`, and
verified with an actual `ruff`/`mypy` run on a reconstruction of what the worker
would produce.** Fix every finding in one pass. Do not dispatch and let the
reviewer find them one at a time.

Observed payoff: Task 3 (#1175) was dispatched before this discipline and
bounced **six times** — every bounce a real plan defect, not a worker failure.
Tasks 4–7 were swept exhaustively first (7, 3, 7, and 1 MUST_FIX respectively)
and then cleared plan review and gates largely first-try.

## The recurring MUST_FIX failure classes

These are what the sweep must check for. They recurred across every task:

- **`E402` — imports appended after code.** A "Append to `foo.py`" fence that
  opens with `import ...` lands the imports after the module's existing code.
  Fix: the plan must show imports as a *separate, explicit edit to the existing
  top-of-file import block*, and the append fence must contain no import lines.
- **`I001` — unsorted imports.** New imports added as separate lines instead of
  merged into the existing sorted block. The gate runs `ruff check` **without**
  `--fix`, so this fails hard. Merge into the existing statement.
- **`TCH003` — un-guarded annotation-only import.** With `from __future__ import
  annotations`, a stdlib import used only in a type annotation must be
  `if TYPE_CHECKING:`-guarded (precedent: `src/cw/_util.py`, `src/cw/history.py`).
- **`E501` — lines > 88 chars.** Long Protocol/decorator/signature/`CliRunner`
  lines. Pre-wrap them in the plan; don't rely on the worker's format step.
- **Untested exception / failure branches.** Every `raise`, every `except`,
  every `if not match: return None`, every `(None, False)` vs `(None, True)`
  fork needs a test — patch coverage must clear **90%** on a new file with no
  other coverage to average against. Whole helper classes (e.g. a default
  client used only when `client=None`) are easy to leave 0%-covered.
- **Fabricated test helpers.** A plan that imports `_write_config` when the real
  helper is `_write_project_config_yaml` (in `conftest.py`) produces an
  `ImportError` at collection — failing *every* test in the file, not just the
  new ones.
- **Dropped registration entries.** A `cli/__init__.py` registration tuple
  edited "from memory" silently dropped an existing entry (`review`),
  deregistering an unrelated command group and breaking its passing tests.
- **Source files absent from the worker's worktree.** A plan that says "source
  the fixture from `.handoffs/…`" points the worker at a **gitignored** path
  that a fresh `git worktree` does not carry. Inline the data, or cite a tracked
  file.
- **Missing `## Touch-point Contract`.** The section that Read-quotes every place
  the plan leans on existing code (real signatures, the import-block insertion
  point, the exception convention). Its absence is what let the fabricated
  helper and dropped-registration defects ship. Every task attaching to existing
  code should have one.
- **`Files:` header omits a touched file.** e.g. Step 3 appends to
  `exceptions.py` and the commit includes it, but the header lists only two
  files. Reconcile the header against what actually gets touched.

## Verify with a reconstruction, not by eyeballing

The high-value sweeps *built a throwaway `git worktree` off `main`, applied the
plan's fences as a worker would, and ran `ruff check` + `mypy --strict`*. That
is what caught the `E402`/`I001`/`E501` classes deterministically and even found
lint defects the text review missed (`PLR2004`, `ARG005`, `EM101`). A free-name
`ast` pass over each code block (names loaded but never bound, minus builtins,
accounting for same-file carryover) catches missing imports — which are **not**
`SyntaxError`s and so slip past an `ast.parse`-only check.

## Two dispatch-flow gotchas that cost real time

- **Additive test-file conflicts at finalize.** When two tasks in the same wave
  both append tests to the same file (`test_sprint.py`), the second to reach
  finalize hits a merge conflict and blocks with no PR. The work is fine — it
  just needs a rebase (keep both test blocks). Either sequence such tasks, or
  expect the hand-rebase.
- **A hand-finished ticket needs its issue closed explicitly.** When you open a
  PR by hand (recovering a crashed/blocked finalize), it won't auto-close the
  issue unless the body carries `Closes #N`. Add it, or `gh issue close` after
  merge.

## Premise gate: real vs. spurious parks

A worker parks on `premises_pending_verification` when it can't confirm an
external fact. Distinguish:

- **Genuinely un-verifiable in the sandbox** (e.g. a live Notion/OAuth read —
  headless workers lack those connectors by design): the park is *correct*.
  Verify it out-of-band and answer as a comment.
- **Self-verifiable from authoritative sources** (`--help`, official docs, the
  merged dependency's source): the worker should proceed and log it as friction,
  not park. When it parks anyway, that's the gate defect tracked in #1192 — a
  per-ticket standing-authorization comment unblocks it; enumerating answers
  never converges (there is always one more unlisted claim).
