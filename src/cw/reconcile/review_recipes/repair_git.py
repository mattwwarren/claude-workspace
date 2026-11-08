"""Git and script steps shared by the conflict-repair recipes (#2771, #2917).

The ``repair_changelog_conflict`` recipe's rebase steps live here beside the
helpers every repair recipe reuses (the bounded :func:`_git`, the scratch
tree, the lease push, the arm script). The ``repair_mechanical_conflict``
recipe's merge steps live in ``mechanical_repair_git`` and call git only
through :func:`_git`.

Post-lock only: every function here runs a subprocess, so none may run under
``sessions_lock`` or ``dev_queue_lock``.

Invocation contract (R3):

- Every git call goes through :func:`cw._git.run_git` with ``check=False``,
  ``capture_output=True`` and a per-call ``timeout``, via :func:`_git`, which
  turns ``OSError`` / ``subprocess.TimeoutExpired`` into :class:`RepairStopError`.
  A non-zero exit is a returncode the caller maps to its own disposition.
- Every mutating call in the scratch tree (``rebase``, ``rebase --continue`` /
  ``--abort``, ``merge``, ``commit``) carries :func:`_mutating_flags`: a fixed
  identity (``cw-changelog-repair`` unless the caller names another), no
  hooks, no editor and no signing, so a daemon without a git identity, gpg
  agent or editor cannot silently kill the recipe.
- A recipe works only in its own detached scratch tree
  (``<worktree_base>/<prefix><ticket>``, the prefix defined once in the
  recipe's ``RepairSpec``, e.g. ``.changelog-repair-<ticket>``),
  never in the ticket's worktree. :func:`_remove_scratch` is the one place
  that prunes or removes a worktree (pinned by
  ``tests/test_reconcile_worktree_removal_guard.py``); its
  ``git worktree prune`` is repository-wide but only drops the admin records
  of worktrees whose directory is already gone, never a directory. No git argv
  here uses ``reset``, ``clean``, ``restore``, ``checkout`` or ``stash``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from cw._git import LOCAL_GIT_TIMEOUT_SECONDS, git_clean_env, run_git
from cw.exceptions import CwError
from cw.repair_disposition import RepairDisposition

if TYPE_CHECKING:
    from collections.abc import Iterator

_logger = logging.getLogger("cw.reconcile.review_recipes")

# Per-call timeouts in seconds (R3). Local reads/writes, network, the rebase,
# the CHANGELOG freeze gate, the arm script (4 attempts with 3 s linear backoff
# plus its gh calls), a gh read, and the one gh disarm call.
_T_LOCAL = LOCAL_GIT_TIMEOUT_SECONDS
_T_NET = 120
_T_REBASE = 300
_T_GATE = 60
_T_ARM = 180
_T_GH = 15
_T_DISARM = 30

CHANGELOG = "CHANGELOG.md"
_CHANGELOG_IDENTITY = "cw-changelog-repair"


def _mutating_flags(identity: str = _CHANGELOG_IDENTITY) -> tuple[str, ...]:
    """The ``-c`` flags of a mutating scratch-tree call, committing as *identity*."""
    return (
        "-c",
        f"user.name={identity}",
        "-c",
        f"user.email={identity}@users.noreply.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.editor=true",
        "-c",
        "commit.gpgsign=false",
    )


_ARM_SCRIPT_RELATIVE = Path(".claude/scripts/prep_pr_finalize.py")
_GATE_SCRIPT_RELATIVE = Path(".claude/scripts/check_changelog_frozen.py")
_TAIL_CHARS = 300
_MERGE_TREE_CONFLICTED = 1
_EXCLUDE_CHANGELOG = f":(exclude){CHANGELOG}"

_INDEX_LINE_PREFIX = "index "
_HUNK_HEADER_PREFIX = "@@"


class RepairStopError(CwError):
    """One repair step could not go on; carries the disposition to park with."""

    __slots__ = ("detail", "disposition")

    def __init__(self, disposition: RepairDisposition, detail: str) -> None:
        super().__init__(f"{disposition}: {detail}")
        self.disposition = disposition
        self.detail = detail


class ArmOutcome(NamedTuple):
    """The arm script's exit code (None: could not run / timed out) and output."""

    returncode: int | None
    status: str
    stderr_tail: str


def _tail(text: str) -> str:
    """The last characters of *text*, whitespace-collapsed (``converge_tip._tail``)."""
    return " ".join(text.split())[-_TAIL_CHARS:]


def _git(
    cwd: Path,
    *argv: str,
    timeout: float,
    stop: RepairDisposition,
    timeout_stop: RepairDisposition | None = None,
    mutating: bool = False,
    identity: str = _CHANGELOG_IDENTITY,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded git call in *cwd*; never raises ``WorktreeError``.

    ``OSError`` raises :class:`RepairStopError` with *stop*; a timeout with
    *timeout_stop* (default *stop*). *mutating* prefixes
    :func:`_mutating_flags` for *identity*.
    """
    full = (*_mutating_flags(identity), *argv) if mutating else argv
    try:
        return run_git(full, cwd=cwd, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        detail = f"git {' '.join(argv)} timed out after {timeout:g}s"
        raise RepairStopError(timeout_stop or stop, detail) from exc
    except OSError as exc:
        detail = f"git {' '.join(argv)} could not run: {exc}"
        raise RepairStopError(stop, detail) from exc


def _failed(result: subprocess.CompletedProcess[str], what: str) -> str:
    return f"{what} exit {result.returncode}: {_tail(result.stderr or result.stdout)}"


def _diff_fingerprint(text: str) -> str:
    """A line-number-independent sha256 of a ``git diff -U0`` text.

    ``git patch-id`` semantics without stdin (``run_git`` offers none): drop
    ``index`` lines, rewrite every ``@@ -a,b +c,d @@ ...`` hunk header to
    ``@@``, keep the file headers and the ``+``/``-`` lines.
    """
    kept = [
        _HUNK_HEADER_PREFIX if line.startswith(_HUNK_HEADER_PREFIX) else line
        for line in text.splitlines()
        if not line.startswith(_INDEX_LINE_PREFIX)
    ]
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


def _fetch_and_resolve(repo: Path, default_branch: str, branch: str) -> str:
    """Fetch main and the PR branch; return the branch's pre-image sha."""
    fetched = _git(
        repo,
        "fetch",
        "origin",
        default_branch,
        branch,
        timeout=_T_NET,
        stop=RepairDisposition.FETCH_FAILED,
    )
    if fetched.returncode != 0:
        raise RepairStopError(
            RepairDisposition.FETCH_FAILED, _failed(fetched, "git fetch")
        )
    resolved = _git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/origin/{branch}^{{commit}}",
        timeout=_T_LOCAL,
        stop=RepairDisposition.FETCH_FAILED,
    )
    sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not sha:
        raise RepairStopError(
            RepairDisposition.FETCH_FAILED,
            f"origin/{branch} did not resolve after the fetch",
        )
    return sha


def _changelog_changed(repo: Path, since: str, until: str) -> bool:
    changed = _git(
        repo,
        "diff",
        "--name-only",
        since,
        until,
        "--",
        CHANGELOG,
        timeout=_T_LOCAL,
        stop=RepairDisposition.CONFLICT_UNEXPLAINED,
    )
    if changed.returncode != 0:
        raise RepairStopError(
            RepairDisposition.CONFLICT_UNEXPLAINED,
            _failed(changed, "git diff --name-only"),
        )
    return CHANGELOG in changed.stdout.split()


def _conflict_set(
    repo: Path,
    default_ref: str,
    expected_sha: str,
    *,
    hidden_changelog_conflict: bool = True,
) -> tuple[frozenset[str], str]:
    """The files a merge of *expected_sha* into main conflicts on, and the base.

    ``CHANGELOG.md merge=union`` hides CHANGELOG conflicts from git, so with
    *hidden_changelog_conflict* (the CHANGELOG recipe) the set is
    ``merge-tree``'s conflicted files plus CHANGELOG.md whenever both sides
    changed it since the merge-base (R1). Without it (the mechanical recipe,
    #2917) the set is ``merge-tree``'s conflicted files alone. Read-only apart
    from the objects ``merge-tree --write-tree`` writes.
    """
    unexplained = RepairDisposition.CONFLICT_UNEXPLAINED
    base = _git(
        repo,
        "merge-base",
        default_ref,
        expected_sha,
        timeout=_T_LOCAL,
        stop=unexplained,
    )
    merge_base = base.stdout.strip()
    if base.returncode != 0 or not merge_base:
        raise RepairStopError(unexplained, _failed(base, "git merge-base"))
    merged = _git(
        repo,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        "-z",
        default_ref,
        expected_sha,
        timeout=_T_LOCAL,
        stop=unexplained,
    )
    if merged.returncode not in {0, _MERGE_TREE_CONFLICTED}:
        raise RepairStopError(unexplained, _failed(merged, "git merge-tree"))
    # Why: ``-z`` output is ``<tree-oid>\0<path>\0...``; a bare whitespace split
    # would fragment a conflicted path containing a space (or one git quotes).
    files = set(_split_z(merged.stdout)[1:]) if merged.returncode else set()
    if (
        hidden_changelog_conflict
        and _changelog_changed(repo, merge_base, expected_sha)
        and _changelog_changed(repo, merge_base, default_ref)
    ):
        files.add(CHANGELOG)
    return frozenset(files), merge_base


def _show_blob(repo: Path, rev: str) -> str:
    """``CHANGELOG.md`` at *rev*, byte-exact: no universal-newline translation.

    ``run_git(text=True)`` would turn CRLF and bare CR into LF before
    :func:`~cw.changelog_rebuild.rebuild_changelog` could see them, so its
    mixed-line-ending guard would be dead and the rewrite would silently
    normalise the file.
    """
    unparseable = RepairDisposition.CHANGELOG_UNPARSEABLE
    what = f"git show {rev}:{CHANGELOG}"
    try:
        # ``run_git`` is annotated for the text default; ``text=False`` yields bytes.
        shown = cast(
            "subprocess.CompletedProcess[bytes]",
            run_git(
                ["show", f"{rev}:{CHANGELOG}"],
                cwd=repo,
                check=False,
                capture_output=True,
                text=False,
                timeout=_T_LOCAL,
            ),
        )
    except subprocess.TimeoutExpired as exc:
        raise RepairStopError(
            unparseable, f"{what} timed out after {_T_LOCAL:g}s"
        ) from exc
    except OSError as exc:
        raise RepairStopError(unparseable, f"{what} could not run: {exc}") from exc
    if shown.returncode != 0:
        stderr = shown.stderr.decode("utf-8", errors="replace")
        raise RepairStopError(
            unparseable, f"{what} exit {shown.returncode}: {_tail(stderr)}"
        )
    try:
        return shown.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepairStopError(unparseable, f"{what} is not UTF-8: {exc}") from exc


def _read_three_blobs(
    repo: Path, merge_base: str, expected_sha: str, default_ref: str
) -> tuple[str, str, str]:
    """CHANGELOG.md at the merge-base, the PR tip and main."""
    base = _show_blob(repo, merge_base)
    tip = _show_blob(repo, expected_sha)
    main = _show_blob(repo, default_ref)
    return base, tip, main


def _probe_arm_script(repo: Path) -> Path:
    """The ``prep_pr_finalize.py`` to arm with: the repo's copy, else ~/.claude's.

    Mirrors ``ship-it.md`` Step 4. Raises ``arm_script_missing`` when neither
    exists, before any worktree or push, so the PR is left untouched.
    """
    for candidate in (repo / _ARM_SCRIPT_RELATIVE, Path.home() / _ARM_SCRIPT_RELATIVE):
        if candidate.is_file():
            return candidate
    raise RepairStopError(
        RepairDisposition.ARM_SCRIPT_MISSING,
        f"no {_ARM_SCRIPT_RELATIVE} under {repo} or ~; run scripts/install-skills.sh",
    )


def scratch_path_for(worktree_base: Path, ticket_id: str, prefix: str) -> Path:
    """A recipe's own scratch tree for *ticket_id* (one dispatch loop runs).

    *prefix* is the recipe's ``RepairSpec.scratch_prefix``, defined once there.
    """
    return worktree_base / f"{prefix}{ticket_id}"


def _remove_scratch(repo: Path, path: Path, label: str) -> None:
    """Best-effort prune and remove of the recipe's own scratch tree.

    The ONLY place the recipe prunes or removes a worktree. Never raises, so a
    cleanup failure in a ``finally`` cannot mask the original exception. Warnings
    carry the ``<label>_cleanup_failed`` token of the recipe that owns the tree;
    *label* is required so a caller cannot log under another recipe's token.
    """
    cleanup = RepairDisposition.SCRATCH_WORKTREE_FAILED
    try:
        pruned = _git(repo, "worktree", "prune", timeout=_T_LOCAL, stop=cleanup)
        if pruned.returncode != 0:
            _logger.warning(
                "%s_cleanup_failed step=prune path=%s: %s",
                label,
                path,
                _failed(pruned, "git worktree prune"),
            )
    except RepairStopError as stop:
        _logger.warning("%s_cleanup_failed step=prune: %s", label, stop)
    if not path.exists():
        return
    try:
        removed = _git(
            repo,
            "worktree",
            "remove",
            "--force",
            str(path),
            timeout=_T_LOCAL,
            stop=cleanup,
        )
        if removed.returncode != 0:
            _logger.warning(
                "%s_cleanup_failed step=remove path=%s: %s",
                label,
                path,
                _failed(removed, "git worktree remove"),
            )
    except RepairStopError as stop:
        _logger.warning("%s_cleanup_failed step=remove: %s", label, stop)


@contextmanager
def _scratch_worktree(
    repo: Path, path: Path, expected_sha: str, label: str
) -> Iterator[Path]:
    """A detached scratch tree at *expected_sha*, cleaned up on every exit."""
    _remove_scratch(repo, path, label)
    try:
        added = _git(
            repo,
            "worktree",
            "add",
            "--detach",
            str(path),
            expected_sha,
            timeout=_T_LOCAL,
            stop=RepairDisposition.SCRATCH_WORKTREE_FAILED,
        )
        if added.returncode != 0:
            raise RepairStopError(
                RepairDisposition.SCRATCH_WORKTREE_FAILED,
                _failed(added, "git worktree add"),
            )
        yield path
    finally:
        _remove_scratch(repo, path, label)


def _abort_rebase(scratch: Path) -> None:
    try:
        _git(
            scratch,
            "rebase",
            "--abort",
            timeout=_T_LOCAL,
            stop=RepairDisposition.REBASE_FAILED,
            mutating=True,
        )
    except RepairStopError as stop:
        _logger.warning("changelog_repair_rebase_abort_failed: %s", stop)


def _split_z(text: str) -> list[str]:
    """The non-empty items of NUL-separated git ``-z`` output."""
    return [item for item in text.split("\0") if item]


def _unmerged_paths(scratch: Path, *, stop: RepairDisposition) -> frozenset[str]:
    """The scratch tree's unmerged paths; a git failure raises *stop*.

    ``-z`` keeps a path with whitespace in it intact.
    """
    listed = _git(
        scratch,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=U",
        timeout=_T_LOCAL,
        stop=stop,
    )
    if listed.returncode != 0:
        raise RepairStopError(stop, _failed(listed, "git diff --diff-filter=U"))
    return frozenset(_split_z(listed.stdout))


def _commit_count(scratch: Path, merge_base: str, expected_sha: str) -> int:
    counted = _git(
        scratch,
        "rev-list",
        "--count",
        f"{merge_base}..{expected_sha}",
        timeout=_T_LOCAL,
        stop=RepairDisposition.REBASE_FAILED,
    )
    text = counted.stdout.strip()
    if counted.returncode != 0 or not text.isdigit():
        raise RepairStopError(
            RepairDisposition.REBASE_FAILED, _failed(counted, "git rev-list")
        )
    return int(text)


def _write_changelog(scratch: Path, text: str) -> None:
    """Write *text* over the scratch CHANGELOG.md, byte-exact (no newline mapping)."""
    try:
        (scratch / CHANGELOG).write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise RepairStopError(
            RepairDisposition.REBASE_FAILED,
            f"could not write {CHANGELOG} in the scratch tree: {exc}",
        ) from exc


def _read_changelog(scratch: Path) -> str | None:
    """The scratch CHANGELOG.md text, byte-exact, or None when it is absent."""
    path = scratch / CHANGELOG
    try:
        if not path.is_file():
            return None
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RepairStopError(
            RepairDisposition.REBASE_FAILED,
            f"could not read {CHANGELOG} in the scratch tree: {exc}",
        ) from exc


def _add_changelog(scratch: Path) -> None:
    """``git add CHANGELOG.md``; a failure is the real cause, so surface it."""
    stop = RepairDisposition.REBASE_FAILED
    added = _git(scratch, "add", CHANGELOG, timeout=_T_LOCAL, stop=stop)
    if added.returncode != 0:
        raise RepairStopError(stop, _failed(added, "git add"))


def _rebase_steps(scratch: Path, default_ref: str, main_text: str, cap: int) -> None:
    """Rebase onto main, taking main's CHANGELOG at each CHANGELOG-only stop."""
    rebase = RepairDisposition.REBASE_FAILED
    result = _git(
        scratch, "rebase", default_ref, timeout=_T_REBASE, stop=rebase, mutating=True
    )
    for _ in range(cap):
        if result.returncode == 0:
            return
        unmerged = _unmerged_paths(scratch, stop=rebase)
        if unmerged != {CHANGELOG}:
            others = sorted(unmerged - {CHANGELOG})
            if others:
                raise RepairStopError(
                    RepairDisposition.NON_CHANGELOG_CONFLICT,
                    f"rebase stopped on non-CHANGELOG file(s): {others}",
                )
            raise RepairStopError(rebase, _failed(result, "git rebase"))
        _write_changelog(scratch, main_text)
        _add_changelog(scratch)
        result = _git(
            scratch,
            "rebase",
            "--continue",
            timeout=_T_REBASE,
            stop=rebase,
            mutating=True,
        )
    if result.returncode != 0:
        raise RepairStopError(rebase, f"rebase did not finish within {cap} step(s)")


def _rebase_onto(
    scratch: Path, default_ref: str, main_text: str, merge_base: str, expected_sha: str
) -> None:
    """Rebase the scratch tree onto main; abort on any failure (step 9).

    A client without the union attribute stops on CHANGELOG.md: main's text
    is written over it from Python (no ``checkout --ours/--theirs``), because
    the file is overwritten with the rebuilt text right after anyway.
    """
    cap = _commit_count(scratch, merge_base, expected_sha) + 1
    try:
        _rebase_steps(scratch, default_ref, main_text, cap)
    except RepairStopError:
        _abort_rebase(scratch)
        raise


def _commit_rebuilt(
    scratch: Path, text: str, ticket_id: str, default_branch: str
) -> None:
    """Write the rebuilt CHANGELOG and commit it when it differs (step 10)."""
    if _read_changelog(scratch) == text:
        return
    _write_changelog(scratch, text)
    _add_changelog(scratch)
    failed = RepairDisposition.REBASE_FAILED
    message = (
        f"chore(changelog): rebuild [Unreleased] onto {default_branch} (#{ticket_id})"
    )
    committed = _git(
        scratch,
        "commit",
        "--no-verify",
        "-m",
        message,
        timeout=_T_LOCAL,
        stop=failed,
        mutating=True,
    )
    if committed.returncode != 0:
        raise RepairStopError(failed, _failed(committed, "git commit"))


def _head_sha(
    scratch: Path, stop: RepairDisposition = RepairDisposition.REBASE_FAILED
) -> str:
    head = _git(scratch, "rev-parse", "HEAD", timeout=_T_LOCAL, stop=stop)
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        raise RepairStopError(stop, _failed(head, "git rev-parse"))
    return sha


def _non_changelog_diff(scratch: Path, since: str, until: str) -> str:
    diffed = _git(
        scratch,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        since,
        until,
        "--",
        ".",
        _EXCLUDE_CHANGELOG,
        timeout=_T_LOCAL,
        stop=RepairDisposition.VERIFY_FAILED,
    )
    if diffed.returncode != 0:
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED, _failed(diffed, "git diff")
        )
    return diffed.stdout


def _range_diff_stat(
    scratch: Path, merge_base: str, expected_sha: str, default_ref: str, new_sha: str
) -> str:
    """``git range-diff --stat`` output as a failure diagnostic, best effort."""
    try:
        ranged = _git(
            scratch,
            "range-diff",
            "--stat",
            f"{merge_base}..{expected_sha}",
            f"{default_ref}..{new_sha}",
            timeout=_T_LOCAL,
            stop=RepairDisposition.VERIFY_FAILED,
        )
    except RepairStopError as stop:
        return f"(unavailable: {stop.detail})"
    return _tail(ranged.stdout or ranged.stderr)


def _verify_non_changelog_identical(
    scratch: Path, merge_base: str, expected_sha: str, default_ref: str, new_sha: str
) -> None:
    """The branch's non-CHANGELOG change is unchanged by the rebase (step 11)."""
    before = _non_changelog_diff(scratch, merge_base, expected_sha)
    after = _non_changelog_diff(scratch, default_ref, new_sha)
    if _diff_fingerprint(before) == _diff_fingerprint(after):
        return
    diagnostic = _range_diff_stat(
        scratch, merge_base, expected_sha, default_ref, new_sha
    )
    raise RepairStopError(
        RepairDisposition.VERIFY_FAILED,
        f"the non-CHANGELOG diff changed across the rebase; range-diff: {diagnostic}",
    )


# ``git range-diff --no-patch --no-color`` pair line (#2921): ``N:  abbrev <mark>
# M:  abbrev subject``, with ``-`` and ``-------`` standing in for an absent side.
# Anchored at both ends. Marks: ``=`` equal, ``!`` changed, ``<`` dropped,
# ``>`` added.
_RANGE_DIFF_PAIR_RE = re.compile(
    r"^(?:\d+|-):[ \t]+\S+[ \t]+([=!<>])[ \t]+(?:\d+|-):[ \t]+\S+(?:[ \t].*)?$"
)
_RANGE_DIFF_EQUAL = "="


def _range_diff_marks(scratch: Path, old_range: str, new_range: str) -> list[str]:
    """The pair marks of ``git range-diff --no-patch`` for two ranges (#2921).

    Strict: a non-zero exit, a non-blank line that is not a pair line, or no
    pair at all raises ``VERIFY_FAILED``. The caller treats that as unprovable.
    """
    ranged = _git(
        scratch,
        "range-diff",
        "--no-color",
        "--no-patch",
        old_range,
        new_range,
        timeout=_T_LOCAL,
        stop=RepairDisposition.VERIFY_FAILED,
    )
    if ranged.returncode != 0:
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED, _failed(ranged, "git range-diff")
        )
    marks: list[str] = []
    for line in ranged.stdout.splitlines():
        if not line.strip():
            continue
        match = _RANGE_DIFF_PAIR_RE.match(line)
        if match is None:
            raise RepairStopError(
                RepairDisposition.VERIFY_FAILED,
                f"unparseable range-diff line: {_tail(line)}",
            )
        marks.append(match.group(1))
    if not marks:
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED, "git range-diff printed no commit pairs"
        )
    return marks


def _all_pairs_equal(marks: list[str]) -> bool:
    """True when every range-diff pair is ``=`` (the per-commit equality proof)."""
    return all(mark == _RANGE_DIFF_EQUAL for mark in marks)


def _changelog_only_delta(scratch: Path, rebased: str, new_sha: str) -> None:
    """The head is *rebased* or one commit on it that touches only CHANGELOG.md.

    Raises ``VERIFY_FAILED`` on any other delta, a parent mismatch, or a git
    failure. The rebuilt CHANGELOG commit is the only commit the recipe adds.
    """
    if new_sha == rebased:
        return
    parent = _git(
        scratch,
        "rev-parse",
        f"{new_sha}^",
        timeout=_T_LOCAL,
        stop=RepairDisposition.VERIFY_FAILED,
    )
    if parent.returncode != 0 or parent.stdout.strip() != rebased:
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED,
            f"head {new_sha} is not one commit on the rebased series {rebased}",
        )
    changed = _git(
        scratch,
        "diff",
        "--name-only",
        "-z",
        rebased,
        new_sha,
        timeout=_T_LOCAL,
        stop=RepairDisposition.VERIFY_FAILED,
    )
    if changed.returncode != 0:
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED, _failed(changed, "git diff --name-only")
        )
    stray = [path for path in _split_z(changed.stdout) if path != CHANGELOG]
    if stray:
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED,
            f"the CHANGELOG rebuild also touched: {sorted(stray)}",
        )


def _prove_series_unchanged(
    scratch: Path,
    merge_base: str,
    expected_sha: str,
    default_ref: str,
    rebased: str,
    new_sha: str,
) -> int:
    """Prove every branch commit is unchanged by the rebase; the commit count (#2921).

    Requires each old-series commit to pair with a new-series commit as ``=``
    (no dropped, added or changed commit), and the new head to differ from the
    rebased series only by a CHANGELOG-only commit. Writes nothing.
    """
    marks = _range_diff_marks(
        scratch, f"{merge_base}..{expected_sha}", f"{default_ref}..{rebased}"
    )
    if not _all_pairs_equal(marks):
        raise RepairStopError(
            RepairDisposition.VERIFY_FAILED,
            f"range-diff marks {''.join(marks)}: a branch commit changed"
            " across the rebase",
        )
    _changelog_only_delta(scratch, rebased, new_sha)
    return len(marks)


def _run_changelog_gate(repo: Path, scratch: Path) -> None:
    """Run the client's CHANGELOG freeze gate on the rebuilt file (step 12, G2).

    The gate script is the client workspace's copy, never the scratch tree's:
    the scratch tree is the PR branch under repair, and a branch must not
    supply the code that validates it. An absent script skips the gate (logged
    with the probed path). ``--require-tags`` plus a checked ``git fetch
    --tags`` make an unfetched ``since_tag`` fail closed instead of passing.
    """
    gate = RepairDisposition.GATE_FAILED
    script = repo / _GATE_SCRIPT_RELATIVE
    if not script.is_file():
        _logger.info("changelog_repair_gate_skipped script_absent path=%s", script)
        return
    fetched = _git(scratch, "fetch", "--tags", timeout=_T_NET, stop=gate)
    if fetched.returncode != 0:
        raise RepairStopError(gate, _failed(fetched, "git fetch --tags"))
    argv = [
        sys.executable,
        str(script),
        "--changelog",
        str(scratch / CHANGELOG),
        "--require-tags",
        "--json",
    ]
    try:
        gated = subprocess.run(
            argv,
            cwd=scratch,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_T_GATE,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepairStopError(gate, f"freeze gate did not run: {exc}") from exc
    if gated.returncode != 0:
        raise RepairStopError(
            gate,
            f"check_changelog_frozen exit {gated.returncode}: "
            f"{_tail(gated.stderr + gated.stdout)}",
        )


def _push_with_lease(
    scratch: Path, branch: str, expected_sha: str, new_sha: str
) -> None:
    """Force-push *new_sha* over *expected_sha* only (step 14).

    The verified object is named explicitly, never ``HEAD``: whatever a
    recipe's commands did to the scratch tree after *new_sha* was captured and
    checked, only *new_sha* can reach origin.

    A non-zero exit is ``push_rejected`` (transient); a timeout is
    ``push_outcome_unknown`` (the push may have landed, so never retried).
    """
    pushed = _git(
        scratch,
        "push",
        "--no-verify",
        f"--force-with-lease=refs/heads/{branch}:{expected_sha}",
        "origin",
        f"{new_sha}:refs/heads/{branch}",
        timeout=_T_NET,
        stop=RepairDisposition.PUSH_REJECTED,
        timeout_stop=RepairDisposition.PUSH_OUTCOME_UNKNOWN,
    )
    if pushed.returncode != 0:
        raise RepairStopError(
            RepairDisposition.PUSH_REJECTED, _failed(pushed, "git push")
        )


def _ls_remote_tip(repo: Path, branch: str) -> str | None:
    """Origin's tip for *branch*, or None when it could not be read."""
    try:
        listed = _git(
            repo,
            "ls-remote",
            "origin",
            f"refs/heads/{branch}",
            timeout=_T_NET,
            stop=RepairDisposition.POST_PUSH_FAILED,
        )
    except RepairStopError:
        return None
    fields = listed.stdout.split()
    if listed.returncode != 0 or not fields:
        return None
    return fields[0]


def _run_arm(script: Path, pr_number: int, scratch: Path, new_sha: str) -> ArmOutcome:
    """``prep_pr_finalize.py arm-automerge`` pinned to *new_sha* (step 17)."""
    argv = [
        sys.executable,
        str(script),
        "arm-automerge",
        str(pr_number),
        "--repo-path",
        str(scratch),
        "--head-sha",
        new_sha,
    ]
    try:
        armed = subprocess.run(
            argv,
            cwd=scratch,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_T_ARM,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ArmOutcome(returncode=None, status="", stderr_tail=_tail(str(exc)))
    try:
        parsed = json.loads(armed.stdout)
    except ValueError:
        parsed = None
    status = parsed.get("status") if isinstance(parsed, dict) else None
    return ArmOutcome(
        returncode=armed.returncode,
        status=status if isinstance(status, str) else "",
        stderr_tail=_tail(armed.stderr),
    )
