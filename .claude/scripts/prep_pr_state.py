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
  gate-pass     — Record a gate pass at HEAD, or check whether one still applies
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

try:
    yaml: ModuleType | None = import_module("yaml")
except ImportError:  # pragma: no cover - downstream repo without PyYAML
    yaml = None

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# State file lives in the repo's .claude/ directory
STATE_FILE = Path(".claude/prep-pr-state.json")

# Per-repo config, read for quality_gates.timeouts (cwd-relative like STATE_FILE)
PROJECT_CONFIG_PATH = Path(".claude/project-config.yaml")

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
class GatePass:
    """A gate that passed at ``sha`` with ``command`` on a clean worktree."""

    name: str
    command: str
    sha: str
    recorded: str
    skipped_at: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatePass:
        """Create from dictionary."""
        return cls(
            name=data["name"],
            command=data["command"],
            sha=data["sha"],
            recorded=data["recorded"],
            skipped_at=list(data.get("skipped_at", [])),
        )


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
    gate_passes: list[GatePass] = field(default_factory=list)

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
            "gate_passes": [p.to_dict() for p in self.gate_passes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrepPrState:
        """Create state from dictionary."""
        data["snapshots"] = [
            ScopeSnapshot.from_dict(s) for s in data.get("snapshots", [])
        ]
        data["gates"] = [Gate.from_dict(g) for g in data.get("gates", [])]
        data["gate_passes"] = [
            GatePass.from_dict(p) for p in data.get("gate_passes", [])
        ]
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


def _run_git(args: list[str], *, default: str = "") -> str:
    """Run a git command and return stdout.

    Returns *default* (empty string unless given) on failure instead of raising.
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
        return default
    except subprocess.CalledProcessError as e:
        logger.warning("git command failed: %s\n%s", " ".join(cmd), e.stderr.strip())
        return default
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
# GATE_TIMEOUT_FALLBACK_SECONDS. Keys match by longest hyphen-delimited
# prefix of the gate name (see gate_timeout_seconds), so derived names such
# as ``pytest-integration`` inherit the ``pytest`` ceiling.
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

# The Bash tool's own foreground timeout cap (600000 ms). A configured ceiling
# above it cannot be honoured, so every resolved ceiling is clamped to it (#2758).
BASH_FOREGROUND_CAP_SECONDS: int = 600

# project-config.yaml location of per-repo ceiling overrides:
# quality_gates.timeouts.<gate-name>: <seconds> (#2758).
GATE_TIMEOUT_CONFIG_SECTION = "quality_gates"
GATE_TIMEOUT_CONFIG_KEY = "timeouts"

# One ``key: value`` entry line inside the canonical timeouts block, and the
# integer shape the PyYAML-free scanner converts (anything else stays a string
# so the shared validator rejects it, as PyYAML's float/bool/str would be).
_TIMEOUT_ENTRY_RE = re.compile(r"(?P<key>[^\s:]+):[ ]+(?P<value>\S.*)")
_INT_RE = re.compile(r"-?\d+")


def _matching_key(name: str, table: Mapping[str, object]) -> str | None:
    """Longest hyphen-delimited prefix of *name* that is a key of *table*.

    The full name first, then with trailing ``-segment`` parts dropped one at a
    time, so ``pytest-integration`` matches ``pytest`` and ``pre-commit`` still
    matches its own exact key. None when no prefix is a key.
    """
    parts = name.split("-")
    for end in range(len(parts), 0, -1):
        prefix = "-".join(parts[:end])
        if prefix in table:
            return prefix
    return None


def gate_timeout_seconds(name: str) -> int:
    """Foreground ceiling for a gate by name, falling back to the default.

    Lookup is by longest hyphen-delimited prefix (see ``_matching_key``), so a
    derived name such as ``pytest-integration`` resolves to ``pytest``.
    """
    key = _matching_key(name, GATE_TIMEOUT_DEFAULTS)
    if key is None:
        return GATE_TIMEOUT_FALLBACK_SECONDS
    return GATE_TIMEOUT_DEFAULTS[key]


def _non_canonical(detail: str) -> ValueError:
    msg = (
        f"{GATE_TIMEOUT_CONFIG_SECTION}.{GATE_TIMEOUT_CONFIG_KEY} is not in the "
        f"canonical block shape the PyYAML-free reader needs ({detail})"
    )
    return ValueError(msg)


def _timeouts_block(lines: list[str]) -> list[str]:
    """Stripped entry lines under a column-0 ``quality_gates:`` / ``timeouts:``.

    The block ends at the first line indented at or below ``timeouts:``. Raises
    ValueError on an inline (flow-style) value for either key.
    """
    section_header = f"{GATE_TIMEOUT_CONFIG_SECTION}:"
    key_header = f"{GATE_TIMEOUT_CONFIG_KEY}:"
    in_section = False
    block_indent: int | None = None
    block: list[str] = []
    for line in lines:
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if block_indent is not None:
            if indent <= block_indent:
                break
            block.append(stripped)
        elif indent == 0:
            in_section = stripped == section_header
            if not in_section and stripped.startswith(section_header):
                raise _non_canonical(stripped)
        elif in_section and stripped.startswith(key_header):
            if stripped != key_header:
                raise _non_canonical(stripped)
            block_indent = indent
    return block


def _scan_timeouts(text: str) -> dict[object, object]:
    """Read ``quality_gates.timeouts`` without PyYAML (canonical block shape).

    Comment lines and trailing `` #`` comments are dropped. Integer values
    become ``int``; any other value stays a string for the validator to reject.
    Raises ValueError on an entry that is not ``key: value``.
    """
    lines = [_strip_inline_comment(line).rstrip() for line in text.splitlines()]
    lines = [
        line for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    entries: dict[object, object] = {}
    for entry in _timeouts_block(lines):
        match = _TIMEOUT_ENTRY_RE.fullmatch(entry)
        if match is None:
            raise _non_canonical(entry)
        value = match["value"]
        entries[match["key"]] = int(value) if _INT_RE.fullmatch(value) else value
    return entries


def _nested_mapping(
    parent: Mapping[object, object], key: str
) -> Mapping[object, object]:
    """``parent[key]`` as a mapping: absent/null is empty, anything else raises."""
    child = parent.get(key)
    if child is None:
        return {}
    if not isinstance(child, dict):
        msg = f"{key} is not a mapping"
        raise TypeError(msg)
    return child


def _parse_timeouts(text: str) -> Mapping[object, object]:
    """The raw ``quality_gates.timeouts`` mapping from config *text*."""
    if yaml is None:
        return _scan_timeouts(text)
    doc = yaml.safe_load(text)
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        msg = "root is not a mapping"
        raise TypeError(msg)
    section = _nested_mapping(doc, GATE_TIMEOUT_CONFIG_SECTION)
    return _nested_mapping(section, GATE_TIMEOUT_CONFIG_KEY)


def load_timeout_overrides(config_path: Path = PROJECT_CONFIG_PATH) -> dict[str, int]:
    """Valid ``quality_gates.timeouts`` entries from *config_path*.

    A missing file is silently empty. An unreadable or malformed file, or a
    wrong shape at either level (TypeError), logs a warning and yields ``{}``.
    Each entry needs a string key and a positive integer (not bool) value; an
    invalid entry is logged and skipped without affecting its siblings.
    """
    if not config_path.exists():
        return {}
    errors: tuple[type[Exception], ...] = (OSError, ValueError, TypeError)
    if yaml is not None:
        errors = (*errors, yaml.YAMLError)
    try:
        raw = _parse_timeouts(config_path.read_text(encoding="utf-8"))
    except errors as exc:
        logger.warning("Ignoring gate timeouts in %s: %s", config_path, exc)
        return {}
    overrides: dict[str, int] = {}
    for key, value in raw.items():
        if (
            isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        ):
            overrides[key] = value
        else:
            logger.warning(
                "Ignoring %s.%s.%s = %r in %s: not a positive integer of seconds",
                GATE_TIMEOUT_CONFIG_SECTION,
                GATE_TIMEOUT_CONFIG_KEY,
                key,
                value,
                config_path,
            )
    return overrides


def resolve_gate_timeout(
    name: str, config_path: Path = PROJECT_CONFIG_PATH
) -> dict[str, Any]:
    """Resolve a gate's foreground ceiling: config override, default, clamp.

    A configured key (exact or longest hyphen prefix) wins over the built-in
    table. The result is clamped to the Bash tool's 600 s foreground cap, and
    ``clamped``/``note`` say so when the clamp changed it.
    """
    overrides = load_timeout_overrides(config_path)
    key = _matching_key(name, overrides)
    configured = overrides[key] if key is not None else None
    value = configured if configured is not None else gate_timeout_seconds(name)
    clamped = value > BASH_FOREGROUND_CAP_SECONDS
    note = (
        f"configured {value}s exceeds the {BASH_FOREGROUND_CAP_SECONDS}s Bash "
        f"foreground cap; using {BASH_FOREGROUND_CAP_SECONDS}s"
        if clamped
        else None
    )
    return {
        "gate": name,
        "foreground_ceiling_s": min(value, BASH_FOREGROUND_CAP_SECONDS),
        "poll_ceiling_s": GATE_POLL_CEILING_SECONDS,
        "source": "project-config" if configured is not None else "default",
        "configured_s": configured,
        "clamped": clamped,
        "note": note,
    }


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


# A bullet gate name is one identifier-like token: names are passed as CLI args
# (``gate-timeout <name>``) and printed in ``gate: <name>`` blocks, so they
# cannot contain spaces or backticks. This is what separates ``- mypy: cmd``
# from a prose bullet such as ``- Test suite: 100% pass rate required``.
_GATE_NAME_RE = re.compile(r"[\w][\w.+-]*")

# A subcommand token (``check`` in ``ruff check``) usable as a name qualifier.
_SUBCOMMAND_RE = re.compile(r"[a-z][a-z-]*")

# ``-m <expr>`` pytest marker expression, bare or single/double quoted.
_MARKER_RE = re.compile(r"""(?:^|\s)-m\s+(?P<q>['"]?)(?P<expr>.+?)(?P=q)(?:\s|$)""")

# A command wrapped in exactly one backtick pair: ``- name: `cmd` ``.
_BACKTICKS_IN_FENCE = 2


def _split_command(command: str) -> tuple[str, list[str]]:
    """Split a shell command into ``(executable, remaining_tokens)``.

    - ``uv run [options] <cmd> ...`` → ``<cmd>`` (skips uv-run flags and their
      args; the remaining tokens are those after ``<cmd>``)
    - ``npx <cmd> ...`` → ``<cmd>``
    - Anything else → first token
    """
    tokens = command.split()
    if not tokens:
        return "", []
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
                return tok, tokens[i + 1 :]
        return "", []
    if tokens[0] == "npx" and len(tokens) >= 2:
        return tokens[1], tokens[2:]
    return tokens[0], tokens[1:]


def _gate_qualifier(command: str) -> str:
    """Name qualifier that tells a gate apart from same-executable siblings.

    - the first argument when it is a bare subcommand (``ruff check`` →
      ``check``, ``ruff format`` → ``format``)
    - otherwise the slugified ``-m <marker>`` expression
      (``-m 'not integration'`` → ``not-integration``)
    - otherwise ``""`` (no qualifier available)
    """
    _, args = _split_command(command)
    if args and _SUBCOMMAND_RE.fullmatch(args[0]):
        return args[0]
    marker = _MARKER_RE.search(command)
    if marker:
        return re.sub(r"[^a-z0-9]+", "-", marker.group("expr").lower()).strip("-")
    return ""


def _disambiguate_gate_names(gates: list[Gate]) -> list[Gate]:
    """Make gate names unique, qualifying only the names that collide.

    A gate keeps its bare executable name unless another gate shares it; the
    colliding gates get ``-<qualifier>`` (see ``_gate_qualifier``). Any names
    still duplicated afterwards get an ordinal suffix on the second and later
    occurrences (``mypy``, ``mypy-2``).
    """
    counts = Counter(g.name for g in gates)
    qualified: list[Gate] = []
    for gate in gates:
        qualifier = _gate_qualifier(gate.command) if counts[gate.name] > 1 else ""
        qualified.append(
            replace(gate, name=f"{gate.name}-{qualifier}") if qualifier else gate
        )

    seen: Counter[str] = Counter()
    unique: list[Gate] = []
    for gate in qualified:
        prior = seen[gate.name]
        seen[gate.name] += 1
        unique.append(replace(gate, name=f"{gate.name}-{prior + 1}") if prior else gate)
    return unique


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
            name, _ = _split_command(command)
            if name:
                gates.append(Gate(name=name, command=command))
    if pending.strip():
        command = _strip_inline_comment(pending.strip()).strip()
        if command and not command.startswith("#"):
            name, _ = _split_command(command)
            if name:
                gates.append(Gate(name=name, command=command))
    return gates


@dataclass(frozen=True)
class ClaudeMdGates:
    """Gates parsed from a CLAUDE.md ``## Quality Gates`` section.

    ``authoritative`` is True when a bash block contributed at least one gate:
    the block then defines the complete gate set, so ecosystem defaults must
    not be merged in.
    """

    gates: list[Gate]
    authoritative: bool


def _strip_backtick_fence(text: str) -> str:
    """Unwrap exactly one surrounding backtick pair; otherwise return unchanged."""
    if (
        text.count("`") == _BACKTICKS_IN_FENCE
        and text.startswith("`")
        and text.endswith("`")
    ):
        return text[1:-1].strip()
    return text


def _parse_bullet_gate(gate_text: str) -> Gate | None:
    """Parse ``name: command [| autofix]`` into a Gate, or None for prose.

    The name before the first ``": "`` must be a single identifier-like token
    (``_GATE_NAME_RE``), and the command must be non-empty. Anything else is
    treated as prose, not a gate. A command or autofix wrapped in one backtick
    pair is unwrapped.
    """
    name, separator, rest = gate_text.partition(": ")
    name = name.strip()
    if not separator or not _GATE_NAME_RE.fullmatch(name):
        return None
    command, _, autofix = rest.partition(" | ")
    command = _strip_backtick_fence(command.strip())
    autofix = _strip_backtick_fence(autofix.strip())
    if not command:
        return None
    return Gate(name=name, command=command, autofix=autofix or None)


def _parse_claude_md_gates(claude_md_path: Path) -> ClaudeMdGates:
    """Parse quality gates from a CLAUDE.md file.

    Supports two formats inside the ``## Quality Gates`` section:

    Bullet lines (single-token name, optional autofix)::

        - name: command
        - name: command | autofix_command
        - name: `command` | `autofix_command`

    A bullet is a gate only when the text before the first ``": "`` is one
    identifier-like token and the command is non-empty; prose bullets such as
    ``- Test suite: 100% pass rate required`` are ignored. One wrapping
    backtick pair around the command or autofix is stripped.

    Bash code block (gate name derived from executable)::

        ```bash
        uv run ruff check src/ tests/
        uv run diff-cover coverage.xml --compare-branch=origin/main \\
          --fail-under=90
        ```

    Bash-block gates keep their bare executable name unless another gate in the
    section shares it, in which case each colliding gate is qualified by its
    subcommand or ``-m`` marker (``ruff-check``, ``ruff-format``,
    ``pytest-not-integration``); leftover duplicates get an ordinal suffix.
    The result is authoritative when a bash block yielded at least one gate
    (see ``ClaudeMdGates``); an empty or unclosed block is not.

    When both formats appear in the same section, bash-block gates override
    bullet gates that share the same name (bullets first, then bash-block wins).
    Inline comments (`` # …``) are stripped from bash-block commands.
    An unclosed fence returns no bash-block gates and raises no exception.
    """
    if not claude_md_path.exists():
        return ClaudeMdGates(gates=[], authoritative=False)

    try:
        content = claude_md_path.read_text()
    except OSError:
        return ClaudeMdGates(gates=[], authoritative=False)

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
            bullet_gate = _parse_bullet_gate(stripped[2:].strip())
            if bullet_gate is not None:
                bullet_gates.append(bullet_gate)

    # Disambiguate once, after every fence in the section has been collected,
    # so same-executable gates split across fences are still told apart.
    block_gates = _disambiguate_gate_names(block_gates)

    # Bash-block gates override bullet gates of the same name; bullets kept first
    # for any names not overridden.
    block_names = {g.name for g in block_gates}
    merged = [g for g in bullet_gates if g.name not in block_names]
    merged.extend(block_gates)
    return ClaudeMdGates(gates=merged, authoritative=bool(block_gates))


def detect_gates(claude_md_path: Path | None = None) -> dict[str, Any]:
    """Auto-detect quality gates from project files and CLAUDE.md overrides.

    Returns dict with 'gates' list and 'detected_from' list.

    A CLAUDE.md bash block that yields at least one gate is authoritative:
    ecosystem defaults are dropped and ``detected_from`` lists only
    ``CLAUDE.md``. Otherwise CLAUDE.md bullet gates override same-named
    ecosystem defaults.
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

    parsed = _parse_claude_md_gates(claude_md_path)
    if parsed.authoritative:
        # The bash block is the complete gate set: drop every ecosystem default.
        gates = []
        detected_from = ["CLAUDE.md"]
    elif parsed.gates:
        detected_from.append("CLAUDE.md")
        # Override: if a CLAUDE.md gate has the same name as a default, replace it
        override_names = {g.name for g in parsed.gates}
        gates = [g for g in gates if g.name not in override_names]
    gates.extend(parsed.gates)

    return {
        "gates": [g.to_dict() for g in gates],
        "detected_from": detected_from,
    }


# --- Gate-Pass Stamps (#2758) ---

# _run_git default for `git status`: a failed status reads as dirty (fail closed).
_GIT_STATUS_FAILED = "git-status-failed"

# Dependency-sync gates change the environment as they run, so a prior pass is
# not sufficient evidence that re-running them is redundant. Keep this list
# deliberately focused on explicit install/sync commands; ordinary `uv run`
# gates remain eligible for same-SHA skips.
_ENVIRONMENT_MUTATING_GATE_RE = re.compile(
    r"(?<![\w-])(?:"
    r"uv\s+sync|uv\s+pip\s+install|"
    r"(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?pip\s+install|"
    r"(?:npm|yarn|pnpm)\s+(?:install|ci)|poetry\s+install|cargo\s+fetch"
    r")(?![\w-])"
)


def _is_environment_mutating_gate(command: str) -> bool:
    """Whether *command* explicitly installs or synchronizes dependencies."""
    return _ENVIRONMENT_MUTATING_GATE_RE.search(command) is not None


def _gate_command(name: str) -> str | None:
    """Command of the gate called *name* in ``detect_gates()``, else None.

    The command is looked up here rather than passed in, so a re-quoted
    command string can never drift from the one ``detect-gates`` printed.
    """
    for gate in detect_gates()["gates"]:
        if gate["name"] == name:
            return str(gate["command"])
    return None


def _worktree_state() -> tuple[str, bool]:
    """``(HEAD sha or "", dirty)`` for the cwd worktree.

    Untracked non-ignored files count as dirty; ignored files do not. The state
    file is excluded by pathspec, so it never dirties the tree whether or not
    the repo ignores it.
    """
    head = _run_git(["rev-parse", "HEAD"])
    status = _run_git(
        ["status", "--porcelain", "--", ".", f":(exclude){STATE_FILE.as_posix()}"],
        default=_GIT_STATUS_FAILED,
    )
    return head, bool(status)


def gate_skip_reason(
    stamp: GatePass | None, command: str, head: str, dirty: bool
) -> str:
    """Why a gate may (``matched``) or may not be skipped, fail-closed order."""
    if stamp is None:
        return "no-stamp"
    if not head:
        return "head-unresolved"
    if dirty:
        return "dirty-worktree"
    if stamp.sha != head:
        return "sha-changed"
    if stamp.command != command:
        return "command-changed"
    return "matched"


def gate_pass_check(name: str) -> dict[str, Any]:
    """Report whether *name* already passed at HEAD; record the skip if so.

    Never creates the state file; writes it only to append a skip timestamp.
    """
    command = _gate_command(name)
    head, dirty = _worktree_state()
    state = load_state()
    stamps = state.gate_passes if state is not None else []
    stamp = next((p for p in stamps if p.name == name), None)
    if command is None:
        reason = "unknown-gate"
    elif _is_environment_mutating_gate(command):
        reason = "environment-mutating"
    else:
        reason = gate_skip_reason(stamp, command, head, dirty)
    skip = reason == "matched"
    message = None
    if skip and state is not None and stamp is not None:
        stamp.skipped_at.append(datetime.now(UTC).isoformat())
        save_state(state)
        message = (
            f"gate-skip: {name} already passed at {head} earlier in this prep-pr "
            "run; worktree clean; not re-run"
        )
    return {
        "gate": name,
        "skip": skip,
        "reason": reason,
        "sha": head or None,
        "stamped_sha": stamp.sha if stamp is not None else None,
        "message": message,
    }


def gate_pass_record(name: str, base: str) -> dict[str, Any]:
    """Stamp *name* as passed at HEAD, only on a clean worktree."""
    command = _gate_command(name)
    if command is None:
        return {"gate": name, "recorded": False, "reason": "unknown-gate"}
    if _is_environment_mutating_gate(command):
        return {"gate": name, "recorded": False, "reason": "environment-mutating"}
    head, dirty = _worktree_state()
    if not head or dirty:
        reason = "head-unresolved" if not head else "dirty-worktree"
        return {"gate": name, "recorded": False, "reason": reason}
    state = load_state() or PrepPrState(base_branch=base)
    state.gate_passes = [p for p in state.gate_passes if p.name != name]
    state.gate_passes.append(
        GatePass(
            name=name,
            command=command,
            sha=head,
            recorded=datetime.now(UTC).isoformat(),
        )
    )
    save_state(state)
    return {"gate": name, "recorded": True, "sha": head}


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
        "gate_skips": [
            {"gate": p.name, "sha": p.sha, "count": len(p.skipped_at)}
            for p in state.gate_passes
            if p.skipped_at
        ],
    }
    print(json.dumps(status, indent=2))


def cmd_clean(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Handle clean subcommand."""
    removed = clean_state()
    print(json.dumps({"removed": removed}))


def cmd_gate_timeout(args: argparse.Namespace) -> None:
    """Handle gate-timeout subcommand."""
    print(json.dumps(resolve_gate_timeout(args.name), indent=2))


def cmd_gate_pass(args: argparse.Namespace) -> None:
    """Handle gate-pass subcommand (``record`` or ``check``)."""
    if args.action == "record":
        result = gate_pass_record(args.name, args.base)
    else:
        result = gate_pass_check(args.name)
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

    # gate-pass
    gate_pass_parser = subparsers.add_parser(
        "gate-pass",
        help=(
            "Skip a redundant gate re-run: record a pass, or check for one at the"
            " current HEAD"
        ),
    )
    gate_pass_parser.add_argument(
        "action",
        choices=["record", "check"],
        help=(
            "record: stamp the gate as passed at HEAD (clean tree only); check:"
            " report whether a stamp still applies"
        ),
    )
    gate_pass_parser.add_argument(
        "name",
        help=(
            "Gate name exactly as detect-gates printed it (its command is looked"
            " up from CLAUDE.md, not passed)"
        ),
    )
    gate_pass_parser.add_argument(
        "--base",
        default="main",
        help=(
            "Base branch recorded if a new state file is created (record only;"
            " default: main)"
        ),
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
        "gate-pass": cmd_gate_pass,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)


if __name__ == "__main__":
    main()
