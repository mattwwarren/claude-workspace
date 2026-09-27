"""Tests for ``cw.dispatch.tracker_mcp_gate`` — pre-dispatch tracker-MCP gate (#2442).

Mirrors ``tests/test_dispatch_branch_freshness.py``'s fixture style: real ``git``
repos built in ``tmp_path`` via the shared ``make_git_repo`` fixture, with the
ticket branch's ``.claude/settings.json`` seeded through
``tests.conftest.commit_tracked_file``. The module under test runs a real
``git show <branch>:<settings_path>`` against the client's git dir, so there is
no subprocess seam worth stubbing.

Every unresolvable state asserts the fail-open contract (no hit — the spawn
proceeds): only an existing, parseable settings file whose ``enabledPlugins``
structurally names the configured plugin as absent or ``false`` is a gate hit.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.models import DevQueueStore, QueueItemStatus, Stage, TicketTask
from cw.models.client import ClientConfig, TrackerMcpGateConfig
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_CLIENT = "acme"
_TICKET = "T-2442"
_BRANCH = f"dev/{_TICKET}"
_PLUGIN = "linear@acme"
_SETTINGS = ".claude/settings.json"


@pytest.fixture(autouse=True)
def _reset_warn_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh warn-once dedupe map (process-lifetime state)."""
    monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._WARNED_FAIL_OPEN", {})


def _client(repo: Path, gate: TrackerMcpGateConfig | None) -> ClientConfig:
    return ClientConfig(name=_CLIENT, workspace_path=repo, tracker_mcp_gate=gate)


def _enabled_gate(plugin_id: str = _PLUGIN) -> TrackerMcpGateConfig:
    return TrackerMcpGateConfig(enabled=True, plugin_id=plugin_id)


def _snapshot(*, stage: Stage = Stage.PLAN, ticket_id: str = _TICKET) -> DevQueueStore:
    return DevQueueStore(
        tasks=[
            TicketTask(
                ticket_id=ticket_id,
                client=_CLIENT,
                status=QueueItemStatus.PENDING,
                stage=stage,
            )
        ]
    )


def _branch_with_file(
    make_git_repo: Callable[..., Path], relpath: str, content: str
) -> Path:
    """Repo whose ``dev/T-2442`` branch commits *relpath*; ``main`` checked out."""
    repo = make_git_repo("tmg-repo")
    git_in(repo, "checkout", "-b", _BRANCH)
    commit_tracked_file(repo, relpath, content)
    git_in(repo, "checkout", "main")
    return repo


def _branch_with_settings(make_git_repo: Callable[..., Path], settings: object) -> Path:
    return _branch_with_file(make_git_repo, _SETTINGS, json.dumps(settings))


def _resolve(client: ClientConfig, snapshot: DevQueueStore) -> dict[str, object]:
    from cw.dispatch.tracker_mcp_gate import resolve_tracker_mcp_gate_hits

    return dict(resolve_tracker_mcp_gate_hits(client, snapshot))


class TestGateToggle:
    def test_gate_disabled_by_default_never_reads_settings_file(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6 case 4: ``tracker_mcp_gate=None`` never touches the branch."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        calls: list[str] = []

        def _spy(client: ClientConfig, branch: str, relpath: str) -> None:
            calls.append(branch)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._read_branch_json", _spy)
        client = ClientConfig(name=_CLIENT, workspace_path=repo)
        assert client.tracker_mcp_gate is None

        assert _resolve(client, _snapshot()) == {}
        assert calls == []

    def test_gate_explicitly_disabled_never_reads_settings_file(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        calls: list[str] = []

        def _spy(client: ClientConfig, branch: str, relpath: str) -> None:
            calls.append(branch)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._read_branch_json", _spy)
        client = _client(repo, TrackerMcpGateConfig(enabled=False, plugin_id=_PLUGIN))

        assert _resolve(client, _snapshot()) == {}
        assert calls == []


class TestPluginEnabled:
    def test_enabled_plugin_present_and_true_dict_shape_is_not_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """R6 case 1: the plugin is enabled on the branch -> spawn proceeds."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {_PLUGIN: True}})

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

    def test_enabled_plugin_present_in_list_shape_is_not_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": [_PLUGIN]})

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}


class TestFailOpen:
    def test_enabled_settings_file_missing_on_existing_branch_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """R6 case 2: an existing branch without the settings file fails open."""
        repo = _branch_with_file(make_git_repo, "feature.py", "x = 1\n")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "missing" in warnings[0].getMessage()
        assert _BRANCH in warnings[0].getMessage()

    def test_fail_open_warning_is_logged_once_across_resolutions(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warn-once: the same fail-open reason is not re-logged every tick."""
        repo = _branch_with_file(make_git_repo, "feature.py", "x = 1\n")
        client = _client(repo, _enabled_gate())

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            _resolve(client, _snapshot())
            _resolve(client, _snapshot())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_enabled_branch_does_not_exist_yet_fails_open(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A first-time PLAN dispatch has no branch yet: nothing to gate on."""
        repo = make_git_repo("tmg-nobranch")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        # The normal first-dispatch case is not warning-worthy.
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_enabled_malformed_json_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _branch_with_file(make_git_repo, _SETTINGS, "{not json")

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "malformed" in warnings[0].getMessage()

    def test_enabled_settings_top_level_not_an_object_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _branch_with_settings(make_git_repo, [_PLUGIN])

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_enabled_plugins_field_missing_entirely_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Adopted assumption 5: no ``enabledPlugins`` key at all fails open."""
        repo = _branch_with_settings(make_git_repo, {"permissions": {"allow": []}})

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "enabledPlugins" in warnings[0].getMessage()

    def test_enabled_plugins_field_wrong_shape_fails_open_and_logs(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": _PLUGIN})

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "enabledPlugins" in warnings[0].getMessage()

    def test_enabled_plugin_non_bool_value_fails_open(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """A present key with a non-boolean value is ambiguous: fail open."""
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {_PLUGIN: "yes"}}
        )

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}


class TestGateHit:
    def _assert_hit(self, hits: dict[str, object], plugin_id: str = _PLUGIN) -> None:
        from cw.dispatch.tracker_mcp_gate import TrackerMcpGateHit

        assert hits == {
            _TICKET: TrackerMcpGateHit(
                branch=_BRANCH, file_inspected=_SETTINGS, expected_plugin=plugin_id
            )
        }

    def test_enabled_plugin_key_absent_from_dict_is_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """R6 case 3: the hit names the branch, the file, and the plugin id."""
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {"github@acme": True}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_enabled_plugin_explicitly_false_in_dict_is_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {_PLUGIN: False}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_enabled_plugin_absent_from_list_is_gated(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": ["other@acme"]})

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_plugin_id_match_is_exact_no_at_suffix_stripping(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {"linear": True}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_plugin_id_match_is_exact_in_the_other_direction(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {_PLUGIN: True}})

        self._assert_hit(
            _resolve(_client(repo, _enabled_gate("linear")), _snapshot()),
            plugin_id="linear",
        )

    def test_plugin_id_match_is_case_sensitive(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(
            make_git_repo, {"enabledPlugins": {"Linear@Acme": True}}
        )

        self._assert_hit(_resolve(_client(repo, _enabled_gate()), _snapshot()))

    def test_custom_settings_path_is_the_file_inspected(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        from cw.dispatch.tracker_mcp_gate import TrackerMcpGateHit

        custom = ".claude/settings.team.json"
        repo = _branch_with_file(
            make_git_repo, custom, json.dumps({"enabledPlugins": {}})
        )
        gate = TrackerMcpGateConfig(
            enabled=True, plugin_id=_PLUGIN, settings_path=custom
        )

        assert _resolve(_client(repo, gate), _snapshot()) == {
            _TICKET: TrackerMcpGateHit(
                branch=_BRANCH, file_inspected=custom, expected_plugin=_PLUGIN
            )
        }

    def test_impl_stage_task_is_gated(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})

        self._assert_hit(
            _resolve(_client(repo, _enabled_gate()), _snapshot(stage=Stage.IMPL))
        )


class TestScoping:
    @pytest.mark.parametrize("stage", [Stage.REVIEW, Stage.FINALIZE])
    def test_review_and_finalize_stage_tasks_never_scanned(
        self, make_git_repo: Callable[..., Path], stage: Stage
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})

        assert _resolve(_client(repo, _enabled_gate()), _snapshot(stage=stage)) == {}

    def test_non_pending_and_other_client_rows_never_scanned(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})
        snapshot = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.PLAN,
                ),
                TicketTask(
                    ticket_id=_TICKET,
                    client="other-client",
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                ),
            ]
        )

        assert _resolve(_client(repo, _enabled_gate()), snapshot) == {}

    def test_git_unavailable_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing git binary (``OSError``) resolves to "not gated"."""
        repo = _branch_with_settings(make_git_repo, {"enabledPlugins": {}})

        def _boom(*_args: str, **_kwargs: object) -> None:
            msg = "git: not found"
            raise OSError(msg)

        monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._run_git", _boom)

        assert _resolve(_client(repo, _enabled_gate()), _snapshot()) == {}

    def test_clean_verdict_rearms_the_warn_once_dedupe(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A branch that resolves cleanly forgets its prior fail-open warning, so
        a later regression to the same reason is logged again."""
        repo = _branch_with_file(make_git_repo, "feature.py", "x = 1\n")
        client = _client(repo, _enabled_gate())

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            _resolve(client, _snapshot())
            git_in(repo, "checkout", _BRANCH)
            commit_tracked_file(
                repo, _SETTINGS, json.dumps({"enabledPlugins": {_PLUGIN: True}})
            )
            _resolve(client, _snapshot())
            git_in(repo, "rm", "-q", _SETTINGS)
            git_in(repo, "commit", "-q", "-m", "drop settings")
            git_in(repo, "checkout", "main")
            _resolve(client, _snapshot())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
