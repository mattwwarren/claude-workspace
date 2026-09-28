# Codex CLI live-session smoke probe

Run one opt-in Codex CLI create/resume smoke test in an isolated temporary Git repository; this does not validate CW queue/executor integration.

This operator-only probe is separate from normal CI and CW's deterministic
executor tests. Run it with an explicit model:

```bash
CW_CODEX_LIVE_SESSION_SMOKE=1 python scripts/probe_codex_live_session.py --model gpt-5.6-luna
```

Without the exact opt-in value, it launches nothing and returns a skipped
result. With opt-in it validates exactly `codex --version`, initializes one
disposable temporary Git repository, then runs:

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

Each subprocess has a 120-second timeout and captures stdout/stderr only in
memory. There is no `-o` file. JSONL acceptance requires one valid
`thread.started` ID before one terminal event; only `turn.completed` passes.
Malformed, duplicate, out-of-order, failed, missing, or invalid events fail.

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
and a failure uses one of the documented `*_timeout`, `*_nonzero_exit`,
JSONL-validation, setup, version, model, ID, or cleanup error codes. A failed
command exits 1; opt-out and a passing probe exit 0.

The temporary repository is always cleaned up. One normal persistent Codex
thread remains in `CODEX_HOME`; the emitted validated ID lets the operator
inspect or delete it with normal Codex tooling. The probe does not modify the
source checkout, CW state, queue, executor, roster, or event history.
