# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.77.2] - 2026-10-25

### Fixed

- **Usage limits worded without "hit" are now detected (#2220).** `USAGE_LIMIT_RE` only matched "You've hit your ... limit", so a spawn, transcript or reconcile scan missed `Usage limit reached`, `usage limit; resets <time>` and `Usage limit reached · limit resets <time>`, and treated a usage-limited worker as an ordinary failure. The wordings now live as rows with provenance in the new `cw._usage_limit_patterns` table and are joined into the same `USAGE_LIMIT_RE`. Reset-bearing rows are anchored by lookahead on a clock time and the bare `Usage limit reached` row skips quoted or backticked mentions, so prose such as "rate-limit resets 3pm" does not match. The non-blocking `until your limit resets at <time>` and `Your <model> limit resets <time>` notices are deliberately not matched: the worker keeps running after them.
- **Runtime messages no longer name `cw` commands that do not exist (#2703).** The `wedge/repo-ahead-of-queue` recipe told operators to run `cw spawn-complete <ticket>`, and `cw peek` pointed at `cw post-mortem`; neither command exists. The wedge recipe now reads `cw spawn complete <session_id> --status shipped` (with a `<session_id>` placeholder when the task has no session recorded), `cw peek` names `cw session result` / `cw session show`, and the duplicate-lane error from `cw orchestrate start` names `cw spawn close <id>` instead of `cw spawn complete <id> --status completed`, whose `completed` is not a valid `--status`. A new walker in `tests/test_cli_surface_conventions.py` parses every `src/cw/**/*.py` runtime string and fails when a `cw ...` mention names a command the Click tree cannot resolve.
- **`cw dev-queue approve` no longer warns of a ticket-body edit when only a trailing newline differs (#2761).** The plan stage hashed the ticket body by hand, and a newline added on the way in made every approve report drift on an unedited body. Approve and a new `cw body-sha` command now share one function, `cw.plan_fingerprint.compute_body_sha`, which strips trailing whitespace before hashing. `cw body-sha TICKET -c CLIENT` fetches the GitHub body; `cw body-sha --body-file PATH` (`-` for stdin) hashes a body the worker wrote to a file, which is the Linear path. Every failure exits 1 with nothing on stdout. The plan-stage prompts now tell the worker to persist the command's output verbatim and never hash by hand. A draft persisted with the old hash warns once more at approve, or takes the full re-scan; the next plan round rewrites the marker.

## [1.77.1] - 2026-10-19

### Docs

- **A tracked `cw-orchestration` skill maps every queue state to the `cw` command that releases it (#2698).** The skill moves into this repo from the global config repo and is rewritten: a state-to-command list that defers to `cw dev-queue show`'s `next:` line and marks each entry orchestrator-run or operator-only, command semantics for `cw dev-queue wait` (exit codes 0-5 and 124), `cw orchestrate watch`, `cw preflight`, `cw lane set`, `cw config concurrency set` and `cw review settle`, and cited cw hazards. A doc test resolves every command and flag in it against the live CLI and checks its first commands against `next_commands`. After upgrading, run `scripts/install-skills.sh` so `~/.claude/skills/cw-orchestration` links to the tracked copy.
- **Orchestrator skills hold one silent Monitor and stop timer polling (#2698).** `orchestrate-sprint` loads `/cw-orchestration` first, reads cw state through `cw dev-queue show`/`tasks`, `cw queue peek` and `cw session result`, bans `ScheduleWakeup`, cron and sleep polling, emits a `CKPT` line only when something changed, and adds `resolve`, `move` and the ADR-0020 standing authorization to Decision ownership. `cw-followup` and `cw-fanout` release parked rows with `cw dev-queue resolve` or `requeue` instead of a refused `cw dev-queue add`, and `cw-fanout` replaces the `dev_queue.json` hand-edit with `requeue --reset-attempts`. `harden-ticket` takes flagged and twice-parked tickets, and `/handoff` gains an orchestrator section that carries state, not recipes.

