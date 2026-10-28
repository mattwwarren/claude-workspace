"""Doc lint: orchestrator-run docs prescribe plain, allowlistable commands (#2697).

An orchestrator session runs the commands its skills prescribe through a
permission allowlist and a worktree-isolation hook that rejects command
substitution, shell loops and jq filter flags. A skill that prescribes one of
those shapes stalls the orchestrator on a permission prompt or a hook refusal,
so this file pins the shape of every prescribed command in the orchestrator
doc set (``.claude/skills/*/SKILL.md``, ``.claude/skills/*/references/*.md``
and ``.claude/commands/queue-issues.md``). Worker-run docs (``auto-dev*``,
``prep-pr``, ``ship-it``, ``review-monitor``) are out of scope.

Reach and limits:

- Only ```bash fences are linted (via ``tests/conftest.py::_bash_fences``).
  Untagged and ``text`` fences hold subagent prompts and pseudo-code by
  convention in these docs and are not executed, so they are out of scope.
- Whole-line ``#`` comments inside a fence are dropped before the scan
  (comment lines never execute); trailing comments on a code line are still
  scanned.
- A fence line fails on ``$(``, an unescaped backtick, a ``for``/``while``/
  ``until`` loop keyword at a command position, or ``--jq``. The short form
  ``-q`` is NOT linted: ``grep -q`` is legitimate and used by the
  changelog-gate snippet.
- Inline code spans in prose (fences removed first) fail only on ``$(`` or
  ``--jq``; loop keywords and backticks are not checked there, since a span
  such as ``until`` is a legitimate prose mention.
- ``_EXEMPT_FENCES`` holds the one fence that is deliberately left as-is,
  each with a reason citing its follow-up ticket. An entry exempts exactly
  one fence of exactly one file; the self-tests below prove it cannot mask
  anything else.

The allowlist test parses ``docs/dispatch-runbook.md`` section "Orchestrator
permission allowlist" and pins every listed ``Bash(cw <verb>:*)`` entry to a
real, read-only command.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from cw.cli import main
from tests.conftest import _bash_fences, walk_click_tree

_REPO = Path(__file__).resolve().parent.parent

_DOC_GLOBS = (
    ".claude/skills/*/SKILL.md",
    ".claude/skills/*/references/*.md",
)
_EXTRA_DOCS = (".claude/commands/queue-issues.md",)
# Docs that dispatch workers run, not the orchestrator (out of lint scope).
_WORKER_RUN = ("auto-dev*", "prep-pr", "ship-it", "review-monitor")
_SKILL_NAME_INDEX = 2  # .claude/skills/<skill>/...

_LOOP_RE = re.compile(r"(?:^\s*|[|;&]\s*)(?:for|while|until)\b")
_BACKTICK_RE = re.compile(r"(?<!\\)`")
_JQ_RE = re.compile(r"--jq\b")
_SUBST = "$("
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCE_DELIMITER = "```"

_CONSTRUCT_SUBST = "command substitution $("
_CONSTRUCT_BACKTICK = "backtick substitution"
_CONSTRUCT_LOOP = "shell loop"
_CONSTRUCT_JQ = "--jq filter"

_SESSION_WATCH = ".claude/skills/cw-session-watch/SKILL.md"
_ARM_MARKER = "prep_pr_finalize.py arm-automerge"
# The exempt fence's residual: HEAD_SHA=, ARM_JSON= and the nested state read
# are the three substitutions; the state read also carries the one --jq.
_RESIDUAL_SUBST_COUNT = 3
_RESIDUAL_JQ_COUNT = 1

# (repo-relative path, fence-identifying substring) -> reason. A fence is
# exempt only when its file matches the path AND its body contains the
# substring, and the substring must identify exactly one fence of that file.
_EXEMPT_FENCES: dict[tuple[str, str], str] = {
    (_SESSION_WATCH, _ARM_MARKER): (
        "The timed-out-impl arm-automerge fence is a merge-safety control: "
        "its exit-code ladder (0/1/3/other), NOT-arming default-deny and "
        "--head-sha pin are executed and pinned by "
        "tests/test_arm_automerge_markdown_sites.py (shared with "
        "review-monitor.md). Rewriting it without command substitution and "
        "--jq while keeping those pins is tracked in #2753."
    ),
}

_READ_ONLY_VERBS = frozenset(
    {
        "dev-queue tasks",
        "dev-queue status",
        "dev-queue show",
        "dev-queue wait",
        "queue peek",
        "session result",
        "session show",
        "session list",
        "event tail",
        "orchestrate watch",
        "review dispositions",
        "review history",
        "lane ls",
        "config show",
        "config concurrency get",
        "preflight",
    }
)
_RUNBOOK = _REPO / "docs" / "dispatch-runbook.md"
_ALLOWLIST_HEADING = "## Orchestrator permission allowlist"
_ALLOW_ENTRY_RE = re.compile(r"Bash\(cw ([a-z][a-z-]*(?: [a-z][a-z-]*)*):\*\)")
_NEXT_SECTION_RE = re.compile(r"^## ", re.MULTILINE)

_SKILL_SCRIPT_RE = re.compile(r"\.claude/skills/[\w-]+/scripts/[\w-]+\.py")
_REMOVED_SCRIPT_DIR = "cw-smoke-test/scripts"


@dataclass(frozen=True)
class Violation:
    """One forbidden construct found in a scoped doc."""

    path: str
    where: str
    construct: str
    text: str

    def render(self) -> str:
        return f"{self.path}: {self.where}: {self.construct}: {self.text}"


def _orchestrator_docs() -> list[str]:
    paths: set[str] = set(_EXTRA_DOCS)
    for pattern in _DOC_GLOBS:
        for path in _REPO.glob(pattern):
            rel = path.relative_to(_REPO).as_posix()
            skill = rel.split("/")[_SKILL_NAME_INDEX]
            if not any(fnmatch.fnmatch(skill, worker) for worker in _WORKER_RUN):
                paths.add(rel)
    return sorted(paths)


_ORCHESTRATOR_DOCS = _orchestrator_docs()


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _code_lines(fence: str) -> list[str]:
    """Return *fence*'s lines minus whole-line ``#`` comments."""
    return [line for line in fence.splitlines() if not line.lstrip().startswith("#")]


def _line_constructs(line: str) -> list[str]:
    found: list[str] = []
    if _SUBST in line:
        found.append(_CONSTRUCT_SUBST)
    if _BACKTICK_RE.search(line):
        found.append(_CONSTRUCT_BACKTICK)
    if _LOOP_RE.search(line):
        found.append(_CONSTRUCT_LOOP)
    if _JQ_RE.search(line):
        found.append(_CONSTRUCT_JQ)
    return found


def _span_constructs(span: str) -> list[str]:
    found: list[str] = []
    if _SUBST in span:
        found.append(_CONSTRUCT_SUBST)
    if _JQ_RE.search(span):
        found.append(_CONSTRUCT_JQ)
    return found


def _prose(content: str) -> str:
    """Return *content* with every fenced block (any tag) removed."""
    kept: list[str] = []
    in_fence = False
    for line in content.splitlines():
        if line.strip().startswith(_FENCE_DELIMITER):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


def _exempt_ordinals(
    path: str, fences: list[str], exempt: Mapping[tuple[str, str], str]
) -> set[int]:
    ordinals: set[int] = set()
    for exempt_path, marker in exempt:
        if exempt_path != path:
            continue
        hits = [n for n, fence in enumerate(fences, start=1) if marker in fence]
        if len(hits) != 1:
            msg = (
                f"{path}: exempt marker {marker!r} must identify exactly one "
                f"fence (matched {len(hits)})"
            )
            raise ValueError(msg)
        ordinals.add(hits[0])
    return ordinals


def scan_doc(
    path: str,
    content: str,
    exempt: Mapping[tuple[str, str], str] = _EXEMPT_FENCES,
) -> list[Violation]:
    """Return every forbidden construct in *content* (repo-relative *path*)."""
    fences = _bash_fences(content)
    skipped = _exempt_ordinals(path, fences, exempt)
    violations: list[Violation] = []
    for ordinal, fence in enumerate(fences, start=1):
        if ordinal in skipped:
            continue
        for line in _code_lines(fence):
            violations.extend(
                Violation(path, f"bash fence {ordinal}", construct, line.strip())
                for construct in _line_constructs(line)
            )
    for span in _SPAN_RE.findall(_prose(content)):
        violations.extend(
            Violation(path, "inline code span", construct, span)
            for construct in _span_constructs(span)
        )
    return violations


# --- the real doc set ---


def test_orchestrator_doc_set_is_populated() -> None:
    assert _ORCHESTRATOR_DOCS
    for known in (
        ".claude/commands/queue-issues.md",
        ".claude/skills/cw-fanout/SKILL.md",
        ".claude/skills/cw-followup/SKILL.md",
        ".claude/skills/cw-followup/references/no-sentinel-patterns.md",
        ".claude/skills/cw-orchestration/SKILL.md",
        ".claude/skills/cw-session-watch/SKILL.md",
        ".claude/skills/cw-validate-result/SKILL.md",
        ".claude/skills/orchestrate-sprint/SKILL.md",
        ".claude/skills/sprint-buildout/SKILL.md",
    ):
        assert known in _ORCHESTRATOR_DOCS, known


def test_worker_run_docs_are_out_of_scope() -> None:
    for rel in _ORCHESTRATOR_DOCS:
        name = (
            Path(rel).stem if rel in _EXTRA_DOCS else rel.split("/")[_SKILL_NAME_INDEX]
        )
        assert not any(fnmatch.fnmatch(name, worker) for worker in _WORKER_RUN), rel


@pytest.mark.parametrize("path", _ORCHESTRATOR_DOCS)
def test_orchestrator_doc_prescribes_no_forbidden_shapes(path: str) -> None:
    violations = scan_doc(path, _read(path))
    assert violations == [], "\n".join(v.render() for v in violations)


# --- the exemption cannot mask anything else ---

_EXEMPT_FENCE_DOC = (
    "```bash\n"
    f"X=$(./{_ARM_MARKER} 1)\n"
    "```\n"
    "\n"
    "```bash\n"
    "Y=$(cw session show 1 --json)\n"
    "```\n"
)


def test_exempt_fence_masks_only_its_own_fence() -> None:
    violations = scan_doc(_SESSION_WATCH, _EXEMPT_FENCE_DOC)

    assert [(v.where, v.construct) for v in violations] == [
        ("bash fence 2", _CONSTRUCT_SUBST)
    ]
    assert violations[0].text == "Y=$(cw session show 1 --json)"


def test_exempt_marker_in_another_path_is_not_exempt() -> None:
    violations = scan_doc(".claude/skills/cw-fanout/SKILL.md", _EXEMPT_FENCE_DOC)

    assert [v.where for v in violations] == ["bash fence 1", "bash fence 2"]


def test_exempt_marker_matching_two_fences_fails() -> None:
    doc = f"```bash\nA=$({_ARM_MARKER})\n```\n\n```bash\nB=$({_ARM_MARKER})\n```\n"

    with pytest.raises(ValueError, match="must identify exactly one fence"):
        scan_doc(_SESSION_WATCH, doc)


def test_exempt_marker_matching_no_fence_fails() -> None:
    with pytest.raises(ValueError, match="must identify exactly one fence"):
        scan_doc(_SESSION_WATCH, "```bash\ncw session list --json\n```\n")


@pytest.mark.parametrize(("path", "marker"), sorted(_EXEMPT_FENCES))
def test_each_exempt_entry_matches_exactly_one_real_fence(
    path: str, marker: str
) -> None:
    fences = _bash_fences(_read(path))

    assert sum(marker in fence for fence in fences) == 1


@pytest.mark.parametrize("key", sorted(_EXEMPT_FENCES))
def test_every_exempt_reason_cites_a_ticket(key: tuple[str, str]) -> None:
    reason = _EXEMPT_FENCES[key]

    assert re.search(r"#\d+", reason), reason
    assert "tests/test_arm_automerge_markdown_sites.py" in reason


def test_session_watch_is_clean_outside_the_exempt_fence() -> None:
    assert scan_doc(_SESSION_WATCH, _read(_SESSION_WATCH)) == []


def test_exempt_fence_residual_census() -> None:
    """Pin the one residual fence's constructs: three ``$(`` and one ``--jq``."""
    fence = next(f for f in _bash_fences(_read(_SESSION_WATCH)) if _ARM_MARKER in f)
    code = "\n".join(_code_lines(fence))
    unexempted = scan_doc(_SESSION_WATCH, f"```bash\n{fence}\n```\n", exempt={})

    assert code.count(_SUBST) == _RESIDUAL_SUBST_COUNT
    assert len(_JQ_RE.findall(code)) == _RESIDUAL_JQ_COUNT
    assert {v.construct for v in unexempted} == {_CONSTRUCT_SUBST, _CONSTRUCT_JQ}
    subst_lines = [v.text for v in unexempted if v.construct == _CONSTRUCT_SUBST]
    assert len(subst_lines) == _RESIDUAL_SUBST_COUNT
    assert subst_lines[0].startswith("HEAD_SHA=$(")
    assert subst_lines[1].startswith("ARM_JSON=$(")
    assert '"$(gh pr view' in subst_lines[2]


# --- regex table ---


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("| while read name path; do", True),
        ("cw config | awk '{print $1}' | while read name path; do", True),
        ('for t in "${T[@]}"; do', True),
        ("  until cw session show 1; do sleep 5; done", True),
        ("true; for x in a b; do :; done", True),
        ('echo "for example"', False),
        ("cw session wait 1a2b --timeout 3600", False),
        ("# see `cw session show`", False),
        ("grep -qE '^(feat|fix)\\('", False),
        ("grep -q '^src/'", False),
        ("gh x --jq .y", True),
        ("X=$(cw session show 1)", True),
        ("X=`date`", True),
        ("echo \\`literal\\`", False),
    ],
)
def test_fence_line_regex_table(line: str, flagged: bool) -> None:
    violations = scan_doc("x.md", f"```bash\n{line}\n```\n", exempt={})

    assert bool(violations) is flagged, violations


@pytest.mark.parametrize(
    ("prose", "flagged"),
    [
        ("Poll with `until` you see it.", False),
        ("Never run `for t in x` here.", False),
        ("Do not write `SID=$(cw session list)`.", True),
        ("Avoid `gh pr view 1 --jq .state`.", True),
        ("Run `cw session show 1 --json` and read `status`.", False),
    ],
)
def test_inline_span_regex_table(prose: str, flagged: bool) -> None:
    violations = scan_doc("x.md", f"{prose}\n", exempt={})

    assert bool(violations) is flagged, violations


def test_inline_spans_ignore_fenced_text() -> None:
    doc = "```text\n`X=$(y)`\n```\nplain prose\n"

    assert scan_doc("x.md", doc, exempt={}) == []


# --- skill script paths ---


@pytest.mark.parametrize("path", _ORCHESTRATOR_DOCS)
def test_mentioned_skill_scripts_exist(path: str) -> None:
    content = _read(path)
    missing = [
        script
        for script in sorted(set(_SKILL_SCRIPT_RE.findall(content)))
        if not (_REPO / script).is_file()
    ]

    assert missing == [], f"{path} names missing scripts: {missing}"
    assert _REMOVED_SCRIPT_DIR not in content, path


# --- orchestrator permission allowlist doc ---


def _allowlist_section() -> str:
    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.index(_ALLOWLIST_HEADING) + len(_ALLOWLIST_HEADING)
    rest = text[start:]
    following = _NEXT_SECTION_RE.search(rest)
    return rest if following is None else rest[: following.start()]


def _command_paths() -> set[str]:
    return {" ".join(path) for path, _cmd in walk_click_tree(main) if path}


def test_allowlist_section_lists_entries() -> None:
    assert _ALLOW_ENTRY_RE.findall(_allowlist_section())


def test_allowlisted_verbs_resolve_and_are_read_only() -> None:
    verbs = _ALLOW_ENTRY_RE.findall(_allowlist_section())
    commands = _command_paths()

    unresolved = [verb for verb in verbs if verb not in commands]
    state_changing = [verb for verb in verbs if verb not in _READ_ONLY_VERBS]
    assert unresolved == []
    assert state_changing == []


def test_pinned_read_only_verbs_resolve() -> None:
    assert sorted(_READ_ONLY_VERBS - _command_paths()) == []


def test_allowlist_pin_rejects_a_state_changing_verb() -> None:
    assert "lane set" in _command_paths()
    assert "lane set" not in _READ_ONLY_VERBS
    assert _ALLOW_ENTRY_RE.findall("`Bash(cw lane set:*)`") == ["lane set"]
