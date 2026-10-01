# Codex CLI live-session smoke probe

Run one opt-in Codex CLI create/resume smoke test in an isolated temporary Git repository; this does not validate CW queue/executor integration.

This operator-only probe is not part of normal CI. Run it with an explicit
model:

```bash
CW_CODEX_LIVE_SESSION_SMOKE=1 python scripts/probe_codex_live_session.py --model gpt-5.6-luna
```

Without the exact opt-in value, it launches no subprocess and returns
`opt_in_required`. With opt-in, it validates `codex --version`, initializes a
disposable repository below `$HOME/.cache/cw-live-tests`, creates a read-only
JSONL session, validates its session ID and completed turn, then resumes that
same ID. The fixed prompts ask Codex not to use tools or modify files.
Create and resume use the same disposable repository and each subprocess has a
120-second timeout.
Subprocess text is decoded as UTF-8 with invalid bytes replaced; replacement
that disrupts the version token or JSONL stream uses the existing sanitized
version-invalid or stage-specific malformed-stream result.

The probe uses the operator's existing `CODEX_HOME` (or the normal
`~/.codex` default) for create/resume. It does not replace, clean, or delete
that home; Codex leaves one normal persistent session there, which the operator
can inspect or remove by the returned ID. The version and Git setup processes
receive no Codex home or API-key variables. Child `HOME` and temporary paths
are isolated under the disposable directory. A preflight check rejects a
configured Codex home or session-artifact directory inside the source checkout.
Once create has returned a validated ID, later failures and Ctrl-C preserve
that ID and the compact create summary so the operator can recover the session.

Resume omits `--sandbox` because it inherits the create session's read-only
policy. The probe never uses `--last`, `--ephemeral`, `--approve-for-me`,
`--dangerously-bypass-approvals-and-sandbox`, or `workspace-write`. It
captures stdout/stderr in memory and writes no output file. It relies on
Python's standard `subprocess.run` timeout behavior for the direct child; it
is not a process-tree supervisor or a security boundary. A normal SIGINT
(`Ctrl-C`) becomes a Python interruption and returns a sanitized failure where
Python unwinds, with temporary-directory cleanup attempted. SIGTERM keeps its
normal process behavior and can bypass cleanup, leaving the disposable
directory behind; SIGKILL is likewise uncatchable. The probe does not install a
signal or child-process supervisor.

Every invocation emits one compact JSON line and no stderr. The object has
exactly seven keys: `status`, `cli_version`, `model`, `session_id`,
`create`, `resume`, and `error_code`. Only validated IDs and compact
summaries are returned; transcripts, prompts, paths, stderr, environment
values, exception text, and authentication material are not included.
Unexpected defects return `internal_error`; temporary-directory cleanup
failures return `cleanup_failed`.

The public result boundary contains one narrowly scoped Ruff `BLE001`
suppression, explicitly approved by the issue owner on 2026-09-30. It maps
unexpected defects to the fixed `internal_error` result without leaking a
traceback to stderr; no other suppression is used by this probe.

The `error_code` values are `null` (success), `opt_in_required`,
`invalid_model`, `version_unavailable`, `version_invalid`, `cli_unavailable`,
`repo_setup_failed`, `internal_error`, `create_timeout`,
`create_nonzero_exit`, `create_malformed_jsonl`, `create_missing_thread`,
`create_invalid_thread_id`, `create_duplicate_thread_started`,
`create_invalid_terminal`, `resume_timeout`, `resume_nonzero_exit`,
`resume_malformed_jsonl`, `resume_missing_thread`,
`resume_invalid_thread_id`, `resume_duplicate_thread_started`,
`resume_id_mismatch`, `resume_invalid_terminal`, and `cleanup_failed`.
A pass requires both zero exit codes, valid `turn.completed` events, and an
exact resume-ID match.

The probe does not intentionally modify the source checkout, CW queue,
executor, roster, or event history. Its temporary Git repository is removed
after the run; its persistent Codex session is not.
