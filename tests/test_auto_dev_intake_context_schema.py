"""Pins the .cw/context.json schema written by auto-dev intake Step 0d.

Two contracts live here:

- #1794: each comment is ``{author, created_at, body}``, not a bare body string.
- #2700: the ticket text is keyed ``ticket_title``/``ticket_body``. The two
  Python readers (``_load_ticket_context``, ``build_task_message``) read those
  keys; the tests below extract the literal heredoc JSON from the intake prose
  and drive both real readers with it, so a key rename on either side fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from cw.codex_review import _load_ticket_context
from cw.codex_review._context import _prepare_review_pass
from cw.codex_runner import FakeCodexRunner
from cw.local_runner import TASK_CONTEXT_RELATIVE_PATH, build_task_message
from tests._codex_review_helpers import _task
from tests.conftest import _cmd, git_in
from tests.test_check_impl_guard_staleness import _run

if TYPE_CHECKING:
    from collections.abc import Callable

_HEREDOC_PATTERN = re.compile(r"<< 'CWCTXEOF'\n(.*?)\nCWCTXEOF", re.DOTALL)
_COMMENT_KEYS = {"author", "created_at", "body"}
_STALE_COMMENT_AT = "2026-08-11T03:28:16Z"
_HEAD_COMMIT_AT = "2026-08-10T22:53:27-04:00"
_SMALL_TIER_WITH_TICKET = [
    "Code Quality Reviewer",
    "SysAdmin Reviewer",
    "Product Manager Reviewer",
]
_SMALL_TIER_NO_TICKET = ["Code Quality Reviewer", "SysAdmin Reviewer"]


def _example_context() -> dict[str, object]:
    """Parse the literal ``CWCTXEOF`` heredoc JSON out of the intake prose."""
    content = _cmd("auto-dev-intake.md")
    match = _HEREDOC_PATTERN.search(content)
    if match is None:
        msg = "auto-dev-intake.md has no `<< 'CWCTXEOF'` heredoc block"
        raise AssertionError(msg)
    data = json.loads(match.group(1))
    assert isinstance(data, dict)
    return data


def _example_text(example: dict[str, object], key: str) -> str:
    value = example[key]
    assert isinstance(value, str)
    assert value
    return value


def _example_comment(example: dict[str, object]) -> dict[str, object]:
    """First dict entry of ``comments`` (the ``"..."`` elision is skipped)."""
    comments = example["comments"]
    assert isinstance(comments, list)
    for entry in comments:
        if isinstance(entry, dict):
            return entry
    msg = "intake example has no dict comment entry"
    raise AssertionError(msg)


def _write_context(worktree: Path, payload: dict[str, object]) -> None:
    cw_dir = worktree / ".cw"
    cw_dir.mkdir(exist_ok=True)
    (cw_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")


def _docs_only_feature_repo(make_git_repo: Callable[[str], Path], name: str) -> Path:
    """A feature-branch repo with one markdown change (no data-safety role)."""
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    (repo / "notes.md").write_text("# notes\n", encoding="utf-8")
    git_in(repo, "add", "notes.md")
    git_in(repo, "commit", "-m", "add notes.md")
    return repo


def test_comments_schema_carries_created_at() -> None:
    content = _cmd("auto-dev-intake.md")
    assert '"created_at"' in content
    assert '"author"' in content


def test_comments_schema_maps_tracker_created_at_field() -> None:
    content = _cmd("auto-dev-intake.md")
    assert "createdAt" in content  # gh's field name, mapped into created_at


def test_comments_schema_no_longer_bare_string_array() -> None:
    """The old `["<comment 1>", ...]` literal must be gone, or a producer
    copying the heredoc verbatim would keep writing the timestamp-less shape."""
    content = _cmd("auto-dev-intake.md")
    assert '"comments": ["<comment 1>"' not in content


def test_example_is_valid_json_with_documented_keys() -> None:
    example = _example_context()
    assert {"ticket_id", "ticket_title", "ticket_body", "comments"} <= set(example)
    assert "title" not in example
    assert "body" not in example


def test_codex_review_reader_loads_intake_example(tmp_path: Path) -> None:
    example = _example_context()
    title = _example_text(example, "ticket_title")
    body = _example_text(example, "ticket_body")
    _write_context(tmp_path, example)

    _plan, ticket_text = _load_ticket_context(tmp_path)

    assert ticket_text is not None
    assert title in ticket_text
    assert body in ticket_text


def test_local_runner_reader_loads_intake_example(tmp_path: Path) -> None:
    example = _example_context()
    title = _example_text(example, "ticket_title")
    body = _example_text(example, "ticket_body")
    _write_context(tmp_path, example)
    (tmp_path / ".cw" / "plan.md").write_text("plan content", encoding="utf-8")

    build_task_message(tmp_path)

    context = (tmp_path / TASK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "## Ticket:" in context
    assert title in context
    assert body in context


def test_example_comment_entry_shape() -> None:
    entry = _example_comment(_example_context())
    assert set(entry) == _COMMENT_KEYS


def test_example_comment_shape_accepted_by_staleness_script(tmp_path: Path) -> None:
    """The context.json comment shape (snake_case ``created_at``) is read by
    the impl-guard staleness script, not silently skipped."""
    entry = dict(_example_comment(_example_context()))
    entry["created_at"] = _STALE_COMMENT_AT
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(json.dumps([entry]), encoding="utf-8")

    code, verdict = _run(
        "--head-commit-at", _HEAD_COMMIT_AT, "--comments-file", str(comments_file)
    )

    assert code == 0
    assert verdict["stale"] is True


def test_intake_example_adds_product_manager_to_small_tier_roster(
    make_git_repo: Callable[[str], Path],
) -> None:
    """#2700 operator decision: with ticket text now reaching the real context
    path, the small tier gains a Product Manager Reviewer carrying the ticket."""
    example = _example_context()
    title = _example_text(example, "ticket_title")
    body = _example_text(example, "ticket_body")
    repo = _docs_only_feature_repo(make_git_repo, "wt-intake-roster")
    _write_context(repo, example)  # untracked: the reviewed diff is unaffected

    prepared = _prepare_review_pass(
        _task(),
        repo,
        "main",
        runner=FakeCodexRunner(),
        session_id="s-intake-roster",
    )

    assert prepared.roles == _SMALL_TIER_WITH_TICKET
    prompt = prepared.prompts_by_role["Product Manager Reviewer"]
    assert "## Ticket Context" in prompt
    assert title in prompt
    assert body in prompt


def test_roster_without_context_json_omits_product_manager(
    make_git_repo: Callable[[str], Path],
) -> None:
    """Negative control: no ticket context, no Product Manager Reviewer."""
    repo = _docs_only_feature_repo(make_git_repo, "wt-no-context-roster")

    prepared = _prepare_review_pass(
        _task(),
        repo,
        "main",
        runner=FakeCodexRunner(),
        session_id="s-no-context-roster",
    )

    assert prepared.roles == _SMALL_TIER_NO_TICKET
