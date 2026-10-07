"""Has the code a settled disposition was granted against moved since? (#2232).

:func:`disposition_drifted` is the predicate the ledger's drift surfacing
rests on. Staleness is this check plus ``ReviewVerdict.stale_dispositions``: a
record whose code moved since it was settled stops being APPLIED and is
reported, rather than being expired — the same "make it visible instead of
adding an expiry" choice the ledger's identity design already made. Split out
of the flat ``review_finding_dispositions.py`` (#2498).

Imports nothing from ``cw`` at module scope beyond this package's own
``_constants`` (see the package docstring's "Import discipline" section):
:func:`cw._git.run_git` is imported inside :func:`disposition_drifted`, so a
test that patches ``cw._git.run_git`` still reaches it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.review_finding_dispositions._constants import _LOGGER_NAME

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(_LOGGER_NAME)

#: ``git diff --quiet``'s two answerable exit codes. Anything else — ``128``
#: for an unresolvable ref or a directory that is not a repository, and any
#: future code git adds — is an unanswered question, and this module answers
#: those toward surfacing.
_GIT_DIFF_UNCHANGED = 0
_GIT_DIFF_CHANGED = 1


def disposition_drifted(
    worktree: Path | None,
    entry_reviewed_sha: str,
    current_sha: str,
    file: str,
) -> bool:
    """Has *file* changed between the two shas in *worktree* (#2232)?

    The predicate the ledger's drift surfacing rests on. ADR-0016 accepted, as
    the price of an identity that is deliberately NOT evidence-anchored, that
    a suppression outlives the code it was granted for; this is how that cost
    stops being silent. It does not expire anything — the caller uses a
    ``True`` here to decline to apply a record for one pass and say so.

    ``False`` — do not surface — for the three cases where there is no
    question to answer: no worktree threaded (the inert default every caller
    that predates this ticket gets), either sha blank (a pre-#2210 record
    carries none, and a missing field must not manufacture drift), or the two
    shas equal (the record was settled against exactly this code). The
    equal-sha case short-circuits before any subprocess, so the common
    settled-this-round path costs nothing.

    Otherwise ``git diff --quiet <a> <b> -- <file>``, whose exit code is the
    whole contract: ``0`` unchanged, ``1`` changed, ``128`` for a ref this
    worktree cannot resolve. Only ``0`` returns ``False``. Everything else —
    an unresolvable ref, a worktree that is not a repository, an ``OSError``
    from a missing git or a vanished directory — returns ``True``, because an
    unanswerable question about whether a suppression is still warranted must
    fail toward the finding staying visible. That is the same direction
    :func:`_ledger_matches` already takes for a contested finding.

    Pure stdlib at module scope by construction: this module may import
    nothing from ``cw`` there (see the module docstring), and the nearest
    existing ``git diff`` runner (``cw.cli.review._diff_integrity``) is
    CLI-scoped, so importing it would invert the dependency direction the
    split maintains. The one ``cw`` helper it does use —
    :func:`cw._git.run_git`, which strips ``GIT_*`` itself — lives in a leaf
    module imported inside this function body, the same shape as the deferred
    ``cw.events`` import below.

    That environment is **load-bearing, not hygiene** (#2232). ``cw`` can run
    inside a git hook, where an inherited ``GIT_DIR``/``GIT_WORK_TREE`` points
    at the hook's repository: the diff would then be taken in a DIFFERENT tree
    than *worktree*, silently, and its answer decides whether a settled
    finding stays suppressed.
    """
    if worktree is None or not entry_reviewed_sha or not current_sha:
        return False
    if entry_reviewed_sha == current_sha:
        return False
    from cw._git import run_git

    try:
        completed = run_git(
            ["diff", "--quiet", entry_reviewed_sha, current_sha, "--", file],
            cwd=worktree,
            capture_output=True,
            check=False,
        )
    except OSError:
        _log.warning(
            "auto-dev: could not run git diff for drift check (file=%s)",
            file,
            exc_info=True,
        )
        return True
    if completed.returncode == _GIT_DIFF_UNCHANGED:
        return False
    if completed.returncode != _GIT_DIFF_CHANGED:
        _log.warning(
            "auto-dev: git diff drift check returned %d for file=%s "
            "(%s..%s); treating the record as stale",
            completed.returncode,
            file,
            entry_reviewed_sha,
            current_sha,
        )
    return True
