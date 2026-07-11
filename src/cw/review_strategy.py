"""Review-strategy resolution for the request_reviewer review recipe (RFC 0010).

Centralises the ``review_strategy`` read from ``.claude/project-config.yaml`` so
``cw.reconcile.review_recipes`` has one resolution point for "who should this
repo request as a PR reviewer." Mirrors ``cw.tracker.resolve_tracker``'s shape
exactly: the same ``PROJECT_CONFIG_RELPATH``, the same ``yaml.safe_load`` +
``isinstance`` walk, and the same "any failure -> safe default" philosophy.

The safe default is ``ReviewStrategy("ci", None)`` — "rely on CI, request no
reviewer." A missing file, malformed YAML, a non-dict root, an absent or
non-dict ``review_strategy`` key, a non-string / unrecognized ``mode``, or a
non-string handle all degrade to it. The runtime therefore never wedges on a
typo; ``cw doctor`` is where the typo is surfaced to the operator as a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

# Repo-relative path to the per-client project config — the SAME file
# cw.tracker.resolve_tracker reads (tracking.primary.system lives there too).
PROJECT_CONFIG_RELPATH = Path(".claude") / "project-config.yaml"

# Recognized review-strategy modes. "ci" is the no-op default; the other two
# name a GitHub handle source (repo owner login, or an org/team slug).
_MODE_CI: Literal["ci"] = "ci"
_MODE_REPO_OWNER: Literal["repo_owner"] = "repo_owner"
_MODE_REVIEWER_TEAM: Literal["reviewer_team"] = "reviewer_team"

# Per-mode key naming the handle to read out of the review_strategy block.
_HANDLE_KEY_BY_MODE: dict[str, str] = {
    _MODE_REPO_OWNER: "repo_owner",
    _MODE_REVIEWER_TEAM: "reviewer_team",
}

ReviewStrategyMode = Literal["ci", "repo_owner", "reviewer_team"]


@dataclass(frozen=True)
class ReviewStrategy:
    """Resolved review-strategy policy for one repo.

    ``mode`` is the reviewer-selection policy; ``handle`` is the repo-owner
    login or ``org/team`` slug to request (``None`` for ``mode == "ci"``, and
    ``None`` too when a ``repo_owner``/``reviewer_team`` mode is missing its
    handle — a misconfiguration the act phase treats as a fail-safe correction).
    """

    mode: ReviewStrategyMode
    handle: str | None


_CI_DEFAULT = ReviewStrategy(_MODE_CI, None)


def _load_review_strategy_block(root: Path) -> dict[str, object] | None:
    """Return the ``review_strategy`` mapping, or None on any read/shape failure.

    Consolidates the four safe-default exits (absent file, unparseable YAML,
    non-dict root, absent/non-dict block) so ``resolve_review_strategy`` stays
    under the return-count ceiling and reads as a single mode dispatch.
    """
    path = root / PROJECT_CONFIG_RELPATH
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    block = raw.get("review_strategy")
    return block if isinstance(block, dict) else None


def resolve_review_strategy(root: Path) -> ReviewStrategy:
    """Return the review strategy from <root>/.claude/project-config.yaml.

    Degrades to ``ReviewStrategy("ci", None)`` on ANY failure — absent file,
    unparseable YAML, non-dict root, absent/non-dict ``review_strategy`` block,
    a non-string or unrecognized ``mode``. A ``repo_owner``/``reviewer_team``
    mode whose handle is absent or non-string resolves with ``handle=None``
    (never wedges; the act phase corrects, ``cw doctor`` warns).
    """
    block = _load_review_strategy_block(root)
    if block is None:
        return _CI_DEFAULT
    mode = block.get("mode")
    if not isinstance(mode, str) or mode not in _HANDLE_KEY_BY_MODE:
        return _CI_DEFAULT
    handle = block.get(_HANDLE_KEY_BY_MODE[mode])
    return ReviewStrategy(
        cast("ReviewStrategyMode", mode),
        handle if isinstance(handle, str) else None,
    )
