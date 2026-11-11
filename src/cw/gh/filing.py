"""Issue and milestone filing for ``cw sprint``."""

from __future__ import annotations

import json
import logging
import re
import subprocess as _sp
import tempfile
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator


_log = logging.getLogger(__name__)

_CREATE_TIMEOUT = 30

#: ``gh label create --force`` budget (#3041).
_ENSURE_LABEL_TIMEOUT = 20

#: ``gh issue list`` budget for the labelled-issue lookup (#3041).
_LIST_TIMEOUT = 30

#: ``gh issue close`` budget for the review-debt close sweep (#3044).
_CLOSE_TIMEOUT = 30

#: ``--limit`` for the labelled-issue lookup. A full page means the listing may
#: be truncated, which the caller must treat as inconclusive (#3041). 1000
#: leaves headroom over the 214 open ``review-debt`` issues seen in #3449, and
#: the ``gh issue list --json number,body`` output for 1000 issues still fits
#: ``_LIST_TIMEOUT``.
_LABEL_LIST_LIMIT = 1000

#: Percent of the limit at which the lookup logs a signal-only WARNING, so the
#: next ceiling is visible before it blocks filing (#3449).
_LABEL_LIST_WARN_PERCENT = 80

#: Longest ``detail`` a typed failure carries.
_DETAIL_MAX_CHARS = 200


_ISSUE_URL_NUMBER_RE = re.compile(r"/(\d+)\s*$")


class GhFailure(StrEnum):
    """Why a ``gh`` filing call failed, told apart by kind (#3041).

    ``network`` covers a timeout, an ``OSError`` and a transport error;
    ``auth`` a credential rejection; ``label`` a label that does not exist;
    ``milestone`` an issue created but not attached to its milestone; and
    ``other`` everything else.
    """

    NETWORK = "network"
    AUTH = "auth"
    LABEL = "label"
    MILESTONE = "milestone"
    OTHER = "other"


class IssueCreateResult(NamedTuple):
    """Outcome of :func:`create_issue`.

    ``number`` is None on every failure, including a created issue whose
    milestone did not stick (``failure`` is ``MILESTONE`` and ``url`` is set),
    so a caller that only reads ``number`` keeps the old "None means failed"
    contract.
    """

    number: int | None
    url: str | None
    failure: GhFailure | None
    detail: str


class LabelledIssue(NamedTuple):
    """One open issue from :func:`list_open_issues_by_label`."""

    number: int
    body: str


class LabelledIssuesLookup(NamedTuple):
    """Outcome of :func:`list_open_issues_by_label`.

    ``truncated`` is True when the listing returned a full page, so an absent
    match cannot be read as "no such issue".
    """

    issues: tuple[LabelledIssue, ...]
    failure: GhFailure | None
    detail: str
    truncated: bool


# stderr substrings for :func:`_classify_failure`, lower-cased. The auth and
# network ones come from captured ``gh`` runs (tests/fixtures/gh_issue_create_
# stderr_*.txt). The label one is uncaptured: the probe that would capture it
# writes to a real repo.
_AUTH_MARKERS = ("http 401", "bad credentials", "gh auth login")
_NETWORK_MARKERS = ("dial tcp", "connection refused", "proxyconnect")
_LABEL_MARKERS = ("could not add label",)


def _first_line(text: str) -> str:
    """Return *text*'s first non-empty line, capped at the detail length."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:_DETAIL_MAX_CHARS]
    return ""


def _classify_failure(returncode: int, stderr: str) -> GhFailure:
    """Map a failed ``gh`` call's exit and stderr onto a :class:`GhFailure`."""
    del returncode  # gh exits 1 for every kind; only stderr tells them apart.
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return GhFailure.AUTH
    if any(marker in lowered for marker in _NETWORK_MARKERS):
        return GhFailure.NETWORK
    if any(marker in lowered for marker in _LABEL_MARKERS):
        return GhFailure.LABEL
    return GhFailure.OTHER


@contextmanager
def _body_file(body: str) -> Iterator[Path]:
    """Write *body* to a temp file for --body-file.

    Why: issue/milestone bodies and titles carry em-dashes, ampersands, and
    backticks. Passing them on argv invites a quoting bug on every call; a
    --body-file never can.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", encoding="utf-8", delete=False
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def create_issue(
    title: str,
    body: str,
    *,
    labels: list[str],
    milestone: int | None = None,
    timeout: int = _CREATE_TIMEOUT,
    cwd: Path | None = None,
) -> IssueCreateResult:
    """Create an issue, attach it to *milestone* when given; return a typed result.

    Two calls when a milestone is given, deliberately. ``gh issue create
    --milestone`` resolves a milestone BY NAME (``-m, --milestone name``), not
    by id — handing it ``str(11)`` would look for a milestone *titled* "11" and
    fail. ``gh issue edit -m`` is name-only too. So the milestone is attached
    afterwards through the REST endpoint, which does take the numeric id. This
    keeps ``milestone: int`` in the signature (the id is what ``apply_plan``
    holds, and it is unambiguous where a title is not). Review-debt filing
    (#3041) passes no milestone and makes one call.

    ``-F`` (not ``-f``) sends a *typed* field, so ``milestone`` arrives as a JSON
    number rather than the string "11", which is what the API expects.

    *cwd* scopes every call to a client's repo; multi-client callers MUST pass
    it, as for ``post_issue_comment``.
    """
    cmd = ["gh", "issue", "create", "--title", title]
    for label in labels:
        cmd += ["--label", label]
    try:
        with _body_file(body) as path:
            cmd += ["--body-file", str(path)]
            result = _sp.run(
                cmd, capture_output=True, timeout=timeout, check=False, cwd=cwd
            )
    except (OSError, _sp.TimeoutExpired) as exc:
        return IssueCreateResult(None, None, GhFailure.NETWORK, _first_line(str(exc)))
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        failure = _classify_failure(result.returncode, stderr)
        return IssueCreateResult(None, None, failure, _first_line(stderr))
    stdout = result.stdout.decode("utf-8", "replace")
    match = _ISSUE_URL_NUMBER_RE.search(stdout)
    if not match:
        return IssueCreateResult(None, None, GhFailure.OTHER, _first_line(stdout))
    number = int(match.group(1))
    url = stdout.strip()
    if milestone is not None and not _attach_milestone(
        number, milestone, timeout, cwd=cwd
    ):
        detail = f"issue #{number} created but not attached to milestone {milestone}"
        return IssueCreateResult(None, url, GhFailure.MILESTONE, detail)
    return IssueCreateResult(number, url, None, "")


def _attach_milestone(
    number: int, milestone: int, timeout: int, *, cwd: Path | None = None
) -> bool:
    """Attach *number* to *milestone* by numeric id; confirm it actually stuck.

    Why read the response back instead of trusting the exit code: GitHub's
    "Update an issue" reference states that "without push access to the
    repository, milestone changes are silently dropped" — a 200 with the
    milestone simply not applied. Exit-code-only would report success and leave
    the issue off the milestone, which is precisely the half-applied buildout
    apply_plan exists to prevent. So confirm the echoed milestone number.
    """
    try:
        result = _sp.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}",
                "-X",
                "PATCH",
                "-F",
                f"milestone={milestone}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (OSError, _sp.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return False
    attached = payload.get("milestone") if isinstance(payload, dict) else None
    if not isinstance(attached, dict):
        return False
    return attached.get("number") == milestone


def update_issue_body(
    number: int,
    body: str,
    *,
    timeout: int = _CREATE_TIMEOUT,
    cwd: Path | None = None,
) -> bool:
    """Replace an issue's body via ``gh issue edit --body-file``. True on success."""
    try:
        with _body_file(body) as path:
            result = _sp.run(
                ["gh", "issue", "edit", str(number), "--body-file", str(path)],
                capture_output=True,
                timeout=timeout,
                check=False,
                cwd=cwd,
            )
    except (OSError, _sp.TimeoutExpired):
        return False
    return result.returncode == 0


def create_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> int | None:
    """Create a milestone via the REST API; return its number, or None on failure."""
    try:
        result = _sp.run(
            [
                "gh",
                "api",
                "repos/{owner}/{repo}/milestones",
                "-f",
                f"title={title}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    number = payload.get("number") if isinstance(payload, dict) else None
    return number if isinstance(number, int) else None


def find_milestone(
    title: str, *, timeout: int = _CREATE_TIMEOUT
) -> tuple[int | None, bool]:
    """Return (number, ok) for an existing milestone titled *title*, open OR closed.

    ``?state=all`` is load-bearing, not decoration. GitHub's "List milestones"
    defaults ``state`` to ``open``, so without it a milestone that has been
    CLOSED (which is what happens to a sprint's milestone once the sprint ends)
    is invisible here — apply_plan would read that as "no such milestone",
    create a SECOND one with the same title, and orphan the issues filed under
    the first. That is precisely the duplicate-filing this function exists to
    prevent. It goes in the path as a query string: ``-f state=all`` would flip
    gh's auto-method from GET to POST.

    ``&per_page=100`` is equally load-bearing. ``gh api`` pagination is
    opt-in — without an explicit ``per_page``, GitHub returns only the first
    page (30 items) of milestones. A repo with more than 30 milestones would
    silently drop the older ones from this scan, reintroducing the exact
    duplicate-milestone bug ``?state=all`` was added to prevent.

    ``ok=False`` means the gh call itself failed (non-zero exit, OSError,
    timeout) — the caller cannot conclude the milestone is absent, only that it
    could not check. ``ok=True`` with ``number=None`` is a genuine miss: the
    call succeeded and no milestone with this title exists yet. Conflating
    these two is exactly what breaks idempotency on a re-run after a transient
    gh failure (see ``cw.sprint.apply_plan``).
    """
    try:
        result = _sp.run(
            [
                "gh",
                "api",
                "repos/{owner}/{repo}/milestones?state=all&per_page=100",
                "--jq",
                ".[] | {number, title}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("title") == title and isinstance(entry.get("number"), int):
            number: int = entry["number"]
            return number, True
    return None, True


def latest_release_tag(*, timeout: int = _CREATE_TIMEOUT) -> tuple[str | None, bool]:
    """Return (tag, ok) for the repo's latest GitHub release.

    Policy-free: never logs, raises, or defaults a version — that judgment
    belongs to the caller (``cw.cli.sprint._resolve_version``). ``ok=False``
    means the gh call itself failed (non-zero exit, OSError, timeout, or
    empty output) — never a signal that no version has ever been released;
    the caller decides what "could not determine the latest tag" means.
    """
    try:
        result = _sp.run(
            ["gh", "release", "view", "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    tag = result.stdout.decode("utf-8", "replace").strip()
    if not tag:
        return None, False
    return tag, True


def milestone_issue_titles(
    milestone: int, *, timeout: int = _CREATE_TIMEOUT
) -> tuple[dict[str, int] | None, bool]:
    """Return ({issue title: number}, ok) for every issue on *milestone*.

    ``ok=False`` means the gh call itself failed (non-zero exit, OSError,
    timeout, unparseable JSON) — this is what makes ``cw sprint apply``
    idempotent: a caller must not read a failed lookup as "milestone has no
    issues yet" and re-file everything as duplicates. On success the dict may
    legitimately be empty (milestone exists but nothing has been filed under it
    yet); that is ``({}, True)``, not ``(None, True)``.

    Note: if two issues under *milestone* share the exact same title, this dict
    comprehension keeps only the last one seen. The idempotent re-entry check
    in ``apply_plan`` assumes ticket/epic titles are unique within a milestone.

    Note also the ``--limit 200`` cap below: only the 200 most recent issues
    under *milestone* are considered. A milestone with more than 200 issues
    filed against it will silently omit the older ones from the returned dict.

    Passing the numeric id to ``gh issue list --milestone`` is correct here and
    is NOT the same bug as in ``create_issue``: list documents its flag as
    "Filter by milestone number or title", whereas ``issue create``/``issue
    edit`` take a name only. Do not "fix" this one.
    """
    try:
        result = _sp.run(
            [
                "gh",
                "issue",
                "list",
                "--milestone",
                str(milestone),
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,title",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None, False
    if not isinstance(payload, list):
        return None, False
    titles = {
        str(item["title"]): int(item["number"])
        for item in payload
        if isinstance(item, dict) and "title" in item and "number" in item
    }
    return titles, True


def ensure_label(
    name: str,
    color: str,
    description: str,
    *,
    timeout: int = _ENSURE_LABEL_TIMEOUT,
    cwd: Path | None = None,
) -> GhFailure | None:
    """Create or update label *name* via ``gh label create --force``.

    ``--force`` makes it idempotent: an existing label has its color and
    description updated instead of failing. Returns None on success, else the
    failure kind.
    """
    cmd = [
        "gh",
        "label",
        "create",
        name,
        "--force",
        "--color",
        color,
        "--description",
        description,
    ]
    try:
        result = _sp.run(
            cmd, capture_output=True, timeout=timeout, check=False, cwd=cwd
        )
    except (OSError, _sp.TimeoutExpired):
        return GhFailure.NETWORK
    if result.returncode != 0:
        return _classify_failure(
            result.returncode, result.stderr.decode("utf-8", "replace")
        )
    return None


def close_issue(
    number: int,
    comment: str,
    *,
    reason: str,
    timeout: int = _CLOSE_TIMEOUT,
    cwd: Path | None = None,
) -> GhFailure | None:
    """Close issue *number* with *comment* in one ``gh issue close`` call (#3044).

    The comment and the close land atomically: there is never a comment
    without a close or a close without its comment. ``gh issue close`` has no
    body-file flag, so *comment* is one argv element (no shell, no stdin) and
    is posted verbatim; the caller adds any provenance marker. Returns None on
    success, else the failure kind, classified as :func:`ensure_label` does.
    """
    cmd = [
        "gh",
        "issue",
        "close",
        str(number),
        "--comment",
        comment,
        "--reason",
        reason,
    ]
    try:
        result = _sp.run(
            cmd, capture_output=True, timeout=timeout, check=False, cwd=cwd
        )
    except (OSError, _sp.TimeoutExpired):
        return GhFailure.NETWORK
    if result.returncode != 0:
        return _classify_failure(
            result.returncode, result.stderr.decode("utf-8", "replace")
        )
    return None


def _parse_labelled_issues(
    stdout: str,
) -> tuple[tuple[LabelledIssue, ...], int] | None:
    """Parse ``gh issue list --json number,body`` output; None if malformed.

    The whole page is rejected when any item is malformed. Filtering malformed
    items would make an incomplete lookup look authoritative and could permit a
    duplicate ticket to be created. The raw item count is returned for the
    truncation check.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    issues: list[LabelledIssue] = []
    for item in payload:
        if (
            not isinstance(item, dict)
            or isinstance(item.get("number"), bool)
            or not isinstance(item.get("number"), int)
            or not isinstance(item.get("body"), str)
        ):
            return None
        issues.append(LabelledIssue(item["number"], item["body"]))
    return tuple(issues), len(payload)


def list_open_issues_by_label(
    label: str,
    *,
    limit: int = _LABEL_LIST_LIMIT,
    timeout: int = _LIST_TIMEOUT,
    cwd: Path | None = None,
) -> LabelledIssuesLookup:
    """Return the OPEN issues carrying *label*, with their bodies.

    No ``--search``: the caller filters bodies client-side for its marker, so
    the lookup does not depend on GitHub's search index. ``--state open`` makes
    a CLOSED match a miss by construction. A full page sets ``truncated``.
    A listing at 80% of the limit logs a WARNING naming the count and the limit.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--label",
        label,
        "--state",
        "open",
        "--json",
        "number,body",
        "--limit",
        str(limit),
    ]
    try:
        result = _sp.run(
            cmd, capture_output=True, timeout=timeout, check=False, cwd=cwd
        )
    except (OSError, _sp.TimeoutExpired) as exc:
        return LabelledIssuesLookup((), GhFailure.NETWORK, _first_line(str(exc)), False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        failure = _classify_failure(result.returncode, stderr)
        return LabelledIssuesLookup((), failure, _first_line(stderr), False)
    parsed = _parse_labelled_issues(result.stdout.decode("utf-8", "replace"))
    if parsed is None:
        detail = "unparseable gh issue list output"
        return LabelledIssuesLookup((), GhFailure.OTHER, detail, False)
    issues, raw_count = parsed
    if raw_count * 100 >= limit * _LABEL_LIST_WARN_PERCENT:
        _log.warning(
            "gh issue list --label %s returned %d issues against its limit of %d"
            " (>= %d%%); at the limit the dedup lookup is inconclusive",
            label,
            raw_count,
            limit,
            _LABEL_LIST_WARN_PERCENT,
        )
    return LabelledIssuesLookup(issues, None, "", raw_count >= limit)
