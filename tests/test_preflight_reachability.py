"""Parity and behaviour tests for the tick-side binding reachability check (#3744).

RFC 0019 H2b replaces the pre-dispatch tracker-MCP gate (#2442) with the
tick-side preflight. ``tests/fixtures/stage_preflight/old_binding_parks.json``
was captured from the old gate by a one-off script (not committed) that ran
:func:`_observe` over every key of :func:`tests._shared.reach_cases.all_case_keys`.
:func:`test_parity_with_the_old_binding_parks` replays it through the
:data:`_RESOLVE` seam.

Each case stores the hit as a 4-tuple (branch, file inspected, expected,
backend) or ``null``, the ``cw.dispatch`` WARNING count and the MCP config read
count, plus the same three for the opted-out (D-C3) build of the case. Paths
under the per-test ``tmp_path`` are stored as ``<tmp>``.

Also ports the old gate's behaviour tests (fail-open, hit shape, scoping,
warn-once) onto the seam, with the hand-written ``gated`` / ``expect_server_hit``
oracle from ``tests/_shared/reach_cases.py``.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest

from cw.backend_mcp_config import McpConfigReading
from cw.models import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    OPENCODE_BACKEND,
    DevQueueStore,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.models.client import ClientConfig, TrackerMcpGateConfig
from tests._backend_mcp_helpers import McpStage, stage_mcp_config
from tests._doctor_helpers import make_stage_pipeline
from tests._shared.cli_preflight import (
    _write_project_config_yaml,
    _write_tracker_config,
)
from tests._shared.git_repos import (
    branch_with_file,
    branch_with_settings,
    commit_tracked_file,
    git_in,
)
from tests._shared.reach_cases import (
    ALL_SERVERS,
    BRANCH,
    CLIENT,
    GATE_STATES,
    OPENCODE_SERVERS,
    PLUGIN,
    SERVER,
    SETTINGS,
    STAGES,
    TICKET,
    TRACKERS,
    all_case_keys,
    build_case,
    enabled_gate,
    expect_server_hit,
    gated,
    on,
    reach_client,
    snapshot,
    tracker_label,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: The resolver under test, as ``module:function``. Phase 1 pointed it at the
#: old gate (``cw.dispatch.tracker_mcp_gate:resolve_tracker_mcp_gate_hits``)
#: to prove the replay green on the code the fixture was captured from.
_RESOLVE = "cw.dispatch.claim.preflight_reach:resolve_binding_hits"
#: Modules whose ``read_backend_mcp`` binding the resolver reads through.
_READ_SITES: tuple[str, ...] = ("cw.binding_probe",)
#: Modules whose ``_run_git`` / ``_ref_exists`` bindings the claude read uses.
_GIT_SITES: tuple[str, ...] = ("cw.binding_probe",)

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "stage_preflight"
    / "old_binding_parks.json"
)


def _resolve(client: ClientConfig, queue: DevQueueStore) -> dict[str, object]:
    module_name, func_name = _RESOLVE.split(":")
    func = getattr(importlib.import_module(module_name), func_name)
    return dict(func(client, queue))


def _dispatch_warning_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """The resolver's own WARNING records (``cw.dispatch``).

    A client built from the legacy ``tracker_mcp_gate`` also logs the
    ``cw.models.client`` deprecation warning (#3702), which is not the gate's.
    """
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "cw.dispatch"
    ]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in _dispatch_warning_records(caplog)]


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Wrap every :data:`_READ_SITES` ``read_backend_mcp`` and record each call."""
    calls: list[tuple[str, str]] = []
    for name in _READ_SITES:
        module = importlib.import_module(name)
        real = module.read_backend_mcp

        def _counting(
            backend: str,
            server: str,
            root: Path,
            _real: Callable[[str, str, Path], McpConfigReading] = real,
        ) -> McpConfigReading:
            calls.append((backend, server))
            return _real(backend, server, root)

        monkeypatch.setattr(module, "read_backend_mcp", _counting)
    return calls


def _count_git(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every branch-existence check the claude-native read makes."""
    calls: list[str] = []
    for name in _GIT_SITES:
        module = importlib.import_module(name)
        real = module._ref_exists

        def _spy(
            branch: str, cwd: Path, _real: Callable[[str, Path], bool] = real
        ) -> bool:
            calls.append(branch)
            return _real(branch, cwd)

        monkeypatch.setattr(module, "_ref_exists", _spy)
    return calls


class _HitLike(Protocol):
    """The four fields every hit type across the seam carries."""

    branch: str
    file_inspected: str
    expected: str
    backend: str


def _hit_tuple(hit: object, tmp_path: Path) -> list[str] | None:
    if hit is None:
        return None
    typed = cast("_HitLike", hit)
    fields = [typed.branch, typed.file_inspected, typed.expected, typed.backend]
    return [f.replace(str(tmp_path), "<tmp>") for f in fields]


def _observe(
    key: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
    enabled: bool = True,
) -> dict[str, object]:
    """Resolve one case and report its hit tuple, warning count and read count."""
    client, queue = build_case(
        key,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        make_git_repo=make_git_repo,
        enabled=enabled,
    )
    reads = _count_reads(monkeypatch)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
        hits = _resolve(client, queue)
    return {
        "hit": _hit_tuple(hits.get(TICKET), tmp_path),
        "warnings": len(_dispatch_warning_records(caplog)),
        "reads": len(reads),
    }


_CAPTURED: dict[str, dict[str, object]] = (
    json.loads(_FIXTURE.read_text(encoding="utf-8")) if _FIXTURE.exists() else {}
)
#: codex FINALIZE reads the ticket snapshot (RFC 0019 T2): the tickets binding is
#: not needed, so it is never read, parked or warned about. The one intended
#: divergence from the old gate's capture.
_NOT_NEEDED: dict[str, object] = {"hit": None, "reads": 0, "warnings": 0}


def _is_codex_finalize(key: str) -> bool:
    kind, *parts = key.split("/")
    codex = parts[1] == "codex" if kind == "server" else kind == "unbound"
    return codex and parts[-1] == "finalize"


_OLD: dict[str, dict[str, object]] = {
    key: {**old, **_NOT_NEEDED} if _is_codex_finalize(key) else old
    for key, old in _CAPTURED.items()
}


@pytest.mark.usefixtures("isolated_backend_layers")
@pytest.mark.parametrize("key", sorted(_OLD))
def test_parity_with_the_old_binding_parks(
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    old = _OLD[key]
    observed = _observe(
        key,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        make_git_repo=make_git_repo,
        caplog=caplog,
    )
    assert observed == {k: old[k] for k in ("hit", "warnings", "reads")}


@pytest.mark.usefixtures("isolated_backend_layers")
@pytest.mark.parametrize("key", sorted(_OLD))
def test_an_opted_out_client_gets_no_park_and_no_read(
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-C3: ``preflight.enabled`` false resolves ``{}`` before any read."""
    observed = _observe(
        key,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        make_git_repo=make_git_repo,
        caplog=caplog,
        enabled=False,
    )
    assert observed == _OLD[key]["disabled"]
    assert observed == {"hit": None, "warnings": 0, "reads": 0}


def test_the_capture_covers_every_case_key() -> None:
    assert sorted(_OLD) == all_case_keys()


@pytest.mark.parametrize("key", [k for k in all_case_keys() if k.startswith("server/")])
def test_the_capture_agrees_with_the_hand_written_oracle(key: str) -> None:
    _, tracker, backend, state, stage = key.split("/")
    expected = expect_server_hit(
        None if tracker == "unset" else tracker,
        backend,
        next(s for s in GATE_STATES if s == state),
        Stage(stage),
    )
    assert (_OLD[key]["hit"] is not None) is expected
    if tracker != "linear" or not gated(backend, Stage(stage)):
        assert _OLD[key]["reads"] == 0


def test_the_capture_holds_every_unbound_and_claude_shape() -> None:
    unbound_hits = {
        k for k, v in _OLD.items() if k.startswith("unbound/") and v["hit"] is not None
    }
    assert unbound_hits == {
        f"unbound/linear/{servers}/{stage}"
        for servers in ("unset", "invalid")
        for stage in ("plan",)
    }
    claude_hits = {
        k.split("/")[1]
        for k, v in _OLD.items()
        if k.startswith("claude/") and v["hit"] is not None
    }
    assert claude_hits == {
        "dict_false",
        "dict_absent",
        "list_absent",
        "case_mismatch",
        "at_suffix",
    }
    assert {tracker_label(t) for t in TRACKERS} == {"github-issues", "linear", "unset"}
    assert len(STAGES) == 4


class TestGateToggle:
    def test_no_bindings_never_reads_the_branch(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )
        calls = _count_git(monkeypatch)
        client = ClientConfig(name=CLIENT, workspace_path=repo)
        assert client.bindings.tickets is None

        assert _resolve(client, snapshot()) == {}
        assert calls == []

    def test_explicitly_disabled_never_reads_the_branch(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )
        calls = _count_git(monkeypatch)
        client = reach_client(repo, enabled_gate(enabled=False))

        assert _resolve(client, snapshot()) == {}
        assert calls == []


class TestFailOpen:
    def test_settings_file_missing_on_existing_branch_warns(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = branch_with_file(
            make_git_repo, "feature.py", "x = 1\n", branch=BRANCH, name="reach-repo"
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(reach_client(repo, enabled_gate()), snapshot()) == {}

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "missing" in warnings[0]
        assert BRANCH in warnings[0]

    def test_fail_open_warning_is_logged_once_across_resolutions(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = branch_with_file(
            make_git_repo, "feature.py", "x = 1\n", branch=BRANCH, name="reach-repo"
        )
        client = reach_client(repo, enabled_gate())

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            _resolve(client, snapshot())
            _resolve(client, snapshot())

        assert len(_warnings(caplog)) == 1

    def test_missing_branch_is_debug_only(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("reach-nobranch")

        with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
            assert _resolve(reach_client(repo, enabled_gate()), snapshot()) == {}

        assert not _dispatch_warning_records(caplog)
        debug = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG and r.name == "cw.dispatch"
        ]
        assert any("not created yet" in m and BRANCH in m for m in debug)

    @pytest.mark.parametrize(
        ("relpath", "content", "needle"),
        [
            (SETTINGS, "{not json", "JSON"),
            (SETTINGS, json.dumps([PLUGIN]), "JSON object"),
            (SETTINGS, json.dumps({"permissions": {}}), "enabledPlugins"),
            (SETTINGS, json.dumps({"enabledPlugins": PLUGIN}), "enabledPlugins"),
        ],
    )
    def test_unreadable_settings_warn_once_naming_the_cause(
        self,
        make_git_repo: Callable[..., Path],
        caplog: pytest.LogCaptureFixture,
        relpath: str,
        content: str,
        needle: str,
    ) -> None:
        repo = branch_with_file(
            make_git_repo, relpath, content, branch=BRANCH, name="reach-repo"
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(reach_client(repo, enabled_gate()), snapshot()) == {}

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert needle in warnings[0]
        assert "spawn proceeds" in warnings[0]


class TestGateHit:
    def _assert_hit(
        self, hits: dict[str, object], plugin_id: str = PLUGIN, file: str = SETTINGS
    ) -> None:
        (hit,) = hits.values()
        assert list(hits) == [TICKET]
        assert _hit_tuple(hit, Path("/nonexistent")) == [
            BRANCH,
            file,
            plugin_id,
            CLAUDE_NATIVE_BACKEND,
        ]

    def test_plugin_id_match_is_exact_in_the_other_direction(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = branch_with_settings(
            make_git_repo,
            {"enabledPlugins": {PLUGIN: True}},
            branch=BRANCH,
            name="reach-repo",
        )

        self._assert_hit(
            _resolve(reach_client(repo, enabled_gate("linear")), snapshot()),
            plugin_id="linear",
        )

    def test_custom_settings_path_is_the_file_inspected(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        custom = ".claude/settings.team.json"
        repo = branch_with_file(
            make_git_repo,
            custom,
            json.dumps({"enabledPlugins": {}}),
            branch=BRANCH,
            name="reach-repo",
        )
        gate = TrackerMcpGateConfig(
            enabled=True, plugin_id=PLUGIN, settings_path=custom
        )

        self._assert_hit(_resolve(reach_client(repo, gate), snapshot()), file=custom)

    def test_impl_stage_task_is_gated(self, make_git_repo: Callable[..., Path]) -> None:
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )

        self._assert_hit(
            _resolve(reach_client(repo, enabled_gate()), snapshot(stage=Stage.IMPL))
        )


class TestScoping:
    def test_non_pending_and_other_client_rows_never_scanned(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )
        calls = _count_git(monkeypatch)
        queue = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=TICKET,
                    client=CLIENT,
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.PLAN,
                ),
                TicketTask(
                    ticket_id=TICKET,
                    client="other-client",
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                ),
            ]
        )

        assert _resolve(reach_client(repo, enabled_gate()), queue) == {}
        assert calls == []

    def test_git_unavailable_fails_open(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )

        def _boom(*_args: str, **_kwargs: object) -> None:
            msg = "git: not found"
            raise OSError(msg)

        for name in _GIT_SITES:
            monkeypatch.setattr(importlib.import_module(name), "_run_git", _boom)

        assert _resolve(reach_client(repo, enabled_gate()), snapshot()) == {}

    def test_clean_verdict_rearms_the_warn_once_dedupe(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = branch_with_file(
            make_git_repo, "feature.py", "x = 1\n", branch=BRANCH, name="reach-repo"
        )
        client = reach_client(repo, enabled_gate())

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            _resolve(client, snapshot())
            git_in(repo, "checkout", BRANCH)
            commit_tracked_file(
                repo, SETTINGS, json.dumps({"enabledPlugins": {PLUGIN: True}})
            )
            _resolve(client, snapshot())
            git_in(repo, "rm", "-q", SETTINGS)
            git_in(repo, "commit", "-q", "-m", "drop settings")
            git_in(repo, "checkout", "main")
            _resolve(client, snapshot())

        assert len(_warnings(caplog)) == 2

    def test_claude_row_under_servers_only_gate_is_skipped(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No plugin_id bound: not applied, never read, and never warned."""
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )
        calls = _count_git(monkeypatch)
        client = reach_client(repo, enabled_gate(None, servers=dict(OPENCODE_SERVERS)))

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, snapshot()) == {}

        assert calls == []
        assert _warnings(caplog) == []

    def test_claude_native_finalize_row_never_reads_the_branch(
        self, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )
        calls = _count_git(monkeypatch)
        client = reach_client(repo, enabled_gate(), tracker="linear")

        assert _resolve(client, snapshot(stage=Stage.FINALIZE)) == {}
        assert calls == []

    def test_mixed_gate_evaluates_each_row_by_its_backend(
        self,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A plugin_id+servers gate: claude row by plugin, opencode row by server."""
        repo = branch_with_settings(
            make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
        )
        git_in(repo, "checkout", "-b", "dev/T-OC")
        git_in(repo, "checkout", "main")
        config_path = stage_mcp_config(
            monkeypatch, tmp_path, OPENCODE_BACKEND, "disabled"
        )
        client = ClientConfig(
            name=CLIENT,
            workspace_path=repo,
            tracker_mcp_gate=enabled_gate(PLUGIN, servers=dict(OPENCODE_SERVERS)),
            lanes=[
                {"name": "oc", "pipeline": make_stage_pipeline(on(OPENCODE_BACKEND))}
            ],
        )
        _write_tracker_config(repo, "linear")
        queue = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=TICKET,
                    client=CLIENT,
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                ),
                TicketTask(
                    ticket_id="T-OC",
                    client=CLIENT,
                    status=QueueItemStatus.PENDING,
                    stage=Stage.IMPL,
                    lane="oc",
                ),
            ]
        )

        hits = _resolve(client, queue)

        assert {k: _hit_tuple(v, Path("/nonexistent")) for k, v in hits.items()} == {
            TICKET: [BRANCH, SETTINGS, PLUGIN, CLAUDE_NATIVE_BACKEND],
            "T-OC": ["dev/T-OC", str(config_path), SERVER, OPENCODE_BACKEND],
        }


@pytest.mark.usefixtures("isolated_backend_layers")
class TestBackendServerGate:
    @pytest.mark.parametrize(
        "servers", [dict(OPENCODE_SERVERS), {CODEX_BACKEND: "a.b"}]
    )
    def test_codex_finalize_with_unusable_server_is_not_a_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        servers: dict[str, str],
    ) -> None:
        (tmp_path / "ws" / ".git").mkdir(parents=True)
        client = reach_client(
            tmp_path / "ws",
            enabled_gate(None, servers=servers),
            executors=on(CODEX_BACKEND),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)
        assert _resolve(client, snapshot(stage=Stage.FINALIZE)) == {}
        assert reads == []

    @pytest.mark.parametrize(
        "servers", [dict(OPENCODE_SERVERS), {CODEX_BACKEND: "a.b"}]
    )
    def test_codex_plan_with_unusable_server_is_a_hit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        servers: dict[str, str],
    ) -> None:
        (tmp_path / "ws" / ".git").mkdir(parents=True)
        client = reach_client(
            tmp_path / "ws",
            enabled_gate(None, servers=servers),
            executors=on(CODEX_BACKEND),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)
        (hit,) = _resolve(client, snapshot(stage=Stage.PLAN)).values()
        assert _hit_tuple(hit, tmp_path) == [
            BRANCH,
            "clients.yaml tracker_mcp_gate.servers",
            servers.get(CODEX_BACKEND, "servers.codex (unset)"),
            CODEX_BACKEND,
        ]
        assert reads == []

    @pytest.mark.parametrize(
        "config",
        [None, 'tracking:\n  primary:\n    system: ""\n'],
        ids=["absent", "empty-string"],
    )
    def test_unset_tracker_warns_once_and_never_reads(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        config: str | None,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        if config is not None:
            _write_project_config_yaml(tmp_path, config)
        client = reach_client(
            tmp_path,
            enabled_gate(None, servers=dict(OPENCODE_SERVERS)),
            executors=on(OPENCODE_BACKEND),
        )
        reads = _count_reads(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, snapshot()) == {}
            assert _resolve(client, snapshot()) == {}

        assert reads == []
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "opencode:tracker" in warnings[0]
        assert "tracker unset" in warnings[0]

    def test_the_tracker_is_read_once_per_reader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The memo holds ``None`` too: an unset read must not re-read per call."""
        from cw.dispatch.claim import preflight_reach
        from cw.dispatch.claim.preflight_reach import ReachReader, assess_binding
        from cw.tracker import resolve_tracker

        reads: list[Path] = []

        def _spy(root: Path) -> str | None:
            reads.append(root)
            return resolve_tracker(root)

        monkeypatch.setattr(preflight_reach, "resolve_tracker", _spy)
        client = reach_client(
            tmp_path, enabled_gate(None, servers=dict(OPENCODE_SERVERS))
        )
        reader = ReachReader(client, tmp_path)
        assess_binding(reader, OPENCODE_BACKEND, Stage.PLAN, "b")
        assess_binding(reader, OPENCODE_BACKEND, Stage.PLAN, "b")
        assert len(reads) == 1

        _write_project_config_yaml(
            tmp_path, "tracking:\n  primary:\n    system: github-issues\n"
        )
        github_reader = ReachReader(client, tmp_path)
        assess_binding(github_reader, OPENCODE_BACKEND, Stage.PLAN, "b")
        assess_binding(github_reader, OPENCODE_BACKEND, Stage.PLAN, "b")
        assert len(reads) == 2

    def test_unknown_reading_warns_once_naming_the_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "missing")
        client = reach_client(
            tmp_path,
            enabled_gate(None, servers=dict(OPENCODE_SERVERS)),
            executors=on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, snapshot()) == {}
            assert _resolve(client, snapshot()) == {}

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert f"opencode:{path}" in warnings[0]
        assert "does not exist" in warnings[0]

    def test_probe_found_layer_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        (tmp_path / ".git").mkdir()
        (tmp_path / "opencode.json").write_text("{}", encoding="utf-8")
        client = reach_client(
            tmp_path,
            enabled_gate(None, servers=dict(OPENCODE_SERVERS)),
            executors=on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            assert _resolve(client, snapshot()) == {}

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "not parsed" in warnings[0]

    @pytest.mark.parametrize("middle", ["enabled", "absent"])
    def test_a_verdict_rearms_the_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        middle: McpStage,
    ) -> None:
        client = reach_client(
            tmp_path,
            enabled_gate(None, servers=dict(OPENCODE_SERVERS)),
            executors=on(OPENCODE_BACKEND),
            tracker="linear",
        )

        with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "malformed")
            _resolve(client, snapshot())
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, middle)
            _resolve(client, snapshot())
            stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "malformed")
            _resolve(client, snapshot())

        assert len(_warnings(caplog)) == 2

    def test_one_config_read_per_tick_per_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = reach_client(
            tmp_path,
            enabled_gate(None, servers=dict(OPENCODE_SERVERS)),
            executors=on(OPENCODE_BACKEND),
            tracker="linear",
        )
        queue = DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=f"T-{n}",
                    client=CLIENT,
                    status=QueueItemStatus.PENDING,
                    stage=Stage.PLAN,
                )
                for n in range(3)
            ]
        )
        reads = _count_reads(monkeypatch)

        with caplog.at_level(logging.INFO, logger="cw.dispatch"):
            hits = _resolve(client, queue)

        assert sorted(hits) == ["T-0", "T-1", "T-2"]
        assert reads == [(OPENCODE_BACKEND, SERVER)]
        info = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "cw.dispatch"
        ]
        assert info == [
            "dispatch: tracker-MCP gate holds 3 task(s) for acme (expected "
            "['linear']): ['T-0', 'T-1', 'T-2']"
        ]

    def test_opencode_row_under_plugin_only_gate_is_not_gated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "absent")
        client = reach_client(
            tmp_path,
            enabled_gate(PLUGIN),
            executors=on(OPENCODE_BACKEND),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)

        with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
            assert _resolve(client, snapshot()) == {}

        assert reads == []
        assert any(
            "not applied to acme T-2442 (backend opencode)" in r.getMessage()
            for r in caplog.records
        )

    def test_codex_impl_row_is_not_gated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = reach_client(
            tmp_path,
            enabled_gate(None, servers=dict(ALL_SERVERS)),
            executors=on(CODEX_BACKEND),
            tracker="linear",
        )
        reads = _count_reads(monkeypatch)

        assert _resolve(client, snapshot(stage=Stage.IMPL)) == {}
        assert reads == []


# --- RFC 0019 H2b (#3744): reasons, tool check, stage set, doctor mode --------


@pytest.mark.usefixtures("isolated_backend_layers")
@pytest.mark.parametrize("key", sorted(k for k, v in _OLD.items() if v["hit"]))
def test_every_parity_hit_carries_its_new_reason(
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
) -> None:
    from cw.dispatch.claim.preflight_reach import BindingHit
    from cw.stage_preflight import PreflightReason

    client, queue = build_case(
        key, monkeypatch=monkeypatch, tmp_path=tmp_path, make_git_repo=make_git_repo
    )
    hit = _resolve(client, queue)[TICKET]
    assert isinstance(hit, BindingHit)
    unbound = key.startswith("unbound/")
    expected = (
        PreflightReason.BINDING_UNBOUND
        if unbound
        else PreflightReason.BINDING_UNREACHABLE
    )
    assert hit.reason is expected
    assert hit.expected_kind == ("plugin" if key.startswith("claude/") else "server")
    assert hit.missing_tools == ()


def _tools_client(
    workspace: Path, tools: dict[str, str] | None, *, enabled: bool = True
) -> ClientConfig:
    from cw.models.bindings import Bindings

    realization: dict[str, object] = {"server": SERVER}
    if tools is not None:
        realization["tools"] = tools
    bindings = Bindings.model_validate(
        {
            "tickets": {
                "system": "linear",
                "mcp": {CODEX_BACKEND: realization},
                "preflight": {"enabled": enabled},
            }
        }
    )
    _write_tracker_config(workspace, "linear")
    return ClientConfig(
        name=CLIENT,
        workspace_path=workspace,
        bindings=bindings,
        pipeline=make_stage_pipeline(on(CODEX_BACKEND)),
    )


def _codex_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = stage_mcp_config(monkeypatch, tmp_path, CODEX_BACKEND, "enabled")
    text = path.read_text(encoding="utf-8").replace(
        f"[mcp_servers.{SERVER}]\n",
        f'[mcp_servers.{SERVER}]\nenabled_tools = ["get_issue"]\n',
    )
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.usefixtures("isolated_backend_layers")
class TestToolNameCheck:
    def test_an_excluded_declared_tool_parks_tool_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from cw.stage_preflight import PreflightReason

        path = _codex_allowlist(monkeypatch, tmp_path)
        workspace = tmp_path / "ws"
        (workspace / ".git").mkdir(parents=True)
        client = _tools_client(
            workspace, {"get": "get_issue", "comment": "save_comment"}
        )

        (hit,) = _resolve(client, snapshot(stage=Stage.PLAN)).values()

        assert hit.reason is PreflightReason.BINDING_TOOL_MISSING
        assert hit.missing_tools == ("save_comment",)
        assert _hit_tuple(hit, tmp_path) == [
            BRANCH,
            str(path).replace(str(tmp_path), "<tmp>"),
            SERVER,
            CODEX_BACKEND,
        ]

    @pytest.mark.parametrize(
        "tools", [None, {"get": "get_issue"}], ids=["undeclared", "allowed"]
    )
    def test_no_park_when_nothing_declared_is_excluded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tools: dict[str, str] | None,
    ) -> None:
        _codex_allowlist(monkeypatch, tmp_path)
        workspace = tmp_path / "ws"
        (workspace / ".git").mkdir(parents=True)

        assert _resolve(_tools_client(workspace, tools), snapshot()) == {}

    def test_another_layer_fails_the_tool_check_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tests._backend_mcp_helpers import write_project_layer

        _codex_allowlist(monkeypatch, tmp_path)
        workspace = tmp_path / "ws"
        (workspace / ".git").mkdir(parents=True)
        write_project_layer(workspace, CODEX_BACKEND)
        client = _tools_client(workspace, {"comment": "save_comment"})

        assert _resolve(client, snapshot()) == {}

    def test_an_opted_out_client_runs_no_tool_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _codex_allowlist(monkeypatch, tmp_path)
        workspace = tmp_path / "ws"
        (workspace / ".git").mkdir(parents=True)
        client = _tools_client(workspace, {"comment": "x"}, enabled=False)

        assert _resolve(client, snapshot()) == {}


def test_the_binding_stages_derive_from_stage_needs() -> None:
    from cw.dispatch.claim.preflight_reach import BINDING_STAGES

    assert {Stage.PLAN, Stage.IMPL} == BINDING_STAGES


@pytest.mark.parametrize(
    ("backend", "stage", "needed"),
    [
        *((CLAUDE_NATIVE_BACKEND, s, s in (Stage.PLAN, Stage.IMPL)) for s in STAGES),
        *((OPENCODE_BACKEND, s, gated(OPENCODE_BACKEND, s)) for s in STAGES),
        *((CODEX_BACKEND, s, gated(CODEX_BACKEND, s)) for s in STAGES),
        *(("local", s, False) for s in STAGES),
        ("nope", Stage.PLAN, False),
    ],
)
def test_the_needed_set_matches_the_hand_written_oracle(
    backend: str, stage: Stage, needed: bool
) -> None:
    from cw.dispatch.claim.preflight_reach import ReachState, assess_binding

    reader = _reader(ClientConfig(name=CLIENT, workspace_path=Path("/nonexistent")))
    state = assess_binding(reader, backend, stage, BRANCH).state
    assert (state is not ReachState.NOT_NEEDED) is needed


def _reader(client: ClientConfig) -> object:
    from cw.dispatch.claim.preflight_reach import ReachReader

    return ReachReader(client, client.workspace_path)


def test_a_branchless_claude_assessment_is_no_branch_without_a_read(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor mode: no branch to read is ``no_branch``, never a park or a read."""
    from cw.dispatch.claim.preflight_reach import ReachState, assess_binding

    repo = branch_with_settings(
        make_git_repo, {"enabledPlugins": {}}, branch=BRANCH, name="reach-repo"
    )
    calls = _count_git(monkeypatch)
    reader = _reader(reach_client(repo, enabled_gate()))

    assessment = assess_binding(reader, CLAUDE_NATIVE_BACKEND, Stage.PLAN, None)

    assert assessment.state is ReachState.NO_BRANCH
    assert assessment.hit is None
    assert calls == []
