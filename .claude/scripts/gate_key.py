#!/usr/bin/env python3
"""Gate key and owner claim for the Step 2.5 gate paths (#2436).

Usage (from the Step 2.5 fences in `auto-dev-impl.md` and `auto-dev.md`):
    python3 .claude/scripts/gate_key.py --branch '<branch-name>'
    python3 .claude/scripts/gate_key.py --branch '<branch-name>' --claim \\
        --key "$GATE_KEY" --gate-root "<gate root>"
    python3 .claude/scripts/gate_key.py --release --key "$GATE_KEY" \\
        --gate-root "<gate root>"

Context: the gate worktree and its TMPDIR were named after the shell's
ambient session id, which is not unique across workers sharing a host, so two
concurrent gates could share one path and destroy each other's run. This
script is the one place the gate paths are named.

Key mode (`--branch` alone) prints a 16-hex key: the first 16 hex characters
of sha256 over the clone's absolute git common dir and the branch, so every
worktree of one clone agrees and two clones or two branches never collide.
It is the only mode that writes stdout.

`--claim` records which run owns the path before the fence's destructive
self-heal, in `<gate root>/cw-gate-owner-<key>.json`. The claim runs under an
`fcntl.flock` on `cw-gate-owner-<key>.lock`, held for the claim only: it
reads the owner record, judges whether its owner is alive (pid alive, not a
zombie, start token matching; another host or pid namespace fails closed),
and publishes a v1 record naming the worker pid and the fence's shell pid,
each with its start token. The worker pid comes from `CW_GATE_OWNER_PID`
(an explicit override), else `CLAUDE_PID`, accepted only when it is an
ancestor of this process; otherwise the shell pid stands in, with a WARN.
`--release` removes the record when it names this process's shell.

Stdlib-only by design and written to a Python 3.9 floor: it runs under a
client host's stock `python3`, not the repo's interpreter, and nothing in
`.claude/scripts/` imports `cw`.

Exit codes:
    0  — key printed, path claimed (or a dead owner taken over), or released
    1  — key or claim could not be computed or recorded (git failure, empty
         git common dir or branch, malformed key, unreadable shell start
         token, unusable gate root, claim-lock timeout, publish failure)
    2  — usage error (argparse)
    3  — claim refused: a live run, or an owner record that cannot be shown
         dead (another host or pid namespace, malformed, future version)
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

KEY_RE = re.compile(r"^[0-9a-f]{16}$")
KEY_LENGTH = 16
PROC_TOKEN_RE = re.compile(
    r"^proc:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9]{1,20}$"
)
PS_TOKEN_RE = re.compile(
    r"^ps:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
    r" (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r" [0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}$"
)

PROC_ROOT = Path("/proc")
GIT_TIMEOUT_S = 30.0
PS_TIMEOUT_S = 5.0
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05
MAX_ANCESTOR_DEPTH = 64
RECORD_VERSION = 1
# `ps` prints `lstart` in local time with locale-dependent names; pinning both
# keeps a live owner's token stable across an operator-side TZ/locale change.
PS_ENV_PINS = {"LC_ALL": "C", "TZ": "UTC"}
ZOMBIE_STATE = "Z"
VALUE_PREVIEW = 40

# Indices into the fields after the last `)` of /proc/<pid>/stat: field 3
# (state), field 4 (ppid) and field 22 (start time).
STAT_STATE = 0
STAT_PPID = 1
STAT_START = 19

OWNER_ENV_VARS = ("CW_GATE_OWNER_PID", "CLAUDE_PID")
SHELL_PID_SOURCE = "shell-pid"
UNREADABLE = "unreadable"
UNKNOWN = "unknown"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3


class GateKeyError(Exception):
    """The gate key could not be derived or validated."""


@dataclass(frozen=True)
class Probe:
    """Where process facts come from; tests swap in a fake tree and kill."""

    proc_root: Path = PROC_ROOT
    ps_cmd: str = "ps"
    kill: Callable[[int, int], None] = os.kill
    own_pid: int | None = None
    shell_pid: int | None = None

    def own(self) -> int:
        """This process's pid."""
        return self.own_pid if self.own_pid is not None else os.getpid()

    def shell(self) -> int:
        """The fence's shell: this process's parent."""
        return self.shell_pid if self.shell_pid is not None else os.getppid()


@dataclass(frozen=True)
class Proc:
    """A pid plus the start token that tells it apart from a recycled pid."""

    pid: int
    token: str


@dataclass(frozen=True)
class Claimant:
    """Who is claiming: the worker, the fence's shell, and how it was known."""

    worker: Proc
    shell: Proc
    source: str
    note: str
    pidns: str | None


@dataclass(frozen=True)
class OwnerRecord:
    """A v1 owner record that passed the shape check."""

    pid: int
    start: str
    shell_pid: int
    shell_start: str
    host: str
    pidns: str | None
    claimed_at: str
    source: str


# --- Key -----------------------------------------------------------------------


def require_hex_key(value: str) -> str:
    """Return *value* if it is a 16-char lowercase hex key, else raise."""
    if KEY_RE.fullmatch(value) is None:
        msg = f"not a 16-hex gate key: {value!r}"
        raise GateKeyError(msg)
    return value


def git_common_dir() -> str:
    """The absolute, resolved git common dir of the cwd's repository."""
    command = ["git", "rev-parse", "--git-common-dir"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"git rev-parse --git-common-dir could not run: {exc}"
        raise GateKeyError(msg) from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        msg = f"git rev-parse --git-common-dir exited {result.returncode}: {detail}"
        raise GateKeyError(msg)
    common = result.stdout.strip()
    if not common:
        msg = "git rev-parse --git-common-dir printed no git common dir"
        raise GateKeyError(msg)
    common_path = Path(common)
    try:
        resolved = common_path.resolve()
    except (OSError, RuntimeError) as exc:
        msg = f"git common dir {common_path} could not be resolved: {exc}"
        raise GateKeyError(msg) from exc
    return str(resolved)


def compute_key(common_dir: str, branch: str) -> str:
    """sha256 over the common dir and branch, first 16 hex characters."""
    if not common_dir:
        msg = "empty git common dir"
        raise GateKeyError(msg)
    if not branch:
        msg = "empty branch"
        raise GateKeyError(msg)
    payload = f"{common_dir}\n{branch}\n".encode()
    return require_hex_key(hashlib.sha256(payload).hexdigest()[:KEY_LENGTH])


# --- Process facts ---------------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _stat_fields(pid: int, probe: Probe) -> list[str] | None:
    """The fields after the last `)` of /proc/<pid>/stat (comm may hold `)`)."""
    text = _read_text(probe.proc_root / str(pid) / "stat")
    if text is None:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    return text[close + 1 :].split()


def _run_ps(probe: Probe, field: str, pid: int) -> str | None:
    """One `ps -o <field>= -p <pid>` read, whitespace-normalised, or None."""
    env = {**os.environ, **PS_ENV_PINS}
    command = [probe.ps_cmd, "-o", f"{field}=", "-p", str(pid)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=PS_TIMEOUT_S,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = " ".join(result.stdout.split())
    return text or None


def token_shape_ok(token: str) -> bool:
    """Whether *token* has the shape /proc or `ps` is expected to print."""
    return bool(PROC_TOKEN_RE.fullmatch(token) or PS_TOKEN_RE.fullmatch(token))


def process_start_token(pid: int, probe: Probe) -> str | None:
    """The pid's start token, or None when it cannot be read.

    With a /proc directory: `proc:<boot_id>:<start time>`, accepted only in
    the expected shape (a dead pid never falls through to `ps`). Without one:
    `ps:<lstart>` as read, whatever its shape, so the claimant's own tokens
    are recorded verbatim; `foreign_start_token` applies the shape check.
    """
    if probe.proc_root.is_dir():
        fields = _stat_fields(pid, probe)
        boot_id = _read_text(probe.proc_root / "sys" / "kernel" / "random" / "boot_id")
        if fields is None or boot_id is None or len(fields) <= STAT_START:
            return None
        token = f"proc:{boot_id.strip()}:{fields[STAT_START]}"
        return token if PROC_TOKEN_RE.fullmatch(token) else None
    lstart = _run_ps(probe, "lstart", pid)
    return None if lstart is None else f"ps:{lstart}"


def foreign_start_token(pid: int, probe: Probe) -> str | None:
    """The start token of a pid this process does not own, shape-checked."""
    token = process_start_token(pid, probe)
    if token is None or not token_shape_ok(token):
        return None
    return token


def process_state(pid: int, probe: Probe) -> str | None:
    """The process state letter (`Z` for a zombie), or None when unreadable."""
    if probe.proc_root.is_dir():
        fields = _stat_fields(pid, probe)
        return fields[STAT_STATE] if fields else None
    stat = _run_ps(probe, "stat", pid)
    return stat[0] if stat else None


def parent_pid(pid: int, probe: Probe) -> int | None:
    """The pid's parent pid, or None when unreadable."""
    if probe.proc_root.is_dir():
        fields = _stat_fields(pid, probe)
        raw = fields[STAT_PPID] if fields and len(fields) > STAT_PPID else None
    else:
        raw = _run_ps(probe, "ppid", pid)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_pidns(probe: Probe) -> str | None:
    """This process's pid-namespace link text, e.g. `pid:[4026531836]`."""
    try:
        return str((probe.proc_root / "self" / "ns" / "pid").readlink())
    except OSError:
        return None


def is_ancestor(
    candidate: int, start: int, probe: Probe, max_depth: int = MAX_ANCESTOR_DEPTH
) -> bool:
    """Whether *candidate* is on *start*'s parent chain."""
    current = start
    for _ in range(max_depth):
        parent = parent_pid(current, probe)
        if parent is None or parent < 1:
            return False
        if parent == candidate:
            return True
        if parent == 1:
            return False
        current = parent
    return False


def _signal_shows_dead(pid: int, probe: Probe) -> bool:
    """Signal 0: only `ProcessLookupError` proves the pid is gone.

    `PermissionError`, any other `OSError` and `OverflowError` (a pid outside
    the C `pid_t` range, as in a hand-edited record) cannot show it dead.
    """
    try:
        probe.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        return False
    return False


def proc_alive(proc: Proc, probe: Probe) -> bool:
    """Whether *proc* still runs; anything unprovable reads as alive."""
    if _signal_shows_dead(proc.pid, probe):
        return False
    if process_state(proc.pid, probe) == ZOMBIE_STATE:
        return False
    current = foreign_start_token(proc.pid, probe)
    if current is None:
        return True
    return current == proc.token


# --- Claimant ----------------------------------------------------------------------


def _verify_owner_pid(name: str, raw: str, probe: Probe) -> tuple[Proc | None, str]:
    """Check one owner variable; return the worker, or the fallback reason."""
    try:
        pid = int(raw)
    except ValueError:
        pid = 0
    if pid <= 0:
        preview = repr(raw[:VALUE_PREVIEW])
        return None, f"{name} is not a positive integer (got {preview})"
    token = process_start_token(pid, probe)
    if token is None:
        return None, f"{name}={pid} has no readable start token"
    if not is_ancestor(pid, probe.own(), probe):
        return None, f"{name}={pid} is not an ancestor of this process"
    return Proc(pid, token), ""


def resolve_claimant(environ: Mapping[str, str], probe: Probe) -> Claimant | None:
    """Identify the claimant, or None when the shell's token is unreadable.

    The first owner variable present decides; if it fails, the claimant is
    unidentified and the fence's shell stands in as the worker.
    """
    shell_pid = probe.shell()
    shell_token = process_start_token(shell_pid, probe)
    if shell_token is None:
        return None
    shell = Proc(shell_pid, shell_token)
    pidns = read_pidns(probe)
    present = [name for name in OWNER_ENV_VARS if name in environ]
    if not present:
        note = "neither CW_GATE_OWNER_PID nor CLAUDE_PID is set"
        return Claimant(shell, shell, SHELL_PID_SOURCE, note, pidns)
    name = present[0]
    worker, note = _verify_owner_pid(name, environ[name], probe)
    if worker is not None:
        return Claimant(worker, shell, name, "", pidns)
    for other in present[1:]:
        note = f"{note}; {other} is set but not consulted"
    return Claimant(shell, shell, SHELL_PID_SOURCE, note, pidns)


def claimant_source_line(claimant: Claimant) -> str:
    """The one stderr line naming how the claimant was identified."""
    if claimant.source == SHELL_PID_SOURCE:
        return (
            f"gate_key: WARN claimant identified by shell-pid {claimant.shell.pid}"
            f" ({claimant.note}); overlap is still refused while this shell lives"
        )
    return (
        f"gate_key: claimant identified by {claimant.source}"
        f" (worker pid {claimant.worker.pid})"
    )


# --- Messages ----------------------------------------------------------------------


def _operator_exit(owner: Path) -> str:
    return f"If no run owns the path, remove {owner} and re-run"


def refuse_live_worker(pid: int, claimed_at: str, source: str, owner: Path) -> str:
    """M1: the recorded worker is alive."""
    return (
        f"gate_key: gate path in use by a live run, pid {pid} (claimed {claimed_at}"
        f" via {source}; owner file {owner}); refusing destructive cleanup."
        f" {_operator_exit(owner)}"
    )


def refuse_live_shell_same_worker(shell_pid: int, pid: int, owner: Path) -> str:
    """M2: this same worker's earlier gate shell is still running."""
    return (
        f"gate_key: gate path in use by a live run, pid {shell_pid} (a still-running"
        f" gate shell of this same worker, pid {pid}; owner file {owner}); refusing"
        f" destructive cleanup. {_operator_exit(owner)}"
    )


def refuse_live_shell_dead_worker(shell_pid: int, pid: int, owner: Path) -> str:
    """M3: the recorded worker is gone but its gate shell is alive."""
    return (
        f"gate_key: gate path in use by a live run, pid {shell_pid} (its worker pid"
        f" {pid} is gone; owner file {owner}); refusing destructive cleanup."
        f" {_operator_exit(owner)}"
    )


def refuse_foreign_host(recorded: str, host: str, pid: int, owner: Path) -> str:
    """M4: the record was written on another host."""
    return (
        f"gate_key: gate path claimed on host {recorded}, not this host ({host}):"
        f" pid {pid}; liveness cannot be checked across hosts, refusing destructive"
        f" cleanup. If that run is gone, remove {owner} and re-run"
    )


def refuse_unsupported_version(owner: Path, version: int) -> str:
    """M5: a record version this script does not understand."""
    return (
        f"gate_key: owner file {owner} has unsupported record version {version};"
        " refusing destructive cleanup. If its run is gone, remove the file and"
        " re-run"
    )


def refuse_malformed(owner: Path, why: str) -> str:
    """M6: the record cannot be read or fails the v1 shape check."""
    return (
        f"gate_key: owner file {owner} is unreadable or malformed ({why}); refusing"
        " destructive cleanup. If no run owns the path, remove the file and re-run"
    )


def refuse_foreign_pidns(
    owner: Path, recorded: str | None, pid: int, own: str | None
) -> str:
    """M7: the record was written from another pid namespace."""
    recorded_text = UNREADABLE if recorded is None else recorded
    own_text = UNREADABLE if own is None else own
    return (
        f"gate_key: owner file {owner} was written from pid namespace"
        f" {recorded_text} (pid {pid}), this process runs in {own_text}; liveness"
        " cannot be checked across pid namespaces, refusing destructive cleanup."
        f" {_operator_exit(owner)}"
    )


def unreadable_shell_token(shell_pid: int) -> str:
    """M8: the fence's own shell has no readable start token."""
    return (
        "gate_key: cannot record an owner: the start token of this fence's shell"
        f" (pid {shell_pid}) could not be read from /proc or from ps; a claim is"
        " never written without an owner, so the gate path was left untouched."
        f" There is no owner file to remove: run ps -o lstart= -p {shell_pid} from"
        " this shell and, if it prints nothing, report that host's /proc and ps"
        " output"
    )


def note_reentry(shell_pid: int, owner: Path) -> str:
    """Rule 5a: the recorded shell is this claimant's own shell."""
    return (
        f"gate_key: re-entry from the same shell (pid {shell_pid}) that holds the"
        f" owner record; taking over (owner file {owner})"
    )


def note_same_worker(shell_pid: int, pid: int, owner: Path) -> str:
    """Rule 5: this same worker's earlier gate shell is gone."""
    return (
        f"gate_key: previous gate shell (pid {shell_pid}) of this same worker (pid"
        f" {pid}) is gone; taking over (owner file {owner})"
    )


def note_both_dead(pid: int, shell_pid: int, owner: Path) -> str:
    """Rule 8: the recorded worker and shell are both gone."""
    return (
        f"gate_key: previous owner is gone (worker pid {pid}, shell pid"
        f" {shell_pid}); taking over (owner file {owner})"
    )


# --- Owner record --------------------------------------------------------------


def owner_path(gate_root: Path, key: str) -> Path:
    """The owner record, a sibling of (never inside) the gate paths."""
    return gate_root / f"cw-gate-owner-{key}.json"


def lock_path(gate_root: Path, key: str) -> Path:
    """The claim lock; never unlinked, since unlinking races with waiters."""
    return gate_root / f"cw-gate-owner-{key}.lock"


def build_record(key: str, branch: str, claimant: Claimant) -> dict[str, object]:
    """The v1 owner record for *claimant*."""
    return {
        "v": RECORD_VERSION,
        "pid": claimant.worker.pid,
        "start": claimant.worker.token,
        "shell_pid": claimant.shell.pid,
        "shell_start": claimant.shell.token,
        "host": socket.gethostname(),
        "pidns": claimant.pidns,
        "key": key,
        "branch": branch,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        "source": claimant.source,
    }


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _display(value: object) -> str:
    return value if isinstance(value, str) else UNKNOWN


def _shape_error(data: dict[str, object]) -> str:
    """The M6 reason a v1 record fails the shape check, or "" when it passes."""
    for name in ("pid", "shell_pid"):
        if not _is_positive_int(data.get(name)):
            return f"missing or malformed field {name}"
    for name in ("shell_start", "start"):
        token = data.get(name)
        if not isinstance(token, str) or not token_shape_ok(token):
            return f"bad token in {name}"
    host = data.get("host")
    if not isinstance(host, str) or not host:
        return "missing or malformed field host"
    pidns = data.get("pidns", "")
    if pidns is not None and (not isinstance(pidns, str) or not pidns):
        return "missing or malformed field pidns"
    return ""


def parse_record(raw: str, owner: Path) -> tuple[OwnerRecord | None, str]:
    """Parse an owner file's text; return the record or the refusal message."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, refuse_malformed(owner, "invalid JSON")
    if not isinstance(data, dict):
        return None, refuse_malformed(owner, "not a JSON object")
    version = data.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        return None, refuse_malformed(owner, "missing or malformed field v")
    if version != RECORD_VERSION:
        return None, refuse_unsupported_version(owner, version)
    why = _shape_error(data)
    if why:
        return None, refuse_malformed(owner, why)
    record = OwnerRecord(
        pid=data["pid"],
        start=data["start"],
        shell_pid=data["shell_pid"],
        shell_start=data["shell_start"],
        host=data["host"],
        pidns=data["pidns"],
        claimed_at=_display(data.get("claimed_at")),
        source=_display(data.get("source")),
    )
    return record, ""


def judge(
    record: OwnerRecord, claimant: Claimant, owner: Path, probe: Probe
) -> tuple[int, str]:
    """Apply judgement rules 4 to 8 to a well-formed record, in order."""
    host = socket.gethostname()
    if record.host != host:
        return EXIT_REFUSED, refuse_foreign_host(record.host, host, record.pid, owner)
    if record.pidns != claimant.pidns:
        return EXIT_REFUSED, refuse_foreign_pidns(
            owner, record.pidns, record.pid, claimant.pidns
        )
    shell = claimant.shell
    if record.shell_pid == shell.pid and record.shell_start == shell.token:
        return EXIT_OK, note_reentry(record.shell_pid, owner)
    shell_alive = proc_alive(Proc(record.shell_pid, record.shell_start), probe)
    worker = claimant.worker
    if record.pid == worker.pid and record.start == worker.token:
        if shell_alive:
            return EXIT_REFUSED, refuse_live_shell_same_worker(
                record.shell_pid, record.pid, owner
            )
        return EXIT_OK, note_same_worker(record.shell_pid, record.pid, owner)
    if proc_alive(Proc(record.pid, record.start), probe):
        return EXIT_REFUSED, refuse_live_worker(
            record.pid, record.claimed_at, record.source, owner
        )
    if shell_alive:
        return EXIT_REFUSED, refuse_live_shell_dead_worker(
            record.shell_pid, record.pid, owner
        )
    return EXIT_OK, note_both_dead(record.pid, record.shell_pid, owner)


def _open_publish_temp(tmp: Path) -> object:
    """Create the owner temp file without following a shared-root symlink."""
    try:
        mode = tmp.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(mode):
            msg = f"temporary owner path is not a regular file: {tmp}"
            raise OSError(errno.EEXIST, msg)
        tmp.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(tmp, flags, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _publish(owner: Path, record: dict[str, object]) -> str:
    """Write *record* to a temp file in the gate root, fsync, then rename it.

    Returns "" on success or the error message; on failure the temp file is
    removed and the previous record is left as it was.
    """
    tmp = owner.with_name(f"{owner.name}.tmp")
    payload = json.dumps(record, sort_keys=True) + "\n"
    created = False
    try:
        with _open_publish_temp(tmp) as handle:
            created = True
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(owner)
    except OSError as exc:
        if created:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return f"gate_key: could not publish the owner record {owner}: {exc}"
    return ""


def _acquire(handle: int) -> bool:
    """Poll for the claim lock until `LOCK_TIMEOUT_S` elapses."""
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_S)
        else:
            return True


def _claim_locked(
    gate_root: Path, key: str, branch: str, claimant: Claimant, probe: Probe
) -> tuple[int, str]:
    owner = owner_path(gate_root, key)
    note = ""
    try:
        raw = owner.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = None
    except (OSError, UnicodeDecodeError):
        return EXIT_REFUSED, refuse_malformed(owner, "invalid JSON")
    if raw is not None:
        record, refusal = parse_record(raw, owner)
        if record is None:
            return EXIT_REFUSED, refusal
        code, note = judge(record, claimant, owner, probe)
        if code != EXIT_OK:
            return code, note
    error = _publish(owner, build_record(key, branch, claimant))
    if error:
        return EXIT_ERROR, error
    return EXIT_OK, note


def claim(
    gate_root: Path, key: str, branch: str, claimant: Claimant, probe: Probe
) -> tuple[int, str]:
    """Claim the gate path for *claimant*; return (exit code, stderr text).

    Writes to neither stream itself, so a caller (or a threaded test) can read
    each claim's own message.
    """
    lock = lock_path(gate_root, key)
    try:
        gate_root.mkdir(parents=True, exist_ok=True)
        handle = lock.open("a")
    except OSError as exc:
        return EXIT_ERROR, f"gate_key: cannot use gate root {gate_root}: {exc}"
    with handle:
        if not _acquire(handle.fileno()):
            return EXIT_ERROR, (
                f"gate_key: timed out after {LOCK_TIMEOUT_S}s waiting for the claim"
                f" lock {lock}"
            )
        return _claim_locked(gate_root, key, branch, claimant, probe)


def release(gate_root: Path, key: str, probe: Probe) -> int:
    """Remove the owner record if it names this process's shell; always 0."""
    owner = owner_path(gate_root, key)
    try:
        data = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return EXIT_OK
    if not isinstance(data, dict):
        return EXIT_OK
    shell_pid = probe.shell()
    token = process_start_token(shell_pid, probe)
    ours = (
        token is not None
        and data.get("shell_pid") == shell_pid
        and data.get("shell_start") == token
        and "pidns" in data
        and data["pidns"] == read_pidns(probe)
    )
    if ours:
        with contextlib.suppress(OSError):
            owner.unlink()
    return EXIT_OK


# --- CLI -----------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print the Step 2.5 gate key, or claim/release its gate path."
    )
    parser.add_argument("--branch", help="Branch the gate runs for")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--claim", action="store_true", help="Claim the gate path")
    mode.add_argument("--release", action="store_true", help="Release the claim")
    parser.add_argument("--key", help="Gate key printed earlier (claim/release)")
    parser.add_argument("--gate-root", help="Gate root directory (claim/release)")
    return parser


def _usage_error(args: argparse.Namespace) -> str:
    """The usage error for this flag combination, or ""."""
    if args.claim or args.release:
        mode = "--claim" if args.claim else "--release"
        missing = [
            flag
            for flag, value in (("--key", args.key), ("--gate-root", args.gate_root))
            if value is None
        ]
        if args.claim and args.branch is None:
            missing.insert(0, "--branch")
        return f"{mode} requires {', '.join(missing)}" if missing else ""
    if args.branch is None:
        return "--branch is required"
    if args.key is not None or args.gate_root is not None:
        return "--key and --gate-root are only valid with --claim or --release"
    return ""


def _main_key(branch: str) -> int:
    try:
        key = compute_key(git_common_dir(), branch)
    except GateKeyError as exc:
        print(f"gate_key: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(key)
    return EXIT_OK


def _main_claim(args: argparse.Namespace, probe: Probe) -> int:
    try:
        key = require_hex_key(args.key)
    except GateKeyError as exc:
        print(f"gate_key: {exc}", file=sys.stderr)
        return EXIT_ERROR
    claimant = resolve_claimant(os.environ, probe)
    if claimant is None:
        print(unreadable_shell_token(probe.shell()), file=sys.stderr)
        return EXIT_ERROR
    print(claimant_source_line(claimant), file=sys.stderr)
    code, text = claim(Path(args.gate_root), key, args.branch, claimant, probe)
    if text:
        print(text, file=sys.stderr)
    return code


def _main_release(args: argparse.Namespace, probe: Probe) -> int:
    try:
        key = require_hex_key(args.key)
    except GateKeyError as exc:
        print(f"gate_key: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return release(Path(args.gate_root), key, probe)


def main(argv: Sequence[str] | None = None, *, probe: Probe | None = None) -> int:
    """Run one mode; return the exit code (usage errors exit 2 via argparse)."""
    parser = _parser()
    args = parser.parse_args(argv)
    error = _usage_error(args)
    if error:
        parser.error(error)
    active = probe if probe is not None else Probe()
    if args.claim:
        return _main_claim(args, active)
    if args.release:
        return _main_release(args, active)
    return _main_key(args.branch)


if __name__ == "__main__":
    sys.exit(main())
