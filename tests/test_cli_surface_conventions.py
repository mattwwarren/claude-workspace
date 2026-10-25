"""Whole-tree invariants over the ``cw`` Click command surface (#2690).

Two conventions an orchestrator relies on without reading source:

- every ``--client`` option also accepts ``-c``;
- every ``cw ...`` command named in help text (command help, short help,
  epilog, option help) resolves to a real command;
- every ``cw ...`` command named in a runtime string anywhere under
  ``src/cw`` (wedge recipes, ``CwError`` messages, hints) resolves too (#2703).

The first two are walked over the live Click tree rather than grepped from
source, so a new command or a rewrapped help string is covered without touching
this file. The third parses each source file with :mod:`ast`, because runtime
strings live in Python source, not in Click metadata.

Known limitations of the runtime walk: an explicit ``"a" + "b"`` (``BinOp``)
concatenation splits a string into two constants, so a mention that straddles
the ``+`` is read as two fragments (implicit adjacent-literal concatenation is
fine). Mention styles the heuristic does not recognize (for example bare
``- cw x`` bullets in ``prompts.py``) are not covered. Only command paths are
resolved, never flags or option values.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import click
import pytest

import cw
from cw.cli import main
from tests.conftest import walk_click_tree

# Commands where ``-c`` is bound to a non-client option, so ``--client`` cannot
# also take it. Keyed by command path, valued by the reason. Empty today: no
# command binds ``-c`` to anything but its client option.
_CLIENT_SHORT_EXEMPT: dict[tuple[str, ...], str] = {}

# ``cw ...`` help and runtime mentions that intentionally name something that is
# not a command. Empty today: every mention must resolve.
_MENTION_EXEMPT: set[str] = set()

_MIN_COMMAND_COUNT = 40
_MIN_RUNTIME_FILES = 100
_MIN_RUNTIME_MENTIONS = 150
_EXPR_PLACEHOLDER = "<expr>"
_LINE_PREFIXES = ("Recipe:", "Example:")
_MENTION_RE = re.compile(r"\bcw((?: [a-z][a-z0-9_-]*)*)")
_BACKTICK_SPAN_RE = re.compile(r"(`+)([^`]+)\1")
# ``cw ...`` introduced by a quote, or by a verb that precedes a command in
# prose ("Run cw x", "Suggested: cw x"). Backticks are not listed: backtick
# spans are already their own source above. Plain "the cw binary" prose is not.
_INTRODUCED_RE = re.compile(
    r"""(?:['"]|\b(?:Run|run|Use|use|Suggested|Try|with):?\s)"""
    r"(cw(?: [a-z][a-z0-9_-]*)*)(?![\w-])"
)
_STOP_PREFIXES = ("-", "<", "[", ".")


def _ctx() -> click.Context:
    return click.Context(main)


def _help_texts(cmd: click.Command) -> list[str]:
    """Every help string Click renders for *cmd*: its own and its options'."""
    texts = [cmd.help, cmd.short_help, cmd.epilog]
    texts.extend(p.help for p in cmd.params if isinstance(p, click.Option))
    return [t for t in texts if t]


def _strip_line_prefix(line: str) -> str:
    stripped = line.strip()
    for prefix in _LINE_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def _extract_mentions(text: str, *, runtime: bool = False) -> list[list[str]]:
    """Return the subcommand tokens of each ``cw ...`` mention in *text*.

    Order matters: whole lines starting with ``cw `` are read from the raw text
    first, because whitespace normalization (needed so backticked spans may
    wrap across lines) leaves no lines behind. With ``runtime=True`` the bare
    line-start rule additionally requires leading whitespace, so a runtime
    string that merely begins with prose "cw ..." is not read as a command.
    """
    sources: list[str] = []
    for line in text.split("\n"):
        candidate = _strip_line_prefix(line)
        if candidate.startswith("cw ") and not (runtime and not line[:1].isspace()):
            sources.append(candidate)
    normalized = " ".join(text.split())
    sources.extend(
        match.group(2)
        for match in _BACKTICK_SPAN_RE.finditer(normalized)
        if _MENTION_RE.search(match.group(2))
    )
    sources.extend(match.group(1) for match in _INTRODUCED_RE.finditer(normalized))
    return [
        match.group(1).split()
        for source in sources
        for match in _MENTION_RE.finditer(source)
    ]


def _unresolved(tokens: list[str]) -> bool:
    """True if *tokens* name a subcommand that does not exist under ``main``."""
    ctx = _ctx()
    cmd: click.Command = main
    for token in tokens:
        if token.startswith(_STOP_PREFIXES) or token[:1].isupper():
            return False
        if not isinstance(cmd, click.Group):
            return False
        sub = cmd.get_command(ctx, token)
        if sub is None:
            return True
        cmd = sub
    return False


def _mention_unresolved(mention: str) -> bool:
    return _unresolved(mention.split()[1:])


def _docstring_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every docstring constant (module, class, function)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _joined_text(node: ast.JoinedStr) -> str:
    """Text of an f-string with each replacement field turned into ``<expr>``."""
    return "".join(
        part.value
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
        else _EXPR_PLACEHOLDER
        for part in node.values
    )


def _runtime_strings(source: str) -> list[tuple[int, str]]:
    """``(lineno, text)`` for each runtime string literal in *source*.

    Docstrings are skipped. An f-string is one string whose replacement fields
    read as ``<expr>`` (a stop token for :func:`_unresolved`), and its constant
    pieces are not also reported on their own.
    """
    tree = ast.parse(source)
    consumed = _docstring_ids(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in consumed:
            continue
        if isinstance(node, ast.JoinedStr):
            consumed.update(id(child) for child in ast.walk(node) if child is not node)
            found.append((node.lineno, _joined_text(node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def _runtime_mentions(source: str) -> list[tuple[int, list[str]]]:
    """``(lineno, tokens)`` for every ``cw ...`` mention in runtime strings."""
    return [
        (lineno, tokens)
        for lineno, text in _runtime_strings(source)
        for tokens in _extract_mentions(text, runtime=True)
    ]


def _runtime_mentions_unresolved(source: str) -> list[str]:
    """``line: cw ...`` for each runtime mention that names a missing command."""
    return [
        f"{lineno}: {mention}"
        for lineno, tokens in _runtime_mentions(source)
        if (mention := " ".join(["cw", *tokens])) not in _MENTION_EXEMPT
        and _unresolved(tokens)
    ]


class TestTreeWalk:
    def test_tree_sanity(self) -> None:
        paths = [path for path, _cmd in walk_click_tree(main)]
        assert len(paths) > _MIN_COMMAND_COUNT
        assert ("dev-queue", "tasks") in paths
        assert ("dev-queue", "show") in paths
        assert ("event", "tail") in paths


class TestClientShortOption:
    def test_every_client_option_accepts_dash_c(self) -> None:
        missing = [
            "cw " + " ".join(path)
            for path, cmd in walk_click_tree(main)
            if path not in _CLIENT_SHORT_EXEMPT
            for param in cmd.params
            if isinstance(param, click.Option)
            and "--client" in param.opts
            and "-c" not in param.opts
        ]
        assert missing == [], f"--client without -c on: {missing}"

    def test_exempt_entries_are_real_collisions(self) -> None:
        commands = dict(walk_click_tree(main))
        for path in _CLIENT_SHORT_EXEMPT:
            cmd = commands[path]
            assert any(
                isinstance(p, click.Option)
                and "-c" in p.opts
                and "--client" not in p.opts
                for p in cmd.params
            ), f"stale exemption: cw {' '.join(path)}"


class TestHelpMentionsResolve:
    def test_help_mentions_resolve(self) -> None:
        unresolved: list[str] = []
        for path, cmd in walk_click_tree(main):
            for text in _help_texts(cmd):
                for tokens in _extract_mentions(text):
                    mention = " ".join(["cw", *tokens])
                    if mention in _MENTION_EXEMPT:
                        continue
                    if _unresolved(tokens):
                        unresolved.append(f"cw {' '.join(path)}: {mention}")
        assert unresolved == [], "help names missing commands:\n" + "\n".join(
            unresolved
        )


class TestRuntimeMentionsResolve:
    def test_runtime_mentions_resolve(self) -> None:
        root = Path(cw.__file__).parent
        files = sorted(root.rglob("*.py"))
        unresolved: list[str] = []
        scanned = 0
        for path in files:
            source = path.read_text(encoding="utf-8")
            scanned += len(_runtime_mentions(source))
            unresolved.extend(
                f"{path.relative_to(root.parent)}:{finding}"
                for finding in _runtime_mentions_unresolved(source)
            )
        # A broken collector must not pass vacuously.
        assert len(files) > _MIN_RUNTIME_FILES
        assert scanned > _MIN_RUNTIME_MENTIONS
        assert unresolved == [], "runtime strings name missing commands:\n" + "\n".join(
            unresolved
        )


class TestRuntimeStrings:
    def test_fstring_phantom_is_flagged_with_expr_placeholder(self) -> None:
        source = 'x = f"Suggested: cw spawn-complete {task.ticket_id}."\n'
        assert _runtime_strings(source) == [
            (1, f"Suggested: cw spawn-complete {_EXPR_PLACEHOLDER}.")
        ]
        assert _runtime_mentions_unresolved(source) == ["1: cw spawn-complete"]

    def test_corrected_recipe_is_not_flagged(self) -> None:
        source = 'x = f"Suggested: cw spawn complete {sid} --status shipped."\n'
        assert _runtime_mentions_unresolved(source) == []

    def test_quoted_phantom_is_flagged(self) -> None:
        source = "x = f\"Run 'cw post-mortem {x}' for it\"\n"
        assert _runtime_mentions_unresolved(source) == ["1: cw post-mortem"]

    def test_implicit_concatenation_phantom_is_flagged(self) -> None:
        source = (
            'x = (\n    "Transcript missing."\n    f" Run \'cw post-mortem {i}\'"\n)\n'
        )
        assert _runtime_mentions_unresolved(source) == ["2: cw post-mortem"]

    def test_docstrings_are_not_scanned(self) -> None:
        source = (
            '"""Module: run cw state."""\n'
            "class C:\n"
            '    """Class: Run \'cw state\'."""\n'
            "    def f(self) -> None:\n"
            '        """Func: `cw state`."""\n'
            "    async def g(self) -> None:\n"
            '        """Async: `cw state`."""\n'
        )
        assert _runtime_strings(source) == []
        assert _runtime_mentions_unresolved(source) == []

    def test_prose_mentions_are_ignored(self) -> None:
        source = 'a = "cw binary not on PATH"\nb = "the cw dev-queue row"\n'
        assert _runtime_mentions(source) == []

    def test_runtime_ignores_column_zero_cw_but_accepts_indented(self) -> None:
        assert _extract_mentions("cw foo", runtime=True) == []
        assert _extract_mentions("  cw doctor --reap", runtime=True) == [["doctor"]]

    def test_introduced_source_reads_verbs_and_quotes(self) -> None:
        assert _extract_mentions("Run: cw doctor --reap") == [["doctor"]]
        assert _extract_mentions("then 'cw spawn close' it") == [["spawn", "close"]]
        assert _extract_mentions("Suggested: cw spawn-complete <x>") == [
            ["spawn-complete"]
        ]

    def test_exempt_mention_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys.modules[__name__], "_MENTION_EXEMPT", {"cw nonesuch"})
        assert _runtime_mentions_unresolved("x = \"Run 'cw nonesuch'\"\n") == []


class TestResolver:
    def test_phantom_subcommand_is_unresolved(self) -> None:
        assert _mention_unresolved("cw dev-queue inspect")

    def test_real_command_with_flags_resolves(self) -> None:
        assert not _mention_unresolved("cw dev-queue tasks -t <T>")

    def test_real_command_with_positional_resolves(self) -> None:
        assert not _mention_unresolved("cw spawn close --confirmed-dead <id>")

    def test_unknown_top_level_command_is_unresolved(self) -> None:
        assert _mention_unresolved("cw spawn-complete <ticket_id>")

    def test_uppercase_placeholder_ends_resolution(self) -> None:
        assert not _mention_unresolved("cw dev-queue TICKET")


class TestExtractMentions:
    def test_wrapped_backtick_span_resolves(self) -> None:
        mentions = _extract_mentions("see `cw review\n    dispositions` first")
        assert mentions == [["review", "dispositions"]]
        assert not _unresolved(mentions[0])

    def test_recipe_line_in_multiline_docstring_is_extracted(self) -> None:
        text = (
            "Branch is pushed but the task is RUNNING.\n"
            "      Recipe: cw spawn complete <session_id> --status shipped\n"
            "    trailing prose\n"
        )
        assert _extract_mentions(text) == [["spawn", "complete"]]

    def test_bare_line_starting_with_cw_is_extracted(self) -> None:
        assert _extract_mentions("  cw doctor --reap") == [["doctor"]]

    def test_prose_without_backticks_is_ignored(self) -> None:
        assert _extract_mentions("the worker knows cw cannot see it") == []

    def test_multiple_mentions_in_one_span(self) -> None:
        mentions = _extract_mentions("`cw dev-queue tasks` or `cw event tail`")
        assert mentions == [["dev-queue", "tasks"], ["event", "tail"]]

    def test_backtick_span_without_cw_is_ignored(self) -> None:
        assert _extract_mentions("pass `--status shipped` here") == []

    def test_introduced_mention_is_extracted_in_help_mode(self) -> None:
        text = "Suggested: cw spawn complete <id> --status shipped, then Run cw doctor."
        assert _extract_mentions(text) == [["spawn", "complete"], ["doctor"]]
