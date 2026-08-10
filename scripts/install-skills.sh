#!/usr/bin/env bash
# Symlink cw slash commands and skill directories, and copy subagents, into
# ~/.claude/
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
#   Overwrite safety (#1784): a baseline shadow-copy store at
#   ~/.claude/.cw-agents-baseline/ records the exact content cw itself last
#   wrote for each installed agent.  On each run, before copying an agent
#   file: if the destination doesn't exist yet, or matches the source, or
#   matches the recorded baseline, the copy proceeds normally (this also
#   covers the ordinary "cw's source legitimately changed" case with zero
#   added friction).  Otherwise the destination has been hand-edited (or has
#   unknown provenance, e.g. no baseline was ever recorded) and the script
#   refuses to overwrite it, printing the source/destination paths and
#   exiting non-zero.  Pass --force to overwrite anyway.
#
# PORTABILITY:
#   Targets bash 3.2 (macOS /bin/bash) as well as modern bash on Linux.  Do not
#   introduce namerefs (`local -n`, bash 4.3+), associative arrays, or `readarray`.
#   Empty arrays are expanded via the "${arr[@]+"${arr[@]}"}" idiom because
#   `set -u` errors on a bare "${arr[@]}" when the array is empty under 3.2.
set -euo pipefail

# --force bypasses the agent overwrite-safety check below (#1784), matching
# the repo-wide --force convention (cli/sessions.py, cli/spawn.py,
# worktree_gc.py). No other flags are accepted.
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *)
            echo "Error: unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Project-scoped commands that must NEVER be installed into ~/.claude/commands.
#
# Why: /prep-pr resolves /ship-it against the *current project's*
# .claude/commands/ship-it.md and treats its absence as a BLOCK (see
# .claude/commands/auto-dev-finalize.md).  A global copy would make every other
# repo look like it has one, then ship it with claude-workspace's conventions —
# origin/main base, this repo's test plan, its finalize scripts.
#
# scripts/excluded-commands.txt is the single source of truth for this list —
# both this script and cw doctor's skills-commands-drift check (which must
# not flag an excluded command as "missing" on the global side) read it. See
# src/cw/doctor/skills_drift.py's _load_excluded_commands (#1535).
EXCLUDED_COMMANDS_FILE="$SCRIPT_DIR/excluded-commands.txt"
EXCLUDED_COMMANDS=()
if [ -f "$EXCLUDED_COMMANDS_FILE" ]; then
    while IFS= read -r excluded_name || [ -n "$excluded_name" ]; do
        [ -n "$excluded_name" ] || continue
        EXCLUDED_COMMANDS+=("$excluded_name")
    done < "$EXCLUDED_COMMANDS_FILE"
fi

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

# Baseline shadow-copy store for agent overwrite-safety (#1784): one copy per
# installed agent file, holding the exact content cw itself last wrote there.
# This is what lets the agent-copy loop below tell "cw's own source legitimately
# changed" apart from "something else edited the destination directly" —
# mtime doesn't work (plain cp resets mtime to now on every write), and git
# status doesn't work either (a normal cw install intentionally leaves
# global-claude's copy uncommitted, per the NOTE ON AGENTS above).
AGENTS_BASELINE_DIR="$CLAUDE_HOME/.cw-agents-baseline"

# ---------------------------------------------------------------------------
# 1. Validate source directories
# ---------------------------------------------------------------------------
if [ ! -d "$COMMANDS_SRC" ]; then
    echo "Error: Commands source not found: $COMMANDS_SRC" >&2
    exit 1
fi

# -p is a no-op when the path is an existing dir OR a symlink to one, so this is
# safe for the agents symlink-into-global-claude layout described above.
mkdir -p "$COMMANDS_DST" "$SKILLS_DST" "$AGENTS_DST" "$AGENTS_BASELINE_DIR"

# _agent_conflict_reason <src_file> <dst_file> <baseline_file>
# Echoes a reason and returns 0 if installing src_file over dst_file would
# clobber a change cw did not itself make. Returns 1 (safe to install) when
# dst_file doesn't exist yet, is byte-identical to src_file, or is
# byte-identical to the recorded baseline (i.e. nothing has touched it since
# cw last wrote it there).
_agent_conflict_reason() {
    local src_file="$1"
    local dst_file="$2"
    local baseline_file="$3"

    if [ ! -e "$dst_file" ]; then
        return 1
    fi
    if cmp -s "$src_file" "$dst_file"; then
        return 1
    fi
    if [ -f "$baseline_file" ] && cmp -s "$baseline_file" "$dst_file"; then
        return 1
    fi
    if [ ! -f "$baseline_file" ]; then
        echo "destination differs from cw's source and cw has no record of installing it (no baseline on file)"
        return 0
    fi
    echo "destination differs from cw's source and from the last copy cw installed — something other than cw modified it"
    return 0
}

# _print_agent_conflict <name> <src_file> <dst_file> <reason>
_print_agent_conflict() {
    local name="$1"
    local src_file="$2"
    local dst_file="$3"
    local reason="$4"

    {
        echo "ERROR: refusing to overwrite a modified agent spec."
        echo ""
        echo "  agent:               $name"
        echo "  cw source:           $src_file"
        echo "  install destination: $dst_file"
        echo "  reason:              $reason"
        echo ""
        echo "  To keep those changes:  re-import them into claude-workspace, commit, and re-run."
        echo "  To discard and overwrite: ./scripts/install-skills.sh --force"
    } >&2
}

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
    ln -sf "$src_file" "$COMMANDS_DST/$name"
    new_entries+=("commands/$name")
    cmd_count=$((cmd_count + 1))
done

agent_count=0
excluded_agent_count=0
agent_conflicts=()
if [ -d "$AGENTS_SRC" ]; then
    for src_file in "$AGENTS_SRC"/*.md; do
        [ -f "$src_file" ] || continue
        name="$(basename "$src_file")"
        if _is_excluded "$name" "${EXCLUDED_AGENTS[@]+"${EXCLUDED_AGENTS[@]}"}"; then
            excluded_agent_count=$((excluded_agent_count + 1))
            continue
        fi

        dst_file="$AGENTS_DST/$name"
        baseline_file="$AGENTS_BASELINE_DIR/$name"

        # See _agent_conflict_reason above. Skipped entirely when --force is
        # passed. The `if` condition (rather than a plain `reason=$(...)`
        # assignment) is deliberate: under `set -e`, a bare assignment from a
        # command substitution that returns non-zero (the "safe" case here)
        # would abort the whole script.
        if [ "$FORCE" -eq 0 ] && reason="$(_agent_conflict_reason "$src_file" "$dst_file" "$baseline_file")"; then
            _print_agent_conflict "$name" "$src_file" "$dst_file" "$reason"
            agent_conflicts+=("$name")
            continue
        fi

        cp -p "$src_file" "$dst_file"
        cp -p "$src_file" "$baseline_file"
        new_entries+=("agents/$name")
        agent_count=$((agent_count + 1))
    done
fi

# Deferred abort: must happen here, before the skills loop, the manifest
# write, and (critically) the prune step below. A conflicting agent is
# deliberately withheld from new_entries above so its destination is left
# untouched — but the prune step treats any old-manifest entry absent from
# new_entries as an orphan and deletes it. Continuing past this point with a
# conflict pending would make this run's own prune logic delete the very
# hand-edited file this feature exists to protect.
if [ "${#agent_conflicts[@]}" -gt 0 ]; then
    echo "" >&2
    echo "ERROR: ${#agent_conflicts[@]} agent(s) have hand-edited destinations and were not installed:" >&2
    for conflicted_name in "${agent_conflicts[@]}"; do
        echo "  - $conflicted_name" >&2
    done
    exit 1
fi

skill_count=0
if [ -d "$SKILLS_SRC" ]; then
    for src_dir in "$SKILLS_SRC"/*/; do
        [ -d "$src_dir" ] || continue
        skill_name="$(basename "$src_dir")"
        src_dir_abs="${src_dir%/}"
        dst_dir="$SKILLS_DST/$skill_name"

        # Steady-state correctness (not just migration): ln -sf alone silently
        # fails to replace a regular directory (places the link *inside* it) or
        # an existing symlink pointing at the wrong directory (follows it and
        # drops the new link inside THAT dir — readlink still shows the stale
        # target, nothing errors). -e alone also misses a *broken* existing
        # symlink (dangling because its old target vanished) — bare ln -s
        # would then refuse outright since the directory entry still exists.
        # -L catches that case too. Clear first, then link, every run.
        #
        # Already-correct is a THIRD state, not just "no clear needed": if
        # dst_dir is already a symlink resolving to src_dir_abs, `ln -s` must
        # be skipped entirely, not just the `rm -rf`. dst_dir still exists at
        # that point, and ln -s treats an existing destination that resolves
        # to a directory as "install inside that directory" rather than
        # replacing it — silently planting a self-referential symlink inside
        # the real source tree on every steady-state re-run (#1535 review).
        if [ -L "$dst_dir" ] && [ "$(readlink "$dst_dir")" = "$src_dir_abs" ]; then
            : # already correctly linked — nothing to do
        else
            if [ -e "$dst_dir" ] || [ -L "$dst_dir" ]; then
                rm -rf "$dst_dir"
            fi
            ln -s "$src_dir_abs" "$dst_dir"
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
            # -e alone misses a broken symlink: once a command/skill's source
            # is removed from the repo, its ~/.claude symlink from a prior
            # run still resolves to nothing, so -e (which follows the link)
            # reports false and the orphan would never be pruned. -L catches
            # that case. rm -rf then safely removes a plain file, a real
            # directory (a pre-symlink-era copy), a symlink-to-file, a
            # symlink-to-dir (unlinks only the link — see the SAFETY
            # INVARIANT header), or a broken symlink, so one branch covers
            # every shape a prior install could have left behind.
            if [ -e "$target" ] || [ -L "$target" ]; then
                rm -rf "$target"
                prune_count=$((prune_count + 1))
                pruned_names+=("$old_entry")
                # A pruned agent's baseline entry must go too, so a future
                # re-add under the same filename doesn't inherit stale
                # baseline state left over from before it was removed (#1784).
                case "$old_entry" in
                    agents/*)
                        rm -f "$AGENTS_BASELINE_DIR/${old_entry#agents/}"
                        ;;
                esac
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
