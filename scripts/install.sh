#!/usr/bin/env bash
# Install cw CLI tool globally via uv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Installing cw from $PROJECT_DIR..."
uv tool install --from "$PROJECT_DIR" --force claude-workspace

echo ""
echo "Installed! Run 'cw --help' to get started."
echo ""
echo "First time setup:"
echo "  1. Edit ~/.config/cw/clients.yaml to configure your clients"
echo "  2. Install zellij: https://zellij.dev/documentation/installation"
echo "  3. Install yazi (optional, for file tree): https://yazi-rs.github.io/"
echo "  4. Run 'cw start <client>' to begin!"
