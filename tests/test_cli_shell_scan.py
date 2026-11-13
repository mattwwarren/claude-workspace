"""Tests for ``cw.cli._shell_scan``, the quote-aware shell scanner (#1418).

Extracted from ``cw.cli.guard_hook_install`` (#3076) when ``cw guard-git-stash``
became the second guard to need it. ``tests/test_cli_guard_hook_install.py``
stays the end-to-end regression net for the extraction; these are the direct
tests of the helpers.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cw.cli._shell_scan import (
    MAX_NESTING,
    basename,
    command_matches,
    dash_c_script,
    nested_scripts,
    segments,
)


def _starts_with(word: str) -> Callable[[list[str]], bool]:
    def _predicate(segment: list[str]) -> bool:
        return segment[:1] == [word]

    return _predicate


class TestSegments:
    def test_splits_on_every_separator(self) -> None:
        assert segments("a b && c | d; e || f & g") == [
            ["a", "b"],
            ["c"],
            ["d"],
            ["e"],
            ["f"],
            ["g"],
        ]

    def test_newline_separates_segments(self) -> None:
        assert segments("a\nb c\n\nd") == [["a"], ["b", "c"], ["d"]]

    def test_subshell_parentheses_separate(self) -> None:
        assert segments("(a b) c") == [["a", "b"], ["c"]]

    def test_quoted_separators_stay_in_one_token(self) -> None:
        assert segments("echo 'a && b' \"c; d\"") == [["echo", "a && b", "c; d"]]

    def test_comment_line_consumes_only_itself(self) -> None:
        assert segments("# note\nreal cmd") == [["real", "cmd"]]

    def test_empty_command_has_no_segments(self) -> None:
        assert segments("") == []
        assert segments(" ; ; ") == []

    @pytest.mark.parametrize("command", ['echo "oops', "echo 'oops", "echo oops\\"])
    def test_unterminated_quote_or_escape_raises(self, command: str) -> None:
        with pytest.raises(ValueError, match=r"."):
            segments(command)


class TestBasename:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("git", "git"),
            ("/usr/bin/git", "git"),
            (".venv/bin/pre-commit", "pre-commit"),
            ("a/", ""),
            ("", ""),
        ],
    )
    def test_basename(self, token: str, expected: str) -> None:
        assert basename(token) == expected


class TestDashCScript:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["-c", "echo hi"], "echo hi"),
            (["-lc", "echo hi"], "echo hi"),
            (["--norc", "-c", "echo hi"], "echo hi"),
            (["-e", "-c", "x"], "x"),
        ],
    )
    def test_finds_the_script(self, args: list[str], expected: str) -> None:
        assert dash_c_script(args) == expected

    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["-c"],
            ["-lc"],
            ["-e"],
            ["--command", "x"],
            ["script.sh", "-c", "x"],
        ],
    )
    def test_no_script(self, args: list[str]) -> None:
        assert dash_c_script(args) is None


class TestNestedScripts:
    def test_eval_joins_its_arguments(self) -> None:
        assert nested_scripts(["eval", "git", "status"]) == ["git status"]

    @pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "dash", "/bin/bash"])
    def test_shell_dash_c(self, shell: str) -> None:
        assert nested_scripts([shell, "-c", "git status"]) == ["git status"]

    def test_shell_without_dash_c_has_no_script(self) -> None:
        assert nested_scripts(["bash", "-e"]) == []
        assert nested_scripts(["bash", "script.sh"]) == []

    def test_other_commands_have_no_scripts(self) -> None:
        assert nested_scripts(["git", "status"]) == []

    def test_wrapper_prefix_is_scanned(self) -> None:
        assert nested_scripts(["env", "FOO=1", "bash", "-c", "x"]) == ["x"]


class TestCommandMatches:
    def test_top_level_match(self) -> None:
        assert command_matches("a && b", _starts_with("b"))

    def test_no_match(self) -> None:
        assert not command_matches("a && b", _starts_with("z"))

    def test_descends_into_bash_c_and_eval(self) -> None:
        predicate = _starts_with("needle")
        assert command_matches("bash -c 'needle'", predicate)
        assert command_matches("eval needle", predicate)

    def test_depth_cap(self) -> None:
        assert MAX_NESTING == 2
        predicate = _starts_with("needle")
        two = "bash -c \"bash -c 'needle'\""
        three = 'bash -c "bash -c \\"bash -c \'needle\'\\""'
        assert command_matches(two, predicate)
        assert not command_matches(three, predicate)

    def test_unterminated_quote_raises(self) -> None:
        with pytest.raises(ValueError, match=r"."):
            command_matches('needle "oops', _starts_with("needle"))
