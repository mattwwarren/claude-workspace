"""Tests for the ``cw guard-git-stash`` PreToolUse hook command (#1418).

The git stash stack is one ``refs/stash`` reflog shared by every worktree of a
repository. In a headless dispatch worker the guard refuses the stash forms that
act on the stack's default (newest) entry or push an untagged one (exit 2):
bare ``git stash``, ``stash pop``, ``stash clear`` and friends. Every other
outcome is a no-op (exit 0, empty stderr): a stash form that names its target
or tags its entry, the words appearing only inside a quoted argument, an
operator or interactive session, a payload the guard cannot read, or a fault
while reading the worker's context file.

None of these tests launch git; they classify command strings. The Bash payload
envelope is the sibling guard test's ``_payload``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli._lazy import GUARD_GIT_STASH_NAME
from cw.cli.guard_git_stash import REFUSAL_REASON, SAFE_FORM, classify_git_stash
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from tests._shared.stop_hook_context import (
    _headless_worktree,
    _invoke_hook_command,
    _write_hook_context_file,
)
from tests.test_cli_guard_hook_install import _payload

_BLOCK_EXIT = 2
_COMMAND = GUARD_GIT_STASH_NAME
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELP_LINE = (
    "Refuse bare git stash, stash pop and other shared-stack stash use from a "
    "headless worker's Bash call."
)
# Typed out rather than imported, so drift in the module constant is caught.
_SAFE_FORM_SENTENCE = (
    'If you must stash, run `git stash push -u -m "<unique-tag>"`, capture the '
    "new entry's SHA with `git stash list --format='%H %gs'`, restore with "
    "`git stash apply <sha>` (never pop), then re-find the entry by tag and "
    "drop it."
)

_REFUSED = [
    "git stash",
    "git stash -u",
    "git stash -k",
    "git stash -a",
    "git stash -- path/to/file",
    "git stash push",
    "git stash push -u",
    "git stash push -- path/to/file",
    "git stash push -- -m tag",
    "git stash save x",
    "git stash save",
    "git stash pop",
    "git stash pop stash@{0}",
    "git stash pop --index",
    "git stash clear",
    "git stash apply",
    "git stash apply --index",
    "git stash drop",
    "git stash drop -q",
    "git stash branch",
    "git stash branch x",
    "git stash store abc123",
    'git stash store -m "" abc123',
    'git stash -m ""',
    'git stash push -m ""',
    'git stash push --message ""',
    "git stash --message=",
    "git stash push -m",
    "git -C d stash pop",
    "git -c k=v stash",
    "git --git-dir=/x stash",
    "git --git-dir /x --work-tree /y stash",
    "git --no-pager stash",
    "git -P stash pop",
    "/usr/bin/git stash",
    "command git stash pop",
    "env FOO=1 git stash",
    "FOO=1 git stash",
    "cd x && git stash",
    "git status; git stash",
    "git stash;git stash pop",
    "git stash &&\ngit stash pop",
    "git status\ngit stash\necho done",
    "# set it aside\ngit stash",
    "git stash > /dev/null",
    "git stash 2>&1",
    "git stash pop >/dev/null 2>&1",
    "git stash &> /dev/null",
    'bash -c "git stash pop"',
    "bash -lc 'git stash'",
    "bash --norc -c 'git stash'",
    "eval git stash pop",
    "eval 'git stash'",
    "bash -c \"bash -c 'git stash pop'\"",
]

_ALLOWED = [
    "git stash list",
    "git stash list --format='%H %gs'",
    "git stash show -p",
    "git stash show stash@{1}",
    "git stash create",
    "git stash create 'msg'",
    "git stash apply abc123",
    "git stash apply --index abc123",
    "git stash apply -q stash@{2}",
    "git stash apply abc123 >/dev/null",
    "git stash drop stash@{2}",
    "git stash drop -q stash@{1}",
    'git stash push -u -m "wip-tag"',
    "git stash push -u -m tag",
    "git stash push -um tag",
    "git stash push -mtag",
    "git stash push --message=tag",
    "git stash push --message tag",
    "git stash push -m tag -- path/to/file",
    "git stash -m tag",
    "git stash -u -m tag",
    "git stash store -m tag abc123",
    "git stash store abc123 -m tag",
    "git stash branch x abc123",
    "git stash branch x stash@{1}",
    "git -C d stash list",
    "git log --grep stash",
    "git status",
    "git help stash",
    "git stash --help",
    "git stash -h",
    "git stash frobnicate",
    "git",
    "git -C",
    'echo "git stash pop"',
    "echo 'git stash'",
    'git commit -m "docs: never git stash pop"',
    'gh issue comment --body "git stash pop"',
    "echo hi # git stash",
    "bash script.sh",
    "bash -c",
    "bash -e",
    'bash -c "bash -c \\"bash -c \'git stash pop\'\\""',
]


def _invoke(cwd: object, command: object, **kwargs: Any) -> Any:
    return _invoke_hook_command(_COMMAND, _payload(cwd, command, **kwargs))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", _REFUSED)
def test_refuses_shared_stack_stash_in_headless_worker(
    tmp_path: Path, command: str
) -> None:
    result = _invoke(_headless_worktree(tmp_path), command)

    assert result.exit_code == _BLOCK_EXIT
    assert result.stderr == REFUSAL_REASON + "\n"


def test_refusal_text_names_the_safe_alternative(tmp_path: Path) -> None:
    result = _invoke(_headless_worktree(tmp_path), "git stash pop")

    stderr = result.stderr
    assert stderr == REFUSAL_REASON + "\n"
    assert REFUSAL_REASON.startswith("BLOCKED (#1418): ")
    assert "Do not retry this command or another spelling of it" in REFUSAL_REASON
    assert "temporary WIP commit" in REFUSAL_REASON
    assert "push -u -m" in REFUSAL_REASON
    assert "stash list --format='%H %gs'" in REFUSAL_REASON
    assert "apply <sha>" in REFUSAL_REASON
    assert "never pop" in REFUSAL_REASON
    assert "-F <file>" in REFUSAL_REASON
    assert SAFE_FORM in REFUSAL_REASON
    assert SAFE_FORM == _SAFE_FORM_SENTENCE


def test_docs_quote_the_refusal_text() -> None:
    reference = (_REPO_ROOT / "config" / "CONFIG_REFERENCE.md").read_text(
        encoding="utf-8"
    )

    assert SAFE_FORM in reference
    assert _HELP_LINE in reference
    assert "BLOCKED (#1418): " in reference


# ---------------------------------------------------------------------------
# Allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", _ALLOWED)
def test_allows_safe_forms_in_headless_worker(tmp_path: Path, command: str) -> None:
    result = _invoke(_headless_worktree(tmp_path), command)

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_in_non_headless_context(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_hook_context_file(worktree)

    result = _invoke(worktree, "git stash pop")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_when_cwd_has_no_context(ancestor_free_dir: Path) -> None:
    result = _invoke(ancestor_free_dir / "bare", "git stash pop")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_a_non_bash_tool(tmp_path: Path) -> None:
    result = _invoke(_headless_worktree(tmp_path), "git stash pop", tool_name="Monitor")

    assert result.exit_code == 0
    assert result.stderr == ""


@pytest.mark.parametrize("command", [123, None])
def test_allows_a_non_string_command(tmp_path: Path, command: object) -> None:
    result = _invoke(_headless_worktree(tmp_path), command)

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_a_missing_command(tmp_path: Path) -> None:
    payload = {
        **_payload(_headless_worktree(tmp_path)),
        "tool_input": {"run_in_background": False},
    }

    result = _invoke_hook_command(_COMMAND, payload)

    assert result.exit_code == 0
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"])
def test_malformed_or_empty_stdin_is_a_no_op(stdin: str) -> None:
    result = CliRunner().invoke(main, [_COMMAND], input=stdin)

    assert result.exit_code == 0
    assert result.stderr == ""


@pytest.mark.parametrize("exc_type", [OSError, ValueError, TypeError])
def test_classifier_fault_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    def _raise(_payload: object) -> str | None:
        raise exc_type

    monkeypatch.setattr("cw.cli.guard_git_stash.classify_git_stash", _raise)

    result = _invoke(_headless_worktree(tmp_path), "git stash pop")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_undecodable_context_file_fails_open(tmp_path: Path) -> None:
    worktree = _headless_worktree(tmp_path)
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_bytes(b'{"headless": \xff\xfe}')

    result = _invoke(worktree, "git stash pop")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_nul_in_cwd_fails_open() -> None:
    result = _invoke("/x\x00y", "git stash pop")

    assert result.exit_code == 0
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# classify_git_stash, directly
# ---------------------------------------------------------------------------


def test_classifier_refuses_in_headless_worker(tmp_path: Path) -> None:
    payload = _payload(_headless_worktree(tmp_path), "git stash")

    assert classify_git_stash(payload) == REFUSAL_REASON


def test_classifier_allows_a_safe_form_in_headless_worker(tmp_path: Path) -> None:
    payload = _payload(_headless_worktree(tmp_path), "git stash push -m tag")

    assert classify_git_stash(payload) is None


def test_classifier_none_payload() -> None:
    assert classify_git_stash(None) is None


@pytest.mark.parametrize("tool_input", [["git stash"], None])
def test_classifier_non_dict_tool_input(tmp_path: Path, tool_input: object) -> None:
    payload = {**_payload(_headless_worktree(tmp_path)), "tool_input": tool_input}

    assert classify_git_stash(payload) is None


@pytest.mark.parametrize("command", [123, None])
def test_classifier_non_string_command(tmp_path: Path, command: object) -> None:
    assert classify_git_stash(_payload(_headless_worktree(tmp_path), command)) is None


def test_classifier_missing_command(tmp_path: Path) -> None:
    payload = {**_payload(_headless_worktree(tmp_path)), "tool_input": {}}

    assert classify_git_stash(payload) is None


@pytest.mark.parametrize("cwd", [42, ""])
def test_classifier_odd_cwd(cwd: object) -> None:
    assert classify_git_stash(_payload(cwd, "git stash")) is None


def test_classifier_unterminated_quote_gives_no_verdict(tmp_path: Path) -> None:
    payload = _payload(_headless_worktree(tmp_path), 'git stash "oops')

    assert classify_git_stash(payload) is None
