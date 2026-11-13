"""Tests for :mod:`cw.client_resolution` (#3170).

The cwd-to-client matcher moved here from ``cw.statusline`` and gained the
tie, nested and no-match semantics ``cw orchestrator-start`` needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw._util import NO_CLIENTS_MSG
from cw.client_resolution import (
    ClientInferenceError,
    client_roots,
    infer_client,
    match_client_roots,
    resolve_client_for_cwd,
)
from cw.exceptions import CwError
from cw.models import ClientConfig
from cw.worktree import effective_worktree_bases
from tests._clients_yaml import ClientSpec, write_clients_yaml


def _client(name: str, workspace: Path, **fields: object) -> ClientConfig:
    return ClientConfig.model_validate(
        {"name": name, "workspace_path": workspace, **fields}
    )


def _clients(*clients: ClientConfig) -> dict[str, ClientConfig]:
    return {c.name: c for c in clients}


# ---------------------------------------------------------------------------
# resolve_client_for_cwd (moved from tests/test_statusline.py)
# ---------------------------------------------------------------------------


def test_resolve_client_for_cwd_matches_workspace(tmp_config_dir: Path) -> None:
    ws_a = tmp_config_dir / "ws" / "client-a"
    write_clients_yaml(
        ClientSpec("client-a", ws_a),
        ClientSpec("client-b", tmp_config_dir / "ws" / "client-b"),
        ensure_workspaces=True,
    )

    assert resolve_client_for_cwd(ws_a / "sub") == "client-a"


def test_resolve_client_for_cwd_unmapped_returns_none(tmp_config_dir: Path) -> None:
    write_clients_yaml(
        ClientSpec("client-a", tmp_config_dir / "ws" / "client-a"),
        ensure_workspaces=True,
    )

    assert resolve_client_for_cwd(tmp_config_dir / "elsewhere") is None


def test_resolve_client_for_cwd_tie_returns_first_in_config_order(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "shared"
    clients = _clients(_client("a", ws), _client("b", ws))

    assert resolve_client_for_cwd(ws / "x", clients) == "a"


# ---------------------------------------------------------------------------
# client_roots / match_client_roots
# ---------------------------------------------------------------------------


def test_matches_repo_path_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    client = _client("c", tmp_path / "ws", repo_path=repo)

    assert repo.resolve() in client_roots(client)
    match = match_client_roots([repo / "src"], _clients(client))
    assert match.winners == ("c",)
    assert match.root == repo.resolve()


def test_matches_dispatch_worktree_base(tmp_path: Path) -> None:
    base = tmp_path / "wt-base"
    client = _client("c", tmp_path / "ws", worktree_base=base)

    match = match_client_roots([base / "dev-7"], _clients(client))

    assert match.winners == ("c",)
    assert match.root == base.resolve()


def test_matches_hashed_worktree_base(tmp_path: Path) -> None:
    client = _client("c", tmp_path / "ws")
    hashed = next(b for b in effective_worktree_bases(client) if ".cw" in b.parts)

    match = match_client_roots([hashed / "dev-7"], _clients(client))

    assert match.winners == ("c",)
    assert match.root == hashed.resolve()


def test_nested_longest_root_wins_and_reports_shadowed_client(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "sub"
    clients = _clients(_client("outer", outer), _client("inner", inner))

    match = match_client_roots([inner / "deep"], clients)

    assert match.winners == ("inner",)
    assert match.root == inner.resolve()
    assert match.shadowed == (("outer", outer.resolve()),)


def test_same_client_multiple_roots_not_reported_as_nested(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    client = _client("c", ws, repo_path=ws / "repo")

    match = match_client_roots([ws / "repo" / "src"], _clients(client))

    assert match.winners == ("c",)
    assert match.root == (ws / "repo").resolve()
    assert match.shadowed == ()


def test_tie_same_root_reports_all_winners(tmp_path: Path) -> None:
    ws = tmp_path / "shared"
    clients = _clients(_client("a", ws), _client("b", ws))

    match = match_client_roots([ws], clients)

    assert match.winners == ("a", "b")
    assert match.root == ws.resolve()
    assert match.shadowed == ()


def test_no_match_has_no_winners(tmp_path: Path) -> None:
    clients = _clients(_client("a", tmp_path / "a"))

    match = match_client_roots([tmp_path / "elsewhere"], clients)

    assert match.winners == ()
    assert match.root is None


# ---------------------------------------------------------------------------
# infer_client
# ---------------------------------------------------------------------------


def test_infer_client_single_match_returns_name_and_root(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    clients = _clients(_client("a", ws), _client("b", tmp_path / "other"))

    inferred = infer_client([ws / "sub"], clients)

    assert inferred.name == "a"
    assert inferred.root == ws.resolve()
    assert inferred.warnings == ()


def test_infer_client_tie_raises_naming_both_and_shared_path(tmp_path: Path) -> None:
    ws = tmp_path / "shared"
    clients = _clients(_client("a", ws), _client("b", ws))

    with pytest.raises(ClientInferenceError) as exc_info:
        infer_client([ws / "x"], clients)

    msg = str(exc_info.value)
    assert "'a'" in msg
    assert "'b'" in msg
    assert str(ws.resolve()) in msg
    assert "pass -c <name>" in msg
    assert isinstance(exc_info.value, CwError)


def test_infer_client_no_match_raises_listing_clients_and_no_fallback(
    tmp_path: Path,
) -> None:
    clients = _clients(_client("a", tmp_path / "a"), _client("b", tmp_path / "b"))
    cwd = tmp_path / "elsewhere"

    with pytest.raises(ClientInferenceError) as exc_info:
        infer_client([cwd], clients)

    msg = str(exc_info.value)
    assert f"could not infer a client from {cwd}" in msg
    assert "pass -c <name>" in msg
    assert "Configured clients: a, b" in msg


def test_infer_client_nested_returns_warning_naming_both(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "sub"
    clients = _clients(_client("outer", outer), _client("inner", inner))

    inferred = infer_client([inner], clients)

    assert inferred.name == "inner"
    assert len(inferred.warnings) == 1
    warning = inferred.warnings[0]
    assert "'inner'" in warning
    assert "'outer'" in warning
    assert str(inner.resolve()) in warning
    assert str(outer.resolve()) in warning


def test_infer_client_multiple_candidate_paths_pooled_and_deduped(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    clients = _clients(_client("a", ws), _client("b", tmp_path / "other"))

    inferred = infer_client([ws / "sub", ws], clients)

    assert inferred.name == "a"
    assert inferred.warnings == ()


def test_infer_client_no_clients_configured_raises(tmp_path: Path) -> None:
    with pytest.raises(ClientInferenceError, match="No clients configured") as exc:
        infer_client([tmp_path], {})
    assert str(exc.value) == NO_CLIENTS_MSG


def test_infer_client_three_way_tie_names_all(tmp_path: Path) -> None:
    ws = tmp_path / "shared"
    clients = _clients(_client("a", ws), _client("b", ws), _client("c", ws))

    with pytest.raises(ClientInferenceError) as exc_info:
        infer_client([ws], clients)

    assert "clients 'a', 'b' and 'c' all claim" in str(exc_info.value)


def test_infer_client_no_paths_names_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    clients = _clients(_client("a", tmp_path / "a"))

    with pytest.raises(ClientInferenceError) as exc_info:
        infer_client([], clients)

    assert f"could not infer a client from {Path.cwd()}" in str(exc_info.value)
