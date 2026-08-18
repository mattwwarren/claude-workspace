"""Parse the plan's ``## Files Modified`` manifest (#1905).

LocalExecutor pre-flight uses this to hand aider an explicit ``--file`` list
instead of letting aider's own path-mention heuristic decide which files enter
the chat (``Coder.check_for_file_mentions``). Per #1881, ``## Files Modified``
is the plan's *complete* file inventory — every test file and mechanical
companion, not a source-only subset — so it is the right manifest for that.

MIRRORED, NOT SHARED. ``.claude/scripts/check_plan_scope_conformance.py``
(``_parse_files_modified`` and its ``_extract_path_token`` / ``_looks_like_path``
helpers) implements the identical algorithm and is the origin of this copy. It
cannot be imported from here: ``.claude/scripts/`` is excluded from the ``cw``
wheel (``pyproject.toml``'s ``packages = ["src/cw"]``) and must stay a
dependency-free stdlib script so the gate runs in client repos that do not have
``cw`` installed. That script already establishes this exact idiom in reverse —
its ``_load_scope_thresholds`` re-implements rather than imports
``cw.codex_review._context``'s fallback-gate loader for the same reason.
**The two copies must be kept in lockstep by hand**; ``tests/test_plan_files.py``
and ``tests/test_check_plan_scope_conformance.py`` assert against a shared
fixture builder (``tests/conftest.py::_plan_text``) so drift shows up as a
failing test in one suite but not the other.
"""

from __future__ import annotations

# The canonical heading is "## Files Modified", but real plans have used
# variant wording (e.g. "## Files touched, with estimated line deltas", #1784),
# so the match is on the prefix, not the full literal.
_FILES_HEADING_PREFIX = "## Files"

# A bullet's or table cell's first token counts as a path only if it looks like
# one. This is what lets prose bullets ("- Note that nothing else is touched")
# and table header/separator rows ("| File |", "|---|---|") sit under the
# heading without being counted as files.
_PATH_MARKERS = ("/", ".")
_STRIP_CHARS = "`*_'\",;:()[]"

# A markdown table row needs at least a leading and a trailing pipe before its
# first cell can be read as a path. Named here (the mirrored script inlines the
# literal) because src/ is linted under PLR2004, which forbids the magic value.
_MIN_TABLE_PIPES = 2


def _looks_like_path(token: str) -> bool:
    """Return True if *token* is plausibly a repo-relative file path."""
    if not token or token in {".", "..", "/"}:
        return False
    if any(ch.isspace() for ch in token):
        return False
    return any(marker in token for marker in _PATH_MARKERS)


def _extract_path_token(text: str) -> str | None:
    """Return *text*'s first whitespace-delimited token if it looks like a path.

    List/emphasis/table decoration is stripped first, so ``- `a/b.py` `` and
    ``- **a/b.py**`` both yield ``a/b.py``.
    """
    token = text.strip().lstrip("*_ ").split(maxsplit=1)[0:1]
    if not token:
        return None
    candidate = token[0].strip(_STRIP_CHARS)
    return candidate if _looks_like_path(candidate) else None


def parse_plan_files_modified(plan_text: str) -> list[str]:
    """Extract the file paths listed under the plan's files-modified section.

    The heading is matched by prefix (``_FILES_HEADING_PREFIX``). Within the
    section body, paths may be carried either as bullets (``- path`` /
    ``* path``) or as the first cell of a markdown table row (``| path | ... |``)
    — both shapes occur in real plans (#1779, #1784). Returns them in document
    order, de-duplicated.

    Returns an empty list when no matching heading is found. Unlike the gate
    script's caller (which treats that as a parse error), LocalExecutor treats
    an empty manifest as "emit no ``--file`` flags" — i.e. it falls back to the
    pre-#1905 behaviour of letting aider pick files itself, rather than
    hard-blocking a plan that predates the manifest convention.
    """
    lines = plan_text.splitlines()
    try:
        start = next(
            i
            for i, line in enumerate(lines)
            if line.strip().startswith(_FILES_HEADING_PREFIX)
        )
    except StopIteration:
        return []

    paths: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("####"):
            break  # next section — the Files Modified body has ended
        candidate: str | None
        if stripped.startswith(("- ", "* ")):
            candidate = _extract_path_token(stripped[2:])
        elif stripped.startswith("|") and stripped.count("|") >= _MIN_TABLE_PIPES:
            cells = stripped.split("|")
            candidate = _extract_path_token(cells[1]) if len(cells) > 1 else None
        else:
            continue
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths
