"""Review configuration read from the default branch, never the reviewed branch (#3038).

``.claude/review-extras.md``, ``.claude/review-policy.md`` and the
sensitive-files manifests steer every review of the branch that is being
reviewed. Read from that branch's own working tree they let the branch weaken
the review of itself (RFC 0017 I2). This leaf module reads them from a pinned
commit of the default branch instead:

- :func:`resolve_review_config_source` resolves the commit once per pass through
  :func:`~cw.review_convergence.resolve_default_branch_ref`, the same chain the
  review diff uses (``origin/<default>`` first, else the local ``<default>``),
  so configuration and diff cannot name different refs. It never fetches, never
  raises, and logs exactly one WARNING per unresolved outcome.
- :class:`ReviewConfigSource` reads blobs from that commit
  (``git cat-file blob``), never the working tree. ``read`` pins strict UTF-8
  and universal newlines (what ``Path.read_text`` did); ``exists`` is separate
  so a present-but-unreadable manifest still wins the large tier's first-hit.
- :func:`branch_modified_review_config` records which of the four paths the
  reviewed branch changed; :func:`review_config_status` packages source and
  detection into the verdict's :class:`~cw.review_findings.ReviewConfigStatus`.

Absence is the only degraded state: an unresolvable ref, a git failure or a
non-UTF-8 blob all read as "not there". The worktree copy is never a fallback.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from cw._git import FULL_COMMIT_ID_RE, LOCAL_GIT_TIMEOUT_SECONDS, run_git
from cw.exceptions import CwError
from cw.review_convergence import git_stdout_or_none, resolve_default_branch_ref
from cw.review_findings import ReviewConfigStatus

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

REVIEW_EXTRAS_PATH: Final = ".claude/review-extras.md"
REVIEW_POLICY_PATH: Final = ".claude/review-policy.md"
SENSITIVE_CLAUDE_PATH: Final = ".claude/sensitive-files.yml"
SENSITIVE_GITHUB_PATH: Final = ".github/sensitive-files.yml"

#: The four configuration paths in bytewise-sorted order, which is also
#: ``git diff --name-only`` output order, so detection needs no re-sort.
REVIEW_CONFIG_PATHS: Final = (
    REVIEW_EXTRAS_PATH,
    REVIEW_POLICY_PATH,
    SENSITIVE_CLAUDE_PATH,
    SENSITIVE_GITHUB_PATH,
)

#: The ``_SensitiveHit`` category the fix loop's scope gate uses when the
#: configuration could not be resolved and the registry therefore cannot be read.
REVIEW_CONFIG_UNAVAILABLE_CATEGORY: Final = "review-config-unavailable"

_GIT_FAILURES: Final = (OSError, ValueError, subprocess.TimeoutExpired)


@dataclass(frozen=True)
class ReviewConfigSource:
    """A pinned default-branch commit that review configuration is read from.

    ``ref`` is the display name (``origin/<d>`` or ``<d>``) and ``commit`` the
    full sha, both ``None`` when neither ref resolved. An unresolved source
    reads nothing: every ``read`` is ``None`` and every ``exists`` is ``False``.
    """

    worktree: Path
    default_branch: str
    ref: str | None
    commit: str | None

    @property
    def resolved(self) -> bool:
        """Whether a default-branch commit was found to read from."""
        return self.commit is not None

    def exists(self, relpath: str) -> bool:
        """Whether *relpath* names an object at the pinned commit.

        Deliberately independent of :meth:`read`: a directory or a non-UTF-8
        blob exists but reads as ``None``. A git failure counts as absent.
        """
        if self.commit is None:
            return False
        try:
            result = run_git(
                ["cat-file", "-e", f"{self.commit}:{relpath}"],
                cwd=self.worktree,
                capture_output=True,
                timeout=LOCAL_GIT_TIMEOUT_SECONDS,
            )
        except _GIT_FAILURES:
            return False
        return result.returncode == 0

    def read(self, relpath: str) -> str | None:
        """The text of *relpath* at the pinned commit, or ``None``.

        ``None`` for an unresolved source, a missing path, a tree, a git
        failure or timeout, and a blob that is not strict UTF-8. ``\\r\\n`` and
        bare ``\\r`` read as ``\\n``, matching ``Path.read_text``.
        """
        if self.commit is None:
            return None
        try:
            # ``run_git`` is annotated for the text default; ``text=False`` yields
            # bytes, which is what pins the decode to UTF-8 instead of the locale.
            result = cast(
                "subprocess.CompletedProcess[bytes]",
                run_git(
                    ["cat-file", "blob", f"{self.commit}:{relpath}"],
                    cwd=self.worktree,
                    capture_output=True,
                    text=False,
                    timeout=LOCAL_GIT_TIMEOUT_SECONDS,
                ),
            )
        except _GIT_FAILURES:
            return None
        if result.returncode != 0:
            return None
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return text.replace("\r\n", "\n").replace("\r", "\n")


def resolve_review_config_source(
    worktree: Path, default_branch: str
) -> ReviewConfigSource:
    """Resolve the default-branch commit review configuration is read from.

    Never raises and never fetches. Each unresolved outcome (a commit id given
    as the branch name, git unable to run, neither ref resolving) logs exactly
    one WARNING and returns a source that reads nothing.
    """
    if FULL_COMMIT_ID_RE.fullmatch(default_branch):
        _log.warning(
            "review configuration unavailable: %s is a commit id, not a default "
            "branch name (worktree %s); no ref was probed and no review "
            "configuration is read",
            default_branch,
            worktree,
        )
        return ReviewConfigSource(worktree, default_branch, None, None)
    try:
        found = resolve_default_branch_ref(worktree, default_branch)
    except CwError as exc:
        _log.warning(
            "review configuration unavailable: git could not run in %s while "
            "resolving origin/%s or %s (%s); no review configuration is read",
            worktree,
            default_branch,
            default_branch,
            exc,
        )
        return ReviewConfigSource(worktree, default_branch, None, None)
    if found is None:
        _log.warning(
            "review configuration unavailable: neither origin/%s nor %s resolves in "
            "%s; no review configuration is read",
            default_branch,
            default_branch,
            worktree,
        )
        return ReviewConfigSource(worktree, default_branch, None, None)
    return ReviewConfigSource(worktree, default_branch, found.ref, found.commit)


def _warn_detection_failed(source: ReviewConfigSource, step: str) -> None:
    _log.warning(
        "could not detect branch edits to the review configuration in %s: %s against "
        "%s failed; recording none",
        source.worktree,
        step,
        source.ref,
    )


def branch_modified_review_config(source: ReviewConfigSource) -> tuple[str, ...]:
    """The configuration paths the reviewed branch changed, in config order.

    ``git diff --name-only --no-renames <merge-base(commit, HEAD)> HEAD`` over
    the four paths, so add, delete, modify and rename-away all show and
    default-branch commits the branch has not merged never count as its edit.
    Independent of the pass's own changed-file set, so cycle-0, delta and
    resolution passes agree. An unresolved source gives ``()`` silently (its
    WARNING is already logged); a detection git failure gives ``()`` and one
    WARNING. Never raises.
    """
    if source.commit is None:
        return ()
    base = git_stdout_or_none(source.worktree, ["merge-base", source.commit, "HEAD"])
    if base is None or not base.strip():
        _warn_detection_failed(source, "git merge-base")
        return ()
    changed = git_stdout_or_none(
        source.worktree,
        [
            "diff",
            "--name-only",
            "--no-renames",
            base.strip(),
            "HEAD",
            "--",
            *REVIEW_CONFIG_PATHS,
        ],
    )
    if changed is None:
        _warn_detection_failed(source, "git diff")
        return ()
    return tuple(line for line in changed.splitlines() if line)


def review_config_status(source: ReviewConfigSource) -> ReviewConfigStatus:
    """The verdict record for *source*: what was read and what the branch changed."""
    return ReviewConfigStatus(
        default_branch=source.default_branch,
        ref=source.ref,
        commit=source.commit,
        present=[path for path in REVIEW_CONFIG_PATHS if source.exists(path)],
        modified=list(branch_modified_review_config(source)),
    )
