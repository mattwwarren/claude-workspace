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

# Verify version in pyproject.toml matches
PYPROJECT_VERSION=$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml)
if [ "$PYPROJECT_VERSION" != "$VERSION" ]; then
    echo "Error: pyproject.toml version ($PYPROJECT_VERSION) does not match $VERSION"
    echo "Update pyproject.toml first, then re-run."
    exit 1
fi

# Verify __version__ matches
PKG_VERSION=$(uv run python -c "import cw; print(cw.__version__)")
if [ "$PKG_VERSION" != "$VERSION" ]; then
    echo "Error: src/cw/__init__.py version ($PKG_VERSION) does not match $VERSION"
    echo "Update __init__.py first, then re-run."
    exit 1
fi

# Run quality gates
echo "Running quality gates..."
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/ -v

echo ""
echo "All checks passed. Creating tag v$VERSION..."
git tag -a "v$VERSION" -m "Release v$VERSION"
echo "Pushing tag..."
git push origin "v$VERSION"

echo ""
echo "Done! Release v$VERSION will be created by GitHub Actions."
echo "Check: https://github.com/mattwwarren/claude-workspace/actions"
