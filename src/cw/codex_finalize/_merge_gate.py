"""Step 4a, the merge gate, as cw-run Python (#3062, RFC 0018 C3).

A port of the finalize doc's single-call fence. It wraps the same tools the
fence calls (``check_merge_gate_overlap.py filter``, ``cw dev-queue
adopted-prs``, ``gh pr list``/``gh pr diff`` and ``git merge-tree``) and
reimplements none of their logic. Every other open pipeline PR is evaluated;
one PR's conflict or tooling failure never skips the rest, and the gate
decides once, after the loop. It runs on the create path only (the reuse
path skips 4a, per the doc).

Outcomes: ``None`` (clear, or disabled), a :class:`FinalizeBlock`
(``agent_block`` for a stale filter or a failed inventory, which fail
closed), or the conflict details that :func:`run_merge_gate`'s caller turns
into a ``merge_gate_blocked`` result.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from cw._git import git_clean_env, git_output, run_git
from cw.codex_finalize._gates import run_cw_cli
from cw.codex_finalize._result import AGENT_BLOCK, FinalizeBlock
from cw.codex_fix_loop.guard_script import find_guard_script, script_marker_ok
from cw.codex_fix_loop.posted_text import redact_and_cap
from cw.gh import OpenPrHead, fetch_open_pr_heads, fetch_pr_changed_files
from cw.merge_probe import merge_tree

if TYPE_CHECKING:
    from collections.abc import Sequence

OVERLAP_SCRIPT_NAME: Final = "check_merge_gate_overlap.py"
MIN_OVERLAP_SCRIPT_VERSION: Final = 1
DISABLED_FRICTION: Final = "MERGE_GATE_DISABLED: merge gate disabled by client config"
ABSENT_FRICTION: Final = "check_merge_gate_overlap: script absent, skipped"
_ADOPTED_MISSING_EXITS: Final = frozenset({127, 2})
_FILTER_CLEAR: Final = 0
_FILTER_OVERLAP: Final = 1
_MERGE_CLEAN: Final = 0
_MERGE_CONFLICT: Final = 1
_GIT_TIMEOUT: Final = 120
_TAIL: Final = 300


def _tail(text: str) -> str:
    return redact_and_cap(text.strip()[:_TAIL])


@dataclass(frozen=True)
class MergeGateResult:
    """The gate's decision: a fail-closed block, the conflicts, and friction."""

    block: FinalizeBlock | None = None
    conflicts: list[str] = field(default_factory=list)
    friction: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Gate:
    worktree: Path
    branch: str
    branch_prefix: str
    script: Path | None
    ignore_paths: tuple[str, ...]
    branch_files: tuple[str, ...]


def _inventory_block(what: str, detail: str) -> FinalizeBlock:
    return FinalizeBlock(
        AGENT_BLOCK,
        f"Step 4a {what}, tooling error, failing closed: {_tail(detail)}",
    )


def _adopted_heads(
    worktree: Path, client_name: str
) -> list[OpenPrHead] | FinalizeBlock:
    """Open PRs carried by live adopted rows (the ``adopted`` tag of the fence)."""
    done = run_cw_cli(worktree, ["dev-queue", "adopted-prs", "-c", client_name])
    if done is None or done.returncode != 0:
        code = "did not finish" if done is None else f"exited {done.returncode}"
        if done is not None and done.returncode in _ADOPTED_MISSING_EXITS:
            what = (
                f"adopted-PR inventory unavailable (cw dev-queue adopted-prs {code}: "
                "cw is missing or older than this command; upgrade cw)"
            )
        else:
            what = f"adopted-PR inventory failed (cw dev-queue adopted-prs {code})"
        return _inventory_block(what, done.stderr if done is not None else "")
    rows = [line.split(maxsplit=1) for line in done.stdout.splitlines() if line]
    if not rows:
        return []
    numbers = {row[0] for row in rows}
    refs = {row[1] for row in rows if len(row) > 1}
    every = fetch_open_pr_heads(None, cwd=worktree)
    if every is None:
        return _inventory_block(
            "open-PR inventory for adopted rows failed (gh pr list)", ""
        )
    return [pr for pr in every if str(pr.number) in numbers or pr.head_ref_name in refs]


def _candidates(
    gate: _Gate, mine: Sequence[OpenPrHead], adopted: Sequence[OpenPrHead]
) -> list[OpenPrHead]:
    """The fence's loop filter: prefix or adopted, never this branch, deduped."""
    adopted_numbers = {pr.number for pr in adopted}
    seen: set[int] = set()
    picked: list[OpenPrHead] = []
    for pr in [*mine, *adopted]:
        prefixed = pr.head_ref_name.startswith(f"{gate.branch_prefix}/")
        if not (prefixed or pr.number in adopted_numbers):
            continue
        if pr.head_ref_name == gate.branch or pr.number in seen:
            continue
        seen.add(pr.number)
        picked.append(pr)
    return picked


def _merge_tree(worktree: Path, number: int) -> str:
    """``"0"``/``"1"`` from ``git merge-tree`` against the PR head, else why not."""
    fetched = run_git(
        ["fetch", "--quiet", "origin", f"pull/{number}/head"],
        cwd=worktree,
        capture_output=True,
        timeout=_GIT_TIMEOUT,
    )
    if fetched.returncode != 0:
        return "fetch failed"
    answer = merge_tree(worktree, "HEAD", "FETCH_HEAD", timeout=_GIT_TIMEOUT)
    if answer.status == "clean":
        return str(_MERGE_CLEAN)
    if answer.status == "conflict":
        return str(_MERGE_CONFLICT)
    return f"unavailable: {answer.detail}"


def _filter(
    gate: _Gate, script: Path, pr_files: list[str], scratch: Path, pr: OpenPrHead
) -> tuple[int, list[str]] | str:
    """Run the overlap filter: ``(exit, overlap)`` or a tooling-failure clause."""
    branch_list = scratch / "branch-files"
    pr_list = scratch / f"pr-{pr.number}-files"
    branch_list.write_text("\n".join(gate.branch_files) + "\n", encoding="utf-8")
    pr_list.write_text("\n".join(pr_files) + "\n", encoding="utf-8")
    argv = [
        sys.executable,
        str(script),
        "filter",
        "--branch-files",
        str(branch_list),
        "--pr-files",
        str(pr_list),
    ]
    for path in gate.ignore_paths:
        argv += ["--ignore-path", path]
    argv.append("--json")
    try:
        done = subprocess.run(
            argv,
            cwd=gate.worktree,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"check_merge_gate_overlap.py did not finish: {_tail(str(exc))}"
    if done.returncode not in (_FILTER_CLEAR, _FILTER_OVERLAP):
        return (
            f"check_merge_gate_overlap.py exited {done.returncode}, tooling error, "
            f"failing closed: {_tail(done.stderr)}"
        )
    return _validated(done.returncode, done.stdout)


def _validated(code: int, stdout: str) -> tuple[int, list[str]] | str:
    """The fence's ``jq -e`` validation of the filter JSON."""
    bad = "malformed check_merge_gate_overlap.py output, tooling error, failing closed"
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return bad
    if not isinstance(data, dict):
        return bad
    blocking = data.get("blocking")
    overlap = data.get("overlap_after_ignore")
    if (
        not isinstance(blocking, bool)
        or blocking != (code == _FILTER_OVERLAP)
        or not isinstance(overlap, list)
        or not all(isinstance(item, str) for item in overlap)
        or bool(overlap) != blocking
    ):
        return bad
    return code, [str(item) for item in overlap]


def _probed(gate: _Gate, pr: OpenPrHead, label: str, joined: str) -> str | None:
    """The merge-tree escalation for overlap that survived the ignore list."""
    probe = _merge_tree(gate.worktree, pr.number)
    if probe == str(_MERGE_CLEAN):
        return None
    if probe == str(_MERGE_CONFLICT):
        return (
            f"{label} is open and conflicts textually with this branch "
            f"(git merge-tree) in: {joined}"
        )
    return (
        f"{label} is open and shares files with this branch: {joined} — "
        f"merge-tree probe failed ({probe}), tooling error, failing closed"
    )


def _evaluate(gate: _Gate, pr: OpenPrHead, scratch: Path) -> str | None:
    """One PR's conflict clause, or ``None`` when it does not block."""
    label = f"PR #{pr.number} ({pr.head_ref_name})"
    pr_files = fetch_pr_changed_files(pr.number, cwd=gate.worktree)
    if pr_files is None:
        return f"{label} could not be checked — gh pr diff failed, failing closed"
    if gate.script is None:
        overlap = sorted(set(gate.branch_files) & set(pr_files))
        if not overlap:
            return None
        return f"{label} is open and shares files with this branch: " + ", ".join(
            overlap
        )
    filtered = _filter(gate, gate.script, pr_files, scratch, pr)
    if isinstance(filtered, str):
        return f"{label} could not be checked — {filtered}"
    code, overlap = filtered
    if code == _FILTER_CLEAR:
        return None
    return _probed(gate, pr, label, ", ".join(overlap))


def _prepare(
    worktree: Path, fork_point: str
) -> tuple[Path | None, tuple[str, ...]] | FinalizeBlock:
    """The marker-checked filter script and this branch's changed files."""
    script = find_guard_script(worktree, OVERLAP_SCRIPT_NAME)
    if script is not None and not script_marker_ok(script, MIN_OVERLAP_SCRIPT_VERSION):
        return FinalizeBlock(
            AGENT_BLOCK,
            f"Step 4a: HEADLESS BLOCK — {OVERLAP_SCRIPT_NAME} at {script} — "
            f"missing/stale cw-script-version marker (need >= "
            f"{MIN_OVERLAP_SCRIPT_VERSION})",
        )
    try:
        names = git_output(
            ["diff", "--name-only", f"{fork_point}...HEAD"], cwd=worktree
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return _inventory_block("branch file inventory failed (git diff)", str(exc))
    return script, tuple(line for line in names.splitlines() if line)


def run_merge_gate(
    worktree: Path,
    *,
    enabled: bool,
    ignore_paths: Sequence[str],
    branch: str,
    branch_prefix: str,
    fork_point: str,
    client_name: str,
) -> MergeGateResult:
    """Run Step 4a; see the module docstring for the outcomes."""
    if not enabled:
        return MergeGateResult(friction=[DISABLED_FRICTION])
    prepared = _prepare(worktree, fork_point)
    if isinstance(prepared, FinalizeBlock):
        return MergeGateResult(block=prepared)
    script, branch_files = prepared
    friction = [ABSENT_FRICTION] if script is None else []
    mine = fetch_open_pr_heads("@me", cwd=worktree)
    if mine is None:
        return MergeGateResult(
            block=_inventory_block("open-PR inventory failed (gh pr list)", "")
        )
    adopted = _adopted_heads(worktree, client_name)
    if isinstance(adopted, FinalizeBlock):
        return MergeGateResult(block=adopted)
    gate = _Gate(
        worktree, branch, branch_prefix, script, tuple(ignore_paths), branch_files
    )
    with tempfile.TemporaryDirectory(prefix="cw-mergegate-") as tmp:
        conflicts = [
            clause
            for pr in _candidates(gate, mine, adopted)
            if (clause := _evaluate(gate, pr, Path(tmp))) is not None
        ]
    return MergeGateResult(conflicts=conflicts, friction=friction)
