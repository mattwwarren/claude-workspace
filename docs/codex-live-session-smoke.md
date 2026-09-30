# Codex CLI live-session smoke probe

Run one opt-in Codex CLI create/resume smoke test in an isolated temporary Git repository; this does not validate CW queue/executor integration.

This operator-only probe is separate from normal CI and CW's deterministic
executor tests. Run it with an explicit model:

```bash
CW_CODEX_LIVE_SESSION_SMOKE=1 python scripts/probe_codex_live_session.py --model gpt-5.6-luna
```

Without the exact opt-in value, it launches nothing and returns a skipped
result. With opt-in it validates exactly `codex --version`, initializes one
disposable temporary Git repository below `$HOME/.cache/cw-live-tests`, then
runs:

```text
codex exec --json --sandbox read-only --ignore-user-config --model MODEL PROMPT
codex exec resume SESSION_ID --json --ignore-user-config --model MODEL PROMPT
```

The create prompt is `Reply exactly cw-session-smoke-ok. Do not use tools or
modify files.` The resume prompt is `Reply exactly cw-session-smoke-resumed-ok.
Do not use tools or modify files.` Resume deliberately has no `--sandbox`: it
inherits the create session's read-only policy. The probe never uses `--last`,
`--ephemeral`, `--approve-for-me`,
`--dangerously-bypass-approvals-and-sandbox`, or `workspace-write`.

Each subprocess has a 120-second timeout and runs in its own process group on
POSIX. Timeout, output overflow, or a parent SIGINT/SIGTERM kills the group,
including descendants that might otherwise keep stdout open. Parent-side
exceptions also trigger process-group termination, bounded reader waiting, and
child reaping. The daemonized reader owns and closes its pipe when it finishes;
an incomplete reader cannot hold interpreter shutdown hostage. A descendant
that deliberately escapes the POSIX process group is outside the termination
guarantee, but its open pipe is reported as incomplete rather than blocking the
probe. If a child does not reap after the bounded cleanup retries, the probe
reports `internal_error` rather than claiming a timeout or unavailable CLI.
Reader-worker exceptions are handed back to the process-waiting thread
and become `internal_error`, without a worker-thread traceback. An operator
interrupt returns the
sanitized timeout result for an interrupted subprocess, or `repo_setup_failed`
if interrupted outside a subprocess, instead of leaving Codex running or
printing a traceback. Unexpected internal errors produce a sanitized
`internal_error` result and a nonzero exit rather than being mislabeled as a
setup failure. After child exit, the stdout reader has a bounded drain window;
the probe then terminates the process group if capture is still incomplete.
Stdout is read incrementally with a 1 MiB
cap; exceeding it fails JSONL validation. Stderr is discarded, and no `-o`
file is created. The parser reads the bounded JSONL stream line by line and
releases it before starting the resume command. The first nonblank event must
be `thread.started`; later nonterminal events before it are malformed. It
recognizes `turn.started`, `item.started`, `item.updated`, and
`item.completed`; a top-level `error` event must contain a string message.
Well-formed unrelated event types are ignored for forward compatibility after
`thread.started`, while unknown `turn.*` or terminal-shaped event types fail
JSONL validation as `*_malformed_jsonl`. An error event followed by
`turn.completed` cannot pass;
`turn.failed` remains a failure. Acceptance requires one valid `thread.started`
ID before one terminal event; only `turn.completed` passes. Malformed,
duplicate, out-of-order, failed, missing, oversized, or invalid events fail.

The model value must match `^[A-Za-z0-9._-]{1,64}$`; a leading `-` is rejected
separately so a model cannot be interpreted as a CLI option. Reserved Codex
flags and policy values—including `--last`, `-o`, `--sandbox`, and
`workspace-write`—are rejected before any subprocess is launched. Even an
option-shaped value such as `--model --last` produces the sanitized
`invalid_model` JSON result with no argparse text on stderr.

The disposable repo and subprocess scratch directory are explicitly placed
under the home tree so snap-confined Codex can access them. The `codex --version`
check uses an allowlisted environment without `CODEX_HOME` or API keys. Create
and resume receive a minimal allowlisted environment: executable search path,
an isolated temporary `HOME`, the existing `CODEX_HOME` (or normal default),
`CODEX_API_KEY`/`OPENAI_API_KEY`, proxy/TLS transport variables,
`LANG`/`LC_ALL`/`LC_CTYPE`, and platform runtime paths.
Other `LC_*` variables are not inherited. Git initialization uses a separate
allowlist that excludes `CODEX_HOME` and API keys. Git routing, XDG, Codex
policy/profile, and unrelated runtime variables are not inherited. Git setup
also disables system Git configuration, so host-wide init templates are not
copied into the disposable repository.
The existing `CODEX_HOME` (or the normal `~/.codex` default) is passed as an
absolute path. Existing pre-launch path checks catch a configured home or
session-artifact path that already resolves into the source checkout when
validation runs. They guard against accidental redirection; this opt-in probe
assumes a trusted local user and is not an atomic sandbox against concurrent
same-user changes. The probe deliberately preserves and uses the operator's
existing `CODEX_HOME`; it does not attempt to isolate Codex authentication in a
replacement home or prevent a hostile same-user path swap during execution.

Every invocation, including `--help` and argument errors, prints exactly one
compact JSON object to stdout and nothing to stderr. `--help` is not a separate
human-readable mode; with opt-in enabled it returns `invalid_model` without
launching a subprocess. Its fixed keys
are `status`, `cli_version`, `model`, `session_id`,
`create`, `resume`, and `error_code`; it never prints paths, prompts, raw JSONL,
stderr, environment values, exception text, or authentication material.

The result contract is revision 2 (2026-09-29). It preserves the same seven
keys and field shapes as revision 1, and adds `internal_error` to the closed
`error_code` enum. This code is emitted only when an unexpected defect escapes
the expected-failure mappings; the result is failed, the CLI exits nonzero,
and no exception details are included. There are no repository-local
downstream consumers to update; external consumers that validate the v1
closed enum must add `internal_error` before adopting revision 2.

`create` is either null or `{"exit_code": INTEGER_OR_NULL,
"terminal_event": EVENT_OR_NULL}`. `resume` is either null or
`{"exit_code": INTEGER_OR_NULL, "terminal_event": EVENT_OR_NULL,
"id_matches": BOOLEAN}`. The only terminal-event values are `turn.completed`,
`turn.failed`, and null. `status` is `skipped`, `passed`, or `failed`; a skipped
run uses `error_code: "opt_in_required"`, a passing run uses `error_code: null`,
and a failure uses exactly one of `invalid_model`, `version_unavailable`,
`version_invalid`, `cli_unavailable`, `repo_setup_failed`, `internal_error`,
`create_timeout`, `create_nonzero_exit`, `create_malformed_jsonl`,
`create_missing_thread`, `create_invalid_thread_id`,
`create_duplicate_thread_started`, `create_invalid_terminal`, `resume_timeout`,
`resume_nonzero_exit`, `resume_malformed_jsonl`, `resume_missing_thread`,
`resume_invalid_thread_id`, `resume_duplicate_thread_started`,
`resume_id_mismatch`, `resume_invalid_terminal`, or `cleanup_failed`.
`internal_error` is reserved for an unexpected defect; expected operational
failures retain their specific codes. When known, it preserves the validated
session ID and compact create/resume summaries so the operator can locate the
session, without including transcripts, exception details, or credentials. A
create/resume launch failure uses
`cli_unavailable`; an oversized or unrecognized JSONL event uses the matching
`*_malformed_jsonl` code. SIGINT or SIGTERM during a subprocess uses that
command's timeout error code; an interrupt outside a subprocess uses
`repo_setup_failed`. A failed command exits 1; opt-out and a passing probe
exit 0.

The temporary repository is cleaned up on success and ordinary failure. If
cleanup itself fails, `cleanup_failed` takes precedence over an earlier
unexpected error while preserving the validated session ID and create/resume
summaries so the operator can recover. SIGINT or
SIGTERM received during cleanup is deferred until the temporary directory has
been removed; if cleanup succeeds, the result is `repo_setup_failed`, while an
actual cleanup failure remains `cleanup_failed`. One normal persistent Codex
thread remains in `CODEX_HOME`; the emitted validated ID lets
the operator inspect or delete it with normal Codex tooling. The probe does
not intentionally modify the source checkout, CW state, queue, executor,
roster, or event history.
