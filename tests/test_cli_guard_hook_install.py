"""Tests for the ``cw guard-hook-install`` PreToolUse hook command (#3076).

In a headless dispatch worker the guard refuses ``pre-commit install`` and
``pre-commit uninstall`` (exit 2): both rewrite the ``.git/hooks`` directory
every worktree of the repository shares, and the installed hook pins
``INSTALL_PYTHON`` to the worker's own venv. Every other outcome is a no-op
(exit 0, empty stderr): another ``pre-commit`` subcommand, the words appearing
only inside a quoted argument, an operator or interactive session, a payload
the guard cannot read, or a fault while reading the worker's context file.

The Bash payload envelope is ``tests/test_cli_guard_busy_wait.py``'s
``_BASH_PRE_PAYLOAD`` (see that module's provenance note); only ``cwd``,
``tool_name`` and ``tool_input`` vary here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.guard_hook_install import (
    OPERATOR_ACTION,
    OPERATOR_COMMAND,
    REFUSAL_REASON,
    classify_hook_install,
)
from cw.models import HOOK_CONTEXT_RELATIVE_PATH
from tests._shared.stop_hook_context import (
    _headless_worktree,
    _invoke_hook_command,
    _write_hook_context_file,
)
from tests.test_cli_guard_busy_wait import _BASH_PRE_PAYLOAD

_BLOCK_EXIT = 2
_COMMAND = "guard-hook-install"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_HELP_LINE = (
    "Refuse pre-commit install or uninstall from a headless worker's Bash call."
)
# Typed out rather than imported, so drift in the module constant is caught.
_OPERATOR_SENTENCE = (
    "Operator action: run `uv run pre-commit install --hook-type pre-commit` "
    "from the main checkout (`cw doctor` reports a missing or stale hook as "
    "`commit-hook/<client>` and a hook pinned to a worker venv as "
    "`hook-install-python/<client>`)."
)

_REFUSED = [
    "uv run pre-commit install",
    "pre-commit install --hook-type pre-push",
    "pre-commit uninstall",
    ".venv/bin/pre-commit install",
    "uvx pre-commit install",
    "python -m pre_commit install",
    "cd sub && pre-commit install",
    "FOO=1 pre-commit install",
    "pre-commit -c cfg.yaml install",
    "pre-commit --config=cfg.yaml install",
    'bash -c "pre-commit install"',
    "bash -lc 'pre-commit install'",
    "bash --norc -c 'pre-commit install'",
    "eval pre-commit install",
    "eval 'pre-commit install'",
    "git status\nuv run pre-commit install\necho done",
    "# reinstall the hooks\nuv run pre-commit install",
]

_ALLOWED = [
    "pre-commit run --all-files",
    "uv run pre-commit run diff-cover",
    "pre-commit install-hooks",
    "pre-commit init-templatedir x",
    "pre-commit --version",
    "pre-commit",
    'git commit -m "docs: run pre-commit install"',
    'echo "pre-commit install"',
    'gh issue comment --body "pre-commit install"',
    "echo hi # pre-commit install",
    "bash script.sh",
    "bash -c",
    "bash -e",
    'bash -c "bash -c \\"bash -c \'pre-commit install\'\\""',
]


def _payload(
    cwd: object, command: object = "true", *, tool_name: str = "Bash"
) -> dict[str, object]:
    """The Bash PreToolUse payload pointed at *cwd* carrying *command*."""
    return {
        **_BASH_PRE_PAYLOAD,
        "cwd": cwd if not isinstance(cwd, Path) else str(cwd),
        "tool_name": tool_name,
        "tool_input": {"command": command, "run_in_background": False},
    }


def _invoke(cwd: object, command: object, **kwargs: Any) -> Any:
    return _invoke_hook_command(_COMMAND, _payload(cwd, command, **kwargs))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", _REFUSED)
def test_refuses_hook_install_in_headless_worker(tmp_path: Path, command: str) -> None:
    result = _invoke(_headless_worktree(tmp_path), command)

    assert result.exit_code == _BLOCK_EXIT
    assert result.stderr == REFUSAL_REASON + "\n"


def test_refusal_text_names_the_operator_action(tmp_path: Path) -> None:
    result = _invoke(_headless_worktree(tmp_path), "uv run pre-commit install")

    stderr = result.stderr
    assert stderr.startswith("BLOCKED (#3076): ")
    assert _OPERATOR_SENTENCE in stderr
    assert "pre-commit install --hook-type pre-commit" in stderr
    assert "operator" in stderr.lower()
    assert "cw doctor" in stderr
    assert "commit-hook/" in stderr
    assert "hook-install-python/" in stderr
    assert "#3076" in stderr
    assert stderr == REFUSAL_REASON + "\n"
    assert OPERATOR_ACTION in REFUSAL_REASON
    assert OPERATOR_ACTION == _OPERATOR_SENTENCE


def test_docs_quote_the_refusal_operator_command() -> None:
    claude_md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    reference = (_REPO_ROOT / "config" / "CONFIG_REFERENCE.md").read_text(
        encoding="utf-8"
    )

    assert OPERATOR_COMMAND == "uv run pre-commit install --hook-type pre-commit"
    assert OPERATOR_COMMAND in claude_md
    assert OPERATOR_COMMAND in reference
    assert OPERATOR_ACTION in reference
    assert _HELP_LINE in reference


# ---------------------------------------------------------------------------
# Allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", _ALLOWED)
def test_allows_other_commands_in_headless_worker(tmp_path: Path, command: str) -> None:
    result = _invoke(_headless_worktree(tmp_path), command)

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_in_non_headless_context(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_hook_context_file(worktree)

    result = _invoke(worktree, "uv run pre-commit install")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_when_cwd_has_no_context(ancestor_free_dir: Path) -> None:
    result = _invoke(ancestor_free_dir / "bare", "uv run pre-commit install")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_allows_a_non_bash_tool(tmp_path: Path) -> None:
    result = _invoke(
        _headless_worktree(tmp_path), "pre-commit install", tool_name="Monitor"
    )

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

    monkeypatch.setattr("cw.cli.guard_hook_install.classify_hook_install", _raise)

    result = _invoke(_headless_worktree(tmp_path), "pre-commit install")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_undecodable_context_file_fails_open(tmp_path: Path) -> None:
    worktree = _headless_worktree(tmp_path)
    (worktree / HOOK_CONTEXT_RELATIVE_PATH).write_bytes(b'{"headless": \xff\xfe}')

    result = _invoke(worktree, "pre-commit install")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_nul_in_cwd_fails_open() -> None:
    result = _invoke("/x\x00y", "pre-commit install")

    assert result.exit_code == 0
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# classify_hook_install, directly
# ---------------------------------------------------------------------------


def test_classifier_refuses_in_headless_worker(tmp_path: Path) -> None:
    payload = _payload(_headless_worktree(tmp_path), "pre-commit install")

    assert classify_hook_install(payload) == REFUSAL_REASON


def test_classifier_none_payload() -> None:
    assert classify_hook_install(None) is None


@pytest.mark.parametrize("tool_input", [["pre-commit install"], None])
def test_classifier_non_dict_tool_input(tmp_path: Path, tool_input: object) -> None:
    payload = {**_payload(_headless_worktree(tmp_path)), "tool_input": tool_input}

    assert classify_hook_install(payload) is None


@pytest.mark.parametrize("command", [123, None])
def test_classifier_non_string_command(tmp_path: Path, command: object) -> None:
    assert classify_hook_install(_payload(_headless_worktree(tmp_path), command)) is (
        None
    )


def test_classifier_missing_command(tmp_path: Path) -> None:
    payload = {**_payload(_headless_worktree(tmp_path)), "tool_input": {}}

    assert classify_hook_install(payload) is None


@pytest.mark.parametrize("cwd", [42, ""])
def test_classifier_odd_cwd(cwd: object) -> None:
    assert classify_hook_install(_payload(cwd, "pre-commit install")) is None


def test_classifier_unterminated_quote_gives_no_verdict(tmp_path: Path) -> None:
    payload = _payload(_headless_worktree(tmp_path), 'pre-commit install "oops')

    assert classify_hook_install(payload) is None
