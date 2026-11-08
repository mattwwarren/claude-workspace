"""Issue reads, comments and the agent-authorship marker."""

from __future__ import annotations

import json
import re
import subprocess as _sp
from typing import TYPE_CHECKING, Any

from cw.gh.probes import current_gh_login
from cw.gh.runner import _POST_COMMENT_TIMEOUT_SECONDS, FETCH_COMMENTS_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path


_PLAN_MARKER = "<!-- plan-spec-reviewed"


# Provenance marker appended to every tracker comment this module posts
# (GitHub #2097). An automated session posts under the operator's own gh
# identity, so a comment it writes is byte-indistinguishable from one the
# operator typed -- a later stage read exactly such a comment as a binding
# operator directive and exited on an invented blocker reason. Every comment
# written through post_issue_comment therefore carries this marker unless the
# caller is relaying a decision the operator made themselves (today only
# `cw dev-queue approve --post-marker`, which passes operator_authored=True).
# The consumer-side trust rule lives in .claude/commands/auto-dev.md
# ("Comment provenance rule"); this constant is the machine-readable half.
AGENT_COMMENT_MARKER = "<!-- cw-agent-authored -->"
# Fail-closed fallback for a comment that carries no marker but says in its own
# text that an agent wrote or relayed it (#2617: an orchestrator relayed operator
# words through the tracker MCP, unmarked). The phrase set is closed and narrow;
# the prose list in .claude/commands/auto-dev.md "Comment provenance rule" is
# drift-guarded against it. Bare "on behalf of the user" is excluded on purpose.
_AGENT_NOUN = r"(?:(?:an?|the)\s+)?(?:orchestrator|agent|assistant|claude|ai)\b"
_OPERATOR_S = "operator(?:['\u2019]s)?"
_SELF_DISCLOSURE_RE = re.compile(
    "|".join(
        (
            rf"\b(?:authored|written|drafted|posted|relayed)\s+by\s+{_AGENT_NOUN}",
            r"\brelayed\s+on\s+behalf\s+of\b",
            r"\bon\s+behalf\s+of\s+the\s+operator\b",
            rf"\bposted\s+under\s+the\s+{_OPERATOR_S}\s+(?:own\s+)?"
            r"(?:account|identity|login|credentials)\b",
            rf"\brelaying\s+the\s+{_OPERATOR_S}\s+"
            r"(?:words|text|instructions|dictation|directives?)\b",
        )
    ),
    re.IGNORECASE,
)


def fetch_issue_comments(
    ticket_id: str, timeout: int, *, cwd: Path | None = None
) -> list[dict[str, Any]] | None:
    """Return the issue's comments list, or None on any fetch/parse error.

    *cwd* scopes the ``gh`` call to a client's repo (defaults to None, i.e.
    ambient CWD); multi-client callers MUST pass it, mirroring
    ``post_issue_comment``'s cwd contract (GitHub #1269/#1279/#1419).

    Public since #1730: the codex review backend inlines the live comment
    thread into every reviewer prompt, so this is no longer a module-private
    helper of ``fetch_approved_plan_comment``. Behavior is unchanged.
    """
    try:
        result = _sp.run(
            ["gh", "issue", "view", ticket_id, "--json", "comments"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        return None
    except (OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        data: dict[str, Any] = json.loads(result.stdout)
        comments: list[dict[str, Any]] = data.get("comments") or []
    except (ValueError, AttributeError):
        return None
    return comments


def fetch_issue_body(
    ticket_id: str, timeout: int, *, cwd: Path | None = None
) -> str | None:
    """Return the issue's raw body text, or None on any fetch/parse error.

    Shaped after :func:`fetch_issue_comments` (same *cwd* contract: multi-client
    callers MUST pass it). The string is returned exactly as ``gh issue view
    --json body`` reports it; ``cw.plan_fingerprint.compute_body_sha``
    normalizes it before hashing (#2761). ``cw dev-queue approve``'s advisory
    ticket-body drift check (#2311) and ``cw body-sha`` both hash this value,
    and the drift check fails open on None.
    """
    try:
        result = _sp.run(
            ["gh", "issue", "view", ticket_id, "--json", "body"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        return None
    except (OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        data: dict[str, Any] = json.loads(result.stdout)
        body = data.get("body")
    except (ValueError, AttributeError):
        return None
    return body if isinstance(body, str) else None


def _comment_has_marker(comment: dict[str, Any]) -> bool:
    """Return True if *comment*'s body contains the plan-review marker."""
    body = comment.get("body", "")
    return isinstance(body, str) and _PLAN_MARKER in body


def is_agent_authored(body: str) -> bool:
    """Return True if *body* was written by the pipeline or an agent (#2097, #2617).

    The marker :func:`post_issue_comment` appends is the machine channel. An
    unmarked body still counts when its own text self-discloses non-operator
    authorship (:data:`_SELF_DISCLOSURE_RE`), so a relayed comment cannot pass
    as operator-typed. Policy-free predicate: it answers "was this written by
    the pipeline or an agent under the operator's identity?", not "may this
    comment direct anything". Callers own the trust decision.
    """
    return AGENT_COMMENT_MARKER in body or _SELF_DISCLOSURE_RE.search(body) is not None


def fetch_approved_plan_comment(
    ticket_id: str,
    *,
    timeout: int = FETCH_COMMENTS_TIMEOUT,
    cwd: Path | None = None,
) -> str | None:
    """Return the body of the latest approved plan comment on a GitHub issue.

    *cwd* is the repository directory the comments fetch runs in; ``gh``
    resolves the issue against that repo's remote. ``None`` is the ambient
    working directory, which is only correct on a single-client host: callers
    that know the ticket's client MUST pass its repo path (#2884, same bug
    class as #2798). The identity lookup stays unscoped -- ``gh api user`` is
    a per-user fact, not a per-repo one (GitHub #1269 R3).

    Scans issue comments in reverse order (newest first) for the first one
    containing the ``<!-- plan-spec-reviewed`` marker written by auto-dev-plan
    AND authored by the currently-authenticated ``gh`` identity. A
    marker-bearing comment from any other commenter is skipped (not
    scan-terminating) — it does not count as "no reviewed plan," it simply
    isn't authoritative, so scanning continues for an older trusted match.

    Returns None when:
    - gh binary is absent or returns an error
    - the response cannot be parsed
    - no comment carries the plan marker (Stage 1 not yet complete)
    - no marker-bearing comment is authored by the currently-authenticated
      ``gh`` identity, or that identity cannot be resolved (fail-closed)
    """
    comments = fetch_issue_comments(ticket_id, timeout, cwd=cwd)
    if comments is None:
        return None

    if not any(_comment_has_marker(c) for c in comments):
        return None

    trusted_login = current_gh_login(timeout=timeout)
    if trusted_login is None:
        return None

    for comment in reversed(comments):
        body = comment.get("body", "")
        if not (isinstance(body, str) and _comment_has_marker(comment)):
            continue
        author = comment.get("author")
        if isinstance(author, dict) and author.get("login") == trusted_login:
            return body
    return None


def post_issue_comment(
    ticket_id: str,
    body: str,
    *,
    timeout: int = _POST_COMMENT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    operator_authored: bool = False,
) -> _sp.CompletedProcess[bytes] | None:
    """Post *body* as a GitHub issue comment via ``gh issue comment``.

    Returns the completed subprocess (inspect .returncode / .stderr) or None
    if the subprocess could not run / timed out. Policy-free: callers decide
    whether to log or swallow — this neither logs nor raises on gh failure.

    *cwd* scopes the ``gh`` call to a client's repo (defaults to None, i.e.
    ambient CWD); multi-client callers MUST pass it (GitHub #1269/#1279).

    *operator_authored* suppresses the :data:`AGENT_COMMENT_MARKER` provenance
    line this function otherwise appends (GitHub #2097). Pass it only when the
    comment records a decision the operator themselves made through a cw
    command — not merely "an operator started the run this comment came from".
    """
    posted_body = body if operator_authored else agent_marked_body(body)
    try:
        # The body goes on stdin, not argv (#2810): Linux caps one argv string
        # at 131,072 bytes, which a 60,000-character multibyte part can exceed.
        return _sp.run(
            ["gh", "issue", "comment", ticket_id, "--body-file", "-"],
            input=posted_body.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (OSError, _sp.TimeoutExpired):
        return None


def agent_marked_body(body: str) -> str:
    """Return *body* exactly as :func:`post_issue_comment` posts it (#2810).

    The :data:`AGENT_COMMENT_MARKER` follows a blank line. ``cw post-comment``
    compares a read-back comment against this string, so the two stay one
    definition.
    """
    return f"{body}\n\n{AGENT_COMMENT_MARKER}"
