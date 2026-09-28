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
POSIX. Timeout or output overflow kills the group, including descendants that
might otherwise keep stdout open. The stdout reader has a bounded shutdown
wait, so it cannot defeat the process timeout. Stdout is read incrementally
with a 1 MiB cap; exceeding it fails JSONL validation. Stderr is discarded,
and no `-o` file is created. The parser reads the bounded JSONL stream line by
line and releases it before starting the resume command. Acceptance requires
one valid `thread.started` ID before one terminal event; only
`turn.completed` passes. Malformed, duplicate, out-of-order, failed, missing,
oversized, or invalid events fail.

The model value must be 1–64 characters, begin with an alphanumeric character,
and contain only letters, digits, `.`, `_`, or `-`. Reserved Codex flags and
policy values—including `--last`, `-o`, `--sandbox`, and `workspace-write`—are
rejected before any subprocess is launched. Even an option-shaped value such
as `--model --last` produces the sanitized `invalid_model` JSON result with no
argparse text on stderr.

The disposable repo and subprocess scratch directory are explicitly placed
under the home tree so snap-confined Codex can access them. Child processes
receive a minimal allowlisted environment: executable search path, an isolated
temporary `HOME`, `CODEX_HOME`, `CODEX_API_KEY`/`OPENAI_API_KEY`, proxy/TLS
transport variables, locale, and platform runtime paths. Git routing, XDG,
Codex policy/profile, and unrelated runtime variables are not inherited. The
existing `CODEX_HOME` (or the normal `~/.codex` default) is passed as an
absolute path. If it resolves inside the source checkout, or the temporary
parent cannot be proven to remain outside the checkout, the probe stops with
the approved `error_code: "repo_setup_failed"` rather than emit an error
outside the documented closed enum or risk writing session state into the
checkout.

The script prints exactly one compact JSON object to stdout and nothing to
stderr. Its fixed keys are `status`, `cli_version`, `model`, `session_id`,
`create`, `resume`, and `error_code`; it never prints paths, prompts, raw JSONL,
stderr, environment values, exception text, or authentication material.

`create` is either null or `{"exit_code": INTEGER_OR_NULL,
"terminal_event": EVENT_OR_NULL}`. `resume` is either null or
`{"exit_code": INTEGER_OR_NULL, "terminal_event": EVENT_OR_NULL,
"id_matches": BOOLEAN}`. The only terminal-event values are `turn.completed`,
`turn.failed`, and null. `status` is `skipped`, `passed`, or `failed`; a skipped
run uses `error_code: "opt_in_required"`, a passing run uses `error_code: null`,
and a failure uses one of the closed documented `*_timeout`,
`*_nonzero_exit`, JSONL-validation, setup, version, model, ID, or cleanup error
codes. A create/resume launch failure uses `cli_unavailable`; an oversized
JSONL stream uses the matching `*_malformed_jsonl` code. A failed command exits
1; opt-out and a passing probe exit 0.

The temporary repository is cleaned up on success and ordinary failure. If
cleanup itself fails, the probe reports `cleanup_failed` while preserving the
validated session ID and create/resume summaries so the operator can recover;
cleanup failure takes precedence as the single reported error code. One normal
persistent Codex thread remains in `CODEX_HOME`; the emitted validated ID lets
the operator inspect or delete it with normal Codex tooling. The probe does
not modify the source checkout, CW state, queue, executor, roster, or event
history.
