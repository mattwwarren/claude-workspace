"""Issue and milestone filing for ``cw sprint``."""

from __future__ import annotations

import json
import re
import subprocess as _sp
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


_CREATE_TIMEOUT = 30


_ISSUE_URL_NUMBER_RE = re.compile(r"/(\d+)\s*$")


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
    milestone: int,
    timeout: int = _CREATE_TIMEOUT,
) -> int | None:
    """Create an issue and attach it to *milestone*; return its number, or None.

    Two calls, deliberately. ``gh issue create --milestone`` resolves a milestone
    BY NAME (``-m, --milestone name``), not by id — handing it ``str(11)`` would
    look for a milestone *titled* "11" and fail. ``gh issue edit -m`` is name-only
    too. So the milestone is attached afterwards through the REST endpoint, which
    does take the numeric id. This keeps ``milestone: int`` in the signature (the
    id is what ``apply_plan`` holds, and it is unambiguous where a title is not).

    ``-F`` (not ``-f``) sends a *typed* field, so ``milestone`` arrives as a JSON
    number rather than the string "11", which is what the API expects.
    """
    cmd = ["gh", "issue", "create", "--title", title]
    for label in labels:
        cmd += ["--label", label]
    try:
        with _body_file(body) as path:
            cmd += ["--body-file", str(path)]
            result = _sp.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _ISSUE_URL_NUMBER_RE.search(result.stdout.decode("utf-8", "replace"))
    if not match:
        return None
    number = int(match.group(1))
    return number if _attach_milestone(number, milestone, timeout) else None


def _attach_milestone(number: int, milestone: int, timeout: int) -> bool:
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
    number: int, body: str, *, timeout: int = _CREATE_TIMEOUT
) -> bool:
    """Replace an issue's body via ``gh issue edit --body-file``. True on success."""
    try:
        with _body_file(body) as path:
            result = _sp.run(
                ["gh", "issue", "edit", str(number), "--body-file", str(path)],
                capture_output=True,
                timeout=timeout,
                check=False,
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
