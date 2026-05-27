# Releasing claude-workspace (cw)

This project follows [Semantic Versioning](https://semver.org/) and
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are
cut by the maintainer; this document captures the procedure.

## Cadence

There is no fixed cadence. Cut a release when:

- A meaningful feature or fix has merged to `main`
- Several smaller items have accumulated since the last tag
- A pinned-version installation is needed (e.g. for a downstream user)

For pre-1.0, breaking changes ship in minor versions (`0.x` → `0.y`) and
are flagged in the CHANGELOG. Post-1.0, breaking changes require a major
bump.

## Pre-release checklist

1. **Quality gates clean** on `main`:
   ```bash
   uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -v
   ```
2. **CHANGELOG `[Unreleased]` section is complete.** Move accumulated
   entries from `[Unreleased]` into a new section with the target
   version and today's date. Leave `[Unreleased]` as a placeholder.
3. **Version bumped in all three places:**
   - `pyproject.toml` — `version = "X.Y.Z"`
   - `src/cw/__init__.py` — `__version__ = "X.Y.Z"`
   - `tests/test_cli.py` — version assertion (if present)
4. **Commit the bump** on a release branch:
   ```bash
   git checkout -b release/X.Y.Z
   git commit -am "chore(release): X.Y.Z"
   ```
5. **Open a PR**, wait for CI green, merge to `main`.

## Cutting the tag

After the release PR lands on `main`:

```bash
git checkout main
git pull
./scripts/release.sh X.Y.Z
```

`scripts/release.sh` verifies the three version locations match,
re-runs all quality gates, then creates and pushes the annotated tag
`vX.Y.Z`. GitHub Actions picks up the tag push and creates the GitHub
release.

## Post-release

- **Watch the release workflow** at
  https://github.com/mattwwarren/claude-workspace/actions to confirm
  the release notes auto-generate cleanly.
- **Smoke-test the install** from a fresh shell:
  ```bash
  uv tool install --force \
    git+https://github.com/mattwwarren/claude-workspace.git@vX.Y.Z
  cw --version
  cw doctor
  ```
- **Watch for migration issues** in the first 48 hours, especially for
  state-file schema bumps. Hotfix as a patch release (`X.Y.Z+1`) if
  needed.

## Versioning conventions

| Change type | Bump |
|---|---|
| Bug fix, no behavior change to users | patch |
| New feature, no breaking change | minor |
| Breaking change to CLI, config, or state schema | major (post-1.0) / minor (pre-1.0, flagged) |
| Docs-only, internal refactor | no bump unless paired with the above |

## 1.0 release

The 1.0 cut is tracked in [issue #120](https://github.com/mattwwarren/claude-workspace/issues/120)
and requires an additional migration guide
(`docs/migrating-to-1.0.md`). Follow the checklist in #120 in addition
to the steps above.
