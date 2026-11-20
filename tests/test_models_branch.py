"""``cw.models.branch.resolve_feature_branch``: the one per-row branch seam (#2948).

The resolver returns the row's recorded ``branch`` when one is set, else
``<feature_branch_prefix>/<ticket_id>`` with a ``dev`` fallback when the client
is unknown. Every row on disk today has ``branch=None``, so the fallback must
stay byte-identical to the derivation every site used before.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import cw.models
from cw.models import ClientConfig, resolve_base_branch, resolve_feature_branch
from cw.models.branch import _DEFAULT_BASE_BRANCH
from tests._shared.session_seeding import _make_ticket_task
from tests._shared.workflow_text import _SRC_ROOT

_BRANCH_MODULE = _SRC_ROOT / "cw" / "models" / "branch.py"
_SIBLING_MODULES = frozenset({"cw.models.client", "cw.models.tasks"})


def _client(prefix: str = "dev", default_branch: str = "main") -> ClientConfig:
    return ClientConfig(
        name="client-a",
        workspace_path=Path("/tmp/ws"),
        feature_branch_prefix=prefix,
        default_branch=default_branch,
    )


def test_no_task_uses_the_client_prefix() -> None:
    assert resolve_feature_branch("T-1", _client(), None) == "dev/T-1"
    assert resolve_feature_branch("T-1", _client("feat"), None) == "feat/T-1"


def test_task_defaults_to_none() -> None:
    assert resolve_feature_branch("T-1", _client("feat")) == "feat/T-1"


def test_unknown_client_falls_back_to_dev() -> None:
    assert resolve_feature_branch("T-1", None, None) == "dev/T-1"


def test_recorded_row_branch_wins_over_the_prefix() -> None:
    task = _make_ticket_task(ticket_id="T-1", branch="feature/x")
    assert resolve_feature_branch("T-1", _client("feat"), task) == "feature/x"
    assert resolve_feature_branch("T-1", None, task) == "feature/x"


@pytest.mark.parametrize(
    ("prefix", "ticket_id"),
    [("dev", "T-1"), ("feat", "662"), ("dev", "repo#12"), ("team/x", "GEN-1.2")],
)
def test_unset_row_branch_matches_the_old_derivation(
    prefix: str, ticket_id: str
) -> None:
    """A ``branch=None`` row resolves exactly as ``f"{prefix}/{ticket_id}"``."""
    client = _client(prefix)
    task = _make_ticket_task(ticket_id=ticket_id)
    assert task.branch is None
    expected = f"{prefix}/{ticket_id}"
    assert resolve_feature_branch(ticket_id, client, task) == expected
    assert resolve_feature_branch(ticket_id, client, None) == expected


def test_resolver_is_exported_from_cw_models() -> None:
    assert "resolve_feature_branch" in cw.models.__all__
    assert cw.models.resolve_feature_branch is resolve_feature_branch


def _runtime_sibling_imports(source: str) -> list[str]:
    """Sibling ``cw.models`` imports in *source* outside ``if TYPE_CHECKING``."""
    tree = ast.parse(source)
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            guarded.update(id(child) for child in ast.walk(node))
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom) and node.module in _SIBLING_MODULES:
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(
                alias.name for alias in node.names if alias.name in _SIBLING_MODULES
            )
    return found


def test_branch_module_imports_siblings_only_for_type_checking() -> None:
    """A runtime import of ``client``/``tasks`` would create a models cycle."""
    source = _BRANCH_MODULE.read_text(encoding="utf-8")
    assert _runtime_sibling_imports(source) == []


def test_runtime_import_detector_flags_an_unguarded_import() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from cw.models.client import ClientConfig\n"
        "import cw.models.tasks\n"
        "if TYPE_CHECKING:\n"
        "    from cw.models.tasks import TicketTask\n"
    )
    assert _runtime_sibling_imports(source) == ["cw.models.client", "cw.models.tasks"]


# --- resolve_base_branch (GitHub #3779, RFC 0020 D-3) ---


def test_null_row_base_returns_client_default_branch() -> None:
    task = _make_ticket_task(ticket_id="T-1", base_branch=None)
    assert resolve_base_branch(_client(default_branch="develop"), task) == "develop"


def test_set_row_base_wins_over_the_client() -> None:
    task = _make_ticket_task(ticket_id="T-1", base_branch="epic/3778")
    assert resolve_base_branch(_client(default_branch="develop"), task) == "epic/3778"


def test_no_client_no_task_returns_main() -> None:
    assert resolve_base_branch(None, None) == "main"


def test_null_client_with_set_row_returns_row_base() -> None:
    task = _make_ticket_task(ticket_id="T-1", base_branch="epic/3778")
    assert resolve_base_branch(None, task) == "epic/3778"


def test_null_task_defaults_to_client_default() -> None:
    assert resolve_base_branch(_client(default_branch="develop")) == "develop"


def test_default_constant_matches_clientconfig_default() -> None:
    assert ClientConfig.model_fields["default_branch"].default == _DEFAULT_BASE_BRANCH


def test_base_resolver_is_exported_from_cw_models() -> None:
    assert "resolve_base_branch" in cw.models.__all__
    assert cw.models.resolve_base_branch is resolve_base_branch
