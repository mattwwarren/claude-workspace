"""CLI and preflight helpers: project and tracker config writers, the agent
repo and check-stub installers, and the Click command-tree walker. Moved
from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
import pytest

if TYPE_CHECKING:
    import types
    from collections.abc import Iterator


def _patch_cw_dist_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch importlib.metadata.distribution() to raise PackageNotFoundError,
    simulating a registry/unknown install. Shared helper (#1514).
    """
    import importlib.metadata

    def _raise(_pkg: str) -> object:
        raise importlib.metadata.PackageNotFoundError(_pkg)

    monkeypatch.setattr(importlib.metadata, "distribution", _raise)


def _write_project_config_yaml(root: Path, content: str) -> None:
    """Write .claude/project-config.yaml under *root*.

    Shared by test_tracker.py's and test_review_strategy.py's own private
    `_write_config` copies in shape (write a project-config.yaml under a tmp
    root); this is the canonical version new tests should import instead of
    adding a fourth copy. The two existing private copies in test_tracker.py
    and test_review_strategy.py are left as-is — pre-existing duplication,
    out of scope for this ticket.
    """
    config_dir = root / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project-config.yaml").write_text(content, encoding="utf-8")


def _write_tracker_config(root: Path, system: str) -> None:
    """Write a project-config.yaml under *root* naming tracker *system*.

    Hoisted from ``test_preflight.py``'s private ``_write_config`` (#2697) so
    the ``cw preflight`` CLI tests share it.
    """
    _write_project_config_yaml(root, f"tracking:\n  primary:\n    system: {system}\n")


def _make_agent_repo(root: Path, *, tracker: str | None = "github-issues") -> Path:
    """Build a repo root with the required plan-review agents present locally.

    Hoisted from ``test_preflight.py`` (#2697); also used by
    ``test_cli_preflight.py``. *tracker* ``None`` writes no project-config.
    """
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "plan-reviewer.md").write_text("stub\n", encoding="utf-8")
    (agents / "plan-soundness-reviewer.md").write_text("stub\n", encoding="utf-8")
    if tracker is not None:
        _write_tracker_config(root, tracker)
    return root


def _install_check_stubs(
    pf: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, tuple[str, ...]],
) -> None:
    """Stub ``cw.preflight``'s four subprocess-touching checks (#2697).

    *pf* is the ``cw.preflight`` module. With the stubs in place
    ``run_preflight`` runs with no real ``gh``/``cw``; each stub records its
    arguments in *captured* under ``ticket_open``, ``no_open_pr`` and
    ``not_queued``. Hoisted from ``test_preflight.py`` so
    ``test_cli_preflight.py`` shares it.
    """

    def _fake_doctor() -> tuple[dict[str, object], dict[str, object]]:
        return (
            {
                "name": "cw_backend_healthy",
                "passed": True,
                "severity": "hard",
                "detail": "",
            },
            {
                "name": "cw_doctor_clean",
                "passed": True,
                "severity": "soft",
                "detail": "",
            },
        )

    def _fake_ticket_open(ticket: str, repo: str, tracker: str) -> dict[str, object]:
        captured["ticket_open"] = (ticket, repo, tracker)
        return {"name": "ticket_open", "passed": True, "severity": "hard", "detail": ""}

    def _fake_no_open_pr(ticket: str, repo: str, tracker: str) -> dict[str, object]:
        captured["no_open_pr"] = (ticket, repo, tracker)
        return {
            "name": "no_open_pr_for_ticket",
            "passed": True,
            "severity": "hard",
            "detail": "",
        }

    def _fake_not_queued(ticket: str, client: str) -> dict[str, object]:
        captured["not_queued"] = (ticket, client)
        return {
            "name": "not_already_queued",
            "passed": True,
            "severity": "hard",
            "detail": "",
        }

    monkeypatch.setattr(pf, "_check_cw_doctor", _fake_doctor)
    monkeypatch.setattr(pf, "_check_ticket_open", _fake_ticket_open)
    monkeypatch.setattr(pf, "_check_no_open_pr", _fake_no_open_pr)
    monkeypatch.setattr(pf, "_check_not_queued", _fake_not_queued)


def walk_click_tree(
    root: click.Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    """Yield ``(path, command)`` for *root* and every command beneath it.

    Hoisted from ``test_cli_surface_conventions.py``'s private ``_walk``
    (#2697) so the orchestrator command-shape lint can resolve allowlisted
    verbs against the same live Click tree.
    """
    yield path, root
    if isinstance(root, click.Group):
        ctx = click.Context(root)
        for name in root.list_commands(ctx):
            sub = root.get_command(ctx, name)
            if sub is not None:
                yield from walk_click_tree(sub, (*path, name))
