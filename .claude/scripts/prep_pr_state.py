#!/usr/bin/env python3
"""
PR preparation state management: gate detection, scope tracking, state persistence.

Subcommands:
  detect-gates  — Auto-detect quality gates from project files + CLAUDE.md overrides
  snapshot      — Capture diff metrics, append to state
  check-scope   — Compare latest vs initial snapshot, report growth
  status        — Show current cycle state
  clean         — Remove state file
  gate-timeout  — Foreground/poll timeout ceilings for a named gate
  gate-elapsed  — Elapsed time since a timestamp vs a ceiling
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# State file lives in the repo's .claude/ directory
STATE_FILE = Path(".claude/prep-pr-state.json")

# Scope creep thresholds
SCOPE_FILE_COUNT_THRESHOLD = 0.30  # 30% increase
SCOPE_LINE_COUNT_THRESHOLD = 0.50  # 50% increase

# Minimum fields expected in numstat output (additions, deletions, filename)
_NUMSTAT_MIN_FIELDS = 2

# Minimum snapshots needed for scope comparison
_MIN_SNAPSHOTS_FOR_SCOPE = 2


# --- Data Models ---


@dataclass
class Gate:
    """A quality gate command."""

    name: str
    command: str
    autofix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {"name": self.name, "command": self.command}
        if self.autofix:
            result["autofix"] = self.autofix
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Gate:
        """Create from dictionary."""
        return cls(
            name=data["name"],
            command=data["command"],
            autofix=data.get("autofix"),
        )


@dataclass
class ScopeSnapshot:
    """A snapshot of diff metrics at a point in time."""

    timestamp: str
    cycle: int
    files: list[str]
    additions: int
    deletions: int
    directories: list[str]

    @property
    def file_count(self) -> int:
        """Number of files changed."""
        return len(self.files)

    @property
    def line_count(self) -> int:
        """Total lines changed."""
        return self.additions + self.deletions

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScopeSnapshot:
        """Create from dictionary."""
        return cls(**data)


@dataclass
class PrepPrState:
    """Complete state for a prep-pr session."""

    base_branch: str
    current_cycle: int = 0
    max_cycles: int = 3
    snapshots: list[ScopeSnapshot] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    detected_from: list[str] = field(default_factory=list)
    started: str = ""
    updated: str = ""

    def __post_init__(self) -> None:
        """Set timestamps if not already set."""
        if not self.started:
            self.started = datetime.now(UTC).isoformat()
        if not self.updated:
            self.updated = self.started

    def to_dict(self) -> dict[str, Any]:
        """Convert state to dictionary for JSON serialization."""
        return {
            "base_branch": self.base_branch,
            "current_cycle": self.current_cycle,
            "max_cycles": self.max_cycles,
            "snapshots": [s.to_dict() for s in self.snapshots],
            "gates": [g.to_dict() for g in self.gates],
            "detected_from": self.detected_from,
            "started": self.started,
            "updated": self.updated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrepPrState:
        """Create state from dictionary."""
        data["snapshots"] = [
            ScopeSnapshot.from_dict(s) for s in data.get("snapshots", [])
        ]
        data["gates"] = [Gate.from_dict(g) for g in data.get("gates", [])]
        return cls(**data)

    def add_snapshot(self, snapshot: ScopeSnapshot) -> None:
        """Add a scope snapshot and update timestamp."""
        self.snapshots.append(snapshot)
        self.updated = datetime.now(UTC).isoformat()

    def increment_cycle(self) -> bool:
        """Increment cycle counter. Returns True if within limit."""
        self.current_cycle += 1
        self.updated = datetime.now(UTC).isoformat()
        return self.current_cycle <= self.max_cycles


# --- Persistence ---


def load_state() -> PrepPrState | None:
    """Load state from JSON file."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        return PrepPrState.from_dict(data)
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        logger.exception("Failed to load prep-pr state")
        return None


def save_state(state: PrepPrState) -> None:
    """Save state to JSON file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state.updated = datetime.now(UTC).isoformat()
    STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2) + "\n")


def clean_state() -> bool:
    """Remove state file. Returns True if removed."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        logger.info("Removed %s", STATE_FILE)
        return True
    logger.info("No state file to remove")
    return False


# --- Git Helpers ---


def _run_git(args: list[str]) -> str:
    """Run a git command and return stdout.

    Returns empty string on failure instead of raising.
    """
    cmd = ["git", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logger.exception("git not found")
        return ""
    except subprocess.CalledProcessError as e:
        logger.warning("git command failed: %s\n%s", " ".join(cmd), e.stderr.strip())
        return ""
    return result.stdout.strip()


# --- Gate Detection ---


# Default gate commands per ecosystem marker file
ECOSYSTEM_GATES: dict[str, list[Gate]] = {
    "pyproject.toml": [
        Gate(
            name="ruff",
            command="uv run ruff check .",
            autofix="uv run ruff check --fix .",
        ),
        Gate(
            name="ruff-format",
            command="uv run ruff format --check .",
            autofix="uv run ruff format .",
        ),
        Gate(name="mypy", command="uv run mypy ."),
        Gate(name="pytest", command="uv run pytest"),
    ],
    "package.json": [
        Gate(name="lint", command="npm run lint", autofix="npm run lint -- --fix"),
        Gate(name="typecheck", command="npx tsc --noEmit"),
        Gate(name="test", command="npm test"),
    ],
    "Cargo.toml": [
        Gate(name="clippy", command="cargo clippy -- -D warnings"),
        Gate(name="test", command="cargo test"),
    ],
    "go.mod": [
        Gate(name="vet", command="go vet ./..."),
        Gate(name="test", command="go test ./..."),
    ],
}


# --- Gate Timeout / Liveness (#1432) ---

# Foreground ceiling (seconds) before a gate switches from synchronous
# execution to backgrounded run_in_background + polled-Read liveness
# checking. Deliberately generous-but-bounded "switch to background"
# triggers, NOT sized to guarantee inline completion -- mypy/pytest/
# pre-commit routinely exceed these on repos this size (see #1432,
# GEN-5458/GEN-5463). Tunable per-gate; unlisted gates fall back to
# GATE_TIMEOUT_FALLBACK_SECONDS.
GATE_TIMEOUT_DEFAULTS: dict[str, int] = {
    "mypy": 600,
    "pytest": 600,
    "pre-commit": 480,
}
GATE_TIMEOUT_FALLBACK_SECONDS: int = 480

# Hard poll ceiling (seconds) once a gate has been backgrounded: total
# wall-clock time /prep-pr Step 7 may spend re-Read-ing the backgrounded
# command's output file waiting for a completion notification before
# concluding the result is lost (dead backgrounding path) rather than
# merely slow, and emitting an early gate_timeout PREP_PR_BLOCK. Chosen
# well inside headless_timeout_by_stage[Stage.FINALIZE] (5400s,
# src/cw/models/orchestrator_config.py) so the block fires with stage
# budget to spare.
GATE_POLL_CEILING_SECONDS: int = 1800


def gate_timeout_seconds(name: str) -> int:
    """Foreground ceiling for a gate by name, falling back to the default."""
    return GATE_TIMEOUT_DEFAULTS.get(name, GATE_TIMEOUT_FALLBACK_SECONDS)


def elapsed_exceeds_ceiling(
    started: str, ceiling_s: int, *, now: datetime | None = None
) -> dict[str, Any]:
    """Compute elapsed seconds since an ISO-8601 timestamp vs a ceiling.

    Raises ValueError on a malformed or timezone-naive *started* timestamp
    rather than silently misreporting (a naive timestamp would otherwise
    raise an uncaught TypeError when subtracted from the tz-aware "now").
    """
    started_dt = datetime.fromisoformat(started)
    if started_dt.tzinfo is None:
        msg = f"'started' timestamp must be timezone-aware, got {started!r}"
        raise ValueError(msg)
    now_dt = now if now is not None else datetime.now(UTC)
    elapsed_s = (now_dt - started_dt).total_seconds()
    return {
        "elapsed_s": elapsed_s,
        "ceiling_s": ceiling_s,
        "exceeded": elapsed_s > ceiling_s,
    }


# uv run flags that consume the next token as their argument value
_UV_RUN_ARGUMENT_FLAGS: frozenset[str] = frozenset(
    {
        "--extra",
        "--python",
        "--with",
        "--directory",
        "--project",
        "--package",
        "--env-file",
        "--python-preference",
    }
)


def _strip_inline_comment(line: str) -> str:
    """Remove trailing shell comment (first ' #' to end-of-line)."""
    idx = line.find(" #")
    return line[:idx] if idx != -1 else line


def _derive_gate_name(command: str) -> str:
    """Derive a gate name from a shell command string.

    - ``uv run [options] <cmd>`` → ``<cmd>`` (skips uv-run flags and their args)
    - ``npx <cmd>`` → ``<cmd>``
    - Anything else → first token
    """
    tokens = command.split()
    if not tokens:
        return ""
    if len(tokens) >= 2 and tokens[0] == "uv" and tokens[1] == "run":
        i = 2
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                i += 1
                if (
                    tok.startswith("--")
                    and "=" not in tok
                    and tok in _UV_RUN_ARGUMENT_FLAGS
                    and i < len(tokens)
                ):
                    i += 1  # skip the flag's value token
            else:
                return tok
        return ""
    if tokens[0] == "npx" and len(tokens) >= 2:
        return tokens[1]
    return tokens[0]


def _parse_bash_block_gates(fence_lines: list[str]) -> list[Gate]:
    """Parse Gate objects from the lines inside a bash code fence.

    Joins continuation lines (trailing backslash), strips inline comments,
    and derives gate names from executables. No autofix for bash-block gates.
    """
    gates: list[Gate] = []
    pending = ""
    for raw in fence_lines:
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1].rstrip() + " "
        else:
            full = (pending + raw.strip()).strip()
            pending = ""
            command = _strip_inline_comment(full).strip()
            if not command or command.startswith("#"):
                continue
            name = _derive_gate_name(command)
            if name:
                gates.append(Gate(name=name, command=command))
    if pending.strip():
        command = _strip_inline_comment(pending.strip()).strip()
        if command and not command.startswith("#"):
            name = _derive_gate_name(command)
            if name:
                gates.append(Gate(name=name, command=command))
    return gates


def _parse_claude_md_gates(claude_md_path: Path) -> list[Gate]:
    """Parse quality gates from a CLAUDE.md file.

    Supports two formats inside the ``## Quality Gates`` section:

    Bullet lines (name with optional autofix)::

        - name: command
        - name: command | autofix_command

    Bash code block (gate name derived from executable)::

        ```bash
        uv run ruff check src/ tests/
        uv run diff-cover coverage.xml --compare-branch=origin/main \\
          --fail-under=90
        ```

    When both formats appear in the same section, bash-block gates override
    bullet gates that share the same name (bullets first, then bash-block wins).
    Inline comments (`` # …``) are stripped from bash-block commands.
    An unclosed fence returns no bash-block gates and raises no exception.
    """
    if not claude_md_path.exists():
        return []

    try:
        content = claude_md_path.read_text()
    except OSError:
        return []

    in_gates_section = False
    in_bash_fence = False
    fence_lines: list[str] = []
    bullet_gates: list[Gate] = []
    block_gates: list[Gate] = []

    for line in content.splitlines():
        stripped = line.strip()

        # While inside a bash fence, ## headers are treated as fence content
        # (rare/malformed, but avoids false section-exit on ## inside code blocks).
        if not in_bash_fence and stripped.startswith("## "):
            in_gates_section = stripped == "## Quality Gates"
            continue

        if not in_gates_section:
            continue

        if in_bash_fence:
            if stripped == "```":
                block_gates.extend(_parse_bash_block_gates(fence_lines))
                in_bash_fence = False
                fence_lines = []
            else:
                fence_lines.append(line)
        elif stripped in ("```bash", "```sh", "```"):
            in_bash_fence = True
            fence_lines = []
        elif stripped.startswith("- "):
            gate_text = stripped[2:].strip()
            if ": " not in gate_text:
                continue
            name, rest = gate_text.split(": ", 1)
            name = name.strip()
            if " | " in rest:
                command, autofix = rest.split(" | ", 1)
                bullet_gates.append(
                    Gate(
                        name=name.strip(),
                        command=command.strip(),
                        autofix=autofix.strip(),
                    )
                )
            else:
                bullet_gates.append(Gate(name=name.strip(), command=rest.strip()))

    # Bash-block gates override bullet gates of the same name; bullets kept first
    # for any names not overridden (mirrors detect_gates() dedup logic).
    block_names = {g.name for g in block_gates}
    merged = [g for g in bullet_gates if g.name not in block_names]
    merged.extend(block_gates)
    return merged


def detect_gates(claude_md_path: Path | None = None) -> dict[str, Any]:
    """Auto-detect quality gates from project files and CLAUDE.md overrides.

    Returns dict with 'gates' list and 'detected_from' list.
    """
    cwd = Path.cwd()
    gates: list[Gate] = []
    detected_from: list[str] = []

    # Scan for ecosystem marker files
    for marker, default_gates in ECOSYSTEM_GATES.items():
        if (cwd / marker).exists():
            detected_from.append(marker)
            gates.extend(default_gates)

    # Check for CLAUDE.md overrides
    if claude_md_path is None:
        claude_md_path = cwd / "CLAUDE.md"

    override_gates = _parse_claude_md_gates(claude_md_path)
    if override_gates:
        detected_from.append("CLAUDE.md")
        # Override: if a CLAUDE.md gate has the same name as a default, replace it
        override_names = {g.name for g in override_gates}
        gates = [g for g in gates if g.name not in override_names]
        gates.extend(override_gates)

    return {
        "gates": [g.to_dict() for g in gates],
        "detected_from": detected_from,
    }


# --- Scope Tracking ---


def _resolve_base_ref(base: str, fork_point: str | None = None) -> str:
    """Resolve the best ref for diffing against a base branch.

    If *fork_point* is supplied (a commit SHA recorded at worktree creation),
    it is used directly — this eliminates drift when origin/<base> advances
    between implementation and review.

    Otherwise prefers origin/<base> (up-to-date after fetch) over the local
    branch, which may be stale and cause inflated diffs after merging origin
    into a feature branch.
    """
    if fork_point:
        # Validate the SHA exists in the local object store
        if _run_git(["rev-parse", "--verify", f"{fork_point}^{{commit}}"]):
            return fork_point
        logger.warning(
            "fork-point %s not found locally, falling back to branch ref", fork_point
        )

    remote_ref = f"origin/{base}"
    # Check if the remote ref exists
    if _run_git(["rev-parse", "--verify", remote_ref]):
        return remote_ref
    return base


def capture_snapshot(
    base: str, cycle: int, *, fork_point: str | None = None
) -> ScopeSnapshot:
    """Capture current diff metrics vs base branch."""
    base_ref = _resolve_base_ref(base, fork_point=fork_point)

    # Get changed files
    files_raw = _run_git(["diff", "--name-only", f"{base_ref}...HEAD"])
    files = [f for f in files_raw.splitlines() if f] if files_raw else []

    # Get diff stats
    stat_raw = _run_git(["diff", "--stat", f"{base_ref}...HEAD"])
    additions = 0
    deletions = 0
    if stat_raw:
        # Parse numstat for accurate counts
        numstat_raw = _run_git(["diff", "--numstat", f"{base_ref}...HEAD"])
        for numstat_line in numstat_raw.splitlines():
            parts = numstat_line.split("\t")
            if len(parts) >= _NUMSTAT_MIN_FIELDS:
                with contextlib.suppress(ValueError):
                    additions += int(parts[0])
                with contextlib.suppress(ValueError):
                    deletions += int(parts[1])

    # Get unique directories
    directories = sorted({str(Path(f).parent) for f in files if f})

    return ScopeSnapshot(
        timestamp=datetime.now(UTC).isoformat(),
        cycle=cycle,
        files=files,
        additions=additions,
        deletions=deletions,
        directories=directories,
    )


def check_scope(state: PrepPrState) -> dict[str, Any]:
    """Compare latest vs initial snapshot, report scope changes.

    Returns dict with warnings and metrics.
    """
    if len(state.snapshots) < _MIN_SNAPSHOTS_FOR_SCOPE:
        return {"warnings": [], "metrics": {}}

    initial = state.snapshots[0]
    latest = state.snapshots[-1]

    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "initial_files": initial.file_count,
        "current_files": latest.file_count,
        "initial_lines": initial.line_count,
        "current_lines": latest.line_count,
    }

    # File count growth
    if initial.file_count > 0:
        file_growth = (latest.file_count - initial.file_count) / initial.file_count
        metrics["file_growth_pct"] = round(file_growth * 100, 1)
        if file_growth > SCOPE_FILE_COUNT_THRESHOLD:
            warnings.append(
                f"File count grew by {metrics['file_growth_pct']}% "
                f"({initial.file_count} → {latest.file_count})"
            )

    # Line count growth
    if initial.line_count > 0:
        line_growth = (latest.line_count - initial.line_count) / initial.line_count
        metrics["line_growth_pct"] = round(line_growth * 100, 1)
        if line_growth > SCOPE_LINE_COUNT_THRESHOLD:
            warnings.append(
                f"Line count grew by {metrics['line_growth_pct']}% "
                f"({initial.line_count} → {latest.line_count})"
            )

    # New non-test files
    initial_files = set(initial.files)
    new_files = [f for f in latest.files if f not in initial_files]
    non_test_new = [f for f in new_files if not _is_test_file(f)]
    if non_test_new:
        warnings.append(f"New non-test files added: {', '.join(non_test_new)}")
        metrics["new_non_test_files"] = non_test_new

    # New directories
    initial_dirs = set(initial.directories)
    new_dirs = [d for d in latest.directories if d not in initial_dirs]
    if new_dirs:
        warnings.append(f"New directories touched: {', '.join(new_dirs)}")
        metrics["new_directories"] = new_dirs

    return {"warnings": warnings, "metrics": metrics}


def _is_test_file(filepath: str) -> bool:
    """Check if a file path looks like a test file."""
    parts = Path(filepath).parts
    name = parts[-1] if parts else ""
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in parts
        or "test" in parts
    )


# --- CLI Subcommands ---


def cmd_detect_gates(args: argparse.Namespace) -> None:
    """Handle detect-gates subcommand."""
    claude_md = Path(args.claude_md) if args.claude_md else None
    result = detect_gates(claude_md_path=claude_md)
    print(json.dumps(result, indent=2))


def cmd_snapshot(args: argparse.Namespace) -> None:
    """Handle snapshot subcommand."""
    base = args.base
    fork_point: str | None = getattr(args, "fork_point", None)
    state = load_state()

    if state is None:
        state = PrepPrState(base_branch=base)
        if args.max_cycles:
            state.max_cycles = args.max_cycles

    cycle = state.current_cycle
    snapshot = capture_snapshot(base, cycle, fork_point=fork_point)
    state.add_snapshot(snapshot)
    save_state(state)

    print(json.dumps(snapshot.to_dict(), indent=2))


def cmd_check_scope(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Handle check-scope subcommand."""
    state = load_state()
    if state is None:
        logger.error("No prep-pr state found. Run 'snapshot' first.")
        sys.exit(1)

    result = check_scope(state)
    print(json.dumps(result, indent=2))


def cmd_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Handle status subcommand."""
    state = load_state()
    if state is None:
        print(json.dumps({"error": "No active prep-pr session"}))
        return

    status = {
        "base_branch": state.base_branch,
        "current_cycle": state.current_cycle,
        "max_cycles": state.max_cycles,
        "snapshot_count": len(state.snapshots),
        "gate_count": len(state.gates),
        "started": state.started,
        "updated": state.updated,
    }
    print(json.dumps(status, indent=2))


def cmd_clean(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Handle clean subcommand."""
    removed = clean_state()
    print(json.dumps({"removed": removed}))


def cmd_gate_timeout(args: argparse.Namespace) -> None:
    """Handle gate-timeout subcommand."""
    result = {
        "gate": args.name,
        "foreground_ceiling_s": gate_timeout_seconds(args.name),
        "poll_ceiling_s": GATE_POLL_CEILING_SECONDS,
    }
    print(json.dumps(result, indent=2))


def cmd_gate_elapsed(args: argparse.Namespace) -> None:
    """Handle gate-elapsed subcommand."""
    try:
        result = elapsed_exceeds_ceiling(args.started, args.ceiling_seconds)
    except ValueError:
        logger.exception("Malformed --started timestamp")
        sys.exit(1)
    print(json.dumps(result, indent=2))


# --- CLI Entry Point ---


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="PR preparation: gate detection, scope tracking, state persistence",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # detect-gates
    gates_parser = subparsers.add_parser(
        "detect-gates", help="Auto-detect quality gates"
    )
    gates_parser.add_argument(
        "--claude-md", help="Path to CLAUDE.md (default: ./CLAUDE.md)"
    )

    # snapshot
    snap_parser = subparsers.add_parser("snapshot", help="Capture diff metrics")
    snap_parser.add_argument(
        "--base", default="main", help="Base branch (default: main)"
    )
    snap_parser.add_argument(
        "--fork-point",
        default=None,
        help=(
            "Exact commit SHA the feature branch diverged from"
            " (overrides --base for diff)"
        ),
    )
    snap_parser.add_argument("--max-cycles", type=int, help="Max review cycles")

    # check-scope
    subparsers.add_parser("check-scope", help="Compare latest vs initial snapshot")

    # status
    subparsers.add_parser("status", help="Show current session state")

    # clean
    subparsers.add_parser("clean", help="Remove state file")

    # gate-timeout
    gate_timeout_parser = subparsers.add_parser(
        "gate-timeout", help="Foreground/poll timeout ceilings for a named gate"
    )
    gate_timeout_parser.add_argument("name", help="Gate name (e.g. mypy, pytest)")

    # gate-elapsed
    gate_elapsed_parser = subparsers.add_parser(
        "gate-elapsed", help="Elapsed time since a timestamp vs a ceiling"
    )
    gate_elapsed_parser.add_argument(
        "--started", required=True, help="ISO-8601 start timestamp"
    )
    gate_elapsed_parser.add_argument(
        "--ceiling-seconds",
        type=int,
        required=True,
        dest="ceiling_seconds",
        help="Ceiling in seconds",
    )

    args = parser.parse_args()

    dispatch = {
        "detect-gates": cmd_detect_gates,
        "snapshot": cmd_snapshot,
        "check-scope": cmd_check_scope,
        "status": cmd_status,
        "clean": cmd_clean,
        "gate-timeout": cmd_gate_timeout,
        "gate-elapsed": cmd_gate_elapsed,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)


if __name__ == "__main__":
    main()
