#!/usr/bin/env bash
# Install cw slash commands (skills) to ~/.claude/commands/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SKILLS_SRC="$PROJECT_DIR/.claude/commands"
SKILLS_DST="${HOME}/.claude/commands"

if [ ! -d "$SKILLS_SRC" ]; then
    echo "Error: Skills source directory not found: $SKILLS_SRC"
    exit 1
fi

mkdir -p "$SKILLS_DST"

count=0
for skill in "$SKILLS_SRC"/*.md; do
    [ -f "$skill" ] || continue
    name="$(basename "$skill")"
    cp "$skill" "$SKILLS_DST/$name"
    echo "  Installed: $name"
    count=$((count + 1))
done

echo ""
echo "Installed $count skill(s) to $SKILLS_DST"
echo ""
echo "Available commands:"
for skill in "$SKILLS_DST"/*.md; do
    [ -f "$skill" ] || continue
    name="$(basename "$skill" .md)"
    echo "  /$name"
done
