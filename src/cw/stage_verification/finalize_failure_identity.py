"""Failure identity for the FINALIZE base-branch-state classifier (#3542).

pytest, ruff and mypy exit 1 for almost any failure, so the classifier cannot
compare exit codes alone. This module turns a gate's full output into the set
of failures it reports, and says whether every failure of the branch run also
appears in the base run. Pure functions only: no subprocess, no ``cw.codex_*``
import.

* ``pytest*``: the multiset of ``FAILED`` / ``ERROR`` short-summary node ids;
  their count must equal the final summary's ``failed`` plus ``error`` counts.
  A coverage failure, ``stopping after`` or ``Interrupted`` has no identity.
* ``mypy*``: ``(path, message, code)`` without line numbers; the count must
  equal ``Found N errors``. ``errors prevented further checking`` has none.
* ``ruff*``: ``(code, path, message)`` without line numbers (full or concise
  output), or ``Would reformat:`` paths; each count must equal its summary.
* Any other gate: the whole normalised output, durations replaced by a fixed
  token, must be equal.

Worktree paths (*roots*), gate scratch dirs and ANSI codes are normalised away
first. Empty output, an unparseable output or a count mismatch is ``None``.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.stage_verification.impl_type_gate import parse_mypy_errors

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "FailureIdentity",
    "failure_identity",
    "identity_matches",
    "is_recognised_gate",
    "normalise",
]

_RECOGNISED: Final = ("pytest", "mypy", "ruff")
_ROOT_MARK: Final = "<root>"
_SCRATCH_MARK: Final = "<scratch>"
_DURATION_MARK: Final = "<duration>"

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SCRATCH: Final = re.compile(r"(?:/[^\s/:'\"]+)*/cw-gate-[\w-]+")
_DURATION: Final = re.compile(
    r"(?<![\w.])(?:\d+m ?)?\d+(?:\.\d+)?(?:ns|us|µs|ms|s)(?!\w)"
)

_PYTEST_ENTRY: Final = re.compile(r"^(?P<kind>FAILED|ERROR) (?P<node>\S+)(?: - .*)?$")
_PYTEST_FINAL: Final = re.compile(r"^=*\s*\d+ \w.* in \d+(?:\.\d+)?s\b")
_PYTEST_COUNT: Final = re.compile(r"(\d+) (failed|errors?)\b")
_PYTEST_NO_IDENTITY: Final = (
    "Interrupted:",
    "stopping after",
    "Coverage failure",
    "Required test coverage",
)

_MYPY_FOUND: Final = re.compile(r"^Found (\d+) errors? in ", re.MULTILINE)
_MYPY_PREVENTED: Final = "errors prevented further checking"

_RUFF_HEADER: Final = re.compile(r"^(?P<code>[A-Z]+[0-9]+) (?:\[\*\] )?(?P<msg>\S.*)$")
_RUFF_ARROW: Final = re.compile(r"^\s*--> (?P<path>.+?):\d+:\d+\s*$")
_RUFF_CONCISE: Final = re.compile(
    r"^(?P<path>[^\s:][^:]*):\d+:\d+: (?P<code>[A-Z]+[0-9]+) (?:\[\*\] )?(?P<msg>.+)$"
)
_RUFF_FOUND: Final = re.compile(r"^Found (\d+) errors?\.", re.MULTILINE)
_RUFF_REFORMAT: Final = re.compile(r"^Would reformat: (?P<path>.+?)\s*$")
_RUFF_REFORMAT_COUNT: Final = re.compile(
    r"^(\d+) files? would be reformatted", re.MULTILINE
)

Item = tuple[str, ...]


@dataclass(frozen=True)
class FailureIdentity:
    """The failures one gate run reported, as comparable items."""

    recognised: bool
    items: tuple[Item, ...]


def is_recognised_gate(name: str) -> bool:
    """True for a gate whose output is parsed (``pytest*``, ``mypy*``, ``ruff*``)."""
    return name.startswith(_RECOGNISED)


def normalise(text: str, roots: Sequence[str]) -> str:
    """*text* without ANSI codes, with *roots* and gate scratch dirs tokenised."""
    out = _ANSI.sub("", text)
    variants = {r for root in roots if root for r in (root, os.path.realpath(root))}
    for root in sorted(variants, key=len, reverse=True):
        out = out.replace(root, _ROOT_MARK)
    return _SCRATCH.sub(_SCRATCH_MARK, out)


def _pytest_items(text: str) -> tuple[Item, ...] | None:
    if any(marker in text for marker in _PYTEST_NO_IDENTITY):
        return None
    items: list[Item] = []
    final: str | None = None
    for line in text.splitlines():
        entry = _PYTEST_ENTRY.match(line)
        if entry is not None:
            items.append((entry.group("kind"), entry.group("node")))
        elif _PYTEST_FINAL.match(line):
            final = line
    if final is None:
        return None
    expected = sum(int(n) for n, _word in _PYTEST_COUNT.findall(final))
    return tuple(items) if expected == len(items) else None


def _mypy_items(text: str) -> tuple[Item, ...] | None:
    if _MYPY_PREVENTED in text:
        return None
    found = _MYPY_FOUND.findall(text)
    if len(found) != 1:
        return None
    errors = parse_mypy_errors(text)
    if int(found[0]) != len(errors):
        return None
    return tuple((e.path, e.message, e.code or "") for e in errors)


def _ruff_check_items(lines: list[str]) -> list[Item]:
    items: list[Item] = []
    for index, line in enumerate(lines):
        concise = _RUFF_CONCISE.match(line)
        if concise is not None:
            items.append(
                (concise.group("code"), concise.group("path"), concise.group("msg"))
            )
            continue
        header = _RUFF_HEADER.match(line)
        arrow = _RUFF_ARROW.match(lines[index + 1]) if index + 1 < len(lines) else None
        if header is not None and arrow is not None:
            items.append(
                (header.group("code"), arrow.group("path"), header.group("msg"))
            )
    return items


def _ruff_items(text: str) -> tuple[Item, ...] | None:
    lines = text.splitlines()
    checks = _ruff_check_items(lines)
    reformats = [
        ("reformat", m.group("path"))
        for m in (_RUFF_REFORMAT.match(line) for line in lines)
        if m is not None
    ]
    found = _RUFF_FOUND.findall(text)
    would = _RUFF_REFORMAT_COUNT.findall(text)
    if not found and not would:
        return None
    if found and sum(int(n) for n in found) != len(checks):
        return None
    if would and sum(int(n) for n in would) != len(reformats):
        return None
    return (*checks, *reformats)


def failure_identity(
    gate: str, text: str, *, roots: Sequence[str]
) -> FailureIdentity | None:
    """The failures *gate*'s full output *text* reports, or ``None`` (no identity)."""
    clean = normalise(text, roots)
    if not is_recognised_gate(gate):
        whole = _DURATION.sub(_DURATION_MARK, clean).strip()
        return FailureIdentity(False, ((whole,),)) if whole else None
    if gate.startswith("pytest"):
        items = _pytest_items(clean)
    elif gate.startswith("mypy"):
        items = _mypy_items(clean)
    else:
        items = _ruff_items(clean)
    return FailureIdentity(True, items) if items else None


def identity_matches(branch: FailureIdentity, base: FailureIdentity) -> bool:
    """True when every branch failure also appears in the base run.

    A recognised gate compares multisets (a second instance of a failure
    counts); any other gate requires equal output.
    """
    if branch.recognised != base.recognised:
        return False
    if not branch.recognised:
        return branch.items == base.items
    return not Counter(branch.items) - Counter(base.items)
