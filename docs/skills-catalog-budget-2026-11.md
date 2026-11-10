# Skills-catalog delivery versus its own budget (#1834)

> **Caveat: internal and version-pinned.** Every statement below about the
> budget formula, the truncation algorithm, the `skill_listing` transcript
> attachment and the setting names was read from the Claude Code **2.1.322**
> binary and from real transcripts of that version. None of it is a documented
> contract, and it may change without notice in any release. Re-measure with
> `scripts/measure_skill_catalog.py` before relying on a number here.

This report follows up the bonus finding 5 in
[`token-attribution-2026-08.md`](token-attribution-2026-08.md) (#1810), which
read the debug line `Skill listing over budget: 122 skills, 32039 chars > 8000
budget` as the harness delivering about 4x its own budget.

## Summary

- **The budget is real and enforced.** The harness does not ignore it. The
  `32039 chars` in the WARN is the listing size *before* truncation (demand),
  not what is delivered.
- **There is no bug against cw.** A WARN naming a larger number than the budget
  is the harness announcing that it is about to truncate descriptions.
- **Real worker sessions are delivered about 30,000 chars (152 to 153
  entries), not 8,000.** The 8,000 figure is only the default for a 200K
  context window as seen by the `-d mcp` probe; real workers see a larger
  effective budget (the source of that larger figure is NOT_FOUND offline, see
  Limitations).
- **Pruning repo skills cannot lower the delivered size** (fill-to-budget, see
  below). The pruning that matters is in the user-global skill set, which is an
  operator action and is not changed by this ticket.
- **No measured reduction occurred.** Nothing in this ticket changes the
  baseline: no skill was pruned, no budget or setting was changed, and nothing
  under `src/cw` was touched. This ticket ships a measurement tool and this
  report only.

## Evidence

### Budget formula and knobs (binary 2.1.322)

- Budget in chars = `(contextWindow ?? 200000) * 4 * (skillListingBudgetFraction ?? 0.01)`,
  which is 8,000 at a 200K window. The env var `SLASH_COMMAND_TOOL_CHAR_BUDGET`
  overrides it outright. A feature-flag term can raise the budget when the
  fraction is unset; its value is not observable offline.
- Related settings: `skillListingBudgetFraction`, `skillListingMaxDescChars`
  (default 1536), `skillOverrides` (per-skill `name-only`/off), and
  `disableBundledSkills` (equivalently `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`).
  `claude --safe-mode` is listed in `claude --help`.

### Truncation algorithm

The harness computes the full listing size `H`. If `H <= budget` it delivers the
listing as is. Otherwise it logs `Skill listing over budget: <n> skills, <H>
chars > <budget> budget - descriptions will be truncated.`, keeps bundled skills
in full, collapses every other entry to `- name`, and then re-adds descriptions
in priority order until the budget is spent. So the WARN reports demand; what
follows is truncation to the budget.

### Delivered listing (transcript attachment)

Real worker transcripts carry an attachment whose type is `skill_listing`, with
keys `content`, `skillCount`, `isInitial`, `names` and `digests`. `content` is
newline-joined `- name: description` or `- name` entries; a description can
span continuation lines. Recent worker sessions on this machine delivered
`len(content)` of 29,983 to 30,001 chars and a `skillCount` of 152 to 153.
The tool reads the first initial attachment of one transcript, offline, with no
model call.

## Decomposition (counts and chars only)

Delivered listing, one recent worker transcript, regenerated with
`scripts/measure_skill_catalog.py --repo <repo> --cwd <repo>`:

| Source | Entries | Name-only | Chars | Share |
| --- | --- | --- | --- | --- |
| global-claude skills and commands | 61 | 37 | 10,244 | 34.3% |
| bundled/other | 17 | 0 | 8,062 | 27.0% |
| repo skills | 12 | 0 | 6,928 | 23.2% |
| repo commands | 25 | 3 | 2,749 | 9.2% |
| plugin namespaces (`ns:*`, 7 of them) | 37 | 34 | 1,866 | 6.2% |

Total 30,000 chars over 152 parsed entries (the attachment's own `skillCount`
is 152). Repo-owned content (skills plus commands) is about 32% of what is
delivered.

Offline demand for the same trees (frontmatter descriptions, capped at 1,536
chars each, before any truncation):

| Source | Entries | Chars | Share |
| --- | --- | --- | --- |
| global-claude | 61 | 29,033 | 76.9% |
| repo skills | 12 | 6,904 | 18.3% |
| repo commands | 25 | 1,801 | 4.8% |

Demand from these three sources alone is about 37,800 chars, before bundled and
plugin entries are added; that exceeds the delivered 30,000, which is why
truncation is active. Roughly 21 `aws-*` global skills account for about 15,000
chars of the global description demand.

### The 10,217-token gap is not only skills and hooks

#1810 attributed the 10,217-token difference between the `--safe-mode`
true-zero probe and the `strict-empty` baseline "almost entirely" to the skills
catalog plus hooks. The decomposition shows other components that
`--safe-mode` also drops. These are sizes, not a partition of the 10,217:

| Component | Chars | Est. tokens (chars/4) |
| --- | --- | --- |
| global `CLAUDE.md` | 16,838 | |
| `CLAUDE.local.md` | 470 | |
| `rules/**/*.md` | 8,890 | |
| the three context files together | 26,198 | 6,549 |
| delivered skill listing | 30,000 | 7,500 |

This is an offline chars/4 estimate, an uncalibrated one, not a live
measurement. The components are not additive against the gap, because the
`strict-empty` baseline already carries some of them. Agent descriptions are
not measured here. cw's own hooks live in each worktree's
`settings.local.json` and add no prompt text; their cost is not measured and is
expected to be small.

## Why local pruning does not help

Truncation fills the budget. Trimming a repo skill description frees characters
that the harness immediately refills with another skill's description, so the
delivered size stays at the cap. Repo-owned demand is about 8,700 chars (skills
plus commands); even a 50% trim would leave total demand of roughly 33,000 or
more, still over the cap, so delivery would not fall. Trimming would only
degrade how reliably the repo's own skills trigger. The pruning that changes
the delivered size is in the user-global set (the `aws-*`, `wiki-*` and
`peon-ping-*` families), which this repo does not own.

## Resolution

**Acceptance criterion 1 is met by the alternative "the budget is explicitly
revised with rationale": no revision is needed.** The WARN measures
pre-truncation demand, not delivery, so there is nothing to bring "within 8,000
chars". What is actually delivered is about 30,000 chars over 152 to 153
entries. That was measured by `scripts/measure_skill_catalog.py` reading the
`skill_listing` attachment of real worker transcripts (offline, no model call).
No repo pruning was done.

Operator actions (not part of this change):

- Prune or set `skillOverrides` to `name-only` for the `aws-*`, `wiki-*` and
  `peon-ping-*` families in the user-global skill set, and optionally lower
  `skillListingBudgetFraction`.
- Optionally give upstream feedback that the WARN wording reads like a
  violation when it reports pre-truncation demand.

Separate ticket and product decision (not implemented here):
`src/cw/spawn.py::_build_hook_settings` writes each dispatched worktree's
`.claude/settings.local.json`, so it is the place a future change could set
`skillListingBudgetFraction` or `skillOverrides` to shrink every worker's
listing (about 7,500 tokens per worker at chars/4, traded against skill
discoverability). Environment variables set in `native_daemon._spawn_clean_env`
do not reach `claude --bg` workers, so settings, not env, are the lever. Such a
change needs a live before/after measurement with the `strict-empty` and
`default-real` probes; their `probes.json` pipeline was scratchpad-only (see
"Method reproducibility" in `token-attribution-2026-08.md`).

## Acceptance criterion 2: not applicable

Criterion 2 asks to re-verify a baseline reduction with `strict-empty`. It is
conditional on something having been changed to reduce the baseline, and
nothing was. There is therefore nothing to re-verify. For the record, the
current measured numbers are: 30,000 chars delivered (about 7,500 tokens at
chars/4, an estimate) against the 10,217-token gap from #1810. **No measured
reduction occurred.**

## Re-measurement

```bash
uv run python scripts/measure_skill_catalog.py --repo "$PWD" --cwd "$PWD"
uv run python scripts/measure_skill_catalog.py --repo "$PWD" --transcript <path> --json
uv run python scripts/measure_skill_catalog.py --repo "$PWD" --debug-log <path>
```

`--cwd` picks the newest transcript for that directory; `--transcript` names
one explicitly; with neither, only offline demand is reported ("delivered: not
measured"). `--debug-log` parses a WARN line from a `claude --debug-file` run to
show the applied budget. The tool is read-only, never launches `claude`, and
prints counts, chars and bucket names only.

## Limitations

- One machine and a handful of recent sessions; numbers move as skills change.
- The source of the roughly 30K effective budget (window scaling or a feature
  flag term) is NOT_FOUND offline.
- chars/4 is an uncalibrated estimate, and the token figures are not live
  measurements.
- The listing parse is heuristic: it depends on an undocumented text format.
  The tool reports both the attachment's `skillCount` and its own parsed entry
  count so drift is visible.
- Demand covers only the repo and the user home; bundled and plugin entries
  appear only in the delivered view.
