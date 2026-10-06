"""Tests for ``tests/_clients_yaml.py``, the shared clients.yaml writer (#2165)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

import cw.config
from cw.config import _REAL_CONFIG_DIR, load_clients
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    ClientConfig,
    LaneConfig,
    ReapPolicy,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
)
from cw.models.client import TrackerMcpGateConfig
from tests._clients_yaml import (
    ClientSpec,
    executor_backend_extra,
    review_backend_clients,
    staged_client,
    write_clients_yaml,
)

_FOUR_STAGES = [Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE]


def _raw_entries() -> dict[str, dict[str, object]]:
    raw = yaml.safe_load(cw.config.clients_file().read_text())
    assert isinstance(raw, dict)
    clients = raw["clients"]
    assert isinstance(clients, dict)
    return clients


# ---------------------------------------------------------------------------
# Path resolution and isolation
# ---------------------------------------------------------------------------


class TestPathAndIsolation:
    def test_returns_the_patched_clients_file(self, tmp_config_dir: Path) -> None:
        path = write_clients_yaml(ClientSpec("a", "/tmp/ws-a"))

        assert path == tmp_config_dir / ".config" / "cw" / "clients.yaml"
        assert path == cw.config.clients_file()
        assert path.is_relative_to(tmp_config_dir)
        assert path.is_file()

    def test_creates_a_missing_parent_directory(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_config_dir / "nested" / "deeper" / "clients.yaml"
        monkeypatch.setattr("cw.config.CLIENTS_FILE", target)

        path = write_clients_yaml(ClientSpec("a", "/tmp/ws-a"))

        assert path == target
        assert target.is_file()

    def test_honours_a_repatched_clients_file(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution goes through ``clients_file()`` at call time."""
        target = tmp_config_dir / "elsewhere.yaml"
        monkeypatch.setattr("cw.config.CLIENTS_FILE", target)

        write_clients_yaml(ClientSpec("a", "/tmp/ws-a"))

        assert target.is_file()
        assert not (tmp_config_dir / ".config" / "cw" / "clients.yaml").exists()
        assert set(load_clients()) == {"a"}

    def test_refuses_a_real_config_dir_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _REAL_CONFIG_DIR / "clients.yaml"
        monkeypatch.setattr("cw.config.CLIENTS_FILE", target)

        with pytest.raises(CwError, match="refusing real-state write"):
            write_clients_yaml(ClientSpec("a", "/tmp/ws-a"))

        assert not target.exists()


# ---------------------------------------------------------------------------
# Round-trips through cw.config.load_clients()
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_name_and_workspace_path(self, tmp_path: Path) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path / "ws"))

        client = load_clients()["acme"]

        assert client.name == "acme"
        assert client.workspace_path == tmp_path / "ws"

    def test_repo_path_and_branch_make_a_worktree_client(self, tmp_path: Path) -> None:
        write_clients_yaml(ClientSpec("wt", repo_path=tmp_path / "repo", branch="dev"))

        client = load_clients()["wt"]

        assert client.is_worktree_client
        assert client.repo_path == tmp_path / "repo"
        assert client.branch == "dev"
        assert client.workspace_path == tmp_path / "repo"

    def test_branch_worktree_base_and_worker_model(self, tmp_path: Path) -> None:
        write_clients_yaml(
            ClientSpec(
                "acme",
                tmp_path / "ws",
                default_branch="trunk",
                worktree_base=tmp_path / "wts",
                worker_model="claude-sonnet",
            )
        )

        client = load_clients()["acme"]

        assert client.default_branch == "trunk"
        assert client.worktree_base == tmp_path / "wts"
        assert client.worker_model == "claude-sonnet"

    @pytest.mark.parametrize("commands", ["make check", ""])
    def test_quality_gate_commands(self, commands: str) -> None:
        write_clients_yaml(
            ClientSpec("acme", "/tmp/ws-acme", quality_gate_commands=commands)
        )

        assert _raw_entries()["acme"]["quality_gate_commands"] == commands
        assert load_clients()["acme"].quality_gate_commands == commands

    @pytest.mark.parametrize("as_model", [True, False], ids=["model", "mapping"])
    def test_lanes(self, as_model: bool) -> None:
        fields: dict[str, object] = {
            "name": "fast",
            "max_parallel": 3,
            "priority": 5,
            "reap_policy": ReapPolicy.AUTO,
            "gate_recipes": {"auto_approve_clean_review": True},
            "subagent_spawn_guard_enabled": False,
            "park_on_abandoned_exit": {"park_on_abandoned_exit": True},
        }
        lane: LaneConfig | dict[str, object] = (
            LaneConfig.model_validate(fields) if as_model else fields
        )
        write_clients_yaml(ClientSpec("acme", "/tmp/ws-acme", lanes=[lane]))

        loaded = load_clients()["acme"].lanes

        assert loaded == [LaneConfig.model_validate(fields)]
        assert loaded[0].reap_policy is ReapPolicy.AUTO
        assert loaded[0].subagent_spawn_guard_enabled is False

    def test_extra_raw_keys(self) -> None:
        write_clients_yaml(
            ClientSpec(
                "acme",
                "/tmp/ws-acme",
                extra={
                    "blocked_result_requeue_enabled": True,
                    "sentinel_mismatch_veto_enabled": True,
                    "pipeline": {
                        "stages": ["plan", "impl"],
                        "executors": {"review": {"backend": "codex"}},
                    },
                },
            )
        )

        client = load_clients()["acme"]

        assert client.blocked_result_requeue_enabled is True
        assert client.sentinel_mismatch_veto_enabled is True
        assert client.pipeline.stages == [Stage.PLAN, Stage.IMPL]
        assert client.pipeline.executors[Stage.REVIEW].backend == "codex"

    def test_several_clients_keep_their_order(self) -> None:
        write_clients_yaml(
            ClientSpec("zeta", "/tmp/ws-z"),
            ClientSpec("alpha", "/tmp/ws-a"),
            ClientSpec("mid", "/tmp/ws-m"),
        )

        assert list(_raw_entries()) == ["zeta", "alpha", "mid"]
        assert list(load_clients()) == ["zeta", "alpha", "mid"]

    def test_zero_clients(self) -> None:
        path = write_clients_yaml()

        assert yaml.safe_load(path.read_text()) == {"clients": {}}
        assert load_clients() == {}


class TestOmittedFieldDefaults:
    def test_only_workspace_path_is_written(self) -> None:
        write_clients_yaml(ClientSpec("a", "/tmp/ws-a"))

        assert _raw_entries() == {"a": {"workspace_path": "/tmp/ws-a"}}

    def test_loader_defaults_apply(self) -> None:
        write_clients_yaml(ClientSpec("a", "/tmp/ws-a"))

        client = load_clients()["a"]

        assert client.default_branch == "main"
        assert client.worktree_base is None
        assert client.lanes == []


# ---------------------------------------------------------------------------
# Lane attempt_ceiling tri-state (#1751)
# ---------------------------------------------------------------------------


class TestAttemptCeilingTriState:
    @pytest.mark.parametrize("as_model", [True, False], ids=["model", "mapping"])
    @pytest.mark.parametrize("ceiling", [None, False, 25], ids=["none", "false", "25"])
    def test_round_trips_each_state(
        self, as_model: bool, ceiling: bool | int | None
    ) -> None:
        fields: dict[str, object] = {"name": "slow", "attempt_ceiling": ceiling}
        lane: LaneConfig | dict[str, object] = (
            LaneConfig.model_validate(fields) if as_model else fields
        )
        write_clients_yaml(ClientSpec("acme", "/tmp/ws-acme", lanes=[lane]))

        raw_lane = _raw_entries()["acme"]["lanes"]
        assert isinstance(raw_lane, list)
        assert raw_lane[0]["attempt_ceiling"] == ceiling
        assert type(raw_lane[0]["attempt_ceiling"]) is type(ceiling)

        loaded = load_clients()["acme"].lanes[0].attempt_ceiling
        if ceiling is None:
            assert loaded is None
        elif ceiling is False:
            assert loaded is False
        else:
            assert loaded == ceiling


# ---------------------------------------------------------------------------
# Lane codex_fix_loop_enabled tri-state (#2541)
# ---------------------------------------------------------------------------


class TestCodexFixLoopEnabledTriState:
    @pytest.mark.parametrize("as_model", [True, False], ids=["model", "mapping"])
    @pytest.mark.parametrize("flag", [None, False, True], ids=["none", "false", "true"])
    def test_round_trips_each_state(self, as_model: bool, flag: bool | None) -> None:
        fields: dict[str, object] = {"name": "slow", "codex_fix_loop_enabled": flag}
        lane: LaneConfig | dict[str, object] = (
            LaneConfig.model_validate(fields) if as_model else fields
        )
        write_clients_yaml(ClientSpec("acme", "/tmp/ws-acme", lanes=[lane]))

        raw_lane = _raw_entries()["acme"]["lanes"]
        assert isinstance(raw_lane, list)
        assert raw_lane[0]["codex_fix_loop_enabled"] == flag
        assert type(raw_lane[0]["codex_fix_loop_enabled"]) is type(flag)

        loaded = load_clients()["acme"].lanes[0].codex_fix_loop_enabled
        assert loaded is flag


# ---------------------------------------------------------------------------
# ClientSpec.from_config fidelity
# ---------------------------------------------------------------------------


_FIDELITY_CASES = {
    "tracker_gate": ClientConfig(
        name="gated",
        workspace_path=Path("/tmp/ws-gated"),
        tracker_mcp_gate=TrackerMcpGateConfig(enabled=True, plugin_id="tracker@mkt"),
    ),
    "occupancy_false": ClientConfig(
        name="occ", workspace_path=Path("/tmp/ws-occ"), occupancy_gate_enabled=False
    ),
    "occupancy_none": ClientConfig(
        name="occ", workspace_path=Path("/tmp/ws-occ"), occupancy_gate_enabled=None
    ),
    "occupancy_true": ClientConfig(
        name="occ", workspace_path=Path("/tmp/ws-occ"), occupancy_gate_enabled=True
    ),
    "worktree_client": ClientConfig(
        name="wt", repo_path=Path("/tmp/repo-wt"), branch="dev"
    ),
    "lane_ceiling_false": ClientConfig(
        name="lanes",
        workspace_path=Path("/tmp/ws-lanes"),
        lanes=[
            LaneConfig(name="supervised", attempt_ceiling=False),
            LaneConfig(name="fast", max_parallel=4, priority=2, paused=True),
        ],
    ),
    "pipeline_stages": ClientConfig(
        name="staged",
        workspace_path=Path("/tmp/ws-staged"),
        pipeline=StagePipelineConfig(stages=_FOUR_STAGES),
    ),
    "executors": ClientConfig(
        name="exec",
        workspace_path=Path("/tmp/ws-exec"),
        worktree_base=Path("/tmp/wts-exec"),
        pipeline=StagePipelineConfig(
            executors={Stage.REVIEW: StageExecutorConfig(backend="codex")}
        ),
    ),
}


class TestFromConfig:
    @pytest.mark.parametrize(
        "client", list(_FIDELITY_CASES.values()), ids=list(_FIDELITY_CASES)
    )
    def test_loads_back_equal(self, client: ClientConfig) -> None:
        write_clients_yaml(client)

        assert load_clients()[client.name] == client

    @pytest.mark.parametrize(
        "client", list(_FIDELITY_CASES.values()), ids=list(_FIDELITY_CASES)
    )
    def test_spec_and_config_write_the_same_file(self, client: ClientConfig) -> None:
        first = write_clients_yaml(client).read_text()
        second = write_clients_yaml(ClientSpec.from_config(client)).read_text()

        assert first == second

    def test_name_is_not_duplicated_inside_the_entry(self) -> None:
        write_clients_yaml(ClientConfig(name="a", workspace_path=Path("/tmp/ws-a")))

        assert _raw_entries() == {"a": {"workspace_path": "/tmp/ws-a"}}

    def test_extra_adds_an_executor_backend(self) -> None:
        client = _FIDELITY_CASES["pipeline_stages"]

        write_clients_yaml(
            ClientSpec.from_config(
                client, extra=executor_backend_extra("review", "codex")
            )
        )

        loaded = load_clients()["staged"]
        assert loaded.pipeline.executors[Stage.REVIEW].backend == "codex"
        # Deep merge keeps the dumped stages alongside the new executor.
        assert loaded.pipeline.stages == _FOUR_STAGES
        assert "stages" in loaded.pipeline.model_fields_set

    def test_extra_replaces_lists_wholesale(self) -> None:
        client = _FIDELITY_CASES["pipeline_stages"]

        write_clients_yaml(
            ClientSpec.from_config(client, extra={"pipeline": {"stages": ["impl"]}})
        )

        assert load_clients()["staged"].pipeline.stages == [Stage.IMPL]

    def test_replace_wins_over_the_dumped_value(self) -> None:
        client = ClientConfig(
            name="a", workspace_path=Path("/tmp/ws-a"), worker_model="dumped"
        )

        spec = dataclasses.replace(ClientSpec.from_config(client), worker_model="x")
        write_clients_yaml(spec)

        assert load_clients()["a"].worker_model == "x"

    def test_model_copy_update_fields_are_emitted(self) -> None:
        client = ClientConfig(name="a", workspace_path=Path("/tmp/ws-a"))

        write_clients_yaml(
            client.model_copy(
                update={"worker_model": "copied", "quality_gate_commands": ""}
            )
        )

        loaded = load_clients()["a"]
        assert loaded.worker_model == "copied"
        assert loaded.quality_gate_commands == ""


# ---------------------------------------------------------------------------
# ensure_workspaces, duplicates, spec factories
# ---------------------------------------------------------------------------


class TestEnsureWorkspaces:
    def test_creates_workspace_directories(self, tmp_path: Path) -> None:
        ws_a = tmp_path / "a" / "ws"
        ws_b = tmp_path / "b" / "ws"

        write_clients_yaml(
            ClientSpec("a", ws_a),
            ClientSpec("b", str(ws_b)),
            ClientSpec("wt", repo_path=tmp_path / "repo", branch="dev"),
            ensure_workspaces=True,
        )

        assert ws_a.is_dir()
        assert ws_b.is_dir()
        assert not (tmp_path / "repo").exists()

    def test_default_does_not_create_directories(self, tmp_path: Path) -> None:
        ws = tmp_path / "never-made"

        write_clients_yaml(ClientSpec("a", ws))

        assert not ws.exists()


def test_duplicate_names_raise_value_error(tmp_config_dir: Path) -> None:
    with pytest.raises(ValueError, match="dup"):
        write_clients_yaml(
            ClientSpec("dup", "/tmp/ws-1"),
            ClientConfig(name="dup", workspace_path=Path("/tmp/ws-2")),
        )

    assert not cw.config.clients_file().exists()


class TestStagedClient:
    @pytest.mark.parametrize("veto", [False, True])
    def test_loads_the_staged_shape(self, veto: bool) -> None:
        write_clients_yaml(staged_client("c", sentinel_mismatch_veto=veto))

        client = load_clients()["c"]

        assert client.blocked_result_requeue_enabled is True
        assert client.pipeline.stages == _FOUR_STAGES
        assert client.workspace_path == Path("/tmp/ws-staged")
        assert client.default_branch == "main"
        assert client.sentinel_mismatch_veto_enabled is veto
        assert ("sentinel_mismatch_veto_enabled" in _raw_entries()["c"]) is veto

    def test_custom_workspace(self) -> None:
        write_clients_yaml(staged_client("c", "/tmp/ws-test"))

        assert load_clients()["c"].workspace_path == Path("/tmp/ws-test")


def test_executor_backend_extra_shape() -> None:
    assert executor_backend_extra("review", "codex") == {
        "pipeline": {"executors": {"review": {"backend": "codex"}}}
    }


class TestReviewBackendClients:
    def test_lane_reap_policies_and_backends(self, tmp_path: Path) -> None:
        specs = review_backend_clients(
            tmp_path,
            "codex",
            names=("a", "b"),
            lane_reap_policies={"a": ReapPolicy.AUTO},
        )
        write_clients_yaml(*specs)

        clients = load_clients()

        assert [lane.name for lane in clients["a"].lanes] == [DEFAULT_LANE]
        assert clients["a"].lanes[0].reap_policy is ReapPolicy.AUTO
        assert clients["b"].lanes == []
        for name in ("a", "b"):
            assert clients[name].workspace_path == tmp_path
            assert clients[name].default_branch == "main"
            assert clients[name].pipeline.executors[Stage.REVIEW].backend == "codex"

    def test_default_names(self, tmp_path: Path) -> None:
        write_clients_yaml(*review_backend_clients(tmp_path, "claude-native"))

        assert list(load_clients()) == ["client-a"]
