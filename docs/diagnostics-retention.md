# Executor Diagnostics Retention (#1239)

When an executor fails to produce a usable result — a codex reviewer role that
timed out / exited non-zero / returned unparseable output, or an aider
(LocalExecutor) run that never committed or died before exec — `cw` writes a
**diagnostics bundle** capturing what happened, for local post-mortem.

## Bundle location

```
~/.local/share/cw/sessions/<session_id>/diagnostics/
```

(`$XDG_DATA_HOME/cw/...` when `XDG_DATA_HOME` is set.) The path is
executor-neutral and keyed by the cw session id.

## Bundle contents (naming convention)

Each failure writes up to three files, all prefixed `<role-slug>-<reason>`:

- `<role-slug>-<reason>.json` — the typed, **sanitized** `ExecutorFailure`
  record: classified failure category, redacted `argv`, secret-scrubbed and
  length-bounded (4000 char) stdout/stderr/structured-output excerpts, timing,
  and exit code.
- `<role-slug>-<reason>-schema.json` — a raw copy of the output schema handed to
  the executor (codex reviewer roles only), when present.
- `<role-slug>-<reason>-output.json` — a raw, **unredacted** copy of the
  executor's structured output scratch file, when present.

`<role-slug>` is the reviewer role for codex (e.g. `code-quality-reviewer`) or
`aider` for LocalExecutor failures. `<reason>` is the typed failure category:
`timeout`, `nonzero_exit`, `spawn_error`, `missing_output`, `empty_output`,
`invalid_json`, `schema_mismatch`, `runtime_error`, or (reserved)
`semantic_validation_failure`.

The `-schema.json` / `-output.json` copies are the only unredacted tier and are
**state-dir-only** — never echoed to stdout or GitHub. The blocked sentinel's
`blocker.details` carries only a pointer to the bundle directory, never the raw
excerpts.

## Retention

Bundles are swept on a retention window. Every `dispatch_tick` runs a best-effort
cleanup pass (outside any lock, errors swallowed) that removes any bundle whose
newest file is older than the window.

- **Default:** 24 hours.
- **Configurable** via `diagnostics_retention_hours` in `orchestrator.yaml`:

  ```yaml
  diagnostics_retention_hours: 48   # keep bundles for two days
  ```
