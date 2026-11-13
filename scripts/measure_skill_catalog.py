"""Decompose the skills catalog a Claude Code worker is offered (#1834).

Run: uv run python scripts/measure_skill_catalog.py --repo "$PWD" \
    [--home ~] [--transcript PATH | --cwd "$PWD"] [--debug-log PATH] [--json]

Read-only and offline: it never launches ``claude``, makes no model call and
touches no network. It reports two things, using counts, characters and bucket
names only (no absolute paths are printed):

* demand: the skills and commands the repo and the user home would offer,
  enumerated from their frontmatter;
* delivery: the ``skill_listing`` attachment the harness wrote into a real
  worker transcript (``--transcript``, or the newest transcript for ``--cwd``).

The transcript attachment and the budget formula are internal, version-pinned
details of Claude Code 2.1.322, not a documented contract. The tool fails soft
with a clear message when the attachment is absent.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

DESC_CAP = 1536
CHARS_PER_TOKEN = 4
DEFAULT_WINDOW = 200_000
DEFAULT_FRACTION = 0.01
GAP_TOKENS = 10_217
ENTRY_OVERHEAD = 4  # "- " + ": "
NAME_ONLY_OVERHEAD = 2  # "- "
PERCENT = 100
REPO_SKILL = "repo-skill"
REPO_COMMAND = "repo-command"
GLOBAL_CLAUDE = "global-claude"
BUNDLED_OTHER = "bundled/other"
NAMESPACE_PREFIX = "ns:"
ATTACHMENT_KEY = "attachment"
ATTACHMENT_TYPE = "skill_listing"
TYPE_KEY = "type"
CONTENT_KEY = "content"
SKILL_COUNT_KEY = "skillCount"
IS_INITIAL_KEY = "isInitial"
VERSION_CAVEAT = "internal, version-pinned (Claude Code 2.1.322)"
ESTIMATE_CAVEAT = "chars/4 is an uncalibrated estimate"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)
# A name-only entry may carry a trailing alias such as "- a:b (a:c)".
_ENTRY = re.compile(r"- (\S+?)(?:: (.*)| .*)?")
_WARN = re.compile(
    r"Skill listing over budget: (\d+) skills, (\d+) chars > (\d+) budget"
)


@dataclass(frozen=True)
class Entry:
    """One parsed entry of the delivered listing."""

    name: str
    full: bool  # True iff "- name: description"; False for "- name"
    chars: int  # entry text incl. continuation lines, excl. the entry separator


@dataclass(frozen=True)
class DemandEntry:
    """One skill or command the repo and home would offer."""

    name: str
    source: str
    desc_len: int  # description length after DESC_CAP; 0 when absent
    chars: int


@dataclass(frozen=True)
class KnownNames:
    """Disjoint name sets derived from a deduplicated demand list."""

    repo_skills: frozenset[str]
    repo_commands: frozenset[str]
    global_names: frozenset[str]


@dataclass(frozen=True)
class RawListing:
    """The delivered listing text and the attachment's own skill count."""

    content: str
    skill_count: int


@dataclass(frozen=True)
class SourceStats:
    """Per-bucket totals, shared by the delivered and the demand views."""

    entries: int
    name_only: int
    chars: int
    share_pct: float


@dataclass(frozen=True)
class BudgetObserved:
    """Figures parsed from an 'over budget' WARN line in a debug log."""

    skills: int
    demand_chars: int
    budget_chars: int


@dataclass(frozen=True)
class GapComponents:
    """Sizes of the fixed-baseline components; the field names are JSON keys."""

    global_claude_md_chars: int
    claude_local_md_chars: int
    rules_chars: int
    listing_chars: int | None
    est_tokens_context_files: int
    est_tokens_listing: int | None
    gap_tokens: int


@dataclass(frozen=True)
class Report:
    """The full measurement; the field names are the --json top-level keys."""

    delivered_chars: int | None
    delivered_skill_count: int | None
    delivered_entries: int | None
    budget_observed: BudgetObserved | None
    budget_formula_chars: int
    by_source: dict[str, SourceStats] | None
    demand_by_source: dict[str, SourceStats]
    demand_total_chars: int
    gap_components: GapComponents


def parse_frontmatter(text: str) -> Mapping[str, object]:
    """Return the YAML frontmatter mapping, or ``{}`` if absent or unusable."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return {}
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def description_of(meta: Mapping[str, object]) -> str:
    """Return the frontmatter description, or ``""`` if missing or not a string."""
    value = meta.get("description")
    return value if isinstance(value, str) else ""


def demand_entry(name: str, source: str, description: str) -> DemandEntry:
    """Build one demand entry, applying the per-description cap."""
    desc_len = min(len(description), DESC_CAP)
    overhead = ENTRY_OVERHEAD if desc_len else NAME_ONLY_OVERHEAD
    return DemandEntry(name, source, desc_len, len(name) + overhead + desc_len)


def demand_total_chars(entries: Sequence[DemandEntry]) -> int:
    """Total listing size: entry chars plus one newline between entries."""
    if not entries:
        return 0
    return sum(e.chars for e in entries) + len(entries) - 1


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_demand(path: Path) -> str:
    """Read a demand entry; an unreadable file exits by name, never by absolute path."""
    try:
        return _read(path)
    except OSError:
        msg = f"cannot read {path.parent.name}/{path.name}"
        raise SystemExit(msg) from None


def _skills(claude: Path, source: str) -> list[DemandEntry]:
    # Dangling symlinks and directories are not offerings; skipping them matches
    # _chars_of's is_file() check (#3414).
    return [
        demand_entry(
            p.parent.name, source, description_of(parse_frontmatter(_read_demand(p)))
        )
        for p in sorted(claude.glob("skills/*/SKILL.md"))
        if p.is_file()
    ]


def _commands(claude: Path, source: str) -> list[DemandEntry]:
    return [
        demand_entry(p.stem, source, description_of(parse_frontmatter(_read_demand(p))))
        for p in sorted(claude.glob("commands/*.md"))
        if p.is_file()
    ]


def enumerate_demand(repo: Path, home: Path) -> tuple[DemandEntry, ...]:
    """List what repo and home offer; repo skill > repo command > global wins."""
    repo_claude, home_claude = repo / ".claude", home / ".claude"
    ordered = [
        *_skills(repo_claude, REPO_SKILL),
        *_commands(repo_claude, REPO_COMMAND),
        *_skills(home_claude, GLOBAL_CLAUDE),
        *_commands(home_claude, GLOBAL_CLAUDE),
    ]
    seen: dict[str, DemandEntry] = {}
    for entry in ordered:
        seen.setdefault(entry.name, entry)
    return tuple(seen.values())


def known_names(demand: Sequence[DemandEntry]) -> KnownNames:
    """Split a deduplicated demand list into its three disjoint name sets."""

    def names(source: str) -> frozenset[str]:
        return frozenset(e.name for e in demand if e.source == source)

    return KnownNames(names(REPO_SKILL), names(REPO_COMMAND), names(GLOBAL_CLAUDE))


def parse_listing(content: str) -> tuple[Entry, ...]:
    """Parse the delivered listing into entries; heuristic (internal format)."""
    parsed: list[tuple[str, bool, list[str]]] = []
    for line in content.split("\n"):
        if not line.startswith("- "):
            if parsed:  # a continuation line; a preamble before entry 1 is dropped
                parsed[-1][2].append(line)
            continue
        match = _ENTRY.fullmatch(line)
        if match is not None:
            parsed.append((match.group(1), match.group(2) is not None, [line]))
    return tuple(Entry(n, full, len("\n".join(lines))) for n, full, lines in parsed)


def classify(name: str, known: KnownNames) -> str:
    """Bucket a delivered name with the same precedence as ``enumerate_demand``."""
    if name in known.repo_skills:
        return REPO_SKILL
    if name in known.repo_commands:
        return REPO_COMMAND
    if name in known.global_names:
        return GLOBAL_CLAUDE
    if ":" in name:
        return NAMESPACE_PREFIX + name.split(":", 1)[0]
    return BUNDLED_OTHER


def _initial_listing(line: str) -> RawListing | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    attachment = record.get(ATTACHMENT_KEY) if isinstance(record, dict) else None
    if not isinstance(attachment, dict):
        return None
    content = attachment.get(CONTENT_KEY)
    is_listing = attachment.get(TYPE_KEY) == ATTACHMENT_TYPE
    if not (is_listing and attachment.get(IS_INITIAL_KEY) is True):
        return None
    if not isinstance(content, str):
        return None
    count = attachment.get(SKILL_COUNT_KEY)
    return RawListing(content, count if isinstance(count, int) else 0)


def load_listing(transcript: Path) -> RawListing:
    """Return the first initial ``skill_listing`` attachment of a transcript."""
    try:
        with transcript.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                found = _initial_listing(line)
                if found is not None:
                    return found
    except OSError:
        msg = f"cannot read transcript {transcript.name}"
        raise SystemExit(msg) from None
    msg = (
        f"no initial skill_listing attachment found in {transcript.name} "
        f"(the attachment is {VERSION_CAVEAT} and may have changed)"
    )
    raise SystemExit(msg)


def latest_transcript(home: Path, cwd: Path) -> Path:
    """Return the newest transcript for ``cwd`` under ``<home>/.claude/projects``."""
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    project = home / ".claude" / "projects" / encoded
    candidates = sorted(
        project.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime, p.name)
    )
    if not candidates:
        msg = "no transcripts found for the given --cwd"
        raise SystemExit(msg)
    return candidates[-1]


def budget_from_formula(
    window: int = DEFAULT_WINDOW, fraction: float = DEFAULT_FRACTION
) -> int:
    """Budget in chars: window * chars-per-token * fraction (internal formula)."""
    return math.floor(window * CHARS_PER_TOKEN * fraction)


def parse_debug_budget(text: str) -> BudgetObserved | None:
    """Parse the first 'over budget' WARN line, or ``None`` if there is none."""
    match = _WARN.search(text)
    if match is None:
        return None
    skills, demand, budget = (int(g) for g in match.groups())
    return BudgetObserved(skills, demand, budget)


def _chars_of(path: Path) -> int:
    return len(_read(path)) if path.is_file() else 0


def gap_components(home: Path, listing_chars: int | None) -> GapComponents:
    """Size the user-global context files and, if measured, the delivered listing."""
    claude = home / ".claude"
    claude_md = _chars_of(claude / "CLAUDE.md")
    local_md = _chars_of(claude / "CLAUDE.local.md")
    rules = sum(_chars_of(p) for p in sorted((claude / "rules").rglob("*.md")))
    listing_tokens = None if listing_chars is None else listing_chars // CHARS_PER_TOKEN
    return GapComponents(
        global_claude_md_chars=claude_md,
        claude_local_md_chars=local_md,
        rules_chars=rules,
        listing_chars=listing_chars,
        est_tokens_context_files=(claude_md + local_md + rules) // CHARS_PER_TOKEN,
        est_tokens_listing=listing_tokens,
        gap_tokens=GAP_TOKENS,
    )


def _with_shares(
    entries: Counter[str], name_only: Counter[str], chars: Counter[str]
) -> dict[str, SourceStats]:
    total = sum(chars.values())
    order = sorted(entries, key=lambda b: (-chars[b], b))
    return {
        b: SourceStats(
            entries[b],
            name_only[b],
            chars[b],
            round(PERCENT * chars[b] / total, 1) if total else 0.0,
        )
        for b in order
    }


def _stats_by_bucket(
    pairs: Iterable[tuple[str, int, bool]],
) -> dict[str, SourceStats]:
    entries: Counter[str] = Counter()
    name_only: Counter[str] = Counter()
    chars: Counter[str] = Counter()
    for bucket, size, is_name_only in pairs:
        entries[bucket] += 1
        name_only[bucket] += int(is_name_only)
        chars[bucket] += size
    return _with_shares(entries, name_only, chars)


def summarize(
    demand: Sequence[DemandEntry],
    raw: RawListing | None,
    known: KnownNames,
    budget: BudgetObserved | None,
    gap: GapComponents,
) -> Report:
    """Combine demand, delivery, the observed budget and the gap into a report."""
    demand_stats = _stats_by_bucket(
        (e.source, e.chars, e.desc_len == 0) for e in demand
    )
    parsed = () if raw is None else parse_listing(raw.content)
    by_source = (
        None
        if raw is None
        else _stats_by_bucket(
            (classify(e.name, known), e.chars, not e.full) for e in parsed
        )
    )
    return Report(
        delivered_chars=None if raw is None else len(raw.content),
        delivered_skill_count=None if raw is None else raw.skill_count,
        delivered_entries=None if raw is None else len(parsed),
        budget_observed=budget,
        budget_formula_chars=budget_from_formula(),
        by_source=by_source,
        demand_by_source=demand_stats,
        demand_total_chars=demand_total_chars(demand),
        gap_components=gap,
    )


def render_json(report: Report) -> str:
    """Render the report as JSON; dataclass field names are the keys."""
    return json.dumps(asdict(report), indent=2)


def _source_table(title: str, stats: Mapping[str, SourceStats]) -> list[str]:
    rows = [
        f"  {bucket:<16} entries={s.entries} name_only={s.name_only} "
        f"chars={s.chars} share={s.share_pct}%"
        for bucket, s in stats.items()
    ]
    return [title, *rows]


def _budget_lines(report: Report) -> list[str]:
    lines = [
        f"budget formula (200K window default): {report.budget_formula_chars} chars"
    ]
    seen = report.budget_observed
    if seen is not None:
        lines.append(
            f"budget observed (WARN): {seen.budget_chars} chars "
            f"({seen.skills} skills, {seen.demand_chars} chars demand)"
        )
    return lines


def _gap_lines(gap: GapComponents) -> list[str]:
    lines = ["gap components (sizes, not a partition of the gap)"]
    lines += [f"  {name}: {value}" for name, value in asdict(gap).items()]
    return lines


def render_text(report: Report) -> str:
    """Render the report as text; counts, chars and bucket names only."""
    lines = [VERSION_CAVEAT, ESTIMATE_CAVEAT, ""]
    if report.delivered_chars is None:
        lines.append("delivered: not measured")
    else:
        lines.append(f"delivered chars: {report.delivered_chars}")
        lines.append(
            f"delivered skill count (attachment): {report.delivered_skill_count}"
        )
        lines.append(f"delivered entries (parsed): {report.delivered_entries}")
    lines += _budget_lines(report)
    if report.by_source is not None:
        lines += ["", *_source_table("delivered by source", report.by_source)]
    lines += ["", *_source_table("demand by source", report.demand_by_source)]
    lines += [f"demand total chars: {report.demand_total_chars}", ""]
    lines += _gap_lines(report.gap_components)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--transcript", type=Path)
    source.add_argument("--cwd", type=Path)
    parser.add_argument("--debug-log", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def _resolve_raw(args: argparse.Namespace) -> RawListing | None:
    if args.transcript is not None:
        return load_listing(args.transcript)
    if args.cwd is not None:
        return load_listing(latest_transcript(args.home, args.cwd.resolve()))
    return None


def _resolve_budget(args: argparse.Namespace) -> BudgetObserved | None:
    if args.debug_log is None:
        return None
    try:
        return parse_debug_budget(_read(args.debug_log))
    except OSError:
        msg = f"cannot read debug log {args.debug_log.name}"
        raise SystemExit(msg) from None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the measurement and write the report to stdout."""
    args = build_parser().parse_args(argv)
    args.home = args.home.expanduser()
    demand = enumerate_demand(args.repo.resolve(), args.home)
    raw = _resolve_raw(args)
    gap = gap_components(args.home, None if raw is None else len(raw.content))
    report = summarize(demand, raw, known_names(demand), _resolve_budget(args), gap)
    text = render_json(report) if args.json else render_text(report)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
