"""Binding operator constraints for the codex fix loop (#2633).

The fix prompt used to carry only the ticket text, the plan, the scope rules
and the fence, so a fix cycle could re-add a mechanism the operator had ruled
out in the ``auto-dev-preflight-resolutions`` comment. This module is pure:
the driver fetches the ticket's comments once per run and hands them here.

Provenance (the trust anchor of ``gh.fetch_approved_plan_comment``): only a
comment carrying :data:`RESOLUTIONS_MARKER`, written by the operator login,
not agent-authored and not starting with a pipeline fixed header, is a
constraint; the newest one wins. An unresolved login means no constraints
(fail closed), and a ticket body carrying the marker defers to the ticket text
the prompt already inlines.

:func:`extract_forbidden` is deterministic and pinned: only a sentence matching
:data:`_PROHIBITION` contributes, its backticked identifier-, dotted- or
path-shaped spans become forbidden tokens, and the whole word ``lock`` or an
explicit state-file phrase forbids that detector kind. A token or kind only
triggers when the cycle INTRODUCES it (a new file at that path, or an added
line naming a token absent from that file at the cycle's base, or a net-new
detector hit), so a cycle that merely edits something the file already holds
is never parked. Only non-test source files are checked: a cycle that
mentions a forbidden token in documentation or prose is never parked. The rest
of the comment is advisory prompt text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from cw._git import git_output
from cw.codex_fix_loop.baseline import cycle_diff
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT, FenceBreach
from cw.codex_fix_loop.growth import AdditionKind, detect_additions, is_source_path
from cw.codex_fix_loop.posted_text import (
    POSTED_TEXT_MAX_CHARS,
    describe_added_line,
    redact_and_cap,
)
from cw.codex_review import (
    CODEX_FIX_CONSTRAINT_VIOLATION,
    _parse_unified_diff,
)
from cw.gh import is_agent_authored

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.codex_fix_loop.baseline import CycleBaseline

RESOLUTIONS_MARKER = "<!-- auto-dev-preflight-resolutions -->"
# Pipeline-authored comments that quote the marker but never direct anything.
_FIXED_HEADERS = (
    "## Multi-Marker Gate Blocked",
    "## Pending Verification Scan",
    "## Blocking Review Findings",
    "## Operator-Actionable Review Findings",
)
SECTION_HEADING = "## Binding Operator Constraints"
SECTION_MAX_CHARS = 8000
_SECTION_TRUNCATED = "\n\n[truncated: read the full operator comment on the ticket]"
# A sentence ends at `.`, `!` or `?` followed by whitespace, or at a newline.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n")
# The apostrophe in "don't" may be straight or curly (U+2019).
_PROHIBITION = re.compile(
    r"\b(?:(?:do not|don['\N{RIGHT SINGLE QUOTATION MARK}]t)\s+"
    r"(?:add|introduce|create|re-add|build)"
    r"|(?:must not|never)\s+(?:add|introduce|create)|no new)\b",
    re.IGNORECASE,
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
# Stripped from both ends of a backticked span, after surrounding whitespace.
_SPAN_EDGE_CHARS = ".,:;!?'\"()[]{}"
_TOKEN_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./-][A-Za-z0-9_]+)*")
_MIN_TOKEN_CHARS = 4
_LOCK_WORD = re.compile(r"\block\b", re.IGNORECASE)
_STATE_PHRASE = re.compile(
    r"\b(?:state[- ]file|on-disk (?:state )?file|sidecar)\b", re.IGNORECASE
)
_CONSTRAINT_SENTENCE_MAX_CHARS = 200


class ConstraintRule(NamedTuple):
    """One prohibition sentence and what it forbids."""

    sentence: str
    tokens: frozenset[str]
    kinds: frozenset[AdditionKind]


@dataclass(frozen=True)
class OperatorConstraints:
    """The operator's selected resolutions comment and what it forbids."""

    body: str
    author: str
    created_at: str | None
    comment_id: str | None
    rules: tuple[ConstraintRule, ...]

    @property
    def forbidden_tokens(self) -> frozenset[str]:
        """Every forbidden token across the prohibition sentences."""
        return frozenset(t for rule in self.rules for t in rule.tokens)

    @property
    def forbidden_kinds(self) -> frozenset[AdditionKind]:
        """Every forbidden detector kind across the prohibition sentences."""
        return frozenset(k for rule in self.rules for k in rule.kinds)


def _author_login(comment: dict[str, object]) -> str | None:
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) else None


def _is_constraint_comment(comment: dict[str, object], operator_login: str) -> bool:
    body = comment.get("body")
    return (
        isinstance(body, str)
        and RESOLUTIONS_MARKER in body
        and not is_agent_authored(body)
        and not body.lstrip().startswith(_FIXED_HEADERS)
        and _author_login(comment) == operator_login
    )


def _created_at(comment: dict[str, object]) -> str:
    created = comment.get("createdAt")
    return created if isinstance(created, str) else ""


def select_constraint_comment(
    comments: Iterable[dict[str, object]],
    *,
    operator_login: str | None,
    ticket_text: str | None,
) -> dict[str, object] | None:
    """Return the operator's newest marker-bearing comment, or ``None``.

    ``None`` when the login is unresolved (fail closed) or the ticket body
    already carries the marker (the prompt inlines the body as
    ``## Ticket Context``, which is authoritative).
    """
    if operator_login is None:
        return None
    if ticket_text and RESOLUTIONS_MARKER in ticket_text:
        return None
    candidates = [c for c in comments if _is_constraint_comment(c, operator_login)]
    if not candidates:
        return None
    return max(candidates, key=_created_at)


def _clean_token(span: str) -> str | None:
    token = span.strip().strip(_SPAN_EDGE_CHARS)
    if len(token) < _MIN_TOKEN_CHARS or not _TOKEN_SHAPE.fullmatch(token):
        return None
    return token


def _sentence_kinds(sentence: str) -> frozenset[AdditionKind]:
    kinds: set[AdditionKind] = set()
    if _LOCK_WORD.search(sentence):
        kinds.add(AdditionKind.LOCK)
    if _STATE_PHRASE.search(sentence):
        kinds.add(AdditionKind.STATE_FILE)
    return frozenset(kinds)


def constraint_rules(body: str) -> tuple[ConstraintRule, ...]:
    """Return one rule per sentence of *body* that matches the prohibition regex."""
    rules: list[ConstraintRule] = []
    for sentence in _SENTENCE_SPLIT.split(body):
        if not _PROHIBITION.search(sentence):
            continue
        cleaned = (_clean_token(span) for span in _BACKTICKED.findall(sentence))
        tokens = frozenset(token for token in cleaned if token is not None)
        rules.append(
            ConstraintRule(sentence.strip(), tokens, _sentence_kinds(sentence))
        )
    return tuple(rules)


def extract_forbidden(
    body: str,
) -> tuple[frozenset[str], frozenset[AdditionKind]]:
    """Return the forbidden tokens and detector kinds *body* names."""
    rules = constraint_rules(body)
    tokens = frozenset(t for rule in rules for t in rule.tokens)
    kinds = frozenset(k for rule in rules for k in rule.kinds)
    return tokens, kinds


def constraints_from_comment(comment: dict[str, object]) -> OperatorConstraints:
    """Build the constraints a selected comment carries."""
    body = comment.get("body")
    text = body if isinstance(body, str) else ""
    comment_id = comment.get("id")
    return OperatorConstraints(
        body=text,
        author=_author_login(comment) or "",
        created_at=_created_at(comment) or None,
        comment_id=comment_id if isinstance(comment_id, str) else None,
        rules=constraint_rules(text),
    )


def render_section(constraints: OperatorConstraints | None) -> str | None:
    """Render the fix-prompt section for *constraints*, or ``None`` without any."""
    if constraints is None:
        return None
    body = "\n".join(
        line for line in constraints.body.splitlines() if RESOLUTIONS_MARKER not in line
    ).strip()
    if len(body) > SECTION_MAX_CHARS:
        body = body[:SECTION_MAX_CHARS] + _SECTION_TRUNCATED
    return (
        f"{SECTION_HEADING}\n"
        "The operator's resolutions comment on this ticket is binding. Do not "
        "introduce anything it rules out, even when a finding suggests it:\n\n"
        f"{body}"
    )


class ConstraintViolation(NamedTuple):
    """One thing a cycle introduced that a rule forbids (``line`` None: new file)."""

    path: str
    line: int | None
    label: str
    text: str
    rule: ConstraintRule


def _word(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")


def _rule_for(rules: tuple[ConstraintRule, ...], label: str) -> ConstraintRule:
    return next(r for r in rules if label in r.tokens or label in r.kinds)


def _new_files(worktree: Path, baseline: CycleBaseline) -> frozenset[str]:
    out = git_output(
        [
            "diff",
            "--cached",
            "--diff-filter=A",
            "--name-only",
            "--no-renames",
            baseline.tree_sha,
        ],
        cwd=worktree,
    )
    return frozenset(line for line in out.splitlines() if line)


def _base_content(
    worktree: Path, baseline: CycleBaseline, path: str, new_files: frozenset[str]
) -> str:
    if path in new_files:
        return ""
    return git_output(["show", f"{baseline.tree_sha}:{path}"], cwd=worktree)


def _path_violations(
    constraints: OperatorConstraints, new_files: frozenset[str]
) -> list[ConstraintViolation]:
    return [
        ConstraintViolation(path, None, token, "", _rule_for(constraints.rules, token))
        for path in sorted(new_files)
        if is_source_path(path)
        for token in sorted(constraints.forbidden_tokens)
        if path == token or path.endswith(f"/{token}")
    ]


def _token_violations(
    worktree: Path,
    baseline: CycleBaseline,
    constraints: OperatorConstraints,
    added: dict[str, dict[int, str]],
    new_files: frozenset[str],
) -> list[ConstraintViolation]:
    found: list[ConstraintViolation] = []
    patterns = {t: _word(t) for t in sorted(constraints.forbidden_tokens)}
    for path, lines in sorted(added.items()):
        if not is_source_path(path):
            continue
        hits = [
            (n, text, token)
            for n, text in sorted(lines.items())
            for token, pattern in patterns.items()
            if pattern.search(text)
        ]
        if not hits:
            continue
        base = _base_content(worktree, baseline, path, new_files)
        found.extend(
            ConstraintViolation(
                path, n, token, text, _rule_for(constraints.rules, token)
            )
            for n, text, token in hits
            if not patterns[token].search(base)
        )
    return found


def find_violations(
    worktree: Path, baseline: CycleBaseline, constraints: OperatorConstraints
) -> list[ConstraintViolation]:
    """Return what the cycle's staged diff introduces that *constraints* forbid."""
    file_diffs, added, _window, _changed = _parse_unified_diff(
        cycle_diff(worktree, baseline)
    )
    new_files = _new_files(worktree, baseline)
    found = _path_violations(constraints, new_files)
    found += _token_violations(worktree, baseline, constraints, added, new_files)
    found += [
        ConstraintViolation(
            a.path, a.line, a.kind.value, a.text, _rule_for(constraints.rules, a.kind)
        )
        for a in detect_additions(added, file_diffs)
        if a.kind in constraints.forbidden_kinds
    ]
    return found


def _violation_line(violation: ConstraintViolation) -> str:
    if violation.line is None:
        return f"- {violation.path} (new file) adds {violation.label}"
    return (
        f"- {violation.path}:{violation.line} adds {violation.label}: "
        f"{describe_added_line(violation.text)}"
    )


def constraint_breach(
    worktree: Path,
    baseline: CycleBaseline,
    constraints: OperatorConstraints,
    cycle: int,
) -> FenceBreach | None:
    """Return the ``codex_fix_constraint_violation`` park, or ``None``."""
    violations = find_violations(worktree, baseline, constraints)
    if not violations:
        return None
    dated = f", dated {constraints.created_at}" if constraints.created_at else ""
    lines = [
        f"codex fix cycle {cycle} introduced something a binding operator "
        "constraint rules out; the cycle was not committed."
    ]
    for rule in dict.fromkeys(v.rule for v in violations):
        sentence = redact_and_cap(
            rule.sentence, max_line_chars=_CONSTRAINT_SENTENCE_MAX_CHARS
        )
        lines.append(
            "Constraint (from the operator's auto-dev-preflight-resolutions "
            f'comment{dated}): "{sentence}"'
        )
    lines += [_violation_line(v) for v in violations]
    hint = (
        "The operator's resolutions comment rules this out. If the constraint is "
        "stale, post a newer `auto-dev-preflight-resolutions` comment that "
        "supersedes it and requeue REVIEW; otherwise settle the finding that "
        "asked for the mechanism (`cw review settle`). To disable this guard for "
        "a lane, set `codex_fix_loop_growth_guard_enabled: false` on that lane in "
        "clients.yaml (or globally in orchestrator.yaml). "
        f"{LEFT_STAGED_HINT}"
    )
    details = redact_and_cap("\n".join(lines), max_line_chars=POSTED_TEXT_MAX_CHARS)
    paths = tuple(sorted({v.path for v in violations}))
    return FenceBreach(CODEX_FIX_CONSTRAINT_VIOLATION, paths, details, hint)
