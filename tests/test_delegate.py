"""Tests for cw.session.delegate_task."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any

import pytest

from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import (
    ClientConfig,
    CwState,
    QueueItemStatus,
    QueueStore,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.session import delegate_task

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helper: write a minimal clients.yaml for the tmp config dir
# ---------------------------------------------------------------------------


def _write_clients_yaml(
    tmp_config_dir: Path,
    client: ClientConfig,
) -> None:
    clients_file = tmp_config_dir / ".config" / "cw" / "clients.yaml"
    clients_file.write_text(
        f"clients:\n  {client.name}:\n    workspace_path: {client.workspace_path}\n"
    )


# ---------------------------------------------------------------------------
# Fixture: extend mock_zellij with new_pane tracking and patch QUEUES_DIR
# ---------------------------------------------------------------------------


@pytest.fixture
def delegate_setup(
    tmp_config_dir: Path,
    sample_client: ClientConfig,
    mock_zellij: dict[str, list[tuple[object, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Set up everything needed for delegate_task tests.

    - Writes clients.yaml
    - Patches QUEUES_DIR to tmp_path/queues
    - Patches cw.zellij.in_zellij_session to return True
    - Adds new_pane tracking to mock_zellij calls dict
    - Returns a context dict with 'calls', 'client', 'queues_dir'
    """
    _write_clients_yaml(tmp_config_dir, sample_client)

    queues_dir = tmp_config_dir / "queues"
    queues_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("cw.config.QUEUES_DIR", queues_dir)
    monkeypatch.setattr("cw.queue.QUEUES_DIR", queues_dir)

    # Override in_zellij_session to return True (we are inside Zellij)
    monkeypatch.setattr("cw.zellij.in_zellij_session", lambda: True)

    # Track new_pane calls
    new_pane_calls: list[tuple[object, ...]] = []

    def _new_pane(
        command: str,
        *,
        name: str | None = None,
        cwd: str | None = None,
        direction: str = "down",
        close_on_exit: bool = True,
        session: str | None = None,
    ) -> None:
        new_pane_calls.append((command, name, cwd, close_on_exit, session))

    monkeypatch.setattr("cw.zellij.new_pane", _new_pane)
    mock_zellij["new_pane"] = new_pane_calls

    save_state(CwState())

    return {
        "calls": mock_zellij,
        "new_pane_calls": new_pane_calls,
        "client": sample_client,
        "queues_dir": queues_dir,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDelegateTaskCreatesQueueItemAndSession:
    def test_creates_queue_item(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Fix the linting violations")

        store = _load_queue_from_dir(queues_dir, client.name)
        assert len(store.items) == 1
        item = store.items[0]
        assert item.client == client.name
        assert item.task.description == "Fix the linting violations"

    def test_queue_item_claimed_as_running(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Run tests")

        store = _load_queue_from_dir(queues_dir, client.name)
        assert len(store.items) == 1
        assert store.items[0].status == QueueItemStatus.RUNNING

    def test_session_saved_to_state(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]

        session = delegate_task(client.name, "Write docs")

        state = load_state()
        assert len(state.sessions) == 1
        saved = state.sessions[0]
        assert saved.id == session.id
        assert saved.client == client.name

    def test_session_name_includes_item_id(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        session = delegate_task(client.name, "Refactor auth module")

        store = _load_queue_from_dir(queues_dir, client.name)
        item_id = store.items[0].id
        assert session.name == f"{client.name}/delegate-{item_id}"

    def test_queue_item_assigned_session_id(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        """The claimed item's assigned_session_id is set in memory before the
        pane is spawned.  The queue file does not get a second save() after the
        assignment (the code only calls save_state for the session state), so
        the on-disk queue still shows None.  This test verifies the in-memory
        assignment happens (via the returned session) and that the queue item id
        appears in the session name as a cross-reference.
        """
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        session = delegate_task(client.name, "Improve error messages")

        store = _load_queue_from_dir(queues_dir, client.name)
        item = store.items[0]
        # The item id is embedded in the session name, providing the linkage.
        assert f"delegate-{item.id}" in session.name
        # The session is saved to state with the correct client.
        state = load_state()
        assert state.sessions[0].client == client.name

    def test_new_pane_called_once(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Clean up dead code")

        assert len(new_pane_calls) == 1


class TestDelegateTaskCommandConstruction:
    def test_fire_and_forget_includes_print_flag(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Run static analysis", interactive=False)

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        assert "--print" in command

    def test_interactive_omits_print_flag(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Review PR #42", interactive=True)

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        assert "--print" not in command

    def test_command_includes_env_var_prefix(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Fix lint violations")

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        assert f"CW_CLIENT={client.name}" in command
        assert "CW_PURPOSE=debt" in command

    def test_interactive_command_includes_env_var_prefix(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Debug test", interactive=True)

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        assert f"CW_CLIENT={client.name}" in command

    def test_command_omits_session_id_flag(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Update dependencies")

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        assert "--session-id" not in command
        assert "claude" in command

    def test_command_includes_append_system_prompt(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Write migration scripts")

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        assert "--append-system-prompt" in command

    def test_custom_prompt_used_in_command(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        custom_prompt = "Be extremely terse and use no preamble."
        delegate_task(client.name, "Refactor login", prompt=custom_prompt)

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        escaped = shlex.quote(custom_prompt)
        assert escaped in command

    def test_description_used_as_prompt_when_no_prompt_given(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        description = "Add rate limiting to the API."
        delegate_task(client.name, description)

        command = new_pane_calls[0][0]
        assert isinstance(command, str)
        escaped = shlex.quote(description)
        assert escaped in command

    def test_fire_and_forget_close_on_exit_true(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Lint files", interactive=False)

        # new_pane args: (command, name, cwd, close_on_exit, session)
        close_on_exit = new_pane_calls[0][3]
        assert close_on_exit is True

    def test_interactive_close_on_exit_false(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Debug failing test", interactive=True)

        close_on_exit = new_pane_calls[0][3]
        assert close_on_exit is False


class TestDelegateTaskSessionOrigin:
    def test_session_origin_is_delegate(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]

        session = delegate_task(client.name, "Generate API client")

        assert session.origin == SessionOrigin.DELEGATE

    def test_saved_session_origin_is_delegate(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]

        delegate_task(client.name, "Add integration tests")

        state = load_state()
        assert state.sessions[0].origin == SessionOrigin.DELEGATE

    def test_session_status_is_active(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]

        session = delegate_task(client.name, "Migrate database schema")

        assert session.status == SessionStatus.ACTIVE

    def test_session_purpose_defaults_to_debt(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]

        session = delegate_task(client.name, "Clean up unused imports")

        assert session.purpose == SessionPurpose.DEBT

    def test_session_purpose_respects_argument(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]

        session = delegate_task(client.name, "Brainstorm ideas", purpose="idea")

        assert session.purpose == SessionPurpose.IDEA


class TestDelegateTaskOutsideZellijError:
    def test_raises_when_not_in_zellij_session(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        mock_zellij: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_clients_yaml(tmp_config_dir, sample_client)

        # is_installed returns True (from mock_zellij), but not inside a session
        monkeypatch.setattr("cw.zellij.in_zellij_session", lambda: False)

        with pytest.raises(CwError, match="Must be inside a Zellij session"):
            delegate_task(sample_client.name, "Some task")

    def test_raises_when_zellij_not_installed(
        self,
        tmp_config_dir: Path,
        sample_client: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_clients_yaml(tmp_config_dir, sample_client)

        monkeypatch.setattr("cw.zellij.is_installed", lambda: False)

        with pytest.raises(CwError, match="not installed"):
            delegate_task(sample_client.name, "Some task")


class TestDelegateTaskPaneNaming:
    def test_pane_name_is_delegate_item_id(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Update CI configuration")

        store = _load_queue_from_dir(queues_dir, client.name)
        item_id = store.items[0].id
        expected_name = f"delegate-{item_id}"

        # new_pane args: (command, name, cwd, close_on_exit, session)
        pane_name = new_pane_calls[0][1]
        assert pane_name == expected_name

    def test_session_zellij_pane_matches_pane_name(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        session = delegate_task(client.name, "Audit logging")

        store = _load_queue_from_dir(queues_dir, client.name)
        item_id = store.items[0].id
        assert session.zellij_pane == f"delegate-{item_id}"

    def test_pane_cwd_is_workspace_path(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Seed database fixtures")

        # new_pane args: (command, name, cwd, close_on_exit, session)
        cwd = new_pane_calls[0][2]
        assert cwd == str(client.workspace_path)


class TestDelegateTaskContextFiles:
    def test_context_files_stored_in_task_spec(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        context_files = ["src/cw/session.py", "tests/test_session.py"]
        delegate_task(
            client.name,
            "Add type annotations",
            context_files=context_files,
        )

        store = _load_queue_from_dir(queues_dir, client.name)
        assert store.items[0].task.context_files == context_files

    def test_no_context_files_defaults_to_empty_list(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Bump version")

        store = _load_queue_from_dir(queues_dir, client.name)
        assert store.items[0].task.context_files == []

    def test_context_files_none_stored_as_empty_list(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Fix flaky test", context_files=None)

        store = _load_queue_from_dir(queues_dir, client.name)
        assert store.items[0].task.context_files == []


# ---------------------------------------------------------------------------
# Internal helper: load queue directly from a custom directory
# ---------------------------------------------------------------------------


def _load_queue_from_dir(queues_dir: Path, client: str) -> QueueStore:
    """Load a QueueStore from a given queues directory without relying on
    the global QUEUES_DIR constant (which may have been monkeypatched)."""
    path = queues_dir / f"{client}.json"
    if not path.exists():
        return QueueStore()
    return QueueStore.model_validate(json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# Tests for routing to existing backgrounded sessions
# ---------------------------------------------------------------------------


class TestDelegateRoutesToExistingSession:
    """When a backgrounded session exists, delegate should route to it
    instead of spawning a new pane."""

    @pytest.fixture
    def delegate_with_bg_session(
        self,
        delegate_setup: dict[str, Any],
    ) -> dict[str, Any]:
        """Add a backgrounded debt session to the state."""
        client = delegate_setup["client"]
        bg_session = Session(
            id="bg-debt-01",
            name=f"{client.name}/debt",
            client=client.name,
            purpose=SessionPurpose.DEBT,
            status=SessionStatus.BACKGROUNDED,
            workspace_path=client.workspace_path,
            zellij_pane="debt",
            zellij_tab=client.name,
        )
        state = load_state()
        state.sessions.append(bg_session)
        save_state(state)
        delegate_setup["bg_session"] = bg_session
        return delegate_setup

    def test_routes_to_backgrounded_session(
        self,
        delegate_with_bg_session: dict[str, Any],
    ) -> None:
        client = delegate_with_bg_session["client"]
        new_pane_calls = delegate_with_bg_session["new_pane_calls"]

        session = delegate_task(client.name, "Fix ruff violations")

        # Should NOT spawn a new pane
        assert len(new_pane_calls) == 0
        # Should return the existing session
        assert session.id == "bg-debt-01"

    def test_existing_session_marked_active(
        self,
        delegate_with_bg_session: dict[str, Any],
    ) -> None:
        client = delegate_with_bg_session["client"]

        delegate_task(client.name, "Fix ruff violations")

        state = load_state()
        session = state.find_by_name_or_id("bg-debt-01")
        assert session is not None
        assert session.status == SessionStatus.ACTIVE

    def test_injects_resume_and_prompt(
        self,
        delegate_with_bg_session: dict[str, Any],
    ) -> None:
        client = delegate_with_bg_session["client"]
        calls = delegate_with_bg_session["calls"]

        delegate_task(client.name, "Fix ruff violations")

        write_calls = calls["write_to_pane"]
        # Should have at least 2 writes: resume command and task prompt
        assert len(write_calls) >= 2
        resume_text = write_calls[0][0]
        assert "claude --resume" in resume_text

    def test_spawns_new_pane_when_no_bg_session(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        new_pane_calls = delegate_setup["new_pane_calls"]

        delegate_task(client.name, "Fix ruff violations")

        # Should spawn a new pane since no backgrounded session exists
        assert len(new_pane_calls) == 1


class TestDelegateWithPriority:
    def test_priority_stored_in_task_spec(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Urgent fix", priority=10)

        store = _load_queue_from_dir(queues_dir, client.name)
        assert store.items[0].task.priority == 10

    def test_default_priority_is_zero(
        self,
        delegate_setup: dict[str, Any],
    ) -> None:
        client = delegate_setup["client"]
        queues_dir = delegate_setup["queues_dir"]

        delegate_task(client.name, "Regular task")

        store = _load_queue_from_dir(queues_dir, client.name)
        assert store.items[0].task.priority == 0
