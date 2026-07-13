# Design: `/sprint-buildout` — RFC → ticketed sprint block

**Date:** 2026-07-13
**Status:** Design approved; implementation plan pending
**Source:** `.handoffs/handoff-sprint-buildout-retro-2026-07-13-1306.md` (retro of the
RFC 0011 buildout session that produced milestone v1.20.0, epics #1151/#1152,
tickets #1153–#1165, and docs PR #1166)

## Problem

Turning an RFC into a ticketed sprint block is a ~1-hour, ~8-step pipeline that
is almost entirely mechanical, yet is re-derived from scratch every time by
reading prior issues to recover conventions. The RFC 0011 session spent its
cheapest minutes on archaeology (what is the epic title prefix? which Notion DB?
what does a sprint page look like?) and its most dangerous minutes on
un-gated side effects (`gh issue create` has no draft mode — typos ship
instantly).

The operator expects to run sprints on a ~2-day cadence, so this pipeline is not
a quarterly event.

## Non-goals

- **Sprint-boundary automation** (close milestone N, roll unfinished tickets to
  N+1, flip Notion status, spin up the next wave). This is the *higher-frequency*
  repetition — buildout fires once per RFC, the boundary fires every sprint — and
  it is deliberately deferred. Nothing in `src/`, `.claude/`, or `docs/` handles
  it today (verified by grep: zero hits for milestone handling or rollover). The
  plan is to run one real sprint boundary by hand, retro it with the same fidelity
  as the buildout retro, and spec `/sprint-advance` from observed reality rather
  than speculation. This design must not *preclude* that skill — see
  "Forward compatibility".
- Retro-fitting existing RFCs (0001–0010) to the new template.
- Notion prose generation. The sprint page's Goal / risk narrative is model-written;
  only its skeleton and properties are templated.

## Decisions

**D1 — Mechanics live in `src/cw/`, exposed as `cw sprint plan` / `cw sprint apply`;
the skill is a judgment wrapper.**
At a 2-day sprint cadence this code runs ~100×/year. A script in a skill dir gets
none of this repo's gates: `--cov=cw` doesn't see it, `mypy --strict src/` doesn't
see it, and it isn't versioned with releases. Code you run twice a week belongs
inside the coverage boundary.

**D2 — The RFC is a strict input contract**, enforced by a template plus a
validator that refuses non-conforming input with a named missing section.
The RFC 0011 buildout was cheap *because* the RFC was hardened the day before:
sprint composition was a verbatim transcription of its phasing table. Strictness
is what keeps buildout transcription rather than judgment.

**D3 — Exactly one operator gate**: draft everything, show one summary, execute on
confirm. Answers the retro's "typos ship instantly" friction without the per-phase
dribbling that `/orchestrate-sprint` names as an anti-pattern.

**D4 — Adjacent-bug pull-in: the skill does the legwork, the operator makes the
call.** A subagent scans open bugs for path overlap with the RFC's `References`.
The retro is explicit that the *call* is judgment (it required knowing which code
paths the sprint tickets touch). The *search* is not.

**D5 — Conventions and Notion IDs live in `.claude/project-config.yaml`**, not in
the skill body. Every such value in the RFC 0011 session was recovered by searching
for precedent — zero-judgment archaeology. The Notion block is optional; absent it,
the Notion phase silently skips.

## Architecture

Three layers, split on a single rule: **the script owns anything with a
shell-quoting hazard or an ordering constraint; the skill owns anything requiring
a call.**

### 1. Input contract — `docs/rfcs/TEMPLATE.md`

New file. Required sections, each of which the validator checks by heading:

- `## Phasing` — a wave→ticket table. The source of the sprint map and of each
  ticket's Dependencies.
- `## Resolved decisions` — `D-*` entries. The source of each ticket's Scope.
- `## References` — `file:line` refs. The source of the bug-pull-in overlap scan.
- An `Issues:` footer placeholder, back-filled with real numbers post-apply.

Existing RFCs are not migrated; the validator only runs on RFCs passed to the skill.

### 2. Mechanics — `src/cw/sprint.py` + `src/cw/cli/sprint.py`

Reuses existing infrastructure rather than adding parallel machinery:

- `cw.tracker.load_project_config_dict(root)` — the established
  `.claude/project-config.yaml` loader (already shared by `review_strategy.py`
  and `doctor.py`).
- `cw.gh` — the existing `gh` subprocess wrapper. It currently has
  `post_issue_comment` but no issue/milestone creation; those get added here,
  not in a new module.

Two subcommands, side-effect-free until `apply`:

**`cw sprint plan <rfc-path> --out plan.json`**

Pure function of (RFC text, project config, pyproject version). No network except
`gh release view` to derive the milestone version. Steps:

1. Read the RFC from `origin/main` (`git show origin/main:<path>`), falling back
   to disk. *Retro friction #1: the RFC merged to main after the worktree was
   created, so it was absent from the worktree — never assume worktree freshness.*
2. Validate against the D2 contract. Refuse with `missing section: ## Phasing`.
3. Derive the milestone title from the config pattern + pyproject version +
   latest release.
4. Build epics from the RFC's `### Epic` sections; build children from the
   Phasing table rows.
5. Assemble each ticket body by transcription: Context ← RFC section prose,
   Scope ← the `D-*` decisions it cites, Dependencies ← the phasing table,
   Acceptance ← exit-bar bullets, footer ← the configured pattern.
6. Emit `plan.json` (a Pydantic model) plus a human-readable summary.

**This is the unit-test surface** — fixture RFC in, expected plan out. It covers
the title patterns, the wave map, body assembly, and every validation refusal,
which is how the ≥90% patch-coverage gate gets met honestly.

**`cw sprint apply plan.json`**

The three-pass `gh` dance that GitHub's number assignment forces, executed
deterministically:

1. Create the milestone; **capture its number** (needed for the URLs used in both
   the Notion pages and the RFC footer — the retro notes this was hand-carried).
2. Create epics, whose bodies carry a `<!-- children -->` marker templated in from
   the start. *Retro friction #4: inserting the checklist afterward via Python
   string-replace was clunky precisely because the marker wasn't there.*
3. Create children (their bodies can now reference real epic numbers).
4. Back-fill each epic's checklist into its marker.

All bodies are written via `--body-file`, never a heredoc. *Retro friction #3:
milestone titles contain em-dashes and ampersands; `--body-file` sidesteps the
quoting entirely.*

`apply` is idempotent: a re-run after partial failure skips issues that already
exist, matched by title within the milestone.

### 3. Judgment — `.claude/skills/sprint-buildout/SKILL.md`

Ships to `~/.claude/skills/` automatically — `scripts/install-skills.sh` syncs
every directory under `.claude/skills/` with a manifest-scoped prune, so no
install plumbing is needed.

The skill drives the pipeline and owns the calls the script cannot make:

1. `cw sprint plan` → draft.
2. **Bug-pull-in scan** (D4): spawn a sonnet subagent to list open bugs whose
   touched paths overlap the RFC's `References` file:line refs; it returns
   candidates plus overlap evidence, not file dumps.
3. **The single gate** (D3): present titles, the wave→sprint map, and the pull-in
   candidates. Bodies shown on request. Approve / edit / abort.
4. `cw sprint apply` → real numbers.
5. Milestone + rationale comment on any pulled-in bug the operator accepted.
6. **Notion phase** (config-gated): one sprint page per wave — properties and
   section skeleton from config, narrative model-written. Skipped silently when
   the config has no `notion:` block.
7. **RFC footer PR**: back-fill `Issues:` with the real numbers; open a docs PR.

Judgment explicitly retained by the human: wave→sprint granularity (the plan
proposes 1:1, as RFC 0011 used; a thinner RFC may merge waves), the pull-in call,
and anything the RFC defers to `/harden-ticket` at dispatch time.

## Configuration

New `sprint_buildout:` block in `.claude/project-config.yaml`, documented in
`config/CONFIG_REFERENCE.md`:

```yaml
sprint_buildout:
  milestone:
    title_pattern: "v{version} — {rfc_title}"
  epic:
    title_pattern: "epic: {name}"
    labels: []                          # epics are deliberately unlabeled
    children_marker: "<!-- children -->"
  ticket:
    title_pattern: "RFC {rfc_num} {code} — {name}"
    labels: [feature]
    footer_pattern: "Part of RFC {rfc_num} Wave {wave} (Sprint {sprint}), Epic #{epic}"
  notion:                               # omit ⇒ Notion phase skips
    data_source: "collection://673ac7cd-797a-4c76-b9eb-fb5bc7ee050a"
    project_page: "38b59b27-0a42-81da-b234-ea951daa0216"
    sprint_page_properties:
      Type: Sprint
      Status: Planning
      Repo: claude-workspace
```

Absent block ⇒ the skill refuses with a pointer to `CONFIG_REFERENCE.md` rather
than guessing conventions.

## Forward compatibility with `/sprint-advance`

The deferred boundary skill will need the same three things this design already
centralizes, so it should slot in without rework:

- the `sprint_buildout:` config block (rename to `sprint:` with `buildout:` /
  `advance:` sub-keys if and when the second consumer lands);
- issue/milestone creation helpers in `cw.gh` (the boundary needs milestone
  *close* and issue *re-milestone*, siblings of what we add here);
- the Notion IDs, which the boundary needs for the Planning → In Progress → Done
  status flips.

No speculative code is written for it now.

## Testing

- `tests/test_sprint.py` — fixture RFCs → expected plan JSON; one fixture per
  validation refusal (missing Phasing, missing Resolved decisions, missing
  References, missing Issues footer).
- `apply` is tested against a faked `cw.gh` surface, asserting call *order*
  (milestone → epics → children → checklist backfill) and idempotent re-run.
- `tests/test_cli.py` — Click `CliRunner` coverage of `cw sprint plan|apply`,
  matching the existing CLI test pattern.

## Open risks

- **Body assembly is the fuzziest step.** "Context ← RFC section prose" assumes
  the RFC's Epic sections are cleanly delimited. If the first real dry-run (on
  RFC 0012) produces bodies that need hand-editing, the fix is to tighten the
  template further, not to add inference to the parser.
- **The template only helps future RFCs.** The next buildout is only as cheap as
  RFC 0012 is well-formed.

## Validation

Dry-run `cw sprint plan` on RFC 0011 and diff its output against the artifacts
this session actually produced (#1151–#1165). A faithful reproduction is the
acceptance bar; divergence points at either a template gap or a parser gap.
