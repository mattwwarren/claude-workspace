# GitHub Actions Workflows

| Workflow | Schedule | Runner | Purpose |
|---|---|---|---|
| `ci.yml` | Push / PR | ubuntu + macOS | Lint, type check, unit tests, tmux integration, diff coverage |
| `nightly.yml` | 09:00 UTC daily | macOS | cmux integration (requires live cmux daemon) |
| `nightly-native.yml` | Manual (`workflow_dispatch`) | ubuntu | Full test suite (excl. cmux) + native daemon smoke tests |
| `release.yml` | Tag push | ubuntu | Build and publish to PyPI |
| `dispatch-guard.yml` | Push to `main` (dispatch/reconcile/spawn) | ubuntu | Opens a `dispatch-drift` issue when critical files are unreleased; closes on next release |

## Triggering workflows manually

```bash
# Trigger the native nightly
gh workflow run nightly-native.yml

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
