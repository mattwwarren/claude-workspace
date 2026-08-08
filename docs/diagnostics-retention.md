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

Each failure writes up to three files, all prefixed
`<role-slug>-<category>-<timestamp>`:

- `<role-slug>-<category>-<timestamp>.json` — the typed, **sanitized**
  `ExecutorFailure` record: classified failure category, redacted `argv`,
  secret-scrubbed and length-bounded (4000 char) stdout/stderr/
  structured-output excerpts, timing, and exit code.
- `<role-slug>-<category>-<timestamp>-schema.json` — a raw copy of the output
  schema handed to the executor (codex reviewer roles only), when present.
- `<role-slug>-<category>-<timestamp>-output.json` — a raw, **unredacted**
  copy of the executor's structured output scratch file, when present.

`<role-slug>` is the reviewer role for codex (e.g. `code-quality-reviewer`) or
`aider` for LocalExecutor failures. `<category>` is the typed failure
category: `timeout`, `nonzero_exit`, `spawn_error`, `missing_output`,
`empty_output`, `invalid_json`, `schema_mismatch`, `runtime_error`, or
(reserved) `semantic_validation_failure`. `<timestamp>` is the failure's
`occurred_at` field (microsecond precision, `%Y%m%dT%H%M%S%f`), which
disambiguates repeat same-role/same-category failures within one session so
they no longer overwrite each other's bundle files.

The `-schema.json` / `-output.json` copies are the only unredacted tier and are
**state-dir-only** — never echoed to stdout or GitHub. The blocked sentinel's
`blocker.details` carries only a pointer to the bundle directory, never the raw
excerpts.

## Codex filesystem-capability artifact and cache (#1709)

Two files sit alongside the failure bundles above, written on **every** codex
review (not only on failure).

**Per-session artifact** — `codex-capability.json`, in the same
`sessions/<session_id>/diagnostics/` directory:

```json
{
  "session_id": "sess-abc",
  "capable": false,
  "reason": "sandbox_incapable",
  "fingerprint": {
    "cli_version": "0.146.0",
    "platform": "Linux",
    "install_type": "snap",
    "sandbox_mode": "read-only"
  },
  "recorded_at": "2026-08-07T12:00:00+00:00"
}
```

`reason` is `null` when capable; otherwise `sandbox_incapable` (codex could not
reach bubblewrap — typically a snap-confined install), `install_incomplete` (the
binary is there but a supporting host binary is not), `unknown` (a failure
signature not yet classified), or `probe_error` (the probe itself timed out or
could not spawn — this one is never cached). The same mode is also recorded on
the review verdict as `capability_mode` / `capability_reason`.

**Shared cache** — `~/.local/share/cw/codex-review/capability-cache.json`:

```
~/.local/share/cw/codex-review/capability-cache.json
```

This file is **not** swept by the retention window below and has **no TTL**. It
is keyed by the runtime fingerprint above; only a change to one of those four
fields invalidates it. That is deliberate: the probe is a real `codex exec`
round-trip, and the fact it establishes changes only when the codex install
does — not on a timer.

To force a re-probe (e.g. you just changed how codex is installed and do not
want to wait for the version/install-type change to be noticed), delete the
file:

```bash
rm ~/.local/share/cw/codex-review/capability-cache.json
```

`cw.codex_review._capability._reset_filesystem_capability_cache()` does the same
thing in-process. Do not confuse it with `cw.dispatch.claim`'s
`_reset_codex_capability_cache()` — that clears an unrelated, in-process,
60-second TTL cache of a *different* question ("is a codex binary present and
does `--version` parse?").

## Retention

Bundles are swept on a retention window. Every `dispatch_tick` runs a best-effort
cleanup pass (outside any lock, errors swallowed) that removes any bundle whose
newest file is older than the window. The sweep itself is internally throttled
to at most once per hour (tracked via a sentinel file's mtime under
`state_dir()`), independent of the retention window, so a full
`sessions/` filesystem walk doesn't run on every tick.

- **Default:** 24 hours.
- **Configurable** via `diagnostics_retention_hours` in `orchestrator.yaml`:

  ```yaml
  diagnostics_retention_hours: 48   # keep bundles for two days
  ```
