#!/usr/bin/env bash
# Install cw slash commands and skill directories to ~/.claude/
#
# SAFETY INVARIANT (manifest-scoped prune):
#   This script tracks every path it installs in ~/.claude/.cw-skills-manifest.
#   On subsequent runs it reads the PREVIOUS manifest and removes any entry
#   that is no longer in the current set — but ONLY if that entry was in the
#   previous manifest.  Paths not listed in the previous manifest are NEVER
#   touched.  This guarantees that foreign skills installed by other tools
#   (peon-ping-*, wiki-*, superpowers/*, etc.) are never deleted by cw.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Project-scoped commands that must NEVER be installed into ~/.claude/commands.
#
# Why: /prep-pr resolves /ship-it against the *current project's*
# .claude/commands/ship-it.md and treats its absence as a BLOCK (see
# .claude/commands/auto-dev-finalize.md).  A global copy would make every other
# repo look like it has one, then ship it with claude-workspace's conventions —
# origin/main base, this repo's test plan, its finalize scripts.
EXCLUDED_COMMANDS=("ship-it.md")

_is_excluded_command() {
    local candidate="$1"
    local excluded
    for excluded in "${EXCLUDED_COMMANDS[@]}"; do
        if [ "$candidate" = "$excluded" ]; then
            return 0
        fi
    done
    return 1
}

COMMANDS_SRC="$PROJECT_DIR/.claude/commands"
SKILLS_SRC="$PROJECT_DIR/.claude/skills"
CLAUDE_HOME="${HOME}/.claude"
COMMANDS_DST="$CLAUDE_HOME/commands"
SKILLS_DST="$CLAUDE_HOME/skills"
MANIFEST="$CLAUDE_HOME/.cw-skills-manifest"

# ---------------------------------------------------------------------------
# 1. Validate source directories
# ---------------------------------------------------------------------------
if [ ! -d "$COMMANDS_SRC" ]; then
    echo "Error: Commands source not found: $COMMANDS_SRC" >&2
    exit 1
fi

mkdir -p "$COMMANDS_DST" "$SKILLS_DST"

# ---------------------------------------------------------------------------
# 2. Build the NEW manifest (what this run will install)
# ---------------------------------------------------------------------------
new_entries=()

cmd_count=0
excluded_count=0
for src_file in "$COMMANDS_SRC"/*.md; do
    [ -f "$src_file" ] || continue
    name="$(basename "$src_file")"
    if _is_excluded_command "$name"; then
        excluded_count=$((excluded_count + 1))
        continue
    fi
    cp "$src_file" "$COMMANDS_DST/$name"
    new_entries+=("commands/$name")
    cmd_count=$((cmd_count + 1))
done

skill_count=0
if [ -d "$SKILLS_SRC" ]; then
    for src_dir in "$SKILLS_SRC"/*/; do
        [ -d "$src_dir" ] || continue
        skill_name="$(basename "$src_dir")"
        dst_dir="$SKILLS_DST/$skill_name"

        # Use rsync if available for efficient recursive copy; fall back to
        # cp -r.  --delete only applies within this one skill's subtree, never
        # to the parent skills/ dir, so foreign skills are untouched.
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --delete "$src_dir" "$dst_dir/"
        else
            rm -rf "$dst_dir"
            cp -r "$src_dir" "$dst_dir"
        fi

        new_entries+=("skills/$skill_name")
        skill_count=$((skill_count + 1))
    done
fi

# ---------------------------------------------------------------------------
# 3. Manifest-scoped prune: remove OLD entries absent from NEW set
# ---------------------------------------------------------------------------
prune_count=0
pruned_names=()

if [ -f "$MANIFEST" ]; then
    while IFS= read -r old_entry || [ -n "$old_entry" ]; do
        [ -n "$old_entry" ] || continue

        # Is this old entry still in the new set?
        found=0
        for new_entry in "${new_entries[@]+"${new_entries[@]}"}"; do
            if [ "$new_entry" = "$old_entry" ]; then
                found=1
                break
            fi
        done

        if [ "$found" -eq 0 ]; then
            target="$CLAUDE_HOME/$old_entry"
            if [ -f "$target" ]; then
                rm -f "$target"
                prune_count=$((prune_count + 1))
                pruned_names+=("$old_entry")
            elif [ -d "$target" ]; then
                rm -rf "$target"
                prune_count=$((prune_count + 1))
                pruned_names+=("$old_entry")
            fi
        fi
    done < "$MANIFEST"
fi

# ---------------------------------------------------------------------------
# 4. Write the new manifest (overwrite)
# ---------------------------------------------------------------------------
printf '%s\n' "${new_entries[@]+"${new_entries[@]}"}" > "$MANIFEST"

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo "cw skills installed:"
echo "  commands synced : $cmd_count"
echo "  commands skipped: $excluded_count (project-scoped)"
echo "  skill dirs synced: $skill_count"
echo "  orphans pruned  : $prune_count"

if [ "${#pruned_names[@]}" -gt 0 ]; then
    echo "  pruned paths:"
    for p in "${pruned_names[@]}"; do
        echo "    - $p"
    done
fi
