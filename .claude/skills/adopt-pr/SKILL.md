---
name: adopt-pr
description: Adopt an existing open human-authored pull request into the cw dispatch pipeline (RFC 0016) — resolve which ticket the PR belongs to, echo the resolved identity to the operator, then enqueue it with `cw dev-queue add <TICKET> --adopt-pr <N> -c <client>` pinned to its current head, so a green PR enters at review and a non-green one at impl with synthesized repair feedback. Use when the user says "adopt PR 123", "pull this PR into the queue", "run the pipeline on this open PR", "review and ship PR N", or otherwise wants an already-open PR (not a ticket) driven through review and finalize.
---

# adopt-pr

Brings one open, same-repository pull request into the dev queue as a
review-entry row (green PR) or an impl-entry repair row (non-green PR). The CLI primitive does the mechanics and the safety checks;
this skill does the part the CLI deliberately does not (ADR-0013): working out
**which ticket** the PR belongs to, showing that to the operator, and
knocking when it cannot tell.

What adoption means, so you can explain it:

- A green PR enters at **review**, never plan, and is pinned to the PR's
  current head SHA (`adopted_head_sha`). It is always reviewed at that head.
- A non-green PR (CONFLICTING and/or failing CI) enters at impl with
  synthesized repair feedback: cw reads the PR's conflicted files and the
  failing checks' log tails into a bounded, untrusted-data report bound to the
  pinned head, and Stage 2 makes the PR green from it with no plan. To supply
  the feedback yourself, add `--stage impl --review-feedback-file <file>`.
- If someone pushes to the PR outside a cw session, the next claim parks the
  row `adopted_head_drift`. Only the operator releases it, with
  `cw dev-queue requeue <TICKET> -c <client> --repin-adopted-head`, and only
  when the new head descends from the pin.
- GitHub still computing mergeability parks `adopt_pr_state_unknown` (a v1.87.0
  queue may also hold the legacy `adopt_pr_not_green`, no longer stamped). A
  plain `cw dev-queue requeue` re-reads the PR and releases the park only once
  it is green, then re-pins to the fresh head and prints whether it descends
  from the old pin (`history rewritten` if not). If the re-read finds it not
  green, the requeue refuses and names `cw dev-queue remove` then a fresh
  `cw dev-queue add --adopt-pr`.
- A draft PR is adopted as-is; finalize marks it ready immediately before
  arming auto-merge.

## Inputs

- A PR number `<N>` (required) and the client `<client>` (required; ask if the
  user did not name one and the repo maps to more than one client).
- Optionally an explicit ticket from the user (`--ticket 1234`, "it's for
  #1234").

## Step 1: Refuse what can never be adopted

Read the PR once:

```bash
gh pr view <N> --json state,isCrossRepository,isDraft,headRefName,url,closingIssuesReferences
```

- `isCrossRepository: true` → **refuse**: fork PRs are not adoptable (RFC
  0016). Tell the user and stop; do not suggest a workaround.
- `state` not `OPEN` → refuse: there is nothing to adopt.

The CLI re-checks both (and more) itself; checking here just avoids a
pointless echo.

## Step 2: Resolve the ticket — the four rungs, in order

Stop at the first rung that yields exactly one ticket.

1. **Explicit ticket.** The user named one (`--ticket`, "for #1234"). Use it.
2. **`closingIssuesReferences`.** From the Step 1 read: exactly one closing
   issue in this repository → use its number. Zero → next rung. More than
   one → knock (rung 4) listing them.
3. **Agent parse.** Read the PR body, the head branch name and the commit
   subjects (`gh pr view <N> --json body,commits`). Look for an unambiguous
   reference: `Fixes #1234`, `Refs #1234`, a branch like `dev/1234` or
   `fix/1234-foo`. One clear candidate → use it, and say in the echo that it
   was inferred. Several, or only a weak hint → knock.
4. **The knock.** Ask the operator, and wait. Offer exactly these choices:
   - **a ticket** — the operator names the ticket this PR implements;
   - **PR-as-ticket** — adopt the PR as its own ticket (the ticket id is the
     PR number, `<N>`);
   - **stop** — do not adopt.

A cross-tracker id you noticed along the way (for example `ABC-123` in the
body) is shown in the echo as a label only. It never becomes the row's
ticket id and is never written anywhere.

## Step 3: Echo before adding

Before any `cw dev-queue add`, print what is about to happen and why:

```
Adopting PR #<N> (<url>)
  branch:  <headRefName>        draft: <yes|no>
  ticket:  <TICKET>   (resolved by: explicit | closingIssuesReferences | inferred from <where> | operator | PR-as-ticket)
  client:  <client>
  label:   <cross-tracker id, if any>
  enters:  review, pinned to the PR's current head
```

If the ticket came from rung 3, ask for a one-word confirmation before adding.

## Step 4: Add

```bash
cw dev-queue add <TICKET> --adopt-pr <N> -c <client>
```

Read the output; do not paraphrase it away:

- `Adopted ... at review, pinned <sha>` → the row is PENDING; the dispatch
  loop picks it up like any other row.
- `Adopted ... at impl, pinned <sha>; repair feedback synthesized (...)` →
  the PR is non-green (the parenthesis names every condition) and the row is
  PENDING at impl; Stage 2 repairs it from the synthesized feedback.
- `Adopted ..., parked adopt_pr_state_unknown: ...` → the row exists but
  waits for GitHub to compute mergeability; `cw dev-queue requeue <TICKET> -c
  <client>` re-checks and releases it.
- A non-zero exit is a **refusal** and wrote no row. Relay the message
  verbatim. The common ones: the PR or its branch is already carried by a live
  row (inspect that row with `cw dev-queue show`), the ticket already has a
  row, the head branch fails the row charset, the PR is from another
  repository, gh could not read it, or cw could not build the repair feedback
  (the message names the failed step; a head that moved mid-read says re-run).

## Writing to GitHub

This skill normally writes nothing to GitHub. If the operator asks you to
leave a comment on the PR or the ticket (for example, noting the adoption),
author the body via the **Write tool** — see CLAUDE.md's **Agent File Operations** rule — to a file under `.cw/`, then pass it with `--body-file`:

```bash
gh pr comment <N> --body-file .cw/adopt-pr-comment.md
```

## Do not

- Do not adopt a fork PR, and do not suggest pushing it to a branch here.
- Do not pick between several candidate tickets yourself; knock.
- Do not pass `--stage`: adoption always enters at review (anything else is
  rejected).
- Do not run `--repin-adopted-head` on the operator's behalf to clear a drift
  park: it absorbs commits nobody has reviewed. Surface the park and the
  commit range the attention event printed, and let the operator decide.
