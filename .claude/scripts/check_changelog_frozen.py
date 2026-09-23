#!/usr/bin/env python3
"""CI gate: freeze released CHANGELOG sections against their release tags (#2304).

Usage (CI, `.github/workflows/ci.yml`; local pre-commit hook without the flag):
    python .claude/scripts/check_changelog_frozen.py [--changelog PATH]
        [--json] [--require-tags]

Context: `.gitattributes` puts `CHANGELOG.md` on git's `union` merge driver,
which keeps both sides' lines instead of conflicting. Merges therefore corrupt
the file silently — no conflict marker, no review point: released headings
dropped, entries misfiled into already-released sections, headings duplicated.
This script is the backstop: it compares the working-tree `CHANGELOG.md` with
the copy each release tag captured. It reads no commit subject, PR title, or
event context — only tags, the file, and `pyproject.toml`.

Invariants:
    Rule 1 — tagged sections are frozen. For every `vX.Y.Z` tag reachable from
        HEAD at/after `since_tag`, `## [X.Y.Z]` appears exactly once and its
        section is byte-identical to the one in `git show vX.Y.Z:CHANGELOG.md`
        (trailing blank lines excepted — they separate sections, they are not
        content). Kinds: `missing_heading`, `section_changed`.
    Rule 2 — at most one untagged version heading, and it is the release in
        progress: first heading after `## [Unreleased]`, equal to
        `pyproject.toml`'s `[project].version`. Untagged headings older than
        `since_tag` are accepted history. Kind: `entry_outside_unreleased`.
    Rule 3 — no duplicate `## [...]` heading of any kind. Unscoped by
        `since_tag`: it is a parse of the current file, with no pre-cutoff
        population to grandfather. Kind: `duplicate_heading`.

Configuration: `[tool.cw.changelog_freeze].since_tag` in the repo-root
`pyproject.toml` (a `vX.Y.Z` tag). Absent table or key means beginning of
history — every reachable release tag is enforced, never fail-open. A
malformed `pyproject.toml` or `since_tag` fails the check.

Missing tags: when `since_tag` is not present in the local checkout (tags not
fetched), `--require-tags` fails with the fix (`git fetch --tags`); without it
the same message is printed to stderr as a warning and the check exits 0 with
nothing on stdout. Once `since_tag` resolves, both modes enforce identically.

Exit codes:
    0  — every invariant holds (or: `since_tag` unresolvable, no `--require-tags`)
    1  — at least one violation, or a config/IO/git error (message on stderr,
         nothing on stdout)

JSON verdict (`--json`, stdout, whenever the invariants were evaluated):
    {"ok": bool, "since_tag": str, "violations": [
        {"tag": str, "kind": str, "detail": str}, ...]}
`since_tag` is "" when unset. Without `--json` the verdict is a
human-readable summary.

Stdlib-only, like its `.claude/scripts/` siblings. Deliberately carries no
`cw-script-version` marker: it is a CI/pre-commit invariant over this repo's
own release history, not a script a dispatched worker loads.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

KIND_MISSING = "missing_heading"
KIND_CHANGED = "section_changed"
KIND_OUTSIDE = "entry_outside_unreleased"
KIND_DUPLICATE = "duplicate_heading"

_PREFIX = "check_changelog_frozen"
_HEADING_RE = re.compile(r"^## \[(?P<ver>[^\]]+)\]")
_SECTION_BOUNDARY = "## "
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG_PREFIX = "v"
_UNRELEASED = "Unreleased"
_CONFIG_KEYS = ("tool", "cw", "changelog_freeze")

Version = tuple[int, int, int]


class FreezeCheckError(Exception):
    """A config, IO, or git failure that prevents evaluating the invariants."""


@dataclass(frozen=True)
class Heading:
    version: str
    line: int


@dataclass(frozen=True)
class Violation:
    tag: str
    kind: str
    detail: str


def parse_version(text: str) -> Version | None:
    match = _VERSION_RE.match(text)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def parse_tag(tag: str) -> Version | None:
    if not tag.startswith(_TAG_PREFIX):
        return None
    return parse_version(tag.removeprefix(_TAG_PREFIX))


def label_for(version: str) -> str:
    """The `tag` field for a heading: `vX.Y.Z` when semver, else the raw text."""
    return f"{_TAG_PREFIX}{version}" if parse_version(version) else version


def parse_headings(lines: list[str]) -> list[Heading]:
    headings: list[Heading] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is not None:
            headings.append(Heading(version=match["ver"], line=index))
    return headings


def section_text(lines: list[str], start: int) -> str:
    """The section at *start*, up to the next `## ` line, minus trailing blanks."""
    end = start + 1
    while end < len(lines) and not lines[end].startswith(_SECTION_BOUNDARY):
        end += 1
    body = lines[start:end]
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_root(changelog: Path) -> Path:
    result = _git(changelog.parent, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        message = f"{changelog} is not inside a git repository: {result.stderr.strip()}"
        raise FreezeCheckError(message)
    return Path(result.stdout.strip())


def _reachable_tags(root: Path) -> dict[str, Version]:
    result = _git(root, "tag", "--merged", "HEAD")
    if result.returncode != 0:
        message = f"git tag --merged HEAD failed: {result.stderr.strip()}"
        raise FreezeCheckError(message)
    tags: dict[str, Version] = {}
    for tag in result.stdout.split():
        version = parse_tag(tag)
        if version is not None:
            tags[tag] = version
    return tags


def _tag_exists(root: Path, tag: str) -> bool:
    return (
        _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}").returncode
        == 0
    )


def _load_config(root: Path) -> tuple[str, str | None]:
    """Return (`since_tag` or "", `[project].version` or None) from pyproject.toml."""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return "", None
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        message = f"could not parse {pyproject}: {exc}"
        raise FreezeCheckError(message) from exc

    project = data.get("project", {})
    raw_version = project.get("version") if isinstance(project, dict) else None
    version = raw_version if isinstance(raw_version, str) else None

    section: object = data
    for key in _CONFIG_KEYS:
        section = section.get(key, {}) if isinstance(section, dict) else {}
    since_tag = section.get("since_tag", "") if isinstance(section, dict) else ""
    if not isinstance(since_tag, str) or (since_tag and parse_tag(since_tag) is None):
        message = (
            f"[tool.cw.changelog_freeze].since_tag in {pyproject} must be a"
            f" vX.Y.Z release tag, got {since_tag!r}"
        )
        raise FreezeCheckError(message)
    return since_tag, version


def _missing_tag_message(since_tag: str) -> str:
    return (
        f"since_tag {since_tag!r} ([tool.cw.changelog_freeze] in pyproject.toml)"
        " does not exist in this checkout, so released sections cannot be"
        " compared against their tags. Fix: run `git fetch --tags` (in CI, set"
        " `fetch-tags: true` on actions/checkout)."
    )


def check_frozen_sections(
    lines: list[str],
    headings: list[Heading],
    tags: dict[str, Version],
    tag_changelog: dict[str, str | None],
) -> list[Violation]:
    """Rule 1. *tags* is already cutoff-scoped; *tag_changelog* maps tag → file text."""
    violations: list[Violation] = []
    for tag in sorted(tags, key=tags.__getitem__):
        version = tag.removeprefix(_TAG_PREFIX)
        matching = [h for h in headings if h.version == version]
        if not matching:
            violations.append(
                Violation(
                    tag, KIND_MISSING, f"no `## [{version}]` heading in CHANGELOG.md"
                )
            )
            continue
        if len(matching) > 1:
            continue  # Rule 3 reports it; a body compare would be ambiguous.
        tag_lines = (tag_changelog.get(tag) or "").splitlines()
        tag_heads = [h for h in parse_headings(tag_lines) if h.version == version]
        if not tag_heads:
            detail = f"{tag}'s own CHANGELOG.md has no `## [{version}]` section"
            violations.append(Violation(tag, KIND_CHANGED, detail))
            continue
        if section_text(lines, matching[0].line) != section_text(
            tag_lines, tag_heads[0].line
        ):
            detail = (
                f"`## [{version}]` (line {matching[0].line + 1}) differs from the"
                f" section released in {tag}; new entries go under [{_UNRELEASED}]"
            )
            violations.append(Violation(tag, KIND_CHANGED, detail))
    return violations


def check_untagged_headings(
    headings: list[Heading],
    tagged_versions: set[str],
    cutoff: Version | None,
    project_version: str | None,
) -> list[Violation]:
    """Rule 2: the only untagged version heading is the release in progress."""
    unreleased = [i for i, h in enumerate(headings) if h.version == _UNRELEASED]
    expected_index = unreleased[0] + 1 if unreleased else 0
    untagged: list[tuple[int, Heading]] = []
    for index, heading in enumerate(headings):
        if heading.version == _UNRELEASED or heading.version in tagged_versions:
            continue
        parsed = parse_version(heading.version)
        if cutoff is not None and parsed is not None and parsed < cutoff:
            continue
        untagged.append((index, heading))

    violations: list[Violation] = []
    for index, heading in untagged:
        reasons: list[str] = []
        if len(untagged) > 1:
            reasons.append(f"more than one untagged version heading ({len(untagged)})")
        if index != expected_index:
            reasons.append(f"not the first heading after [{_UNRELEASED}]")
        if heading.version != project_version:
            reasons.append(
                f"does not match pyproject.toml [project].version ({project_version!r})"
            )
        if reasons:
            detail = (
                f"untagged `## [{heading.version}]` (line {heading.line + 1}): "
                + "; ".join(reasons)
            )
            violations.append(
                Violation(label_for(heading.version), KIND_OUTSIDE, detail)
            )
    return violations


def check_duplicate_headings(headings: list[Heading]) -> list[Violation]:
    """Rule 3: every `## [...]` heading appears at most once."""
    counts = Counter(h.version for h in headings)
    violations: list[Violation] = []
    for version, count in counts.items():
        if count > 1:
            lines = ", ".join(str(h.line + 1) for h in headings if h.version == version)
            detail = f"`## [{version}]` appears {count} times (lines {lines})"
            violations.append(Violation(label_for(version), KIND_DUPLICATE, detail))
    return violations


def _tag_changelogs(
    root: Path, rel_path: str, tags: dict[str, Version]
) -> dict[str, str | None]:
    texts: dict[str, str | None] = {}
    for tag in tags:
        result = _git(root, "show", f"{tag}:{rel_path}")
        texts[tag] = result.stdout if result.returncode == 0 else None
    return texts


def evaluate(
    changelog: Path, root: Path, since_tag: str, project_version: str | None
) -> list[Violation]:
    try:
        text = changelog.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        message = f"could not read {changelog}: {exc}"
        raise FreezeCheckError(message) from exc
    lines = text.splitlines()
    headings = parse_headings(lines)

    cutoff = parse_tag(since_tag) if since_tag else None
    reachable = _reachable_tags(root)
    in_scope = {
        tag: version
        for tag, version in reachable.items()
        if cutoff is None or version >= cutoff
    }
    rel_path = changelog.resolve().relative_to(root.resolve()).as_posix()
    return [
        *check_frozen_sections(
            lines, headings, in_scope, _tag_changelogs(root, rel_path, in_scope)
        ),
        *check_untagged_headings(
            headings,
            {tag.removeprefix(_TAG_PREFIX) for tag in reachable},
            cutoff,
            project_version,
        ),
        *check_duplicate_headings(headings),
    ]


def _summarize(violations: list[Violation], since_tag: str) -> str:
    scope = since_tag or "the beginning of history"
    if not violations:
        return f"{_PREFIX}: OK — released CHANGELOG sections frozen since {scope}"
    rows = [f"  [{v.kind}] {v.tag}: {v.detail}" for v in violations]
    return "\n".join(
        [f"{_PREFIX}: {len(violations)} violation(s) (since {scope}):", *rows]
    )


def _emit(payload: dict[str, object], summary: str, *, as_json: bool) -> None:
    print(json.dumps(payload, indent=2) if as_json else summary)


def run(args: argparse.Namespace) -> int:
    changelog = Path(args.changelog)
    root = _repo_root(changelog)
    since_tag, project_version = _load_config(root)
    if since_tag and not _tag_exists(root, since_tag):
        message = _missing_tag_message(since_tag)
        if args.require_tags:
            print(f"{_PREFIX}: {message}", file=sys.stderr)
            return 1
        print(f"{_PREFIX}: WARNING: {message} Skipping the check.", file=sys.stderr)
        return 0

    violations = evaluate(changelog, root, since_tag, project_version)
    payload: dict[str, object] = {
        "ok": not violations,
        "since_tag": since_tag,
        "violations": [asdict(v) for v in violations],
    }
    _emit(payload, _summarize(violations, since_tag), as_json=args.json)
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (frozen) or 1 (violation or error)."""
    parser = argparse.ArgumentParser(
        description="Assert released CHANGELOG sections match their release tags.",
    )
    parser.add_argument(
        "--changelog", default="CHANGELOG.md", help="Path to CHANGELOG.md"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the JSON verdict on stdout"
    )
    parser.add_argument(
        "--require-tags",
        action="store_true",
        help="Fail (instead of warn and pass) when since_tag is not fetched",
    )
    args = parser.parse_args(argv)
    try:
        return run(args)
    except FreezeCheckError as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
