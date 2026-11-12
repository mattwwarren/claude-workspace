# Codex live session smoke (operator probe)

Run one opt-in Codex CLI create/resume smoke test in an isolated temporary Git repository; this does not validate CW queue/executor integration.

Invocation (replace `gpt-5.6-luna` with the model your CLI accepts):

```
CW_CODEX_LIVE_SESSION_SMOKE=1 python scripts/probe_codex_live_session.py --model gpt-5.6-luna
```

Without `CW_CODEX_LIVE_SESSION_SMOKE=1` the script prints the opt-out object and launches nothing. It never runs in default CI.

## What it does

1. **Create** runs `codex exec --json --sandbox read-only --ignore-user-config --model MODEL <fixed prompt>` in a fresh temporary repository. The create step is read-only.
2. **Resume** runs `codex exec resume <id> --json --ignore-user-config --model MODEL <fixed prompt>`. It inherits the original session policy and passes no `--sandbox`.
3. The temporary repository is removed unconditionally.

Each subprocess has a 120-second timeout. Stdout and stderr are captured in memory and discarded. The script writes no output file.

## Result

The script prints one compact JSON object on stdout and nothing on stderr. It always has exactly these seven keys:

- `status`: `skipped`, `passed` or `failed`
- `cli_version`: the validated `codex-cli X.Y.Z` string, or null
- `model`: the validated `--model` value, or null
- `session_id`: the validated create thread id, or null
- `create`: null, or `{exit_code, terminal_event}`
- `resume`: null, or `{exit_code, terminal_event, id_matches}`
- `error_code`: null, or one value from the closed enum below

A pass needs both steps to exit 0, both terminal events to be `turn.completed`, and `id_matches` to be true. Exit status is 0 for `skipped` and `passed`, and 1 for `failed`.

The closed `error_code` enum: `opt_in_required`, `invalid_model`, `version_unavailable`, `version_invalid`, `cli_unavailable`, `repo_setup_failed`, `internal_error`, the `create_*` codes, the `resume_*` codes, and `cleanup_failed`.

## Disclosure

- Create leaves one normal persistent thread in `CODEX_HOME` (`~/.codex` unless set). The script prints only the thread id. An operator can inspect or delete it.
- The temporary repository is removed. Its path is never printed.
- The script prints no paths, prompts, raw JSONL, stderr, environment, exception text or auth material.

## Limits

- The 120-second timeout applies to each direct child process only. It is not a supervisor, not a security boundary and not a race-proof sandbox.
- This is a trusted-local-user opt-in tool.
- The approved plan verified `codex-cli 0.156.1`. Your host CLI may differ, so the first live run is the verification on a newer CLI.
- The model name in the invocation is an example. Replace it with a model your CLI accepts.
- An operator Ctrl-C reaches the `sys.excepthook` and prints the `internal_error` object. This is harmless.
