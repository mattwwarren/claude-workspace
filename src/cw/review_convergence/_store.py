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

from pathlib import Path

from cw.atomic import atomic_write_text
from cw.review_convergence._models import ConvergenceLedger
from cw.review_findings import ReviewVerdict

#: The ledger's path relative to a worktree root.
LEDGER_RELATIVE_PATH = Path(".cw/review-convergence.json")
#: The per-round verdict directory sits beside the ledger, named after it.
ROUND_VERDICT_DIR_RELATIVE_PATH = Path(".cw/review-convergence")


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
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, ledger.model_dump_json(indent=2) + "\n")


def save_round_verdict(
    ledger_path: Path, round_number: int, verdict: ReviewVerdict
) -> Path:
    """Atomically persist *verdict* as round *round_number*'s verdict file.

    Returns the path written. Each fix cycle's ``verify-fixes`` reads this file
    (the original verdict dies with the session that produced it).
    """
    path = round_verdict_path(ledger_path, round_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, verdict.model_dump_json(indent=2) + "\n")
    return path


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
