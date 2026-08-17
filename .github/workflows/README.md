# GitHub Actions Workflows

| Workflow | Schedule | Runner | Purpose |
|---|---|---|---|
| `ci.yml` | Push / PR | ubuntu + macOS | Lint, type check, unit tests, tmux integration, diff coverage |
| `nightly.yml` | 09:00 UTC daily | macOS | cmux integration (requires live cmux daemon) |
| `nightly-native.yml` | Manual (`workflow_dispatch`) | ubuntu | Full test suite (excl. cmux) + native daemon smoke tests |
| `nightly-codex.yml` | Manual (`workflow_dispatch`) | ubuntu | Live `codex exec` CLI contract suite (requires `OPENAI_CI_KEY`) |
| `release.yml` | Tag push | ubuntu | Creates a GitHub Release via `softprops/action-gh-release` (fixed install-instructions body) and closes `dispatch-drift` issues on any `v*` tag push; no PyPI publish step exists — this is the manual-tag path (`release-tag.yml` is the automated release path and creates its own release directly, so this rarely fires against a fresh tag) |
| `release-tag.yml` | Push to `main` / Manual (`workflow_dispatch`, `dry_run` input, default `true`) | ubuntu | Detects a `chore(release): ...` version-bump commit, runs quality gates (`ruff`/`mypy`/`pytest`), creates and pushes the version tag, creates the GitHub Release (CHANGELOG-derived notes via `Extract CHANGELOG section` when available, else `--generate-notes`), and closes `dispatch-drift` issues — the real automated release path (`release.yml` reacts to any hand-pushed `v*` tag as a fallback) |
| `dispatch-guard.yml` | Push to `main` (dispatch/reconcile/spawn) | ubuntu | Opens a `dispatch-drift` issue when critical files are unreleased; closed by `release-tag.yml`'s "Close dispatch-drift issues" step on the next automated release (`release.yml` carries an idempotent copy for the manual-tag path) |
| `pr-events.yml` | PR closed / review submitted / CI workflow_run completed | ubuntu | Pushes PR lifecycle events to `cw_pr_events_server` `/pr-event` via an operator-provisioned relay (GitHub #930); no-ops if `CW_PR_EVENTS_RELAY_URL` repo variable is unset |
| `changelog-advisory.yml` | PR (`pull_request`) | ubuntu | Advisory-only `::warning::` when a PR touches `src/cw/**` without updating `CHANGELOG.md` (GitHub #1532); never fails the job, so do not mark it required |
| `changelog-gate.yml` | PR (opened / synchronize / reopened / labeled / unlabeled) | ubuntu | Blocking counterpart to the advisory (GitHub #1612): fails a `feat(`/`fix(` PR that touches `src/` without updating `CHANGELOG.md`, unless the `no-changelog` label is applied; safe to mark required |

## Triggering workflows manually

```bash
# Trigger the native nightly
gh workflow run nightly-native.yml

# Trigger the codex contract nightly
gh workflow run nightly-codex.yml

# Trigger the cmux nightly
gh workflow run nightly.yml

# Watch the run
gh run watch
```

## Debugging failures

1. Open the failed run in the Actions tab and expand the failing step.
2. For native daemon smoke failures: check the `Diagnose claude install` step first.
3. For cmux nightly failures: cmux logs are uploaded as an artifact on failure.
4. Rerun locally with `INTEGRATION_REAL_API=1 uv run pytest tests/test_native_smoke.py -v`.
