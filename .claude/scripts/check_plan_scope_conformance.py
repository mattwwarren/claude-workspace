#!/usr/bin/env python3
"""Gate script: compare a delivered diff's file set against the plan's (#1779).

Usage (from `/auto-dev` Stage 2.5, gate check 2):
    python .claude/scripts/check_plan_scope_conformance.py \\
        --plan .cw/plan.md --touched-files /tmp/touched_files-$CW_SESSION

Context: nothing in the pipeline used to measure the delivered diff against the
approved plan's file enumeration. Step 2.5 gate 2 computed the touched-file set
and then only ever appended a non-blocking ``impl_scope_growth`` friction note,
so a branch could balloon 2-3x past its plan and still reach the reviewers —
where the fix loop could not converge because the diff it was fixing was no
longer the diff anyone had approved.

This script is the mechanical measurement that gate was missing. It is
deliberately *proportional*, not absolute: an implementation that names one
extra call site or one extra test file is ordinary and must not block. Only
growth that outruns both a ratio of the plan's own size and a small absolute
floor is reported as drift.

Both thresholds are overridable per-repo via ``[tool.cw.scope_conformance]`` in
the repo's ``pyproject.toml`` (``ratio``, ``abs_floor``), so retuning a noisy
repo never requires editing this shared script. The override read fails **open**
to the shipped defaults on every error path — a repo whose ``pyproject.toml``
cannot be parsed must not silently change gate behavior.

Exit codes:
    0  — conforming (no extra files, or extras within the allowance)
    1  — DRIFT: extra files exceed the allowance; Step 2.5 exits ``blocked``
         with ``blocker.reason: "plan_scope_drift"``
    2  — usage / parse error (unreadable input, or no parseable files-modified
         section — heading matched by prefix, e.g. ``## Files Modified``;
         paths carried as either bullets or markdown-table rows). Does NOT
         block — same fail-open convention as ``check_not_main_checkout.py``'s
         exit 2.

The JSON verdict is written to stdout on exits 0 and 1:
    {"triggered": bool, "extra_files": [...], "allowed_extra": int,
     "plan_file_count": int, "delivered_file_count": int}

``extra_files`` is sorted, and is the *entire* operator-authorization surface:
Step 2.5 copies it verbatim into ``blocker.details`` so an operator deciding
whether the growth was legitimate can see exactly which paths were unplanned.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

# Threshold constants (v1, #1779). Same shape as prep_pr_state.py's sibling
# SCOPE_*_THRESHOLD constants, and tuned against the incident that motivated
# this gate: a 14-file plan that delivered 31 files must trip, while the same
# plan delivering 16 files must not.
SCOPE_DRIFT_RATIO = 1.5  # v1: allow up to 50% more files than the plan named
SCOPE_DRIFT_ABS_FLOOR = 5  # v1: always allow at least 5 extra files (covers a
# missed call site / one extra test file on a tiny plan, where a pure ratio
# would produce false positives at almost any growth)

_FILES_MODIFIED_HEADING = "## Files Modified"
# The canonical/example heading, used verbatim in messages and quoted in
# auto-dev-plan.md's Step 1b producer instruction. The matcher below is
# deliberately more tolerant than this literal — real plans have used
# variant wording (e.g. "## Files touched, with estimated line deltas",
# #1784) — so parsing tests against the prefix, not this exact string.
_FILES_HEADING_PREFIX = "## Files"

# A bullet's or table cell's first token is treated as a path only if it
# looks like one. This is what lets prose bullets ("- Note that nothing else
# is touched") and table header/separator rows ("| File |", "|---|---|") sit
# under the heading without being counted as files.
_PATH_MARKERS = ("/", ".")
_STRIP_CHARS = "`*_'\",;:()[]"


def _extract_path_token(text: str) -> str | None:
    """Return the first whitespace-delimited token in *text* if it looks
    like a file path, after stripping list/emphasis/table decoration."""
    token = text.strip().lstrip("*_ ").split(maxsplit=1)[0:1]
    if not token:
        return None
    candidate = token[0].strip(_STRIP_CHARS)
    return candidate if _looks_like_path(candidate) else None


def _looks_like_path(token: str) -> bool:
    """Return True if *token* is plausibly a repo-relative file path."""
    if not token or token in {".", "..", "/"}:
        return False
    if any(ch.isspace() for ch in token):
        return False
    return any(marker in token for marker in _PATH_MARKERS)


def _parse_files_modified(plan_text: str) -> list[str]:
    """Extract the file paths listed under the plan's files-modified section.

    The section heading is matched by prefix (``_FILES_HEADING_PREFIX``, e.g.
    ``"## Files Modified"`` or the real-world variant ``"## Files touched,
    with estimated line deltas"``), not by exact wording. Within the section
    body, paths may be carried either as bullets (``- path`` / ``* path``) or
    as the first cell of a markdown table row (``| path | ... |``) — both
    real plans observed in the wild (#1779, #1784) have used one or the
    other. Returns them in document order, de-duplicated.

    Returns an empty list when no matching heading is found — the caller
    treats that as a parse error (exit 2) rather than as "the plan named zero
    files", because an empty baseline would make every delivered file look
    unplanned.
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
        elif stripped.startswith("|") and stripped.count("|") >= 2:
            cells = stripped.split("|")
            candidate = _extract_path_token(cells[1]) if len(cells) > 1 else None
        else:
            continue
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def _find_pyproject(start: Path) -> Path | None:
    """Search upward from *start* for a ``pyproject.toml``."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file():
            return pyproject
    return None


def _load_scope_thresholds(plan_path: Path) -> tuple[float, int]:
    """Read ``[tool.cw.scope_conformance]`` overrides near *plan_path*.

    Mirrors ``_load_agent_spec_fallback_gate``'s ``tomllib.load`` + fail-safe
    idiom (``src/cw/codex_review/_context.py``), re-implemented locally rather
    than imported: nothing in ``.claude/scripts/`` depends on the ``cw``
    package, and this gate must keep working as a standalone stdlib script.

    A missing file, missing table, missing key, wrong-typed value, or malformed
    TOML all fall back to the shipped default for the affected value only.
    """
    pyproject = _find_pyproject(plan_path.parent)
    if pyproject is None:
        return SCOPE_DRIFT_RATIO, SCOPE_DRIFT_ABS_FLOOR
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError, OSError):
        return SCOPE_DRIFT_RATIO, SCOPE_DRIFT_ABS_FLOOR

    section: object = data
    for key in ("tool", "cw", "scope_conformance"):
        section = section.get(key, {}) if isinstance(section, dict) else {}
    if not isinstance(section, dict):
        return SCOPE_DRIFT_RATIO, SCOPE_DRIFT_ABS_FLOOR

    raw_ratio = section.get("ratio", SCOPE_DRIFT_RATIO)
    # bool is an int subclass; an explicit `ratio = true` is a wrong type, not 1.
    ratio = (
        float(raw_ratio)
        if isinstance(raw_ratio, (int, float)) and not isinstance(raw_ratio, bool)
        else SCOPE_DRIFT_RATIO
    )
    raw_floor = section.get("abs_floor", SCOPE_DRIFT_ABS_FLOOR)
    abs_floor = (
        raw_floor
        if isinstance(raw_floor, int) and not isinstance(raw_floor, bool)
        else SCOPE_DRIFT_ABS_FLOOR
    )
    return ratio, abs_floor


def check_scope_conformance(
    plan_files: list[str],
    touched_files: list[str],
    ratio: float,
    abs_floor: int,
) -> dict[str, object]:
    """Compare the delivered file set against the plan's enumeration.

    ``extra_files`` is delivered-but-not-planned only. Planned files *missing*
    from the diff are deliberately not counted here — that is Step 2.5's
    separate "missing work" branch, and folding it in would let an under-
    delivering run masquerade as scope drift.
    """
    plan_set = set(plan_files)
    touched_set = set(touched_files)
    extra_files = sorted(touched_set - plan_set)
    allowed_extra = max(abs_floor, round(len(plan_set) * (ratio - 1)))
    return {
        "triggered": len(extra_files) > allowed_extra,
        "extra_files": extra_files,
        "allowed_extra": allowed_extra,
        "plan_file_count": len(plan_set),
        "delivered_file_count": len(touched_set),
    }


def _read_text(path: Path, label: str) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(
            f"check_plan_scope_conformance: could not read {label} {path}: {exc}",
            file=sys.stderr,
        )
        return None


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (conforming), 1 (drift), or 2 (usage/parse)."""
    parser = argparse.ArgumentParser(
        description="Compare a delivered diff's file set against the plan's.",
    )
    parser.add_argument("--plan", required=True, help="Path to .cw/plan.md")
    parser.add_argument(
        "--touched-files",
        required=True,
        help="Path to a newline-delimited list of touched files (git diff --name-only)",
    )
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    touched_path = Path(args.touched_files)

    plan_text = _read_text(plan_path, "--plan")
    if plan_text is None:
        return 2
    touched_text = _read_text(touched_path, "--touched-files")
    if touched_text is None:
        return 2

    plan_files = _parse_files_modified(plan_text)
    if not plan_files:
        print(
            "check_plan_scope_conformance: no parseable file-list section"
            f" (heading must begin with '{_FILES_HEADING_PREFIX}', e.g."
            f" '{_FILES_MODIFIED_HEADING}') in {plan_path} — cannot measure"
            " scope conformance. Treating as a parse error (non-blocking);"
            " see auto-dev-plan.md Step 1b for the required plan section"
            " format.",
            file=sys.stderr,
        )
        return 2

    touched_files = [line.strip() for line in touched_text.splitlines() if line.strip()]

    ratio, abs_floor = _load_scope_thresholds(plan_path)
    verdict = check_scope_conformance(plan_files, touched_files, ratio, abs_floor)
    print(json.dumps(verdict, indent=2))
    return 1 if verdict["triggered"] else 0


if __name__ == "__main__":
    sys.exit(main())
