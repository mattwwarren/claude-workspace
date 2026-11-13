"""Lazy subcommand loading for the root ``cw`` group (#2934).

Importing every command module on each ``cw`` invocation cost ~0.35s, paid
even by ``cw --version`` and by every hook fire. :class:`LazyGroup` resolves a
top-level command name to its module only when Click asks for that command,
from the static :data:`LAZY_COMMANDS` map.

The map carries each command's short help, so ``cw --help`` and top-level
shell completion render without importing anything: ``short_help`` is the
command's ``get_short_help_str`` at unbounded width, and
:func:`truncate_short_help` re-truncates it per terminal width through Click's
own ``Command.get_short_help_str``, which reproduces Click's output byte for
byte.

Adding a top-level command means adding its entry here.
``tests/test_cli_lazy.py`` fails on any drift between this map and the
registered tree, and prints a regenerated literal to paste.
"""

from __future__ import annotations

import importlib
from gettext import gettext
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import click
from click.shell_completion import CompletionItem

if TYPE_CHECKING:
    from collections.abc import Mapping

# Click's default width for completion help (``Command.get_short_help_str``).
_COMPLETION_HELP_LIMIT = 45
# Click's ``Group.format_commands`` spacing allowance: limit = width - 6 - name.
_HELP_COLUMN_ALLOWANCE = 6

GUARD_HOOK_INSTALL_NAME: Final = "guard-hook-install"
GUARD_HOOK_INSTALL_COMMAND: Final = f"cw {GUARD_HOOK_INSTALL_NAME}"
GUARD_GIT_STASH_NAME: Final = "guard-git-stash"
GUARD_GIT_STASH_COMMAND: Final = f"cw {GUARD_GIT_STASH_NAME}"


def truncate_short_help(text: str, limit: int) -> str:
    """Truncate *text* to *limit* exactly as Click shortens a command's help.

    Goes through ``Command.get_short_help_str`` because
    ``click.utils.make_default_short_help`` is deprecated in Click 8.5 and
    removed in 9.0, and Click offers no public replacement.
    """
    return click.Command("_", help=text).get_short_help_str(limit)


class LazyCommand(NamedTuple):
    """Where a top-level command lives, and its static short help."""

    module: str
    """Module defining the command object, e.g. ``"cw.cli.sessions"``."""
    attr: str
    """Attribute of *module* holding the command, e.g. ``"list_sessions"``."""
    short_help: str
    """``cmd.get_short_help_str(10**6)``: first sentence, untruncated."""
    also_import: tuple[str, ...] = ()
    """Modules that register sub-commands onto this command from outside it."""


LAZY_COMMANDS: Final[dict[str, LazyCommand]] = {
    "agent-spawn-pre": LazyCommand(
        "cw.cli.agent_spawn_stamp",
        "agent_spawn_pre",
        "Apply the spawn-shape policy, then mark the spawn unresolved.",
    ),
    "agent-spawn-verify": LazyCommand(
        "cw.cli.agent_spawn_verify",
        "agent_spawn_verify",
        "Verify an async subagent spawn actually produced a transcript.",
    ),
    "background-tool-guard-pre": LazyCommand(
        "cw.cli.background_tool_guard_pre",
        "background_tool_guard_pre",
        "Refuse a backgrounded Bash call or a Monitor call in a headless worker.",
    ),
    "bg": LazyCommand(
        "cw.cli.sessions",
        "bg",
        "Background the current session (auto-handoff).",
    ),
    "board": LazyCommand(
        "cw.cli.maintenance",
        "board",
        "Lane x stage pipeline cockpit (RFC 0005 D1).",
    ),
    "body-sha": LazyCommand(
        "cw.cli.body_sha",
        "body_sha",
        (
            "Print the body_sha of a ticket body: `cw body-sha TICKET -c CLIENT`"
            " fetches it from GitHub, `cw body-sha --body-file PATH` (- for stdin)"
            " reads it from a file."
        ),
    ),
    "codex": LazyCommand(
        "cw.cli.codex",
        "codex_group",
        "Codex executor subprocess commands.",
    ),
    "completion": LazyCommand(
        "cw.cli.sessions",
        "completion",
        "Output shell completion activation script.",
    ),
    "config": LazyCommand(
        "cw.cli.config_cmds",
        "config_group",
        "Show or manage configuration.",
        also_import=("cw.cli.lane_set",),
    ),
    "daemon": LazyCommand(
        "cw.cli.sessions",
        "daemon",
        "Deprecated no-op — emits a notice and exits.",
    ),
    "dev-queue": LazyCommand(
        "cw.cli.dev_queue._group",
        "dev_queue",
        "Manage the orchestrator development queue.",
    ),
    "doctor": LazyCommand(
        "cw.cli.maintenance",
        "doctor",
        "Run environment preflight checks and print a health report.",
    ),
    "done": LazyCommand(
        "cw.cli.sessions",
        "done",
        "Mark a session as completed (not resumable).",
    ),
    "event": LazyCommand(
        "cw.cli.queues",
        "event",
        "Manage the orchestrator event bus.",
    ),
    "focus": LazyCommand(
        "cw.cli.focus",
        "focus_group",
        "Set or show what this session is working on.",
    ),
    "guard-busy-wait": LazyCommand(
        "cw.cli.guard_busy_wait",
        "guard_busy_wait",
        "Block a Bash tool call that is busy-waiting rather than doing work.",
    ),
    "guard-cwd": LazyCommand(
        "cw.cli.guard",
        "guard_cwd",
        "Block a Bash tool call when the worker cwd is the operator main checkout.",
    ),
    GUARD_GIT_STASH_NAME: LazyCommand(
        "cw.cli.guard_git_stash",
        "guard_git_stash",
        "Refuse bare git stash, stash pop and other shared-stack stash use from a "
        "headless worker's Bash call.",
    ),
    GUARD_HOOK_INSTALL_NAME: LazyCommand(
        "cw.cli.guard_hook_install",
        "guard_hook_install",
        "Refuse pre-commit install or uninstall from a headless worker's Bash call.",
    ),
    "guide": LazyCommand(
        "cw.cli.sessions",
        "guide",
        "Print the cw operator guide (how to drive a sprint with cw).",
    ),
    "init": LazyCommand(
        "cw.cli.maintenance",
        "init",
        "Initialize a new client configuration.",
    ),
    "lane": LazyCommand(
        "cw.cli.config_cmds",
        "lane",
        "Manage dispatch lanes.",
        also_import=("cw.cli.lane_set",),
    ),
    "list": LazyCommand(
        "cw.cli.sessions",
        "list_sessions",
        "List all sessions across clients.",
    ),
    "operator-channel": LazyCommand(
        "cw.cli.channels",
        "operator_channel",
        "Operator channel: push MCP notifications filtered for operator attention.",
    ),
    "orchestrate": LazyCommand(
        "cw.cli.orchestrate",
        "orchestrate",
        "Orchestrator pipeline: status snapshot and PR retirement.",
    ),
    "orchestrator-start": LazyCommand(
        "cw.cli.orchestrate",
        "orchestrator_start",
        (
            "Spawn a long-running cw orchestrator session driven by the"
            " cw-operator channel."
        ),
    ),
    "peek": LazyCommand(
        "cw.cli.sessions",
        "peek",
        "Emit the last N lines of worker output for a session.",
    ),
    "post-comment": LazyCommand(
        "cw.cli.post_comment",
        "post_comment",
        (
            "Redact paths and secret shapes from an agent-authored body, wrap and"
            " split it, post it to a GitHub issue and read every part back."
        ),
    ),
    "pr-channel": LazyCommand(
        "cw.cli.channels",
        "pr_channel",
        "PR channel server: push MCP notifications to subscribed Claude sessions.",
    ),
    "preflight": LazyCommand(
        "cw.cli.preflight",
        "preflight",
        "Run pre-flight checks for one ticket before dispatch.",
    ),
    "queue": LazyCommand(
        "cw.cli.queues",
        "queue",
        "Inspect running dev-queue sessions.",
    ),
    "queue-channel": LazyCommand(
        "cw.cli.channels",
        "queue_channel",
        "Queue channel server: push MCP notifications to subscribed Claude sessions.",
    ),
    "result": LazyCommand(
        "cw.result",
        "result",
        "Validate AutoDevResult sentinels, and emit them onto a session.",
    ),
    "resume": LazyCommand(
        "cw.cli.sessions",
        "resume",
        "Resume a backgrounded session.",
    ),
    "review": LazyCommand(
        "cw.cli.review._group",
        "review",
        "Operator review-request tracking (RFC 0011 S2).",
    ),
    "schema": LazyCommand(
        "cw.cli.maintenance",
        "schema",
        "Inspect Pydantic model schemas for AutoDevResult, TicketTask, Session.",
    ),
    "session": LazyCommand(
        "cw.cli.session_inspect",
        "session_group",
        "Inspect cw session state.",
        also_import=("cw.cli.session_prune", "cw.cli.session_send"),
    ),
    "signal-park": LazyCommand(
        "cw.cli.signal_park",
        "signal_park",
        "Record the worker's park comment for the Stop hook (#2135).",
    ),
    "signal-stop": LazyCommand(
        "cw.cli.stop_hook",
        "signal_stop",
        "Emit SESSION_COMPLETED on a Stop hook fire.",
    ),
    "spawn": LazyCommand(
        "cw.cli.spawn",
        "spawn",
        "Spawn a daemon-managed Claude session or manage spawned sessions.",
    ),
    "sprint": LazyCommand(
        "cw.cli.sprint",
        "sprint",
        "RFC -> GitHub sprint block: plan, then apply.",
    ),
    "start": LazyCommand(
        "cw.cli.sessions",
        "start",
        "Start or resume a Claude Code session for a client.",
    ),
    "status": LazyCommand(
        "cw.cli.sessions",
        "status",
        "Show status dashboard across all clients.",
    ),
    "statusline": LazyCommand(
        "cw.cli.statusline",
        "statusline_group",
        "Render Claude Code statusline segments.",
    ),
    "tests-prepare": LazyCommand(
        "cw.cli.tests_prepare",
        "tests_prepare",
        (
            "Print the tests.prepare command from ROOT's .claude/project-config.yaml,"
            " or nothing when it is unset."
        ),
    ),
    "upgrade-workers": LazyCommand(
        "cw.cli.maintenance",
        "upgrade_workers",
        "Restart all daemon-managed background sessions via ``claude respawn --all``.",
    ),
    "watch": LazyCommand(
        "cw.cli.sessions",
        "watch",
        "Live flat-table work board (sessions + dev-queue tickets).",
    ),
    "watchdog": LazyCommand(
        "cw.cli.watchdog",
        "watchdog",
        "Mainstream watchdog: standalone detect+notify tick + service management.",
    ),
    "worktree": LazyCommand(
        "cw.cli.worktree",
        "worktree_group",
        "Worktree management commands.",
    ),
}


class LazyGroup(click.Group):
    """A Click group that imports each mapped sub-command on first lookup.

    ``--help`` and top-level completion render from the map's static help and
    import nothing; ``get_command`` (used to invoke, and by ``resolve_command``
    during completion of deeper words) imports only the requested entry.
    """

    def __init__(
        self,
        *args: Any,
        lazy_commands: Mapping[str, LazyCommand] | None = None,
        **kwargs: Any,
    ) -> None:
        # ``Any`` because the arguments are forwarded verbatim to click.Group.
        super().__init__(*args, **kwargs)
        self.lazy_commands: Mapping[str, LazyCommand] = lazy_commands or {}
        self._resolved: set[str] = set()

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Sorted union of the eagerly registered and the mapped names."""
        return sorted({*super().list_commands(ctx), *self.lazy_commands})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Import *cmd_name*'s module (and companions) once, then look it up.

        Resolution runs even when the name is already in ``commands``: another
        caller may have imported the defining module directly, without the
        companions that register sub-commands onto it.
        """
        spec = self.lazy_commands.get(cmd_name)
        if spec is not None and cmd_name not in self._resolved:
            self._resolve(cmd_name, spec)
        return super().get_command(ctx, cmd_name)

    def _resolve(self, cmd_name: str, spec: LazyCommand) -> None:
        for companion in spec.also_import:
            importlib.import_module(companion)
        cmd = getattr(importlib.import_module(spec.module), spec.attr)
        # ``result`` and ``session`` are defined on no group; everything else
        # decorated itself onto ``main`` when its module was imported.
        if self.commands.get(cmd_name) is not cmd:
            self.add_command(cmd, cmd_name)
        self._resolved.add(cmd_name)

    def _listed_names(self, ctx: click.Context) -> list[str]:
        """Names shown in help and completion: mapped, or eager and visible."""
        return [
            name
            for name in self.list_commands(ctx)
            if name in self.lazy_commands or not self.commands[name].hidden
        ]

    def _summary(self, name: str, limit: int) -> str:
        spec = self.lazy_commands.get(name)
        if spec is not None:
            return truncate_short_help(spec.short_help, limit)
        return self.commands[name].get_short_help_str(limit)

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        """Click's ``Group.format_commands``, rendered without imports."""
        names = self._listed_names(ctx)
        if not names:
            return
        limit = formatter.width - _HELP_COLUMN_ALLOWANCE - max(len(n) for n in names)
        rows = [(name, self._summary(name, limit)) for name in names]
        with formatter.section(gettext("Commands")):
            formatter.write_dl(rows)

    def shell_complete(
        self, ctx: click.Context, incomplete: str
    ) -> list[CompletionItem]:
        """Complete sub-command names from the map, then options.

        Click's ``Group.shell_complete`` calls ``get_command`` on every
        matching name to read its help, which would import every module on
        ``cw <TAB>``.
        """
        results = [
            CompletionItem(name, help=self._summary(name, _COMPLETION_HELP_LIMIT))
            for name in self._listed_names(ctx)
            if name.startswith(incomplete)
        ]
        results.extend(click.Command.shell_complete(self, ctx, incomplete))
        return results
