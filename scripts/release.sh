#!/usr/bin/env bash
# Create and push a release tag.
# Usage: ./scripts/release.sh 0.3.0
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 0.3.0"
    exit 1
fi

VERSION=$1

# Commit-subject pre-flight guard. Must stay byte-identical to the accepted
# pattern in .github/workflows/release-tag.yml's guard step — pinned by
# tests/test_release_sh.py::test_release_sh_guard_regex_matches_release_tag_workflow.
# Do not edit one without the other.
SUBJECT_PATTERN='^chore\(release\):\ (v|bump\ version\ to\ )([0-9]+\.[0-9]+\.[0-9]+)(\ \(#[0-9]+\))?$'

if [ "${RELEASE_SH_SKIP_SUBJECT_GUARD:-}" != "1" ]; then
    SUBJECT=$(git log -1 --pretty=%s)
    if [[ "$SUBJECT" =~ $SUBJECT_PATTERN ]]; then
        : # Accepted release-commit subject — proceed.
    elif [[ "$SUBJECT" == "chore(release):"* ]]; then
        echo "Error: commit subject starts with 'chore(release):' but matches neither accepted release-commit form:"
        echo "  chore(release): vX.Y.Z"
        echo "  chore(release): bump version to X.Y.Z"
        echo "(both optionally suffixed with ' (#<PR number>)' from a squash merge)"
        echo "Subject was: \"$SUBJECT\""
        echo "See .github/workflows/release-tag.yml's guard step and docs/release-playbook.md's"
        echo "'Release mechanics' section for the commit-subject contract this enforces."
        echo "Override only if you know what you're doing: RELEASE_SH_SKIP_SUBJECT_GUARD=1"
        exit 1
    fi
    # Anything else (no "chore(release):" prefix at all) is not a release
    # commit subject and proceeds unchecked, mirroring release-tag.yml.
fi

# Verify version in pyproject.toml matches
PYPROJECT_VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml)
if [ "$PYPROJECT_VERSION" != "$VERSION" ]; then
    echo "Error: pyproject.toml version ($PYPROJECT_VERSION) does not match $VERSION"
    echo "Update pyproject.toml first, then re-run."
    exit 1
fi

# Verify the installed package version matches (cw.__version__ is resolved
# dynamically from the installed distribution metadata — pyproject.toml is
# the single source of truth; there is no version literal in __init__.py).
PKG_VERSION=$(uv run python -c "import cw; print(cw.__version__)")
if [ "$PKG_VERSION" != "$VERSION" ]; then
    echo "Error: installed package version ($PKG_VERSION) does not match $VERSION"
    echo "Bump pyproject.toml, run 'uv sync', then re-run."
    exit 1
fi

# Run quality gates
echo "Running quality gates..."
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy --strict src/
uv run pytest tests/ -v

echo ""
echo "All checks passed. Creating tag v$VERSION..."
git tag -a "v$VERSION" -m "Release v$VERSION"
echo "Pushing tag..."
git push origin "v$VERSION"

echo ""
echo "Done! Release v$VERSION will be created by GitHub Actions."
echo "Check: https://github.com/mattwwarren/claude-workspace/actions"
