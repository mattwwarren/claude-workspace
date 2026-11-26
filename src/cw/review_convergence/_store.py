"""Persistence of the converge-to-clean ledger (#2787).

The ledger lives at ``<worktree>/.cw/review-convergence.json``, the same
durability class as ``.cw/review-findings-round.json`` (``.cw/`` is
git-excluded). Writes are atomic (:func:`cw.atomic.atomic_write_text`), so a
concurrent reader sees the prior complete file or the new complete one, never a
torn file.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from cw._flock import acquire_flock
from cw._lock_guard import LockRank, held_locks, lock_guard
from cw.atomic import atomic_write_text
from cw.review_convergence._models import (
    LEDGER_STATUS_STOPPED,
    STOP_CONFIRM_MUST_FIX,
    ConvergenceError,
    ConvergenceLedger,
)
from cw.review_findings import ReviewVerdict

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The ledger's path relative to a worktree root.
LEDGER_RELATIVE_PATH = Path(".cw/review-convergence.json")
#: The per-round verdict directory sits beside the ledger, named after it.
ROUND_VERDICT_DIR_RELATIVE_PATH = Path(".cw/review-convergence")
_LOCK_NAME = "review_convergence_ledger"


def _canonical_ledger(path: Path) -> Path:
    canonical = path.parent.resolve(strict=True) / path.name
    if canonical.is_symlink() or canonical.with_suffix("").is_symlink():
        msg = f"Convergence ledger or verdict directory is a symlink: {path}"
        raise ConvergenceError(msg)
    return canonical


def _lock_path(path: Path) -> Path:
    # Custom ledger names with the same stem share a round-verdict directory.
    return path.with_name(f".{path.with_suffix('').name}.lock")


def is_managed_convergence_path(path: Path) -> bool:
    """Identify the default ledger, lock, or round verdict in any worktree."""
    # Case-insensitive filesystems can resolve an alias without correcting its
    # spelling. Reserve case variants even when running on a case-sensitive FS.
    suffix = tuple(part.casefold() for part in path.parts[-3:])
    return suffix[-2:] in (
        LEDGER_RELATIVE_PATH.parts,
        _lock_path(LEDGER_RELATIVE_PATH).parts,
    ) or (
        suffix[:-1] == ROUND_VERDICT_DIR_RELATIVE_PATH.parts
        and re.fullmatch(r"round-[0-9]+-verdict\.json", suffix[-1]) is not None
    )


def _open_nofollow(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)


@contextmanager
def ledger_transaction(path: Path, *, timeout_s: float | None = None) -> Iterator[Path]:
    """Hold the bounded lock for one ledger and its round verdicts.

    The parent must already exist. The lock lives beside the replaceable
    ledger. Callers holding the queue lock may enter; never enter the queue
    from a ledger transaction. The audit inbox is a leaf below both.
    """
    canonical = _canonical_ledger(path)
    lock = _lock_path(canonical)
    with (
        lock_guard(_LOCK_NAME, lock, LockRank.REVIEW_LEDGER),
        open(lock, "a", opener=_open_nofollow) as fd,
    ):
        acquire_flock(fd, lock, lock_name=_LOCK_NAME, bounded=True, timeout_s=timeout_s)
        opened, named = os.fstat(fd.fileno()), lock.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(opened, named)
            or _canonical_ledger(path) != canonical
        ):
            msg = f"Convergence ledger lock identity changed: {path}"
            raise ConvergenceError(msg)
        yield canonical


@contextmanager
def _ledger_write(path: Path) -> Iterator[Path]:
    """Single-artifact writers join their caller's transaction when held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = _canonical_ledger(path)
    if any(lock.path == str(_lock_path(canonical)) for lock in held_locks()):
        yield canonical
    else:
        with ledger_transaction(canonical) as locked:
            yield locked


def round_verdict_path(ledger_path: Path, round_number: int) -> Path:
    """Where round *round_number*'s verdict is persisted, beside *ledger_path*.

    ``<ledger dir>/review-convergence/round-<n>-verdict.json`` for the default
    ledger path, so a custom ``--ledger`` keeps its verdicts next to it.
    """
    return ledger_path.with_suffix("") / f"round-{round_number}-verdict.json"


def load_ledger(path: Path) -> ConvergenceLedger | None:
    """Load the ledger at *path*, or ``None`` when no file exists.

    A corrupt file raises a :class:`ValueError` subclass:
    :class:`pydantic.ValidationError` for malformed JSON or a schema mismatch
    (it wraps the JSON decode error), :class:`UnicodeDecodeError` for non-UTF-8
    bytes. The CLI maps these to a :class:`~cw.exceptions.CwError` so the
    operator sees a one-line message, never a traceback. A genuinely unreadable
    file (permissions) raises :class:`OSError`.
    """
    if not path.exists():
        return None
    return ConvergenceLedger.model_validate_json(path.read_text(encoding="utf-8"))


def save_ledger(path: Path, ledger: ConvergenceLedger) -> None:
    """Atomically write *ledger* to *path*, creating its directory."""
    with _ledger_write(path) as locked:
        atomic_write_text(locked, ledger.model_dump_json(indent=2) + "\n")


def save_round_verdict(
    ledger_path: Path, round_number: int, verdict: ReviewVerdict
) -> Path:
    """Atomically persist *verdict* as round *round_number*'s verdict file.

    Returns the path written. Each fix cycle's ``verify-fixes`` reads this file
    (the original verdict dies with the session that produced it).
    """
    with _ledger_write(ledger_path) as locked:
        path = round_verdict_path(locked, round_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, _verdict_text(verdict))
    return round_verdict_path(ledger_path, round_number)


def _verdict_text(verdict: ReviewVerdict) -> str:
    return verdict.model_dump_json(indent=2) + "\n"


def _read_regular_artifact(path: Path) -> bytes | None:
    # An evidence path can have been replaced by a FIFO. Open without waiting
    # for a writer so even a replaced path cannot stall the queue lock.
    with open(path, "rb", opener=_open_nofollow) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read()


def load_scope_grant_ledger(path: Path) -> ConvergenceLedger | None:
    """Read the same regular, non-redirected evidence that approval can lock."""
    canonical = _canonical_ledger(path)
    try:
        if not stat.S_ISREG(_lock_path(canonical).lstat().st_mode):
            return None
    except FileNotFoundError:
        pass
    raw = _read_regular_artifact(canonical)
    return None if raw is None else ConvergenceLedger.model_validate_json(raw)


def bind_confirm_verdict(
    ledger: ConvergenceLedger, round_number: int, verdict: ReviewVerdict
) -> ConvergenceLedger:
    """Bind a new blocking confirm to its producer's verdict, not a disk read.

    The writer saves the ordinary round artifact and then publishes the
    returned ledger as its commit point under the same transaction.
    """
    # The engine routes accepted findings and records the identities that
    # parked it. A coverage or acceptance SHOULD_FIX can park even when the
    # verdict's MUST_FIX gate is clear.
    engine_stop = (
        ledger.status == LEDGER_STATUS_STOPPED
        and ledger.stop_reason == STOP_CONFIRM_MUST_FIX
        and bool(ledger.stop_identities)
    )
    if not ledger.rounds:
        return ledger
    last = ledger.rounds[-1]
    if (
        last.round != round_number
        or last.kind != "confirm"
        or last.clean
        or last.reviewed_sha != verdict.reviewed_sha
        or not (engine_stop or last.repair_identities)
    ):
        return ledger
    digest = sha256(_verdict_text(verdict).encode()).hexdigest()
    bound = last.model_copy(update={"verdict_sha256": digest})
    return ledger.model_copy(update={"rounds": [*ledger.rounds[:-1], bound]})


def load_bound_round_verdict(
    ledger_path: Path, round_number: int, expected_sha256: str | None
) -> ReviewVerdict | None:
    """Parse exactly the bytes verified against a committed round, or refuse."""
    if expected_sha256 is None:
        return None
    path = round_verdict_path(ledger_path, round_number)
    raw = _read_regular_artifact(path)
    if raw is None or sha256(raw).hexdigest() != expected_sha256:
        return None
    return ReviewVerdict.model_validate_json(raw)


def load_round_verdict(ledger_path: Path, round_number: int) -> ReviewVerdict:
    """Load round *round_number*'s persisted verdict, beside *ledger_path* (#2807).

    The reader for :func:`save_round_verdict`. Raises like :func:`load_ledger`:
    :class:`FileNotFoundError` when the round has no verdict file, another
    :class:`OSError` for an unreadable one, and a :class:`ValueError` subclass
    (:class:`pydantic.ValidationError`, :class:`UnicodeDecodeError`) for a
    corrupt one.
    """
    path = round_verdict_path(ledger_path, round_number)
    return ReviewVerdict.model_validate_json(path.read_text(encoding="utf-8"))
