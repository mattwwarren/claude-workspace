"""Lazy subcommand loading for the root ``cw`` group (#2934).

``cw --version`` used to pay ~0.35s importing every command module. The root
group is now a :class:`cw.cli._lazy.LazyGroup` resolving each top-level name
from the static :data:`cw.cli._lazy.LAZY_COMMANDS` map on first use. These
tests pin:

- the isolation contract: ``import cw.cli``, ``--version``, ``--help`` and
  top-level completion load none of the heavy modules (subprocess probes on
  the same interpreter, the ``tests/test_queue_rows_package.py`` idiom);
- the map against the real command tree (drift test), including each entry's
  static short help at several terminal widths;
- each entry resolving its full subtree alone in a fresh interpreter, which
  catches a missing ``also_import`` companion (``lane_set``, ``session_*``);
- :class:`LazyGroup` behaviour against a synthetic command module.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import click
import pytest
from click.utils import make_default_short_help

from cw.cli import main
from cw.cli._lazy import LAZY_COMMANDS, LazyCommand, LazyGroup

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from click.shell_completion import CompletionItem

# Modules that must stay out of ``sys.modules`` on the cheap paths: the real
# import cost (pydantic, models, config) and the command modules that pull it.
_FORBIDDEN_EXACT = (
    "cw.dispatch",
    "cw.executor",
    "cw.orchestrate",
    "cw.dev_queue",
    "cw.models",
    "cw.config",
    "cw.reconcile",
    "cw.spawn",
    "cw.result",
    "cw.session",
    "cw.cli.orchestrate",
    "cw.cli.dev_queue",
    "cw.cli.sessions",
    "cw.cli.spawn",
    "pydantic",
)
_FORBIDDEN_PREFIXES = (
    "cw.dispatch.",
    "cw.executor.",
    "cw.reconcile.",
    "cw.dev_queue.",
    "cw.models.",
    "cw.cli.dev_queue.",
)

_FORBIDDEN_CHECK = (
    "import sys\n"
    f"_exact = {_FORBIDDEN_EXACT!r}\n"
    f"_prefixes = {_FORBIDDEN_PREFIXES!r}\n"
    "_bad = sorted(k for k in sys.modules if k in _exact or k.startswith(_prefixes))\n"
    "assert not _bad, _bad\n"
)

# Renders a command's subtree as nested ``{name: subtree | None}`` dicts.
_TREE_SOURCE = (
    "def _tree(cmd):\n"
    "    if not isinstance(cmd, click.Group):\n"
    "        return None\n"
    "    ctx = click.Context(cmd)\n"
    "    return {n: _tree(cmd.get_command(ctx, n)) for n in cmd.list_commands(ctx)}\n"
)

_HELP_WIDTHS = (10, 20, 45, 80, 200)
_UNBOUNDED = 10**6

# Imports every submodule of ``cw.cli`` *before* touching the lazy map, then
# reports what the modules registered on their own and each top-level command's
# full subtree. Nothing here depends on ``also_import``: every registering
# module is already loaded, so it is an independent reference for the map.
_IMPORT_ALL_HEAD = (
    "import importlib\n"
    "import json\n"
    "import pkgutil\n"
    "import sys\n"
    "import click\n"
    "import cw.cli\n"
    "from cw.cli import main\n"
    "from cw.cli._lazy import LAZY_COMMANDS\n"
    "for info in pkgutil.walk_packages(cw.cli.__path__, 'cw.cli.'):\n"
    "    importlib.import_module(info.name)\n"
)
_IMPORT_ALL_TAIL = (
    "def _attr(cmd):\n"
    "    module = sys.modules[cmd.callback.__module__]\n"
    "    return next(k for k, v in vars(module).items() if v is cmd)\n"
    "registered = {\n"
    "    n: [c.callback.__module__, _attr(c), c.get_short_help_str(10**6)]\n"
    "    for n, c in main.commands.items()\n"
    "}\n"
    "ctx = click.Context(main)\n"
    "trees = {n: _tree(main.get_command(ctx, n)) for n in LAZY_COMMANDS}\n"
    "print(json.dumps({'registered': registered, 'trees': trees}))\n"
)
_IMPORT_ALL_SOURCE = _IMPORT_ALL_HEAD + _TREE_SOURCE + _IMPORT_ALL_TAIL


def _run_probe(
    body: str, *, forbid: bool = True, env: dict[str, str] | None = None
) -> str:
    """Run *body* in a fresh ``-I`` interpreter (same executable); return stdout.

    With *forbid*, the probe then asserts no forbidden module was imported.
    """
    probe = body + (_FORBIDDEN_CHECK if forbid else "")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        env=env if env is not None else os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@functools.cache
def _import_everything() -> dict[str, Any]:
    """Registered commands and full subtrees with every ``cw.cli`` module loaded.

    ``registered`` maps name -> ``[module, attr, short_help]``; ``trees`` maps
    each ``LAZY_COMMANDS`` name to its nested sub-command dict.
    """
    out = _run_probe(_IMPORT_ALL_SOURCE, forbid=False)
    snapshot: dict[str, Any] = json.loads(out)
    return snapshot


def _resolve(name: str) -> click.Command:
    cmd = main.get_command(click.Context(main), name)
    assert cmd is not None, name
    return cmd


def _group_names() -> list[str]:
    return [n for n in LAZY_COMMANDS if isinstance(_resolve(n), click.Group)]


def _regenerated_map() -> str:
    """A pasteable ``LAZY_COMMANDS`` literal built from the live tree."""
    for name in LAZY_COMMANDS:
        _resolve(name)
    lines = ["LAZY_COMMANDS: Final[dict[str, LazyCommand]] = {"]
    for name in sorted(main.commands):
        cmd = main.commands[name]
        spec = LAZY_COMMANDS.get(name)
        callback = cmd.callback
        module = spec.module if spec else getattr(callback, "__module__", "?")
        attr = spec.attr if spec else getattr(callback, "__name__", "?")
        also = spec.also_import if spec else ()
        short = cmd.get_short_help_str(_UNBOUNDED)
        lines.append(
            f"    {name!r}: LazyCommand({module!r}, {attr!r}, {short!r}"
            + (f", also_import={also!r}" if also else "")
            + "),"
        )
    lines.append("}")
    return "\n".join(lines)


# --- isolation pin ----------------------------------------------------------


class TestIsolationPin:
    def test_import_cw_cli_loads_no_heavy_module(self) -> None:
        _run_probe("import cw.cli\n")

    def test_version_loads_no_heavy_module(self) -> None:
        out = _run_probe(
            "from cw.cli import main\n"
            "try:\n"
            "    main.main(['--version'], prog_name='cw', standalone_mode=False)\n"
            "except SystemExit:\n"
            "    pass\n"
        )
        assert out.startswith("cw, version ")

    def test_help_lists_every_command_and_loads_no_heavy_module(self) -> None:
        out = _run_probe(
            "from cw.cli import main\n"
            "try:\n"
            "    main.main(['--help'], prog_name='cw', standalone_mode=False)\n"
            "except SystemExit:\n"
            "    pass\n"
        )
        listed = {
            line.split()[0]
            for line in out.split("Commands:", 1)[1].splitlines()
            if line.startswith("  ") and line.strip()
        }
        assert listed == set(LAZY_COMMANDS)

    def test_top_level_completion_loads_no_heavy_module(self) -> None:
        out = _run_probe(
            "import click\n"
            "from cw.cli import main\n"
            "items = main.shell_complete(click.Context(main), '')\n"
            "print('\\n'.join(i.value for i in items))\n"
        )
        assert set(LAZY_COMMANDS) <= set(out.split())

    def test_positive_control_resolving_dev_queue_loads_it(self) -> None:
        """The probe is not vacuous: resolving a heavy command does load it."""
        _run_probe(
            "import sys\n"
            "import click\n"
            "from cw.cli import main\n"
            "assert main.get_command(click.Context(main), 'dev-queue') is not None\n"
            "assert 'cw.cli.dev_queue' in sys.modules\n",
            forbid=False,
        )

    def test_negative_control_detector_fires_on_a_forbidden_import(self) -> None:
        """The forbidden-module check can fail: importing dev-queue trips it.

        Every ``forbid=True`` test passes on a clean import, so without this a
        detector that never fires (emptied lists, broken comparison) would
        leave the whole isolation pin green.
        """
        with pytest.raises(AssertionError) as excinfo:
            _run_probe("import cw.cli.dev_queue\n")
        assert "cw.cli.dev_queue" in str(excinfo.value)

    def test_resolving_one_command_does_not_load_another(self) -> None:
        _run_probe(
            "import sys\n"
            "import click\n"
            "from cw.cli import main\n"
            "assert main.get_command(click.Context(main), 'body-sha') is not None\n"
            "assert 'cw.cli.body_sha' in sys.modules\n"
            "assert 'cw.cli.dev_queue' not in sys.modules\n",
            forbid=False,
        )


# --- drift test -------------------------------------------------------------


class TestLazyMapDrift:
    def test_map_names_equal_registered_names(self) -> None:
        for name in LAZY_COMMANDS:
            _resolve(name)
        assert set(main.commands) == set(LAZY_COMMANDS), _regenerated_map()

    def test_every_command_a_cw_cli_module_registers_is_mapped(self) -> None:
        """A new command module absent from the map fails here, not silently.

        The in-process test above only sees commands registered by modules some
        map entry already imported, so a brand-new module is invisible to it
        and the command would vanish from the CLI, ``--help`` and completion.
        This one imports every ``cw.cli`` submodule in a fresh interpreter and
        requires each name it registered on ``main`` to be a map key. The
        reverse direction is not required: ``result`` and ``session`` are added
        by the map itself and never decorated onto ``main``.
        """
        registered = _import_everything()["registered"]
        missing = sorted(set(registered) - set(LAZY_COMMANDS))
        assert not missing, (
            f"commands registered by cw.cli modules but absent from LAZY_COMMANDS: "
            f"{missing}\nadd:\n"
            + "\n".join(
                f"    {name!r}: LazyCommand({module!r}, {attr!r}, {short!r}),"
                for name in missing
                for module, attr, short in [registered[name]]
            )
        )

    @pytest.mark.parametrize("name", sorted(LAZY_COMMANDS))
    def test_entry_points_at_the_registered_command(self, name: str) -> None:
        spec = LAZY_COMMANDS[name]
        cmd = _resolve(name)
        target = getattr(importlib.import_module(spec.module), spec.attr)
        assert target is cmd, _regenerated_map()
        assert cmd.name == name, _regenerated_map()

    @pytest.mark.parametrize("name", sorted(LAZY_COMMANDS))
    def test_static_short_help_matches_at_every_width(self, name: str) -> None:
        spec = LAZY_COMMANDS[name]
        cmd = _resolve(name)
        assert spec.short_help == cmd.get_short_help_str(_UNBOUNDED), _regenerated_map()
        for limit in _HELP_WIDTHS:
            assert make_default_short_help(
                spec.short_help, limit
            ) == cmd.get_short_help_str(limit), (name, limit)

    def test_map_is_alphabetical(self) -> None:
        assert list(LAZY_COMMANDS) == sorted(LAZY_COMMANDS)


# --- subtree isolation ------------------------------------------------------


@pytest.mark.parametrize("name", _group_names())
def test_group_resolves_its_full_subtree_alone(name: str) -> None:
    """Resolving only *name* in a fresh interpreter yields the full subtree.

    Fails when a module registering onto this group (fan-in) is missing from
    the entry's ``also_import``. The reference tree comes from a second
    interpreter that imported every ``cw.cli`` module first, so it does not
    share the map's gap and does not depend on which test modules ran earlier.
    """
    out = _run_probe(
        "import json\n"
        "import click\n"
        "from cw.cli import main\n"
        + _TREE_SOURCE
        + f"cmd = main.get_command(click.Context(main), {name!r})\n"
        + "print(json.dumps(_tree(cmd)))\n",
        forbid=False,
    )
    assert json.loads(out) == _import_everything()["trees"][name]


# --- completion end to end --------------------------------------------------


def _complete(comp_words: str, comp_cword: int, *, forbid: bool = False) -> set[str]:
    """Run the real ``_CW_COMPLETE`` bash path; *forbid* checks heavy imports.

    Click ends completion with ``SystemExit``; the probe swallows it so the
    forbidden-module check appended by ``_run_probe`` still runs.
    """
    env = os.environ.copy()
    env.update(
        {
            "_CW_COMPLETE": "bash_complete",
            "COMP_WORDS": comp_words,
            "COMP_CWORD": str(comp_cword),
        }
    )
    out = _run_probe(
        "from cw.cli import main\n"
        "try:\n"
        "    main(prog_name='cw')\n"
        "except SystemExit:\n"
        "    pass\n",
        forbid=forbid,
        env=env,
    )
    return set(out.splitlines())


class TestCompletionEndToEnd:
    def test_top_level_lists_every_command(self) -> None:
        items = _complete("cw ", 1, forbid=True)
        assert {f"plain,{name}" for name in LAZY_COMMANDS} <= items

    def test_top_level_prefix(self) -> None:
        assert _complete("cw dev-q", 1, forbid=True) == {"plain,dev-queue"}

    def test_subcommand_completion_resolves_the_group(self) -> None:
        assert "plain,status" in _complete("cw dev-queue st", 2)


# --- parity with stock click --------------------------------------------------

_PARITY_WIDTHS = (20, 40, 60, 80, 120, 200)
_PARITY_PREFIXES = ("", "d", "dev", "dev-q", "s", "st", "--", "--v", "zzz")


def _all_resolved_main() -> click.Group:
    """``main`` with every command imported, as stock click needs to render it."""
    for name in LAZY_COMMANDS:
        _resolve(name)
    return main


def _item_tuples(items: list[CompletionItem]) -> list[tuple[str, str | None, str]]:
    return [(i.value, i.help, i.type) for i in items]


class TestStockClickParity:
    """``LazyGroup`` re-implements two click internals; pin them to click itself.

    ``format_commands`` and ``shell_complete`` copy click's width arithmetic
    and the help limit of 45. The expected side here is click's *own* method
    run on the same, fully resolved group, not a recomputed formula, so a
    click upgrade that changes either shows up as a diff.
    """

    @pytest.mark.parametrize("width", _PARITY_WIDTHS)
    def test_format_commands_equals_stock(self, width: int) -> None:
        group = _all_resolved_main()
        ctx = click.Context(group, terminal_width=width, max_content_width=width)
        lazy_fmt, stock_fmt = ctx.make_formatter(), ctx.make_formatter()
        group.format_commands(ctx, lazy_fmt)
        click.Group.format_commands(group, ctx, stock_fmt)
        assert lazy_fmt.getvalue() == stock_fmt.getvalue()
        assert "Commands:" in stock_fmt.getvalue()

    @pytest.mark.parametrize("width", _PARITY_WIDTHS)
    def test_full_help_equals_a_plain_group_with_the_same_commands(
        self, width: int
    ) -> None:
        group = _all_resolved_main()
        plain = click.Group(
            name=group.name,
            help=group.help,
            params=group.params,
            commands=dict(group.commands),
        )
        lazy_ctx = click.Context(
            group, info_name="cw", terminal_width=width, max_content_width=width
        )
        plain_ctx = click.Context(
            plain, info_name="cw", terminal_width=width, max_content_width=width
        )
        assert group.get_help(lazy_ctx) == plain.get_help(plain_ctx)

    @pytest.mark.parametrize("incomplete", _PARITY_PREFIXES)
    def test_shell_complete_equals_stock(self, incomplete: str) -> None:
        group = _all_resolved_main()
        ctx = click.Context(group)
        lazy = group.shell_complete(ctx, incomplete)
        stock = click.Group.shell_complete(group, ctx, incomplete)
        assert _item_tuples(lazy) == _item_tuples(stock)
        if incomplete in ("", "d"):
            assert lazy

    def test_every_mapped_command_is_visible_to_stock_click(self) -> None:
        """The lazy listing assumes mapped commands are never hidden."""
        group = _all_resolved_main()
        assert [n for n in LAZY_COMMANDS if group.commands[n].hidden] == []


# --- LazyGroup unit tests ---------------------------------------------------

_CMDS_MODULE = "_cw_lazyfix_cmds"
_EXTRA_MODULE = "_cw_lazyfix_extra"
_FIXTURE_MODULES = (_CMDS_MODULE, _EXTRA_MODULE)

_CMDS_SOURCE = '''\
import click


@click.command("alpha", help="Alpha does a thing. Then some more detail.")
def alpha() -> None:
    """Unused body."""


@click.group("beta", help="Beta group of things.")
def beta() -> None:
    """Unused body."""
'''

_EXTRA_SOURCE = f'''\
from {_CMDS_MODULE} import beta


@beta.command("gamma")
def gamma() -> None:
    """Gamma registers onto beta from another module."""
'''

_SPECS = {
    "alpha": LazyCommand(_CMDS_MODULE, "alpha", "Alpha does a thing."),
    "beta": LazyCommand(
        _CMDS_MODULE, "beta", "Beta group of things.", also_import=(_EXTRA_MODULE,)
    ),
}


@pytest.fixture
def fixture_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Write the synthetic command modules onto ``sys.path``; unload after."""
    (tmp_path / f"{_CMDS_MODULE}.py").write_text(_CMDS_SOURCE)
    (tmp_path / f"{_EXTRA_MODULE}.py").write_text(_EXTRA_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    for mod in _FIXTURE_MODULES:
        sys.modules.pop(mod, None)
    yield
    for mod in _FIXTURE_MODULES:
        sys.modules.pop(mod, None)


def _make_group(
    specs: dict[str, LazyCommand] | None = None,
    commands: dict[str, click.Command] | None = None,
) -> LazyGroup:
    return LazyGroup(
        name="root",
        commands=commands,
        lazy_commands=_SPECS if specs is None else specs,
    )


@pytest.mark.usefixtures("fixture_modules")
class TestLazyGroup:
    def test_unknown_name_returns_none(self) -> None:
        group = _make_group()
        assert group.get_command(click.Context(group), "nope") is None

    def test_default_lazy_commands_is_empty(self) -> None:
        group = LazyGroup(name="root")
        assert group.lazy_commands == {}
        assert group.list_commands(click.Context(group)) == []

    def test_get_command_imports_module_on_first_use(self) -> None:
        group = _make_group()
        assert _CMDS_MODULE not in sys.modules
        cmd = group.get_command(click.Context(group), "alpha")
        assert _CMDS_MODULE in sys.modules
        assert cmd is sys.modules[_CMDS_MODULE].alpha
        assert group.commands["alpha"] is cmd
        assert _EXTRA_MODULE not in sys.modules

    def test_get_command_imports_companions(self) -> None:
        group = _make_group()
        cmd = group.get_command(click.Context(group), "beta")
        assert isinstance(cmd, click.Group)
        assert _EXTRA_MODULE in sys.modules
        assert sorted(cmd.commands) == ["gamma"]

    def test_preregistered_command_still_imports_companions(self) -> None:
        """A name already in ``commands`` (module imported elsewhere first)
        still runs its companion import on first ``get_command``."""
        module = importlib.import_module(_CMDS_MODULE)
        group = _make_group(commands={"beta": module.beta})
        assert _EXTRA_MODULE not in sys.modules
        cmd = group.get_command(click.Context(group), "beta")
        assert cmd is module.beta
        assert _EXTRA_MODULE in sys.modules

    def test_second_lookup_does_not_reimport(self) -> None:
        group = _make_group()
        ctx = click.Context(group)
        first = group.get_command(ctx, "alpha")
        sys.modules.pop(_CMDS_MODULE)
        assert group.get_command(ctx, "alpha") is first
        assert _CMDS_MODULE not in sys.modules

    def test_list_commands_is_sorted_union(self) -> None:
        group = _make_group(commands={"zeta": click.Command("zeta")})
        ctx = click.Context(group)
        assert group.list_commands(ctx) == ["alpha", "beta", "zeta"]

    def test_help_renders_static_text_without_importing(self) -> None:
        eager = click.Command("zeta", help="Zeta is eager. More.")
        hidden = click.Command("hid", help="Hidden.", hidden=True)
        group = _make_group(commands={"zeta": eager, "hid": hidden})
        text = group.get_help(click.Context(group, terminal_width=80))
        assert "alpha  Alpha does a thing." in text
        assert "beta   Beta group of things." in text
        assert "zeta   Zeta is eager." in text
        assert "hid" not in text
        assert _CMDS_MODULE not in sys.modules

    def test_help_truncates_to_width_like_click(self) -> None:
        group = _make_group()
        ctx = click.Context(group, terminal_width=20, max_content_width=20)
        text = group.get_help(ctx)
        limit = 20 - 6 - len("alpha")
        assert make_default_short_help("Alpha does a thing.", limit) in text

    def test_help_without_visible_commands_has_no_section(self) -> None:
        hidden = click.Command("hid", hidden=True)
        group = _make_group(specs={}, commands={"hid": hidden})
        assert "Commands:" not in group.get_help(click.Context(group))

    def test_shell_complete_is_prefix_filtered_with_help(self) -> None:
        group = _make_group()
        items = group.shell_complete(click.Context(group), "al")
        assert [(i.value, i.help) for i in items] == [("alpha", "Alpha does a thing.")]
        assert _CMDS_MODULE not in sys.modules

    def test_shell_complete_includes_eager_and_skips_hidden(self) -> None:
        eager = click.Command("zeta", help="Zeta is eager.")
        hidden = click.Command("zhid", hidden=True)
        group = _make_group(commands={"zeta": eager, "zhid": hidden})
        items = group.shell_complete(click.Context(group), "z")
        assert [(i.value, i.help) for i in items] == [("zeta", "Zeta is eager.")]

    def test_shell_complete_yields_options(self) -> None:
        group = _make_group()
        items = group.shell_complete(click.Context(group), "--")
        assert "--help" in {i.value for i in items}
        assert _CMDS_MODULE not in sys.modules

    def test_missing_module_propagates(self) -> None:
        group = _make_group(specs={"x": LazyCommand("_cw_lazyfix_absent", "x", "X.")})
        with pytest.raises(ModuleNotFoundError):
            group.get_command(click.Context(group), "x")

    def test_missing_attribute_propagates(self) -> None:
        group = _make_group(specs={"x": LazyCommand(_CMDS_MODULE, "nope", "X.")})
        with pytest.raises(AttributeError):
            group.get_command(click.Context(group), "x")
