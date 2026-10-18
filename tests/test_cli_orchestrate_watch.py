"""``cw orchestrate watch``: option parsing and the first-line scope (#2693, D4).

``AttentionStream.run`` is real (it writes the scope line); only
``AttentionStream.step`` is stubbed to raise, so ``run``'s fatal-error path
ends the command with the scope line and one exit line on stdout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner, Result

from cw.attention_watch import ScopeSource, WatchScope
from cw.cli import main
from cw.focus import set_focus
from cw.models import DEFAULT_LANE
from tests._ticket_view_helpers import ACME, install_acme_client

if TYPE_CHECKING:
    from pathlib import Path

_FAST = "fast"
_SESSION = "sess-1"
_EXIT_LINE = "WATCH | exited: RuntimeError: stop"


@pytest.fixture(autouse=True)
def _acme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_events_dir: Path
) -> None:
    install_acme_client(tmp_path, monkeypatch, lanes=(DEFAULT_LANE, _FAST))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def stub_step(monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(_self: object) -> list[str]:
        msg = "stop"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.attention_stream.AttentionStream.step", stop)


def _watch(*args: str) -> Result:
    return CliRunner().invoke(main, ["orchestrate", "watch", *args])


def _scope_line(scope: WatchScope) -> str:
    return (
        f"WATCH | scope | client={scope.client} | lanes={scope.lanes_text}"
        f" | tickets={scope.tickets_text} | source={scope.source.value}"
        f" | start=now | cursor={scope.consumer}"
    )


def _scope(
    source: ScopeSource,
    *,
    lanes: frozenset[str] = frozenset(),
    tickets: frozenset[str] = frozenset(),
    all_lanes: bool = False,
) -> WatchScope:
    return WatchScope(
        client=ACME, lanes=lanes, tickets=tickets, all_lanes=all_lanes, source=source
    )


def _focus(monkeypatch: pytest.MonkeyPatch, client: str, lane: str | None) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SESSION)
    set_focus(_SESSION, client, lane)


@pytest.mark.usefixtures("stub_step")
class TestScopeLine:
    def _assert_scope(self, result: Result, scope: WatchScope) -> None:
        assert result.exit_code == 1, result.output
        assert result.stdout.splitlines() == [_scope_line(scope), _EXIT_LINE]
        assert "WATCH | scope" not in result.stderr

    def test_focus_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _focus(monkeypatch, ACME, _FAST)
        self._assert_scope(
            _watch(), _scope(ScopeSource.FOCUS, lanes=frozenset({_FAST}))
        )

    def test_whole_client_without_focus(self) -> None:
        self._assert_scope(_watch("-c", ACME), _scope(ScopeSource.CLIENT))

    def test_focus_for_another_client_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _focus(monkeypatch, "other", _FAST)
        self._assert_scope(_watch("-c", ACME), _scope(ScopeSource.CLIENT))

    def test_explicit_flags(self) -> None:
        result = _watch(
            "-c", ACME, "--lane", _FAST, "--lane", DEFAULT_LANE, "--ticket", "T-1"
        )
        scope = _scope(
            ScopeSource.FLAGS,
            lanes=frozenset({_FAST, DEFAULT_LANE}),
            tickets=frozenset({"T-1"}),
        )
        self._assert_scope(result, scope)
        assert "| lanes=default,fast | tickets=T-1 |" in result.stdout

    def test_all_lanes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _focus(monkeypatch, ACME, _FAST)
        self._assert_scope(
            _watch("--all-lanes"), _scope(ScopeSource.ALL_LANES, all_lanes=True)
        )

    def test_ticket_only_ignores_focus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _focus(monkeypatch, ACME, _FAST)
        result = _watch("--ticket", "T-2", "--ticket", "T-1")
        self._assert_scope(
            result, _scope(ScopeSource.FLAGS, tickets=frozenset({"T-1", "T-2"}))
        )
        assert "| lanes=- | tickets=T-1,T-2 |" in result.stdout

    def test_options_parse(self) -> None:
        result = _watch(
            "-c",
            ACME,
            "--stuck-after-minutes",
            "3",
            "--since",
            "abc",
            "--force",
        )
        assert result.exit_code == 1
        assert "| start=since |" in result.stdout.splitlines()[0]


class TestRefusals:
    def test_unknown_explicit_lane(self) -> None:
        result = _watch("-c", ACME, "--lane", "nope")
        assert result.exit_code == 1
        assert (
            f"Lane 'nope' is not declared for client '{ACME}'. Declared lanes:"
            f" {DEFAULT_LANE}, {_FAST}. Run: cw lane add {ACME} nope"
        ) in result.stderr
        assert result.stdout == ""

    def test_stale_focus_lane_is_used_as_is(
        self, monkeypatch: pytest.MonkeyPatch, stub_step: None
    ) -> None:
        _focus(monkeypatch, ACME, "gone")
        result = _watch()
        assert result.stdout.splitlines()[0].startswith(
            f"WATCH | scope | client={ACME} | lanes=gone |"
        )

    def test_no_client_and_no_focus(self) -> None:
        result = _watch()
        assert result.exit_code == 1
        assert "No client to watch" in result.stderr

    def test_all_lanes_with_lane_is_a_usage_error(self) -> None:
        result = _watch("-c", ACME, "--all-lanes", "--lane", _FAST)
        assert result.exit_code == 2
        assert "--all-lanes cannot be combined with --lane" in result.stderr

    def test_stuck_after_minutes_must_be_positive(self) -> None:
        assert _watch("-c", ACME, "--stuck-after-minutes", "0").exit_code == 2


def _normalized(text: str) -> str:
    return " ".join(text.split())


class TestHelp:
    def test_help_strings(self) -> None:
        text = _normalized(_watch("--help").stdout)
        for expected in (
            "Stream actionable orchestrator events, one line each, until stopped.",
            "silence means healthy.",
            "The first line printed is the resolved scope (client, lanes, tickets).",
            "nothing replayed, nothing lost.",
            "Client to watch (default: the client in this session's focus pointer).",
            "Watch this lane (repeatable). Default: the focus-pointer lane, else"
            " every lane.",
            "Also watch this ticket in any lane (repeatable).",
            "Watch every lane of the client, ignoring the focus-pointer lane.",
            "Start after this event id instead of the saved position (a first run"
            " starts at the current end of the event log).",
            "Report a pending ticket as STUCK after this many minutes when its"
            " blocker will not clear on its own (default: 15).",
            "Start even if the saved position names a live watcher pid. A recycled"
            " pid also looks alive, so a stale record needs this flag.",
        ):
            assert _normalized(expected) in text, expected
