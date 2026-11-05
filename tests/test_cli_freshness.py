"""Freshness-block sublines under a stale dispatch tick (``cw.cli._freshness``).

Moved out of ``cw.cli._base`` (#2934) so the root group stops importing
``cw.dispatch`` on every ``cw`` invocation. End-to-end coverage through
``cw dev-queue status`` stays in ``tests/test_dev_queue.py``; this file pins
each branch of the helper directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.cli._freshness import (
    _emit_freshness_subline,
    _resolve_default_branch_and_workspace,
)
from cw.dispatch import (
    FRESHNESS_MAIN_BEHIND,
    FRESHNESS_MAIN_DETACHED,
    FRESHNESS_MAIN_DIRTY_CHECKOUT,
    FRESHNESS_MAIN_DIVERGED,
    FRESHNESS_NON_MAIN_HEAD,
)
from cw.models import ClientConfig

_CLIENT = "acme"
_WORKSPACE = Path("/repo/acme")


@pytest.fixture
def known_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve any client name to a config with a ``trunk`` default branch."""
    monkeypatch.setattr(
        "cw.cli._freshness.get_client",
        lambda name: ClientConfig(
            name=name, workspace_path=_WORKSPACE, default_branch="trunk"
        ),
    )


def _emit(
    capsys: pytest.CaptureFixture[str],
    detail: str | None,
    blocked_branch: str | None = None,
) -> str:
    _emit_freshness_subline(_CLIENT, detail, blocked_branch, 3)
    return capsys.readouterr().out


@pytest.mark.usefixtures("known_client")
class TestEmitFreshnessSubline:
    def test_non_main_head_names_branch_and_fix(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = _emit(capsys, FRESHNESS_NON_MAIN_HEAD, "docs/foo")
        assert out == (
            "  ⚠ base checkout HEAD on 'docs/foo' (not trunk) — 3 pending blocked."
            " Fix: git -C /repo/acme checkout trunk\n"
        )

    def test_non_main_head_detached(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = _emit(capsys, FRESHNESS_NON_MAIN_HEAD, None)
        assert "HEAD on '(detached)'" in out

    def test_main_behind(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = _emit(capsys, FRESHNESS_MAIN_BEHIND)
        assert out == "  ⚠ acme: main behind origin — auto-ff pending/failed\n"

    def test_main_dirty_checkout(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = _emit(capsys, FRESHNESS_MAIN_DIRTY_CHECKOUT)
        assert out == (
            "  ⚠ acme: main checkout dirty — commit or stash changes,"
            " then auto-ff will retry\n"
        )

    def test_main_detached(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = _emit(capsys, FRESHNESS_MAIN_DETACHED)
        assert out == (
            "  ⚠ acme: main checkout has a detached HEAD —"
            " run: git -C /repo/acme checkout trunk\n"
        )

    def test_main_diverged(self, capsys: pytest.CaptureFixture[str]) -> None:
        out = _emit(capsys, FRESHNESS_MAIN_DIVERGED)
        assert out == (
            "  ⚠ acme: main diverged from origin —"
            " reconcile with: git -C <workspace> pull --rebase\n"
        )

    @pytest.mark.parametrize("detail", [None, "something_else"])
    def test_unmatched_detail_prints_nothing(
        self, capsys: pytest.CaptureFixture[str], detail: str | None
    ) -> None:
        assert _emit(capsys, detail) == ""


class TestResolveDefaultBranchAndWorkspace:
    @pytest.mark.usefixtures("known_client")
    def test_known_client(self) -> None:
        assert _resolve_default_branch_and_workspace(_CLIENT) == (
            "trunk",
            str(_WORKSPACE),
        )

    def test_unknown_client_falls_back(self) -> None:
        """No clients are configured (autouse ``tmp_config_dir``), so the real
        ``get_client`` raises ``CwError`` and the bare fallback applies."""
        assert _resolve_default_branch_and_workspace("ghost") == ("main", "ghost")
