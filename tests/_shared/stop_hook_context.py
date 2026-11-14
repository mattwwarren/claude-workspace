"""Stop-hook and hook-context helpers: transcript writers, the context-file
writer and its stamp sentinels, the headless worktree builder, and the
hook invocation driver. Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cw.config import load_state, save_state
from cw.models import (
    AGENT_SPAWN_STAMP_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
    Session,
    SessionOrigin,
    TicketTask,
)
from tests._shared.lock_helpers import _hold_flock
from tests._shared.session_seeding import _make_daemon_session

if TYPE_CHECKING:
    from collections.abc import Iterator


def _write_idle_transcript(
    home: Path,
    worktree: Path,
    filename: str = "fake-short-id-sess.jsonl",
) -> Path:
    """Write a minimal transcript .jsonl under the project dir for *worktree*.

    Default filename starts with ``fake-short-id`` so that
    ``_locate_session_transcript``'s surface_ref-prefix glob finds it when the
    session has ``surface_ref="fake-short-id"`` (the default in
    ``_mk_headless_daemon_session``).
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    record = '{"type": "assistant", "message": {"role": "assistant", "content": []}}\n'
    path.write_text(record)
    return path


def _write_stop_hook_transcript(
    home: Path,
    worktree: Path,
    claude_session_id: str,
    assistant_text: str = "",
    *,
    records: list[dict[str, Any]] | None = None,
) -> Path:
    """Write ``<claude_session_id>.jsonl`` under *worktree*'s project dir.

    Promoted from ``TestSignalStop._write_transcript`` (``tests/test_cli.py``,
    #1692), whose 19 in-class call sites now import and call this helper
    directly. Keyed on an exact ``claude_session_id``, matching how the
    Stop-hook path actually resolves a sentinel: ``_parse_headless_sentinel``
    -> ``_parse_sentinel_from_transcript(cwd_value, csid)``
    (``cw.cli.stop_hook``) looks the transcript up by exact
    ``claude_session_id``, not by a surface_ref-prefix glob -- the mechanism
    :func:`_write_idle_transcript` above targets instead. Param order/return
    type follow that sibling's ``(home, worktree, ...) -> Path`` convention.
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    if records is None:
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": assistant_text}],
                },
            }
        ]
    path = project_dir / f"{claude_session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


# ``_write_hook_context_file``'s ``stamp`` sentinels (#2229): keep the real
# writer's seeded ``{0, None}`` stamp, or delete the key entirely (a legacy
# pre-#1646 context). Any other value replaces the stamp verbatim.
_STAMP_UNCHANGED: object = object()


_STAMP_ABSENT: object = object()


def _write_hook_context_file(
    worktree: Path,
    workspace_path: Path | None = None,
    lane: str | None = None,
    stamp: object = _STAMP_UNCHANGED,
    headless: bool = False,
    *,
    session_id: str = "sess940g",
    ticket_id: str = "940",
    client: str = "client-a",
    task: TicketTask | None = None,
) -> None:
    """Materialize ``<worktree>/.claude/cw-context.json`` via the real writer.

    Hoisted from ``test_cli_guard.py``'s private ``_write_context`` (#1646) so
    the guard hook tests and the agent-spawn-stamp hook tests share one
    materializer. Using the real ``_write_hook_context`` (rather than a
    hand-written JSON literal) is deliberate: it keeps every hook test reading
    the exact context shape production writes, including new schema fields.

    *lane* (#1946) forwards to the real writer's new ``lane`` parameter so
    ``cw guard-busy-wait``'s per-lane config tests read the same ``"lane"``
    key production stamps — an ad hoc parallel JSON writer in the test file
    is exactly the fixture drift this helper's hoist exists to prevent.

    *stamp* (#2229) overrides the seeded ``agent_spawn_stamp`` after the real
    writer runs: ``_STAMP_ABSENT`` deletes the key, any other non-default
    value replaces it, so the Stop hook's stamp-shape edge cases are seeded
    through the same file the production writer produced.

    *headless* (#2211) forwards to the real writer's ``headless`` parameter so
    ``cw agent-spawn-pre``'s spawn-shape policy — which applies only to
    headless dispatch workers — reads the same ``"headless"`` key production
    stamps, for the same anti-drift reason as *lane* above.

    *session_id*, *ticket_id* and *client* (#2591) default to the literals
    every earlier caller relied on, and *task* defaults to ``None``, which
    passes nothing extra to the real writer, so those callers' files are
    byte-identical. A claim's context passes the claim's own values plus
    ``task=`` (the claimed row): the writer then stamps the ``attempt`` key
    from ``task.attempts``, as a dispatch spawn does, and runs
    ``git rev-parse`` (outside any lock here). ``session_name`` stays
    ``client-a/impl`` whatever *client* is: reconcile's adoption reads the
    Session's own name, never this one.
    """
    from cw.spawn import _write_hook_context

    _write_hook_context(
        worktree,
        session_id=session_id,
        session_name="client-a/impl",
        client=client,
        purpose="impl",
        ticket_id=ticket_id,
        origin=SessionOrigin.DAEMON,
        headless=headless,
        task=task,
        workspace_path=workspace_path,
        lane=lane,
    )
    if stamp is _STAMP_UNCHANGED:
        return
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context = json.loads(context_path.read_text(encoding="utf-8"))
    if stamp is _STAMP_ABSENT:
        del context[AGENT_SPAWN_STAMP_KEY]
    else:
        context[AGENT_SPAWN_STAMP_KEY] = stamp
    context_path.write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")


def _headless_worktree(tmp_path: Path, name: str = "wt") -> Path:
    """A worktree whose context marks it a headless dispatch worker (#2211).

    The precondition for every ``cw agent-spawn-pre`` spawn-shape test, since
    the policy applies to headless workers and nowhere else. Lives here rather
    than in either test file because both ``test_cli_agent_spawn_stamp.py``
    and ``test_cli_subagent_policy.py`` need it, and they had grown identical
    private copies.
    """
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree, headless=True)
    return worktree


def _seed_session(tmp_path: Path, sess_id: str = "sess940g") -> Session:
    """Seed a DAEMON session whose id matches ``_write_hook_context_file``'s.

    The default *sess_id* must equal the ``session_id`` default that
    ``_write_hook_context_file`` above hardcodes (``"sess940g"``, #1646) --
    matching the id is what lets whole-object ``state.sessions`` snapshot
    assertions prove the Stop hook's deferral/clear paths never touch session
    state, mirroring ``TestSignalStop._seed_session`` in ``tests/test_cli.py``
    (not reused directly: that helper is a private method of a class in a
    different module). Hoisted from ``tests/test_cli_stop_hook.py`` (#2688) so
    the stop-hook stamp tests share it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    session = _make_daemon_session(
        id=sess_id,
        name="client-a/impl",
        client="client-a",
        workspace_path=workspace,
        worktree_path=worktree,
        surface_ref=None,
        started_at=datetime.now(UTC),
    )
    state = load_state()
    state.sessions.append(session)
    save_state(state)
    return session


def _read_stamp(worktree: Path) -> dict[str, object]:
    """Return the worktree context's ``agent_spawn_stamp`` dict (#2688 hoist)."""
    context = json.loads(
        (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    stamp = context[AGENT_SPAWN_STAMP_KEY]
    assert isinstance(stamp, dict)
    return stamp


def _stop_payload(worktree: Path, **extra: object) -> dict[str, object]:
    """A minimal Stop hook payload for *worktree*, plus *extra* keys (#2688 hoist)."""
    return {
        "session_id": "claude-uuid-2229",
        "cwd": str(worktree),
        "hook_event_name": "Stop",
        **extra,
    }


def _symlink_loop(base: Path, name: str = "loop") -> Path:
    """Create two symlinks under *base* pointing at each other; return one."""
    first, second = base / f"{name}-a", base / f"{name}-b"
    first.symlink_to(second)
    second.symlink_to(first)
    return first


def _write_global_toggle(tmp_config_dir: Path, toggle: str, value: str) -> None:
    """Write an orchestrator.yaml setting one guard's global *toggle* to *value*.

    The global half of a per-lane guard override (a lane mapping such as
    ``{"name": "fast", toggle: False}`` written with
    :func:`tests._clients_yaml.write_clients_yaml`): the shared guard-toggle
    resolver's tests and both guards' kill-switch tests need it.
    """
    orchestrator_path = tmp_config_dir / ".claude-workspace" / "orchestrator.yaml"
    orchestrator_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator_path.write_text(f"{toggle}: {value}\n")


@contextlib.contextmanager
def _hold_context_lock(worktree: Path) -> Iterator[None]:
    """Hold ``<worktree>/.claude/cw-context.json.lock`` exclusively (#1946).

    Reproduces the contended-lock condition every hook write path must fail
    open on. Hoisted from ``test_cli_agent_spawn_stamp.py``'s inline
    ``fcntl.flock`` setup so the three consumers of
    ``cw._hook_context._write_cw_context_locked`` share one technique instead
    of each re-deriving it — the same "don't duplicate the discipline"
    reasoning that promoted the write primitive itself into one shared
    module (now ``cw._hook_context``).

    Callers must also shorten ``cw._hook_context._LOCK_TIMEOUT_SECS_DEFAULT``
    (patched where it is *defined*, never on a re-exporting module) so the
    bounded retry budget expires quickly.
    """
    with _hold_flock(worktree / ".claude" / "cw-context.json.lock"):
        yield


def _invoke_hook_command(command: str, payload: dict[str, object]) -> Any:
    """``CliRunner``-invoke hook subcommand *command* with *payload* on stdin.

    Returns the click ``Result``. Hoisted from ``test_cli_guard.py``'s private
    ``_invoke`` (#1646) and generalized over the command name, since every cw
    hook handler shares the same "JSON object on stdin, exit code is the
    contract" surface.
    """
    from click.testing import CliRunner

    from cw.cli import main

    runner = CliRunner()
    return runner.invoke(main, [command], input=json.dumps(payload))
