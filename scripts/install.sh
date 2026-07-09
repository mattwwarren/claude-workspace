#!/usr/bin/env bash
# Install cw CLI tool globally via uv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Installing cw from $PROJECT_DIR..."
uv tool install --from "$PROJECT_DIR" --force --reinstall --no-cache "claude-workspace[mcp]"

echo ""
echo "Syncing cw skills and commands to ~/.claude/..."
"$SCRIPT_DIR/install-skills.sh"

echo ""
echo "Installed! Run 'cw --help' to get started."
echo ""
echo "First time setup:"
echo "  1. Edit ~/.config/cw/clients.yaml to configure your clients"
echo "  2. On macOS, ensure cmux is installed and running:"
echo "     https://github.com/cmuxio/cmux"
echo "  3. Run 'cw start <client>' to begin!"
