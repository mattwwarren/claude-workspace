"""Guard the ticket-fetch boundary (RFC 0019 T2, #3712).

After T2 a codex stage reads a ticket through the snapshot, not through the
tracker: ``cw.ticket_snapshot`` is the one place a stage's ticket text, comments
and approved plan are fetched. This scan walks ``src/cw`` for calls to the five
tracker readers and fails on any file that is not on the closed allowlist below.

The allowlist is shrink-only. Each entry carries the reason its fetch is not a
stage read, a per-entry vacuity test proves the file still holds a flagged call
(a stale entry fails), and ``_ALLOWLIST_CEILING`` pins the count so a new entry
needs a deliberate edit of this file, in review.

The scan sees direct calls (``fetch_issue_comments(...)``) and attribute calls
(``gh.fetch_issue_comments(...)``), plus an aliased import of a reader, which
would hide the call from the name check. It does not follow a reader passed
around as a value; the five names are searched for by name only.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Final

import pytest

from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files

if TYPE_CHECKING:
    from pathlib import Path

#: The tracker readers a codex stage used to call directly.
FETCH_NAMES: Final = frozenset(
    {
        "fetch_issue_comments",
        "fetch_issue_body",
        "fetch_issue_snapshot",
        "fetch_approved_plan_comment",
        "prefetch_ticket",
    }
)

#: Paths relative to ``src/``, posix form. An entry ending in ``/`` is a package
#: prefix. Shrink-only: delete an entry when its file stops fetching; never add
#: one without raising ``_ALLOWLIST_CEILING`` in the same reviewed change.
_ALLOWLIST: Final[dict[str, str]] = {
    # Intake: the first read of a ticket, before any snapshot exists.
    "cw/ticket_snapshot/_fetch.py": "intake: the one snapshot fetch (gh issue view)",
    "cw/codex_plan/_prompt.py": "intake: the PLAN stage reads the ticket live",
    "cw/codex_plan/_run.py": "intake: the PLAN stage prefetches the ticket",
    # Courier.
    "cw/outbox_drain.py": "courier: reads the thread to dedupe and read back a post",
    # Approval-time reads (operator acts on a ticket, not a worker stage).
    "cw/adopt_pr.py": "approval-time: body fingerprint when adopting a PR",
    "cw/cli/body_sha.py": "approval-time: `cw body-sha` hashes the live body",
    "cw/dev_queue/approval.py": "approval-time: body drift check at approve",
    # Sign-off reads (the operator releases a gate against the live plan).
    "cw/reconcile/gate_recipes.py": "sign-off: gate recipe reads the plan of record",
    "cw/dev_queue/lifecycle.py": "sign-off: lifecycle reads the plan of record",
    "cw/dev_queue/requeue.py": "sign-off: requeue reads the plan of record",
    # With a stated reason.
    "cw/tracker_post.py": "post-comment dedup read-back of the comment just posted",
    "cw/sprint.py": "sprint planning reads issue bodies, outside any stage",
    "cw/review_debt_filing/": "debt filing reads bodies to link and dedupe tickets",
    "cw/gh/": "the package defining the readers; its approved-plan reader calls the"
    " comment reader",
}

#: Pinned maximum entry count. Lower it when an entry is deleted.
_ALLOWLIST_CEILING: Final = 14

#: Files that had a fetch call before T2 and must now read the snapshot instead.
_MUST_BE_CLEAN: Final = (
    "cw/codex_review/_context/core.py",
    "cw/codex_impl/_run.py",
    "cw/codex_finalize/_run.py",
    "cw/local_runner.py",
    "cw/codex_fix_loop/_driver.py",
)


def _fetch_calls(source: str) -> list[tuple[int, str]]:
    """``(lineno, name)`` of every reader call or aliased import in *source*."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if isinstance(func, ast.Attribute):
                name = func.attr
            if name in FETCH_NAMES:
                found.append((node.lineno, name))
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name in FETCH_NAMES and alias.asname is not None
            )
    return found


def _rel(path: Path) -> str:
    return path.relative_to(_SRC_ROOT).as_posix()


def _is_allowed(rel: str) -> bool:
    return any(
        rel == entry or (entry.endswith("/") and rel.startswith(entry))
        for entry in _ALLOWLIST
    )


def _flagged_files() -> dict[str, list[tuple[int, str]]]:
    flagged: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_src_files():
        calls = _fetch_calls(path.read_text(encoding="utf-8"))
        if calls:
            flagged[_rel(path)] = calls
    return flagged


def test_no_unlisted_module_calls_a_tracker_reader() -> None:
    offenders = {
        rel: calls for rel, calls in _flagged_files().items() if not _is_allowed(rel)
    }
    assert offenders == {}, (
        "a stage reads the ticket through cw.ticket_snapshot, not the tracker; "
        "see the module docstring before adding an allowlist entry"
    )


def test_the_allowlist_can_only_shrink() -> None:
    assert len(_ALLOWLIST) <= _ALLOWLIST_CEILING


def test_every_allowlist_entry_states_a_reason() -> None:
    assert all(reason.strip() for reason in _ALLOWLIST.values())


@pytest.mark.parametrize("entry", sorted(_ALLOWLIST))
def test_each_allowlist_entry_is_not_vacuous(entry: str) -> None:
    flagged = _flagged_files()
    if entry.endswith("/"):
        assert any(rel.startswith(entry) for rel in flagged), (
            f"{entry} no longer calls a tracker reader; delete the entry"
        )
    else:
        assert (_SRC_ROOT / entry).is_file(), f"{entry} no longer exists"
        assert entry in flagged, f"{entry} no longer calls a tracker reader"


@pytest.mark.parametrize("rel", _MUST_BE_CLEAN)
def test_converted_stage_modules_are_clean_and_not_allowlisted(rel: str) -> None:
    path = _SRC_ROOT / rel
    assert path.is_file()
    assert _fetch_calls(path.read_text(encoding="utf-8")) == []
    assert not _is_allowed(rel)


def test_the_scan_covers_the_source_tree() -> None:
    rels = {_rel(path) for path in _iter_src_files()}
    assert "cw/ticket_snapshot/_fetch.py" in rels
    assert len(rels) > 300


# --- snippet self-checks: positive and negative controls ---


@pytest.mark.parametrize(
    "snippet",
    [
        "fetch_issue_comments(ticket, 30)",
        "fetch_issue_body(ticket, 30, cwd=cwd)",
        "fetch_issue_snapshot(ticket, 30)",
        "fetch_approved_plan_comment(ticket, login, 30)",
        "prefetch_ticket(task)",
        "gh.fetch_issue_comments(ticket, 30)",
        "cw.gh.fetch_issue_body(ticket, 30)",
        "self._gh.fetch_approved_plan_comment(ticket)",
        "x = [fetch_issue_comments(t) for t in tickets]",
        "from cw.gh import fetch_issue_comments as get",
    ],
)
def test_scanner_flags_each_reader_form(snippet: str) -> None:
    assert _fetch_calls(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "def fetch_issue_comments(ticket): ...",
        "from cw.gh import fetch_issue_comments",
        "reader = 'fetch_issue_body'",
        "# fetch_issue_snapshot(ticket)",
        "snapshot_from_github(ticket, client, cwd)",
        "read_for_stage(task, stage)",
        "post_issue_comment(ticket, body)",
    ],
)
def test_scanner_passes_non_calls(snippet: str) -> None:
    assert _fetch_calls(snippet) == []


def test_scanner_reports_the_name_and_line() -> None:
    source = "x = 1\nfetch_issue_body(t, 5)\n"
    assert _fetch_calls(source) == [(2, "fetch_issue_body")]


def test_prefix_entries_match_only_inside_the_package() -> None:
    assert _is_allowed("cw/gh/issues.py")
    assert _is_allowed("cw/review_debt_filing/_file.py")
    assert not _is_allowed("cw/gh_other.py")
    assert not _is_allowed("cw/codex_impl/_run.py")
