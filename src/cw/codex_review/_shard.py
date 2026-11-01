"""Diff sharding for a review round over the backend's prompt limit (#2864).

A confirm round inlines the whole branch diff into every reviewer prompt. When
the longest role prompt is over the backend's limit, the diff is split by file
into shards that each fit, every role reviews every shard, and the shard
verdicts are merged (:mod:`cw.codex_review._shard_run`). Nothing is ever
truncated: a single file that cannot fit a prompt on its own fails loud with
:class:`ReviewInputTooLargeError` before any codex call is spent.

The arithmetic is exact. ``_build_reviewer_prompt`` reads only the diff text
and the changed-file list, so a role's prompt over a shard is its empty-diff
prompt plus ``len("\\n".join(files))`` plus the shard's diff text. Every shard
prompt is still rendered and measured before the round runs.

Pure functions only; the planner never runs codex.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw.codex_review._diff import _DIFF_GIT_HEADER_RE
from cw.exceptions import CwError
from cw.review_findings import CapturedDiff

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cw.codex_review._context import _ReviewPassInputs

#: The two exit-4 causes ``cw review confirm`` reports for a prompt that cannot
#: be made to fit (#2864).
InputTooLargeCause = Literal["input_too_large", "file_too_large"]
INPUT_TOO_LARGE_CAUSE: InputTooLargeCause = "input_too_large"
FILE_TOO_LARGE_CAUSE: InputTooLargeCause = "file_too_large"

# Splits a unified diff just before every ``diff --git`` line, keeping it.
# ``re.split`` on a lookahead (not ``splitlines``, which also breaks on
# ``\x0c`` and U+2028 inside diff content) is lossless: the pieces concatenate
# back to the original text.
_FILE_HEADER_SPLIT = re.compile(r"(?m)^(?=diff --git )")
_HEADER_PREFIX = "diff --git "


class ReviewInputTooLargeError(CwError):
    """A review prompt cannot be made to fit the backend's limit (#2864).

    ``file`` set means one changed file alone does not fit
    (``file_too_large``); ``file`` unset means the prompt overhead, or a
    rendered shard prompt, is over the limit (``input_too_large``).
    ``actual_chars`` and ``max_chars`` are ``len(str)`` counts. ``str(exc)``
    is the detail ``cw review confirm`` prints.
    """

    __slots__ = ("actual_chars", "cause", "file", "max_chars")

    def __init__(
        self, *, actual_chars: int, max_chars: int, file: str | None = None
    ) -> None:
        sizes = f"actual_chars={actual_chars} max_chars={max_chars}"
        if file is None:
            message = f"review prompt exceeds the limit: {sizes}"
        else:
            message = f"{file}: one changed file does not fit a review prompt: {sizes}"
        super().__init__(message)
        self.actual_chars = actual_chars
        self.max_chars = max_chars
        self.file = file
        self.cause: InputTooLargeCause = (
            INPUT_TOO_LARGE_CAUSE if file is None else FILE_TOO_LARGE_CAUSE
        )

    def payload(self) -> dict[str, object]:
        """The exit-4 JSON keys this failure adds: sizes, and ``file`` if set."""
        extra: dict[str, object] = {
            "actual_chars": self.actual_chars,
            "max_chars": self.max_chars,
        }
        if self.file is not None:
            extra["file"] = self.file
        return extra


@dataclass(frozen=True, slots=True)
class _ShardPlan:
    """One planned shard: its files, diff text and every role's prompt.

    ``index`` is 1-based. ``prompt_chars`` is the longest of ``prompts_by_role``.
    """

    index: int
    files: tuple[str, ...]
    diff_text: str
    prompts_by_role: dict[str, str]
    prompt_chars: int


def _chunk_key(chunk: str) -> str:
    """The changed-file path a ``diff --git`` chunk is keyed by.

    The ``b/`` path of :data:`_DIFF_GIT_HEADER_RE`, the same key the changed
    file list uses (a rename is its new path, a deletion its old one). A
    header the pattern does not match (a quoted path) keeps its raw text, so
    the chunk is still covered rather than dropped.
    """
    header = chunk.split("\n", 1)[0]
    match = _DIFF_GIT_HEADER_RE.match(header)
    return match.group(1) if match else header.removeprefix(_HEADER_PREFIX)


def split_diff_by_file(diff_text: str) -> dict[str, str]:
    """Split *diff_text* into per-file chunks, keyed by path, in diff order.

    Lossless: ``"".join(result.values()) == diff_text``. A path git emits
    twice in a row (a type change is a deletion plus an addition) is one
    entry holding both chunks. Text before the first header (none in git's
    output) is kept on the first chunk, or under ``""`` with no header at all.
    """
    preamble, *pieces = _FILE_HEADER_SPLIT.split(diff_text)
    chunks: dict[str, str] = {}
    for piece in pieces:
        key = _chunk_key(piece)
        chunks[key] = chunks.get(key, "") + piece
    if preamble:
        first = next(iter(chunks), "")
        chunks[first] = preamble + chunks.get(first, "")
    return chunks


def _item_cost(path: str, chunk: str) -> int:
    """A file's share of a shard prompt: its chunk plus its file-list line.

    Conservative by at most one character per shard, since the file list has
    one fewer separator than files.
    """
    return len(chunk) + len(path) + 1


def _group_by_directory(paths: Sequence[str]) -> list[list[str]]:
    """*paths* grouped by directory, groups sorted by directory, files sorted."""
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(posixpath.dirname(path), []).append(path)
    return [sorted(groups[directory]) for directory in sorted(groups)]


def _pack_groups(
    groups: Sequence[Sequence[str]], costs: Mapping[str, int], budget: int
) -> list[list[str]]:
    """Next-fit the directory groups into shards of at most *budget* cost.

    A directory that fits goes whole into the current shard, else the shard is
    closed and the directory starts the next one. A directory larger than
    *budget* is placed file by file, next-fit. Every file is assumed to fit on
    its own (the caller checked).
    """
    shards: list[list[str]] = []
    current: list[str] = []
    used = 0
    for group in groups:
        group_cost = sum(costs[path] for path in group)
        units = [list(group)] if group_cost <= budget else [[path] for path in group]
        for unit in units:
            unit_cost = sum(costs[path] for path in unit)
            if current and used + unit_cost > budget:
                shards.append(current)
                current, used = [], 0
            current.extend(unit)
            used += unit_cost
    if current:
        shards.append(current)
    return shards


def _check_exact_cover(shards: Sequence[Sequence[str]], paths: Sequence[str]) -> None:
    """Raise ``ValueError`` unless every path is in exactly one shard.

    An internal fault, never a size failure: the planner dropped or
    duplicated a file, and a clean verdict over it would not cover the diff.
    """
    planned = sorted(path for shard in shards for path in shard)
    if planned != sorted(paths):
        msg = (
            f"shard plan does not cover the diff exactly once: planned "
            f"{len(planned)} file(s) for {len(paths)} changed file(s)"
        )
        raise ValueError(msg)


def plan_shards(
    chunks: Mapping[str, str], *, overhead: int, max_chars: int
) -> list[list[str]]:
    """Group the files of *chunks* into shards that each fit *max_chars*.

    *overhead* is the longest role prompt over an empty diff, so each shard's
    file cost may use ``max_chars - overhead``. Raises
    :class:`ReviewInputTooLargeError` when nothing fits (``input_too_large``)
    or one file alone does not (``file_too_large``, never truncated), and
    ``ValueError`` if the plan does not cover every file exactly once.
    """
    budget = max_chars - overhead
    if budget <= 0:
        raise ReviewInputTooLargeError(actual_chars=overhead, max_chars=max_chars)
    costs = {path: _item_cost(path, chunk) for path, chunk in chunks.items()}
    for path, cost in costs.items():
        if cost > budget:
            raise ReviewInputTooLargeError(
                actual_chars=overhead + cost, max_chars=max_chars, file=path
            )
    paths = list(chunks)
    shards = _pack_groups(_group_by_directory(paths), costs, budget)
    _check_exact_cover(shards, paths)
    return shards


def _render_shard(
    index: int,
    files: Sequence[str],
    chunks: Mapping[str, str],
    prepared: _ReviewPassInputs,
) -> _ShardPlan:
    """Render every role's prompt over one shard of the diff."""
    if prepared.render_prompts is None:
        msg = "the review pass carries no prompt renderer to shard with"
        raise ValueError(msg)
    diff_text = "".join(chunks[path] for path in files)
    prompts = prepared.render_prompts(CapturedDiff(text=diff_text), files)
    return _ShardPlan(
        index=index,
        files=tuple(files),
        diff_text=diff_text,
        prompts_by_role=prompts,
        prompt_chars=max((len(prompt) for prompt in prompts.values()), default=0),
    )


def plan_prompt_shards(prepared: _ReviewPassInputs, max_chars: int) -> list[_ShardPlan]:
    """The shards of *prepared*'s diff under *max_chars*, or ``[]`` if it fits.

    ``[]`` means the longest role prompt is within the limit and the round
    takes the unsharded path with its prompts unchanged. Otherwise every shard
    prompt of every role is rendered and checked against *max_chars* before
    any codex call; one still over it raises :class:`ReviewInputTooLargeError`
    (``input_too_large``, the longest rendered shard prompt as the size).
    """
    longest = max((len(p) for p in prepared.prompts_by_role.values()), default=0)
    if longest <= max_chars:
        return []
    empty = _render_shard(0, (), {}, prepared)
    chunks = split_diff_by_file(prepared.diff.text)
    groups = plan_shards(chunks, overhead=empty.prompt_chars, max_chars=max_chars)
    plans = [
        _render_shard(index, files, chunks, prepared)
        for index, files in enumerate(groups, start=1)
    ]
    longest_shard = max((plan.prompt_chars for plan in plans), default=0)
    if longest_shard > max_chars:
        raise ReviewInputTooLargeError(actual_chars=longest_shard, max_chars=max_chars)
    return plans
