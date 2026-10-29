"""Git and script steps of the ``repair_changelog_conflict`` recipe (#2771).

Post-lock only: every function here runs a subprocess, so none may run under
``sessions_lock`` or ``dev_queue_lock``.

Invocation contract (R3):

- Every git call goes through :func:`cw._git.run_git` with ``check=False``,
  ``capture_output=True`` and a per-call ``timeout``, via :func:`_git`, which
  turns ``OSError`` / ``subprocess.TimeoutExpired`` into :class:`RepairStopError`.
  A non-zero exit is a returncode the caller maps to its own disposition.
- Every mutating call in the scratch tree (``rebase``, ``rebase --continue`` /
  ``--abort``, ``commit``) carries :data:`_MUTATING_FLAGS`: a fixed identity,
  no hooks, no editor and no signing, so a daemon without a git identity, gpg
  agent or editor cannot silently kill the recipe.
- The recipe works only in its own detached scratch tree
  (``<worktree_base>/.changelog-repair-<ticket>``), never in the ticket's
  worktree. :func:`_remove_scratch` is the one place that prunes or removes a
  worktree (pinned by ``tests/test_reconcile_worktree_removal_guard.py``); its
  ``git worktree prune`` is repository-wide but only drops the admin records
  of worktrees whose directory is already gone, never a directory. No git argv
  here uses ``reset``, ``clean``, ``restore``, ``checkout`` or ``stash``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_clean_env, run_git
from cw.changelog_rebuild import ChangelogRepairDisposition
from cw.exceptions import CwError

if TYPE_CHECKING:
    from collections.abc import Iterator

_logger = logging.getLogger("cw.reconcile.review_recipes")

# Per-call timeouts in seconds (R3). Local reads/writes, network, the rebase,
# the CHANGELOG freeze gate, the arm script (4 attempts with 3 s linear backoff
# plus its gh calls), a gh read, and the one gh disarm call.
_T_LOCAL = 60
_T_NET = 120
_T_REBASE = 300
_T_GATE = 60
_T_ARM = 180
_T_GH = 15
_T_DISARM = 30

CHANGELOG = "CHANGELOG.md"
_MUTATING_FLAGS: tuple[str, ...] = (
    "-c",
    "user.name=cw-changelog-repair",
    "-c",
    "user.email=cw-changelog-repair@users.noreply.invalid",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.editor=true",
    "-c",
    "commit.gpgsign=false",
)
_ARM_SCRIPT_RELATIVE = Path(".claude/scripts/prep_pr_finalize.py")
_GATE_SCRIPT_RELATIVE = Path(".claude/scripts/check_changelog_frozen.py")
_SCRATCH_PREFIX = ".changelog-repair-"
_TAIL_CHARS = 300
_MERGE_TREE_CONFLICTED = 1
_EXCLUDE_CHANGELOG = f":(exclude){CHANGELOG}"

_INDEX_LINE_PREFIX = "index "
_HUNK_HEADER_PREFIX = "@@"


class RepairStopError(CwError):
    """One repair step could not go on; carries the disposition to park with."""

    __slots__ = ("detail", "disposition")

    def __init__(self, disposition: ChangelogRepairDisposition, detail: str) -> None:
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
    stop: ChangelogRepairDisposition,
    timeout_stop: ChangelogRepairDisposition | None = None,
    mutating: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded git call in *cwd*; never raises ``WorktreeError``.

    ``OSError`` raises :class:`RepairStopError` with *stop*; a timeout with
    *timeout_stop* (default *stop*). *mutating* prefixes
    :data:`_MUTATING_FLAGS`.
    """
    full = (*_MUTATING_FLAGS, *argv) if mutating else argv
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
        stop=ChangelogRepairDisposition.FETCH_FAILED,
    )
    if fetched.returncode != 0:
        raise RepairStopError(
            ChangelogRepairDisposition.FETCH_FAILED, _failed(fetched, "git fetch")
        )
    resolved = _git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/origin/{branch}^{{commit}}",
        timeout=_T_LOCAL,
        stop=ChangelogRepairDisposition.FETCH_FAILED,
    )
    sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not sha:
        raise RepairStopError(
            ChangelogRepairDisposition.FETCH_FAILED,
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
        stop=ChangelogRepairDisposition.CONFLICT_UNEXPLAINED,
    )
    if changed.returncode != 0:
        raise RepairStopError(
            ChangelogRepairDisposition.CONFLICT_UNEXPLAINED,
            _failed(changed, "git diff --name-only"),
        )
    return CHANGELOG in changed.stdout.split()


def _conflict_set(
    repo: Path, default_ref: str, expected_sha: str
) -> tuple[frozenset[str], str]:
    """The files a merge of *expected_sha* into main conflicts on, and the base.

    ``CHANGELOG.md merge=union`` hides CHANGELOG conflicts from git, so the set
    is ``merge-tree``'s conflicted files plus CHANGELOG.md whenever both sides
    changed it since the merge-base (R1). Read-only apart from the objects
    ``merge-tree --write-tree`` writes.
    """
    unexplained = ChangelogRepairDisposition.CONFLICT_UNEXPLAINED
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
        default_ref,
        expected_sha,
        timeout=_T_LOCAL,
        stop=unexplained,
    )
    if merged.returncode not in {0, _MERGE_TREE_CONFLICTED}:
        raise RepairStopError(unexplained, _failed(merged, "git merge-tree"))
    files = set(merged.stdout.split()[1:]) if merged.returncode else set()
    if _changelog_changed(repo, merge_base, expected_sha) and _changelog_changed(
        repo, merge_base, default_ref
    ):
        files.add(CHANGELOG)
    return frozenset(files), merge_base


def _read_three_blobs(
    repo: Path, merge_base: str, expected_sha: str, default_ref: str
) -> tuple[str, str, str]:
    """CHANGELOG.md at the merge-base, the PR tip and main."""
    texts: list[str] = []
    for rev in (merge_base, expected_sha, default_ref):
        shown = _git(
            repo,
            "show",
            f"{rev}:{CHANGELOG}",
            timeout=_T_LOCAL,
            stop=ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE,
        )
        if shown.returncode != 0:
            raise RepairStopError(
                ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE,
                _failed(shown, f"git show {rev}:{CHANGELOG}"),
            )
        texts.append(shown.stdout)
    return texts[0], texts[1], texts[2]


def _probe_arm_script(repo: Path) -> Path:
    """The ``prep_pr_finalize.py`` to arm with: the repo's copy, else ~/.claude's.

    Mirrors ``ship-it.md`` Step 4. Raises ``arm_script_missing`` when neither
    exists, before any worktree or push, so the PR is left untouched.
    """
    for candidate in (repo / _ARM_SCRIPT_RELATIVE, Path.home() / _ARM_SCRIPT_RELATIVE):
        if candidate.is_file():
            return candidate
    raise RepairStopError(
        ChangelogRepairDisposition.ARM_SCRIPT_MISSING,
        f"no {_ARM_SCRIPT_RELATIVE} under {repo} or ~; run scripts/install-skills.sh",
    )


def scratch_path_for(worktree_base: Path, ticket_id: str) -> Path:
    """The recipe's own scratch tree for *ticket_id* (one dispatch loop runs)."""
    return worktree_base / f"{_SCRATCH_PREFIX}{ticket_id}"


def _remove_scratch(repo: Path, path: Path) -> None:
    """Best-effort prune and remove of the recipe's own scratch tree.

    The ONLY place the recipe prunes or removes a worktree. Never raises, so a
    cleanup failure in a ``finally`` cannot mask the original exception.
    """
    cleanup = ChangelogRepairDisposition.SCRATCH_WORKTREE_FAILED
    try:
        pruned = _git(repo, "worktree", "prune", timeout=_T_LOCAL, stop=cleanup)
        if pruned.returncode != 0:
            _logger.warning(
                "changelog_repair_cleanup_failed step=prune path=%s: %s",
                path,
                _failed(pruned, "git worktree prune"),
            )
    except RepairStopError as stop:
        _logger.warning("changelog_repair_cleanup_failed step=prune: %s", stop)
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
                "changelog_repair_cleanup_failed step=remove path=%s: %s",
                path,
                _failed(removed, "git worktree remove"),
            )
    except RepairStopError as stop:
        _logger.warning("changelog_repair_cleanup_failed step=remove: %s", stop)


@contextmanager
def _scratch_worktree(repo: Path, path: Path, expected_sha: str) -> Iterator[Path]:
    """A detached scratch tree at *expected_sha*, cleaned up on every exit."""
    _remove_scratch(repo, path)
    try:
        added = _git(
            repo,
            "worktree",
            "add",
            "--detach",
            str(path),
            expected_sha,
            timeout=_T_LOCAL,
            stop=ChangelogRepairDisposition.SCRATCH_WORKTREE_FAILED,
        )
        if added.returncode != 0:
            raise RepairStopError(
                ChangelogRepairDisposition.SCRATCH_WORKTREE_FAILED,
                _failed(added, "git worktree add"),
            )
        yield path
    finally:
        _remove_scratch(repo, path)


def _abort_rebase(scratch: Path) -> None:
    try:
        _git(
            scratch,
            "rebase",
            "--abort",
            timeout=_T_LOCAL,
            stop=ChangelogRepairDisposition.REBASE_FAILED,
            mutating=True,
        )
    except RepairStopError as stop:
        _logger.warning("changelog_repair_rebase_abort_failed: %s", stop)


def _unmerged_paths(scratch: Path) -> frozenset[str]:
    listed = _git(
        scratch,
        "diff",
        "--name-only",
        "--diff-filter=U",
        timeout=_T_LOCAL,
        stop=ChangelogRepairDisposition.REBASE_FAILED,
    )
    return frozenset(listed.stdout.split())


def _commit_count(scratch: Path, merge_base: str, expected_sha: str) -> int:
    counted = _git(
        scratch,
        "rev-list",
        "--count",
        f"{merge_base}..{expected_sha}",
        timeout=_T_LOCAL,
        stop=ChangelogRepairDisposition.REBASE_FAILED,
    )
    text = counted.stdout.strip()
    if counted.returncode != 0 or not text.isdigit():
        raise RepairStopError(
            ChangelogRepairDisposition.REBASE_FAILED, _failed(counted, "git rev-list")
        )
    return int(text)


def _rebase_steps(scratch: Path, default_ref: str, main_text: str, cap: int) -> None:
    """Rebase onto main, taking main's CHANGELOG at each CHANGELOG-only stop."""
    rebase = ChangelogRepairDisposition.REBASE_FAILED
    result = _git(
        scratch, "rebase", default_ref, timeout=_T_REBASE, stop=rebase, mutating=True
    )
    for _ in range(cap):
        if result.returncode == 0:
            return
        unmerged = _unmerged_paths(scratch)
        if unmerged != {CHANGELOG}:
            others = sorted(unmerged - {CHANGELOG})
            if others:
                raise RepairStopError(
                    ChangelogRepairDisposition.NON_CHANGELOG_CONFLICT,
                    f"rebase stopped on non-CHANGELOG file(s): {others}",
                )
            raise RepairStopError(rebase, _failed(result, "git rebase"))
        (scratch / CHANGELOG).write_text(main_text, encoding="utf-8", newline="")
        _git(scratch, "add", CHANGELOG, timeout=_T_LOCAL, stop=rebase)
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
    path = scratch / CHANGELOG
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current == text:
        return
    path.write_text(text, encoding="utf-8", newline="")
    failed = ChangelogRepairDisposition.REBASE_FAILED
    _git(scratch, "add", CHANGELOG, timeout=_T_LOCAL, stop=failed)
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


def _head_sha(scratch: Path) -> str:
    head = _git(
        scratch,
        "rev-parse",
        "HEAD",
        timeout=_T_LOCAL,
        stop=ChangelogRepairDisposition.REBASE_FAILED,
    )
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        raise RepairStopError(
            ChangelogRepairDisposition.REBASE_FAILED, _failed(head, "git rev-parse")
        )
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
        stop=ChangelogRepairDisposition.VERIFY_FAILED,
    )
    if diffed.returncode != 0:
        raise RepairStopError(
            ChangelogRepairDisposition.VERIFY_FAILED, _failed(diffed, "git diff")
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
            stop=ChangelogRepairDisposition.VERIFY_FAILED,
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
        ChangelogRepairDisposition.VERIFY_FAILED,
        f"the non-CHANGELOG diff changed across the rebase; range-diff: {diagnostic}",
    )


def _run_changelog_gate(scratch: Path) -> None:
    """Run the repo's CHANGELOG freeze gate on the rebuilt file (step 12, G2)."""
    try:
        _git(
            scratch,
            "fetch",
            "--tags",
            timeout=_T_NET,
            stop=ChangelogRepairDisposition.GATE_FAILED,
        )
    except RepairStopError as stop:
        _logger.info("changelog_repair_fetch_tags_skipped: %s", stop.detail)
    script = scratch / _GATE_SCRIPT_RELATIVE
    if not script.is_file():
        return
    argv = [sys.executable, str(script), "--changelog", str(scratch / CHANGELOG)]
    try:
        gated = subprocess.run(
            [*argv, "--json"],
            cwd=scratch,
            env=git_clean_env(),
            capture_output=True,
            text=True,
            timeout=_T_GATE,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepairStopError(
            ChangelogRepairDisposition.GATE_FAILED, f"freeze gate did not run: {exc}"
        ) from exc
    if gated.returncode != 0:
        raise RepairStopError(
            ChangelogRepairDisposition.GATE_FAILED,
            f"check_changelog_frozen exit {gated.returncode}: "
            f"{_tail(gated.stderr + gated.stdout)}",
        )


def _push_with_lease(scratch: Path, branch: str, expected_sha: str) -> None:
    """Force-push the scratch HEAD over *expected_sha* only (step 14).

    A non-zero exit is ``push_rejected`` (transient); a timeout is
    ``push_outcome_unknown`` (the push may have landed, so never retried).
    """
    pushed = _git(
        scratch,
        "push",
        "--no-verify",
        f"--force-with-lease=refs/heads/{branch}:{expected_sha}",
        "origin",
        f"HEAD:refs/heads/{branch}",
        timeout=_T_NET,
        stop=ChangelogRepairDisposition.PUSH_REJECTED,
        timeout_stop=ChangelogRepairDisposition.PUSH_OUTCOME_UNKNOWN,
    )
    if pushed.returncode != 0:
        raise RepairStopError(
            ChangelogRepairDisposition.PUSH_REJECTED, _failed(pushed, "git push")
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
            stop=ChangelogRepairDisposition.POST_PUSH_FAILED,
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
