#!/usr/bin/env bash
# Install cw slash commands, skill directories, and subagents to ~/.claude/
#
# SAFETY INVARIANT (manifest-scoped prune):
#   This script tracks every path it installs in ~/.claude/.cw-skills-manifest.
#   On subsequent runs it reads the PREVIOUS manifest and removes any entry
#   that is no longer in the current set — but ONLY if that entry was in the
#   previous manifest.  Paths not listed in the previous manifest are NEVER
#   touched.  This guarantees that foreign skills installed by other tools
#   (peon-ping-*, wiki-*, superpowers/*, etc.) are never deleted by cw.
#
# NOTE ON AGENTS (~/.claude/agents):
#   On a typical setup this path is a SYMLINK into the global-claude repo, so
#   installing agents writes through it and leaves that repo with uncommitted
#   changes.  That is expected: global-claude is the store of record for agents,
#   and the diff is meant to be reviewed and committed there.  The prune rule
#   above still holds — an agent that exists only in global-claude never enters
#   this manifest, so it is never removed by cw.
#
#   Overwrite hazard: `cp` here is unconditional, with no diff/staleness check
#   against the destination.  If an agent is hand-edited directly in
#   global-claude (the canonical source) after this repo's .claude/agents/
#   copy was last refreshed, the next install run silently clobbers that edit
#   back to the stale cw copy.  Re-import from global-claude into this repo's
#   .claude/agents/ before running install if you've been editing there.
#
# PORTABILITY:
#   Targets bash 3.2 (macOS /bin/bash) as well as modern bash on Linux.  Do not
#   introduce namerefs (`local -n`, bash 4.3+), associative arrays, or `readarray`.
#   Empty arrays are expanded via the "${arr[@]+"${arr[@]}"}" idiom because
#   `set -u` errors on a bare "${arr[@]}" when the array is empty under 3.2.
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

# Throwaway / experiment-scoped agents that must NEVER be installed globally.
#
# Why: spike-isolated is a one-off probe tied to a single spike (#107).  A global
# copy advertises it to every repo's agent picker forever, long after the spike
# it was cut for is gone.
EXCLUDED_AGENTS=("spike-isolated.md")

# _is_excluded <candidate> [excluded...] — bash 3.2 safe (no namerefs).
# Call as: _is_excluded "$name" "${LIST[@]+"${LIST[@]}"}"
_is_excluded() {
    local candidate="$1"
    shift
    local excluded
    for excluded in "$@"; do
        if [ "$candidate" = "$excluded" ]; then
            return 0
        fi
    done
    return 1
}

COMMANDS_SRC="$PROJECT_DIR/.claude/commands"
SKILLS_SRC="$PROJECT_DIR/.claude/skills"
AGENTS_SRC="$PROJECT_DIR/.claude/agents"
CLAUDE_HOME="${HOME}/.claude"
COMMANDS_DST="$CLAUDE_HOME/commands"
SKILLS_DST="$CLAUDE_HOME/skills"
AGENTS_DST="$CLAUDE_HOME/agents"
MANIFEST="$CLAUDE_HOME/.cw-skills-manifest"

# ---------------------------------------------------------------------------
# 1. Validate source directories
# ---------------------------------------------------------------------------
if [ ! -d "$COMMANDS_SRC" ]; then
    echo "Error: Commands source not found: $COMMANDS_SRC" >&2
    exit 1
fi

# -p is a no-op when the path is an existing dir OR a symlink to one, so this is
# safe for the agents symlink-into-global-claude layout described above.
mkdir -p "$COMMANDS_DST" "$SKILLS_DST" "$AGENTS_DST"

# ---------------------------------------------------------------------------
# 2. Build the NEW manifest (what this run will install)
# ---------------------------------------------------------------------------
new_entries=()

cmd_count=0
excluded_count=0
for src_file in "$COMMANDS_SRC"/*.md; do
    [ -f "$src_file" ] || continue
    name="$(basename "$src_file")"
    if _is_excluded "$name" "${EXCLUDED_COMMANDS[@]+"${EXCLUDED_COMMANDS[@]}"}"; then
        excluded_count=$((excluded_count + 1))
        continue
    fi
    cp "$src_file" "$COMMANDS_DST/$name"
    new_entries+=("commands/$name")
    cmd_count=$((cmd_count + 1))
done

agent_count=0
excluded_agent_count=0
if [ -d "$AGENTS_SRC" ]; then
    for src_file in "$AGENTS_SRC"/*.md; do
        [ -f "$src_file" ] || continue
        name="$(basename "$src_file")"
        if _is_excluded "$name" "${EXCLUDED_AGENTS[@]+"${EXCLUDED_AGENTS[@]}"}"; then
            excluded_agent_count=$((excluded_agent_count + 1))
            continue
        fi
        cp "$src_file" "$AGENTS_DST/$name"
        new_entries+=("agents/$name")
        agent_count=$((agent_count + 1))
    done
fi

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
echo "  agents synced   : $agent_count"
echo "  agents skipped  : $excluded_agent_count (experiment-scoped)"
echo "  orphans pruned  : $prune_count"

if [ "${#pruned_names[@]}" -gt 0 ]; then
    echo "  pruned paths:"
    for p in "${pruned_names[@]}"; do
        echo "    - $p"
    done
fi
