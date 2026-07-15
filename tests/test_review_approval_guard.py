"""Guard against introducing a GitHub review-approval call path (#1199).

cw's autonomous pipeline relies on never granting a GitHub PR review
approval — branch protection's human-review gate is the last control
between an autonomous run and a merge to the default branch. This is a
regression test for #1199: a source-level deny-list scan asserting no
approving-review call path exists in src/. See docs/adr/0012-cw-never-
grants-github-review-approvals.md for the invariant this test enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"

# A "gh pr review" invocation: the three tokens "gh", "pr", "review" as a
# near-contiguous subsequence, tolerant of quote style / list vs string
# construction, within one call-construction window (see _CALL_WINDOW_LINES).
_GH_PR_REVIEW_RE = re.compile(
    r"""[\"']gh[\"'].{0,40}[\"']pr[\"'].{0,40}[\"']review[\"']""", re.DOTALL
)
_APPROVE_FLAG_RE = re.compile(r"""--approve\b""")
_GRAPHQL_APPROVE_RE = re.compile(r"addPullRequestReview")
_GRAPHQL_APPROVE_EVENT_RE = re.compile(r"""event:\s*APPROVE\b""")
_REST_REVIEWS_ENDPOINT_RE = re.compile(r"""/pulls/\{?\w*\}?/reviews""")
_REST_APPROVE_EVENT_RE = re.compile(r"""["']event["']\s*:\s*["']APPROVE["']""")

# Call-construction window: how many lines to look ahead of a "gh pr review"
# match for a co-occurring --approve token, so a single subprocess.run([...])
# argv list split across lines still counts as "the same call construction"
# per R3, without matching an unrelated --approve appearing elsewhere in the
# same file.
_CALL_WINDOW_LINES = 6


def _iter_src_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _find_gh_pr_review_approve(text: str, path: Path) -> list[str]:
    """Return violation messages for `gh pr review ... --approve` shapes.

    Only trips when --approve co-occurs with a gh/pr/review call
    construction within _CALL_WINDOW_LINES lines (R3): a bare --approve
    elsewhere in the file, or "gh pr review" used for e.g. --request-changes
    with no --approve nearby, does not trip this scanner.
    """
    lines = text.splitlines()
    violations: list[str] = []
    for match in _GH_PR_REVIEW_RE.finditer(text):
        start_line = text.count("\n", 0, match.start())
        window = "\n".join(lines[start_line : start_line + _CALL_WINDOW_LINES])
        if _APPROVE_FLAG_RE.search(window):
            violations.append(
                f"{path}:{start_line + 1}: 'gh pr review' co-occurs with "
                f"--approve within {_CALL_WINDOW_LINES} lines"
            )
    return violations


def _find_graphql_approve(text: str, path: Path) -> list[str]:
    violations: list[str] = []
    for match in _GRAPHQL_APPROVE_RE.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        window_start = max(0, match.start() - 200)
        window_end = min(len(text), match.end() + 200)
        window = text[window_start:window_end]
        if _GRAPHQL_APPROVE_EVENT_RE.search(window):
            violations.append(
                f"{path}:{line_no}: addPullRequestReview co-occurs with event: APPROVE"
            )
    return violations


def _find_rest_approve(text: str, path: Path) -> list[str]:
    violations: list[str] = []
    for match in _REST_REVIEWS_ENDPOINT_RE.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        window_start = max(0, match.start() - 200)
        window_end = min(len(text), match.end() + 200)
        window = text[window_start:window_end]
        if _REST_APPROVE_EVENT_RE.search(window):
            violations.append(
                f'{path}:{line_no}: POST .../reviews co-occurs with "event": "APPROVE"'
            )
    return violations


class TestNoReviewApprovalCallPath:
    """Deny-list scan: no approving-review call path anywhere in src/."""

    def test_no_gh_pr_review_approve(self) -> None:
        violations: list[str] = []
        for path in _iter_src_files():
            text = path.read_text(encoding="utf-8")
            violations.extend(_find_gh_pr_review_approve(text, path))
        assert not violations, "\n".join(violations)

    def test_no_graphql_approve_review(self) -> None:
        violations: list[str] = []
        for path in _iter_src_files():
            text = path.read_text(encoding="utf-8")
            violations.extend(_find_graphql_approve(text, path))
        assert not violations, "\n".join(violations)

    def test_no_rest_approve_review(self) -> None:
        violations: list[str] = []
        for path in _iter_src_files():
            text = path.read_text(encoding="utf-8")
            violations.extend(_find_rest_approve(text, path))
        assert not violations, "\n".join(violations)

    def test_legitimate_gh_pr_neighbors_stay_green(self) -> None:
        """Positive control: known-legitimate gh pr call sites never trip.

        Guards specifically against the scanner over-matching on `gh pr
        edit --add-reviewer` (gh.py) and `gh pr merge --auto` (salvage.py),
        per the ticket's test-plan hint to verify these two neighbors
        explicitly stay green.
        """
        gh_py = (_SRC_ROOT / "cw" / "gh.py").read_text(encoding="utf-8")
        salvage_py = (_SRC_ROOT / "cw" / "reconcile" / "salvage.py").read_text(
            encoding="utf-8"
        )
        assert not _find_gh_pr_review_approve(gh_py, _SRC_ROOT / "cw" / "gh.py")
        assert not _find_gh_pr_review_approve(
            salvage_py, _SRC_ROOT / "cw" / "reconcile" / "salvage.py"
        )
