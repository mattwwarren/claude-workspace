#!/usr/bin/env bash
# Install cw CLI tool globally via uv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Installing cw from $PROJECT_DIR..."

# uv >= 0.11 dropped `uv tool install --from`, so the old
# `--from "$PROJECT_DIR" ... "claude-workspace[mcp]"` form now fails (#2186).
# The PEP 508 direct reference carries the path and the extra in one
# requirement. A space ends the URL token inside a requirement string, and
# `%`, `#` and `?` are URL-significant, so encode them here (`%` first, or the
# escapes introduced below would be re-encoded). These `${v//pat/rep}`
# substitutions are bash 3.2 safe, for macOS's system /bin/bash.
PROJECT_URL_PATH="$PROJECT_DIR"
PROJECT_URL_PATH="${PROJECT_URL_PATH//\%/%25}"
PROJECT_URL_PATH="${PROJECT_URL_PATH// /%20}"
PROJECT_URL_PATH="${PROJECT_URL_PATH//\#/%23}"
PROJECT_URL_PATH="${PROJECT_URL_PATH//\?/%3F}"

uv tool install --force --reinstall --no-cache "claude-workspace[mcp] @ file://${PROJECT_URL_PATH}"

echo ""
echo "Syncing cw skills, commands, and scripts to ~/.claude/..."
"$SCRIPT_DIR/install-skills.sh"

echo ""
echo "Installed! Run 'cw --help' to get started."
echo ""
echo "First time setup:"
echo "  1. Edit ~/.config/cw/clients.yaml to configure your clients"
echo "  2. Run 'cw start <client>' to begin!"
