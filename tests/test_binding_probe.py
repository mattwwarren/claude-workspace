"""Tests for the standalone tri-state binding probe (RFC 0019 H1, #3708)."""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from typing import Any

import pytest

import cw.binding_probe as probe_mod
from cw.binding_probe import Reach, probe_binding
from cw.dispatch.tracker_mcp_gate import _plugin_enabled
from cw.models import ClientConfig
from cw.models.bindings import Bindings
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)
from tests._backend_mcp_helpers import _CAPTURED_SERVER, McpStage, stage_mcp_config
from tests._doctor_helpers import make_client
from tests._shared.git_repos import branch_with_file, branch_with_settings

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_PLUGIN = "linear@marketplace"
_BRANCH = "feature"


def _tickets_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "system": "linear",
        "mcp": {
            CLAUDE_NATIVE_BACKEND: {"plugin_id": _PLUGIN},
            CODEX_BACKEND: {"server": _CAPTURED_SERVER},
            OPENCODE_BACKEND: {"server": _CAPTURED_SERVER},
        },
    }
    body.update(extra)
    return body


def _client(repo: Path, **tickets_extra: Any) -> ClientConfig:
    bindings = Bindings.model_validate({"tickets": _tickets_body(**tickets_extra)})
    return make_client(bindings=bindings, workspace_path=repo)


# --- claude-native: enabledPlugins read from a branch -----------------------


@pytest.mark.parametrize(
    ("enabled_plugins", "expected"),
    [
        ({_PLUGIN: True}, Reach.ENABLED),
        ({_PLUGIN: False}, Reach.ABSENT),
        ({"other@m": True}, Reach.ABSENT),
        ({_PLUGIN: "yes"}, Reach.UNKNOWN),
    ],
)
def test_claude_native_branch_verdicts(
    make_git_repo: Any, enabled_plugins: dict[str, object], expected: Reach
) -> None:
    repo = branch_with_settings(
        make_git_repo, {"enabledPlugins": enabled_plugins}, branch=_BRANCH
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=_BRANCH)
    assert result.state is expected
    assert result.bound is True
    assert result.source == f"{_BRANCH}:.claude/settings.json"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ({_PLUGIN: True}, Reach.ENABLED),
        ({_PLUGIN: False}, Reach.ABSENT),
        ({_PLUGIN: None}, Reach.UNKNOWN),
    ],
)
def test_probe_and_gate_classifier_agree(
    make_git_repo: Any, raw_value: dict[str, object], expected: Reach
) -> None:
    gate_verdict = _plugin_enabled(raw_value, _PLUGIN)
    repo = branch_with_settings(
        make_git_repo, {"enabledPlugins": raw_value}, branch=_BRANCH
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=_BRANCH)
    mapped = {True: Reach.ENABLED, False: Reach.ABSENT, None: Reach.UNKNOWN}[
        gate_verdict
    ]
    assert result.state is expected is mapped


def test_enabled_plugins_key_missing_is_unknown_naming_key(make_git_repo: Any) -> None:
    repo = branch_with_settings(make_git_repo, {"other": 1}, branch=_BRANCH)
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=_BRANCH)
    assert result.state is Reach.UNKNOWN
    assert "enabledPlugins" in result.reason


@pytest.mark.parametrize(
    ("content", "reason_part"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
    ],
)
def test_malformed_or_wrong_shape_settings_are_unknown(
    make_git_repo: Any, content: str, reason_part: str
) -> None:
    repo = branch_with_file(
        make_git_repo, ".claude/settings.json", content, branch=_BRANCH
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=_BRANCH)
    assert result.state is Reach.UNKNOWN
    assert reason_part in result.reason


def test_non_utf8_settings_blob_is_unknown(make_git_repo: Any) -> None:
    repo = branch_with_file(
        make_git_repo, ".claude/settings.json", b"\xff\xfe\x00bad", branch=_BRANCH
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=_BRANCH)
    assert result.state is Reach.UNKNOWN
    assert "UTF-8" in result.reason


def test_settings_file_missing_on_branch_is_unknown(make_git_repo: Any) -> None:
    repo = branch_with_file(make_git_repo, "README.md", "x\n", branch=_BRANCH)
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=_BRANCH)
    assert result.state is Reach.UNKNOWN
    assert "missing" in result.reason


def test_branch_does_not_exist_is_unknown(make_git_repo: Any) -> None:
    repo = branch_with_settings(
        make_git_repo, {"enabledPlugins": {_PLUGIN: True}}, branch=_BRANCH
    )
    result = probe_binding(
        CLAUDE_NATIVE_BACKEND, _client(repo), branch="no-such-branch"
    )
    assert result.state is Reach.UNKNOWN
    assert "does not exist" in result.reason


def test_custom_settings_path_is_honored(make_git_repo: Any) -> None:
    repo = branch_with_settings(
        make_git_repo,
        {"enabledPlugins": {_PLUGIN: True}},
        settings_path="config/tickets.json",
        branch=_BRANCH,
    )
    client = _client(
        repo, preflight={"enabled": True, "settings_path": "config/tickets.json"}
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, client, branch=_BRANCH)
    assert result.state is Reach.ENABLED
    assert result.source == f"{_BRANCH}:config/tickets.json"


def test_git_unavailable_is_unknown_without_mock(tmp_path: Path) -> None:
    client = make_client(
        bindings=Bindings.model_validate({"tickets": _tickets_body()}),
        workspace_path=tmp_path / "missing-repo",
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, client, branch=_BRANCH)
    assert result.state is Reach.UNKNOWN
    assert result.reason == "git unavailable"


def test_no_branch_is_unknown_and_bound(make_git_repo: Any) -> None:
    repo = branch_with_settings(
        make_git_repo, {"enabledPlugins": {_PLUGIN: True}}, branch=_BRANCH
    )
    result = probe_binding(CLAUDE_NATIVE_BACKEND, _client(repo), branch=None)
    assert result.state is Reach.UNKNOWN
    assert result.bound is True
    assert result.reason == "no branch"


def test_claude_native_server_only_realization_is_unbound(make_git_repo: Any) -> None:
    repo = branch_with_settings(
        make_git_repo, {"enabledPlugins": {_PLUGIN: True}}, branch=_BRANCH
    )
    bindings = Bindings.model_validate(
        {
            "tickets": {
                "system": "linear",
                "mcp": {CLAUDE_NATIVE_BACKEND: {"server": "linear"}},
            }
        }
    )
    client = make_client(bindings=bindings, workspace_path=repo)
    result = probe_binding(CLAUDE_NATIVE_BACKEND, client, branch=_BRANCH)
    assert result.bound is False
    assert result.state is Reach.UNKNOWN


# --- codex and opencode: backend MCP config ----------------------------------


@pytest.mark.parametrize("backend", [CODEX_BACKEND, OPENCODE_BACKEND])
@pytest.mark.parametrize(
    ("state", "expected", "reason_part"),
    [
        ("enabled", Reach.ENABLED, ""),
        ("absent", Reach.ABSENT, ""),
        ("disabled", Reach.ABSENT, "disabled"),
        ("missing", Reach.UNKNOWN, ""),
        ("malformed", Reach.UNKNOWN, ""),
        ("non_dict", Reach.UNKNOWN, ""),
        ("non_bool_enabled", Reach.UNKNOWN, ""),
    ],
)
def test_mcp_config_states_map_to_reach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ancestor_free_dir: Path,
    backend: str,
    state: McpStage,
    expected: Reach,
    reason_part: str,
) -> None:
    stage_mcp_config(monkeypatch, tmp_path, backend, state)
    client = _client(tmp_path)
    result = probe_binding(backend, client, root=ancestor_free_dir)
    assert result.state is expected
    assert result.bound is True
    assert reason_part in result.reason
    assert result.source is not None


def test_opencode_jsonc_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ancestor_free_dir: Path
) -> None:
    stage_mcp_config(monkeypatch, tmp_path, OPENCODE_BACKEND, "jsonc")
    result = probe_binding(OPENCODE_BACKEND, _client(tmp_path), root=ancestor_free_dir)
    assert result.state is Reach.UNKNOWN


def test_codex_without_server_is_unbound(tmp_path: Path) -> None:
    bindings = Bindings.model_validate(
        {
            "tickets": {
                "system": "linear",
                "mcp": {CLAUDE_NATIVE_BACKEND: {"plugin_id": _PLUGIN}},
            }
        }
    )
    client = make_client(bindings=bindings, workspace_path=tmp_path)
    result = probe_binding(CODEX_BACKEND, client, root=tmp_path)
    assert result.bound is False
    assert result.state is Reach.UNKNOWN


# --- short circuits -----------------------------------------------------------


def test_cli_gh_is_enabled_without_reading(tmp_path: Path) -> None:
    bindings = Bindings.model_validate(
        {"tickets": {"system": "github-issues", "cli": "gh"}}
    )
    client = make_client(bindings=bindings, workspace_path=tmp_path)
    result = probe_binding(CODEX_BACKEND, client, root=tmp_path)
    assert result.state is Reach.ENABLED
    assert result.bound is True
    assert result.reason == "cli: gh needs no MCP"
    assert result.source is None


def test_no_tickets_binding_is_unbound(tmp_path: Path) -> None:
    client = make_client(workspace_path=tmp_path)
    result = probe_binding(CODEX_BACKEND, client, root=tmp_path)
    assert result.bound is False
    assert result.state is Reach.UNKNOWN


def test_local_is_unbound_and_unknown(tmp_path: Path) -> None:
    result = probe_binding(LOCAL_BACKEND, _client(tmp_path), root=tmp_path)
    assert result.bound is False
    assert result.state is Reach.UNKNOWN


def test_unknown_backend_is_unrecognized(tmp_path: Path) -> None:
    result = probe_binding("nope", _client(tmp_path), root=tmp_path)
    assert result.state is Reach.UNKNOWN
    assert result.bound is False
    assert "unrecognized backend" in result.reason


# --- no observability emissions (R16) ----------------------------------------


def test_probe_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    import cw.events as events_mod

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "probe must not record events"
        raise AssertionError(msg)

    monkeypatch.setattr(events_mod, "record_event", _boom)
    caplog.set_level(logging.DEBUG)
    probe_binding(LOCAL_BACKEND, _client(tmp_path), root=tmp_path)
    probe_binding("nope", _client(tmp_path), root=tmp_path)
    assert caplog.records == []


def test_module_has_no_observability_imports() -> None:
    tree = ast.parse(Path(inspect.getfile(probe_mod)).read_text(encoding="utf-8"))
    banned = {"logging", "warnings", "log_event", "record_event"}
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen |= {a.name for a in node.names}
        if isinstance(node, ast.ImportFrom):
            seen |= {a.name for a in node.names} | (
                {node.module} if node.module else set()
            )
    assert not {s.split(".")[-1] for s in seen} & banned


def test_probe_signature_has_no_needs_parameter() -> None:
    params = set(inspect.signature(probe_binding).parameters)
    assert params == {"backend", "client", "branch", "root"}
