"""Guard the result-publishing door invariant (#1461, RFC 0012 D-B2).

RFC 0012 established a single validated door (``cw.result.emit_result_on`` /
``emit_result_locked``) through which every backend's terminal sentinel is
supposed to reach ``Session.last_result``. This is a regression test for that
invariant: a source-level deny-list scan, structurally identical to
``test_review_approval_guard.py``'s regex + ``Path.rglob("*.py")`` shape,
asserting no ``last_result`` assignment exists in ``src/cw/`` outside the door
module unless it is an explicitly allowlisted, individually-rationaled park-
marker/bookkeeping/erasure site. See ``docs/headless-contract.md`` §11 and
``ARCHITECTURE.md`` §5 for the invariant this test enforces.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from tests.conftest import _REPO_ROOT

# Narrower than conftest's `_SRC_ROOT` (`src/`) — this guard's scan target is
# `src/cw/` only (R4), so a distinctly-named local pair is defined here
# rather than importing/redefining conftest's wider `_iter_src_files`.
_CW_ROOT = _REPO_ROOT / "src" / "cw"

_ASSIGNMENT_RE = re.compile(r"\.last_result\s*=[^=]")

# Relative-to-`_CW_ROOT`, posix form — the sole module allowed to write
# `last_result` without an individual allowlist entry.
_DOOR_MODULE = "result.py"

# Allowlist keyed by (file relative to _CW_ROOT, exact bracket-balanced
# assignment-statement text) -> one-line rationale. Re-derived at build time
# from an actual grep against current source (see plan's site-scan table);
# any new write site trips TestNoLastResultAssignmentOutsideDoor until it is
# individually classified and added here.
_ALLOWLIST: dict[str, dict[str, str]] = {
    "reconcile/idle/_mutations.py": {
        (
            "session.last_result = {\n"
            "_PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON\n"
            "}"
        ): (
            "idle/_mutations.py (_apply_idle_routed_mutations) — park marker, "
            "stage-mismatch-refused; no 'status' key so has_terminal_result() "
            "stays False"
        ),
        'session.last_result = candidate.routed_sentinel.model_dump(mode="json")': (
            "idle/_mutations.py (_apply_idle_routed_mutations) — routed-"
            "sentinel advance; a real terminal sentinel routed via "
            "_apply_sentinel_to_task, carries 'status'"
        ),
    },
    "reconcile/phantom/_mutations.py": {
        (
            "session.last_result = {\n"
            "**existing,\n"
            "_SENTINEL_ADVANCE_REFUSED_KEY: True,\n"
            "}"
        ): (
            "phantom/_mutations.py (_apply_phantom_routed_mutations) — park "
            "marker, stage-mismatch-refused (merge branch: preserves the "
            "caller's existing paused_status under its own key); no 'status' "
            "key added"
        ),
        (
            "session.last_result = {\n"
            "_PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON\n"
            "}"
        ): (
            "phantom/_mutations.py (_apply_phantom_routed_mutations) — park "
            "marker, stage-mismatch-refused (fresh branch: no pre-existing "
            "dict to merge into); no 'status' key"
        ),
        'session.last_result = candidate.routed_sentinel.model_dump(mode="json")': (
            "phantom/_mutations.py (_apply_phantom_routed_mutations) — "
            "routed-sentinel advance; a real terminal sentinel routed via "
            "_apply_sentinel_to_task, carries 'status'"
        ),
    },
    "dev_queue/requeue.py": {
        "session.last_result = None": (
            "requeue.py:322 — deliberate erasure ahead of a requeue; resets "
            "to None rather than publishing a sentinel"
        ),
    },
}


# Why: _iter_cw_files/_line_no/_assignment_statement_text/_run_scan below
# duplicate the scan-skeleton shape already present in
# test_review_approval_guard.py and test_ticket_boundary_guard.py rather than
# extracting a shared tests/_guard_scan.py helper. Deliberate, not an
# oversight: this ticket's file set is pinned to exactly 3 files (no new
# tests/ helper module), and its instruction was to follow the existing
# precedent's shape, not refactor it. Revisit if a fourth guard test makes
# the duplication a real maintenance cost.
def _iter_cw_files() -> list[Path]:
    """Return every ``*.py`` file under ``src/cw/``, sorted for determinism."""
    return sorted(_CW_ROOT.rglob("*.py"))


def _line_no(text: str, pos: int) -> int:
    """Return the 1-based line number of *pos* within *text*."""
    return text.count("\n", 0, pos) + 1


def _assignment_statement_text(lines: list[str], line_no: int) -> str:
    """Return the bracket-balanced, stripped, multi-line statement text
    starting at *line_no* (1-based).

    Accumulates stripped lines while the running count of ``{[(`` minus
    ``}])`` seen so far (starting from the matched line) stays positive. A
    single-line balanced assignment degenerates to ``lines[line_no-1].strip()``.
    Multi-line capture is required, not optional: two same-file sites can
    produce identical single-line text (e.g. phantom.py:720 vs :725) while
    differing in their bracketed body.
    """
    collected: list[str] = []
    depth = 0
    idx = line_no - 1
    while idx < len(lines):
        line = lines[idx]
        collected.append(line.strip())
        depth += sum(line.count(c) for c in "{[(")
        depth -= sum(line.count(c) for c in "}])")
        idx += 1
        if depth <= 0:
            break
    return "\n".join(collected)


def _check_assignment_sites(text: str, path: Path, *, exempt_door: bool) -> list[str]:
    """Core allowlist check, shared by the scan and the door-exemption test.

    *exempt_door* controls whether the door module's own write short-
    circuits to no-violations; ``False`` is used only by
    ``TestDoorModuleIsExempt`` to prove the exemption is load-bearing.
    """
    rel = path.relative_to(_CW_ROOT).as_posix()
    if exempt_door and rel == _DOOR_MODULE:
        return []
    lines = text.splitlines()
    allowed = _ALLOWLIST.get(rel, {})
    violations: list[str] = []
    for match in _ASSIGNMENT_RE.finditer(text):
        line_no = _line_no(text, match.start())
        stmt = _assignment_statement_text(lines, line_no)
        if stmt not in allowed:
            violations.append(
                f"{path}:{line_no}: last_result assignment outside "
                f"{_DOOR_MODULE} not in allowlist (or allowlist text is "
                "stale — update the (file, text) key)"
            )
    return violations


def _find_bypass_sites(text: str, path: Path) -> list[str]:
    return _check_assignment_sites(text, path, exempt_door=True)


def _run_scan(finder: Callable[[str, Path], list[str]]) -> list[str]:
    violations: list[str] = []
    for path in _iter_cw_files():
        text = path.read_text(encoding="utf-8")
        violations.extend(finder(text, path))
    return violations


class TestNoLastResultAssignmentOutsideDoor:
    """Deny-list scan: no last_result write in src/cw/ outside the door
    module unless individually allowlisted."""

    def test_no_bypass_of_result_door(self) -> None:
        violations = _run_scan(_find_bypass_sites)
        assert not violations, "\n".join(violations)


class TestAllowlistStaysHonest:
    """Positive control: the allowlist can't silently go stale (borrowed
    from test_ticket_boundary_guard.py's TestAllowlistStaysHonest)."""

    def test_allowlist_entries_still_exist(self) -> None:
        for rel, entries in _ALLOWLIST.items():
            path = _CW_ROOT / rel
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            found_statements = {
                _assignment_statement_text(lines, _line_no(text, match.start()))
                for match in _ASSIGNMENT_RE.finditer(text)
            }
            for stmt in entries:
                assert stmt in found_statements, (
                    f"{rel}: allowlisted statement no longer found in "
                    f"source — entry is stale, update or remove it: {stmt!r}"
                )


class TestDoorModuleIsExempt:
    """Positive control mirroring test_review_approval_guard.py's
    test_legitimate_gh_pr_neighbors_stay_green: prove the door-module
    exemption is load-bearing, not a no-op."""

    def test_door_module_exemption_is_load_bearing(self) -> None:
        path = _CW_ROOT / "result.py"
        text = path.read_text(encoding="utf-8")
        assert _find_bypass_sites(text, path) == []
        assert _check_assignment_sites(text, path, exempt_door=False) != []


class TestReadOnlySurfacesNeverWrite:
    """R5: blessed read-only transcript surfaces never write last_result.

    `.claude/skills/cw-followup/scripts/parse_sentinel.py` is outside
    src/cw/ and so is structurally uncoverable here — its blessing is
    doc-prose only (docs/headless-contract.md §11.5).
    """

    def test_dev_queue_wait_never_writes_last_result(self) -> None:
        path = _CW_ROOT / "cli" / "dev_queue" / "wait.py"
        text = path.read_text(encoding="utf-8")
        assert not _ASSIGNMENT_RE.search(text), (
            f"{path} now writes last_result — this surface is blessed "
            "read-only; route writes through the door instead"
        )

    def test_queue_peek_never_writes_last_result(self) -> None:
        path = _CW_ROOT / "queue_peek.py"
        text = path.read_text(encoding="utf-8")
        assert not _ASSIGNMENT_RE.search(text), (
            f"{path} now writes last_result — this surface is blessed "
            "read-only; route writes through the door instead"
        )
