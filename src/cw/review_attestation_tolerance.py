"""Which later commits keep a review sign-off attestation valid (#3040, RFC 0017).

``cw review attestation check`` walks ``A..B`` (``A`` the attested head, ``B``
HEAD) first-parent and requires every commit to fall wholly into one class:

- (a) a clean merge from the default branch, judged by the shared
  :func:`~cw.review_convergence.classify_default_merge`, and only when the
  caller passes a :class:`~cw.review_convergence.MergeTolerance` (decided by
  :func:`~cw.review_convergence.review_move_tolerance`; ``None`` means the
  client tolerates no merge, reason ``tolerance_off``);
- (b) a CHANGELOG-only commit;
- (c) a pure rename (``R100``);
- (d) a migration renumber: renames of files matching a rule's ``path_glob``
  whose only edited lines match its ``line_regex``.

The span is bounded (:data:`MAX_TOLERANCE_SPAN_COMMITS`), listed once before
any per-commit git call. Lockless; git only through
:func:`~cw.review_convergence.git_stdout_or_none` (bounded by its timeout).
The glob matcher is ``mechanical_repair_git.path_in_scope``, imported inside
:func:`_renumber_rules` so this module (which ``cw.review_approval`` imports)
does not load ``cw.reconcile.review_recipes``, whose ``repair_fence`` imports
``cw.review_approval`` back.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.models.client import RenumberPattern
from cw.review_convergence import (
    MergeTolerance,
    classify_default_merge,
    git_stdout_or_none,
)
from cw.review_convergence._merge_tolerance import (
    REJECT_TOLERANCE_OFF,
    MergeClean,
    MergeResolution,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

DEFAULT_RENUMBER_LINE_PATTERNS: Final = (
    r"^revision\s*=",
    r"^down_revision\s*=",
    r"^Revision ID:",
    r"^Revises:",
)
"""Alembic revision-identity lines a migration renumber may edit."""

DEFAULT_MIGRATION_PATH_GLOBS: Final = (
    "**/alembic/versions/*.py",
    "**/migrations/versions/*.py",
)
"""Where a migration renumber may rename files (repo-relative, anchored)."""

DEFAULT_RENUMBER_PATTERNS: Final = tuple(
    RenumberPattern(path_glob=glob, line_regex="|".join(DEFAULT_RENUMBER_LINE_PATTERNS))
    for glob in DEFAULT_MIGRATION_PATH_GLOBS
)
"""One rule per default glob; a client's rules extend these."""

CHANGELOG_PATH: Final = "CHANGELOG.md"
MAX_TOLERANCE_SPAN_COMMITS = 200
"""First-parent commits in ``A..B`` above which the verdict is span_too_large."""

REASON_GIT_UNREADABLE: Final = "git_unreadable"
REASON_UNTOLERATED_CHANGE: Final = "untolerated_change"
REASON_MIXED_CLASSES: Final = "mixed_classes"
REASON_EMPTY_COMMIT: Final = "empty_commit"
REASON_MERGE_RESOLUTION: Final = "merge_resolution"

_RENAME_PREFIX: Final = "R"
_EXACT_RENAME: Final = "R100"
_RENAME_FIELDS: Final = 3
_MERGE_FIELDS: Final = 3
_CLASS_CHANGELOG: Final = "changelog"
_CLASS_RENAME: Final = "rename"


class Offender(NamedTuple):
    """The first commit no tolerance class covers, its path, and why."""

    commit: str
    path: str | None
    reason: str


class SpanTooLarge(NamedTuple):
    """``A..B`` holds more first-parent commits than the cap allows."""

    commits: int


class _Change(NamedTuple):
    """One ``--name-status`` entry: status, the old path, the new path."""

    status: str
    old: str
    new: str


class _RenumberRule(NamedTuple):
    matches: Callable[[str], bool]
    line: re.Pattern[str]


def _git_out(repo: Path, *argv: str) -> str | None:
    return git_stdout_or_none(repo, list(argv))


def first_untolerated_commit(
    repo: Path,
    from_sha: str,
    to_sha: str,
    tolerance: MergeTolerance | None,
    renumber_patterns: Sequence[RenumberPattern],
) -> Offender | SpanTooLarge | None:
    """The first commit in ``from_sha..to_sha`` that no class tolerates.

    ``None`` when every commit is tolerated; :class:`SpanTooLarge` (before any
    per-commit git) when the first-parent span exceeds the cap. Fails closed:
    an unreadable span or commit is an offender.
    """
    listed = _git_out(
        repo, "rev-list", "--reverse", "--first-parent", f"{from_sha}..{to_sha}"
    )
    if listed is None:
        return Offender(to_sha, None, REASON_GIT_UNREADABLE)
    commits = listed.split()
    if len(commits) > MAX_TOLERANCE_SPAN_COMMITS:
        return SpanTooLarge(len(commits))
    rules: list[_RenumberRule] | None = None
    for sha in commits:
        parents = _git_out(repo, "rev-list", "--parents", "-n", "1", sha)
        if parents is None:
            return Offender(sha, None, REASON_GIT_UNREADABLE)
        if len(parents.split()) >= _MERGE_FIELDS:
            offender = _merge_offence(repo, sha, tolerance)
        else:
            if rules is None:
                rules = _renumber_rules(renumber_patterns)
            offender = _commit_offence(repo, sha, rules)
        if offender is not None:
            return offender
    return None


def _merge_offence(
    repo: Path, sha: str, tolerance: MergeTolerance | None
) -> Offender | None:
    """Class (a): a clean default-branch merge, only under a tolerance."""
    if tolerance is None:
        return Offender(sha, None, REJECT_TOLERANCE_OFF)
    outcome = classify_default_merge(repo, sha, tolerance)
    if isinstance(outcome, MergeClean):
        return None
    if isinstance(outcome, MergeResolution):
        return Offender(sha, None, REASON_MERGE_RESOLUTION)
    return Offender(sha, None, outcome)


def _renumber_rules(patterns: Sequence[RenumberPattern]) -> list[_RenumberRule]:
    from cw.reconcile.review_recipes.mechanical_repair_git import path_in_scope

    return [
        _RenumberRule(path_in_scope([rule.path_glob]), re.compile(rule.line_regex))
        for rule in patterns
    ]


def _changes(repo: Path, sha: str, *, exact: bool) -> list[_Change] | None:
    """The commit's ``--name-status`` entries (exact renames only when *exact*)."""
    rename = "-M100%" if exact else "-M"
    out = _git_out(
        repo, "diff-tree", "--no-commit-id", "--name-status", "-r", rename, sha
    )
    if out is None:
        return None
    changes: list[_Change] = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) == _RENAME_FIELDS:
            changes.append(_Change(fields[0], fields[1], fields[2]))
        elif len(fields) == _RENAME_FIELDS - 1:
            changes.append(_Change(fields[0], fields[1], fields[1]))
    return changes


def _class_of(change: _Change) -> str | None:
    if change.status == _EXACT_RENAME:
        return _CLASS_RENAME
    if change.old == CHANGELOG_PATH and change.new == CHANGELOG_PATH:
        return _CLASS_CHANGELOG
    return None


def _commit_offence(
    repo: Path, sha: str, rules: list[_RenumberRule]
) -> Offender | None:
    """Classes (b) to (d) for one non-merge commit."""
    exact = _changes(repo, sha, exact=True)
    if exact is None:
        return Offender(sha, None, REASON_GIT_UNREADABLE)
    if not exact:
        return Offender(sha, None, REASON_EMPTY_COMMIT)
    classes = [_class_of(change) for change in exact]
    if None not in classes:
        if len(set(classes)) == 1:
            return None
        odd = next(c for c, k in zip(exact, classes, strict=True) if k != classes[0])
        return Offender(sha, odd.new, REASON_MIXED_CLASSES)
    if _is_renumber(repo, sha, rules):
        return None
    first = next(c for c, k in zip(exact, classes, strict=True) if k is None)
    return Offender(sha, first.new, REASON_UNTOLERATED_CHANGE)


def _rule_for(change: _Change, rules: list[_RenumberRule]) -> _RenumberRule | None:
    if not change.status.startswith(_RENAME_PREFIX):
        return None
    return next(
        (
            rule
            for rule in rules
            if rule.matches(change.old) and rule.matches(change.new)
        ),
        None,
    )


def _is_renumber(repo: Path, sha: str, rules: list[_RenumberRule]) -> bool:
    """Class (d): renames under a rule's glob editing only its revision lines."""
    renames = _changes(repo, sha, exact=False)
    if not renames or not rules:
        return False
    for change in renames:
        rule = _rule_for(change, rules)
        if rule is None:
            return False
        patch = _git_out(
            repo,
            "diff",
            "--unified=0",
            "-M",
            f"{sha}^",
            sha,
            "--",
            change.old,
            change.new,
        )
        if patch is None or not _only_rule_lines(patch, rule.line):
            return False
    return True


def _only_rule_lines(patch: str, line_re: re.Pattern[str]) -> bool:
    """True when every added or removed line of *patch* matches *line_re*."""
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")) and line_re.search(line[1:]) is None:
            return False
    return True
