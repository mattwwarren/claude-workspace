"""Whole-tree invariants over the ``cw`` Click command surface (#2690).

Two conventions an orchestrator relies on without reading source:

- every ``--client`` option also accepts ``-c``;
- every ``cw ...`` command named in help text (command help, short help,
  epilog, option help) resolves to a real command.

Both are walked over the live Click tree rather than grepped from source, so a
new command or a rewrapped help string is covered without touching this file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import click

from cw.cli import main

# Commands where ``-c`` is bound to a non-client option, so ``--client`` cannot
# also take it. Keyed by command path, valued by the reason. Empty today: no
# command binds ``-c`` to anything but its client option.
_CLIENT_SHORT_EXEMPT: dict[tuple[str, ...], str] = {}

# ``cw ...`` help mentions that intentionally name something that is not a
# command. Empty today: every mention must resolve.
_MENTION_EXEMPT: set[str] = set()

_MIN_COMMAND_COUNT = 40
_LINE_PREFIXES = ("Recipe:", "Example:")
_MENTION_RE = re.compile(r"\bcw((?: [a-z][a-z0-9_-]*)*)")
_BACKTICK_SPAN_RE = re.compile(r"(`+)([^`]+)\1")
_STOP_PREFIXES = ("-", "<", "[", ".")


def _ctx() -> click.Context:
    return click.Context(main)


def _walk(
    root: click.Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    """Yield ``(path, command)`` for *root* and every command beneath it."""
    yield path, root
    if isinstance(root, click.Group):
        ctx = _ctx()
        for name in root.list_commands(ctx):
            sub = root.get_command(ctx, name)
            if sub is not None:
                yield from _walk(sub, (*path, name))


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


def _extract_mentions(text: str) -> list[list[str]]:
    """Return the subcommand tokens of each ``cw ...`` mention in *text*.

    Order matters: whole lines starting with ``cw `` are read from the raw text
    first, because whitespace normalization (needed so backticked spans may
    wrap across lines) leaves no lines behind.
    """
    sources: list[str] = []
    for line in text.split("\n"):
        candidate = _strip_line_prefix(line)
        if candidate.startswith("cw "):
            sources.append(candidate)
    normalized = " ".join(text.split())
    sources.extend(
        match.group(2)
        for match in _BACKTICK_SPAN_RE.finditer(normalized)
        if _MENTION_RE.search(match.group(2))
    )
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


class TestTreeWalk:
    def test_tree_sanity(self) -> None:
        paths = [path for path, _cmd in _walk(main)]
        assert len(paths) > _MIN_COMMAND_COUNT
        assert ("dev-queue", "tasks") in paths
        assert ("dev-queue", "show") in paths
        assert ("event", "tail") in paths


class TestClientShortOption:
    def test_every_client_option_accepts_dash_c(self) -> None:
        missing = [
            "cw " + " ".join(path)
            for path, cmd in _walk(main)
            if path not in _CLIENT_SHORT_EXEMPT
            for param in cmd.params
            if isinstance(param, click.Option)
            and "--client" in param.opts
            and "-c" not in param.opts
        ]
        assert missing == [], f"--client without -c on: {missing}"

    def test_exempt_entries_are_real_collisions(self) -> None:
        commands = dict(_walk(main))
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
        for path, cmd in _walk(main):
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
