"""``cw outbox drain`` (#3706, RFC 0019 O2): exit codes, output contract, config.

Clients come from a patched ``cw.config.load_clients``; every ``gh`` call is
answered by :class:`tests._shared.outbox_builders.GhThread`.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import click
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from cw.cli import main
from cw.cli import outbox_drain as drain_cli
from cw.config import state_dir
from cw.models import ClientConfig
from cw.models.bindings import Bindings
from cw.models.client import OUTBOX_MAX_ENTRIES_PER_RUN_CEILING, OutboxConfig
from cw.outbox import (
    OutboxError,
    OutboxReason,
    OutboxRole,
    OutboxState,
    UnreachableMarker,
    _layout,
    append_held,
    list_entries,
    marked_roles,
    read_held,
    write_marker,
)
from tests._shared.lock_helpers import _hold_flock
from tests._shared.outbox_builders import CLIENT, GhThread, client_config, enqueue

if TYPE_CHECKING:
    from pathlib import Path

_OTHER = "client-b"
_REPORT_KEYS = {
    "client",
    "drained",
    "delivered",
    "unverified",
    "skipped",
    "released",
    "held",
    "lease_held",
    "unreachable_roles",
}
_DEFAULT_CAP = 20
_CEILING = 100
_EXIT_ERROR = 1
_EXIT_USAGE = 2


@pytest.fixture(autouse=True)
def _no_readback_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cw.tracker_post._READBACK_DELAY_SECONDS", 0.0)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> GhThread:
    return GhThread().install(monkeypatch)


def _configure(monkeypatch: pytest.MonkeyPatch, *clients: ClientConfig) -> None:
    by_name = {c.name: c for c in clients}
    monkeypatch.setattr("cw.config.load_clients", lambda: dict(by_name))


def _named(cfg: ClientConfig, name: str) -> ClientConfig:
    return cfg.model_copy(update={"name": name})


def _disabled(cfg: ClientConfig) -> ClientConfig:
    return cfg.model_copy(update={"outbox": OutboxConfig(enabled=False)})


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(main, ["outbox", "drain", *args])


def _line(
    client: str = CLIENT,
    *,
    drained: int = 0,
    delivered: int = 0,
    unverified: int = 0,
    skipped: int = 0,
    released: int = 0,
    held: int = 0,
    lease_held: str = "no",
    unreachable: str = "-",
) -> str:
    return (
        f"{client}: drained={drained} delivered={delivered} unverified={unverified}"
        f" skipped={skipped} released={released} held={held}"
        f" lease_held={lease_held} unreachable={unreachable}"
    )


class TestExitAndText:
    def test_empty_store_prints_nothing_to_drain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))

        result = _invoke()

        assert result.exit_code == 0
        assert result.stdout == "No outbox clients to drain.\n"

    def test_full_drain_text_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        entry = enqueue().entry

        result = _invoke("-c", CLIENT)

        assert result.exit_code == 0, result.output
        assert result.stdout == _line(drained=1, delivered=1) + "\n"
        assert list_entries(CLIENT)[0].id == entry.id
        assert list_entries(CLIENT)[0].state is OutboxState.DELIVERED

    def test_client_and_long_option_are_equivalent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        enqueue()

        short = _invoke("-c", CLIENT)
        long = _invoke("--client", CLIENT)

        assert short.exit_code == long.exit_code == 0
        assert long.stdout == _line() + "\n"

    def test_unreachable_roles_in_the_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        enqueue(role=OutboxRole.KNOWLEDGE)

        result = _invoke("-c", CLIENT)

        assert result.exit_code == 0
        assert result.stdout == _line(released=1, unreachable="knowledge") + "\n"

    def test_unknown_client_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))

        result = _invoke("-c", "nobody")

        assert result.exit_code == _EXIT_ERROR
        assert "Unknown client 'nobody'" in result.stderr

    def test_internal_outbox_error_exits_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        enqueue()

        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "store exploded"
            raise OutboxError(msg)

        monkeypatch.setattr("cw.outbox_drain.run_drain", _boom)

        result = _invoke("-c", CLIENT)

        assert result.exit_code == _EXIT_ERROR
        assert "store exploded" in result.stderr

    @pytest.mark.parametrize("limit", ["0", "101"])
    def test_limit_out_of_range_is_a_usage_error(self, limit: str) -> None:
        result = _invoke("--limit", limit)

        assert result.exit_code == _EXIT_USAGE

    def test_lease_held_exits_tempfail_and_touches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        entry = enqueue().entry
        before = _layout.record_path(CLIENT, entry.id).read_bytes()

        with _hold_flock(_layout.drain_lock_path(CLIENT)):
            result = _invoke("-c", CLIENT, "--json")

        assert result.exit_code == os.EX_TEMPFAIL
        payload = json.loads(result.stdout)
        assert payload["clients"][0]["lease_held"] is True
        assert gh.records == []
        assert _layout.record_path(CLIENT, entry.id).read_bytes() == before

    def test_limit_overrides_the_configured_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        for marker in ("a", "b", "c"):
            enqueue(marker=marker)

        result = _invoke("-c", CLIENT, "--limit", "2")

        assert result.exit_code == 0
        assert result.stdout == _line(drained=2, delivered=2) + "\n"


class TestAllClients:
    def test_drains_every_configured_client_with_a_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(
            monkeypatch,
            client_config(tmp_path),
            _named(client_config(tmp_path), _OTHER),
            _named(client_config(tmp_path), "client-no-dir"),
        )
        enqueue()
        enqueue(_OTHER)
        enqueue("orphan-dir")

        result = _invoke()

        assert result.exit_code == 0
        assert result.stdout.splitlines() == [
            _line(drained=1, delivered=1),
            _line(_OTHER, drained=1, delivered=1),
        ]
        assert (
            "unconfigured (no usable gh tickets binding): orphan-dir" in result.stderr
        )

    def test_precedence_error_over_tempfail_over_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        third = "client-c"
        _configure(
            monkeypatch,
            client_config(tmp_path),
            _named(client_config(tmp_path), _OTHER),
            _named(client_config(tmp_path), third),
        )
        for name in (CLIENT, _OTHER, third):
            enqueue(name)
        from cw import outbox_drain

        real = outbox_drain.run_drain

        def _flaky(client: str, *args: Any, **kwargs: Any) -> Any:
            if client == third:
                msg = "store exploded"
                raise OutboxError(msg)
            return real(client, *args, **kwargs)

        monkeypatch.setattr("cw.outbox_drain.run_drain", _flaky)

        with _hold_flock(_layout.drain_lock_path(_OTHER)):
            both = _invoke()
        assert both.exit_code == _EXIT_ERROR
        assert "client-c" in both.stderr

        monkeypatch.setattr("cw.outbox_drain.run_drain", real)
        enqueue(marker="again")
        with _hold_flock(_layout.drain_lock_path(_OTHER)):
            tempfail = _invoke()
        assert tempfail.exit_code == os.EX_TEMPFAIL

    def test_disabled_client_skipped_with_a_note(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(
            monkeypatch,
            _disabled(client_config(tmp_path)),
            _named(client_config(tmp_path), _OTHER),
        )
        entry = enqueue().entry
        enqueue(_OTHER)

        result = _invoke()

        assert result.exit_code == 0
        assert result.stdout.splitlines() == [_line(_OTHER, drained=1, delivered=1)]
        assert f"skipped (outbox.enabled is false): {CLIENT}" in result.stderr
        assert list_entries(CLIENT)[0].id == entry.id
        assert list_entries(CLIENT)[0].state is OutboxState.PENDING

    def test_force_drains_a_disabled_client_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, _disabled(client_config(tmp_path)))
        enqueue()

        result = _invoke("--force")

        assert result.exit_code == 0
        assert result.stdout == _line(drained=1, delivered=1) + "\n"


class TestJson:
    def test_envelope_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(
            monkeypatch,
            client_config(tmp_path),
            _named(
                client_config(tmp_path, bindings=Bindings(), tracker="linear"), _OTHER
            ),
        )
        enqueue()
        enqueue(_OTHER)
        enqueue("disk-only")

        result = _invoke("--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert set(payload) == {"schema_version", "clients", "unconfigured"}
        assert payload["schema_version"] == 1
        assert [c["client"] for c in payload["clients"]] == [CLIENT, _OTHER]
        assert all(set(c) == _REPORT_KEYS for c in payload["clients"])
        assert payload["unconfigured"] == [_OTHER, "disk-only"]
        other = payload["clients"][1]
        assert other["released"] == 1
        assert other["unreachable_roles"] == ["tickets"]

    def test_empty_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure(monkeypatch)

        result = _invoke("--json")

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "schema_version": 1,
            "clients": [],
            "unconfigured": [],
        }


class TestForceAndProbe:
    def test_force_clears_markers_and_held_before_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        entry = enqueue().entry
        append_held(CLIENT, entry.id)
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.CHAT, reason=OutboxReason.BINDING_UNREACHABLE
            ),
        )

        result = _invoke("-c", CLIENT, "--force")

        assert result.exit_code == 0
        assert read_held(CLIENT) == frozenset()
        assert marked_roles(CLIENT) == frozenset()
        assert list_entries(CLIENT)[0].state is OutboxState.DELIVERED

    def test_non_force_run_with_a_marker_still_attempts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, client_config(tmp_path))
        enqueue()
        write_marker(
            CLIENT,
            UnreachableMarker(
                role=OutboxRole.TICKETS, reason=OutboxReason.TRANSIENT_FAILURE
            ),
        )

        result = _invoke("-c", CLIENT)

        assert result.exit_code == 0
        assert len(gh.comment_records()) == 1
        assert marked_roles(CLIENT) == frozenset()


class TestDisabledClient:
    def test_plain_drain_is_refused_and_touches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, _disabled(client_config(tmp_path)))
        entry = enqueue().entry
        directory = state_dir() / "outbox" / CLIENT
        before = sorted(p.name for p in directory.iterdir())

        result = _invoke("-c", CLIENT)

        assert result.exit_code == _EXIT_ERROR
        assert result.stderr == (
            f"Error: Client '{CLIENT}' has outbox.enabled set to false, so its"
            f" outbox is not drained. Run 'cw outbox drain -c {CLIENT} --force'"
            " to drain it anyway.\n"
        )
        assert sorted(p.name for p in directory.iterdir()) == before
        assert "drain.lock" not in before
        assert list_entries(CLIENT)[0].id == entry.id
        assert list_entries(CLIENT)[0].state is OutboxState.PENDING
        assert gh.records == []

    def test_force_drains_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gh: GhThread
    ) -> None:
        _configure(monkeypatch, _disabled(client_config(tmp_path)))
        enqueue()

        result = _invoke("-c", CLIENT, "--force")

        assert result.exit_code == 0
        assert list_entries(CLIENT)[0].state is OutboxState.DELIVERED


class TestHelp:
    def _drain_command(self) -> click.Command:
        group = main.get_command(click.Context(main), "outbox")
        assert isinstance(group, click.Group)
        return group.commands["drain"]

    def test_help_strings_are_verbatim(self) -> None:
        group = main.get_command(click.Context(main), "outbox")
        command = self._drain_command()
        helps = {p.name: p.help for p in command.params if isinstance(p, click.Option)}

        assert group is not None
        assert group.help == ("Inspect and drain the per-client outbox of prose posts.")
        assert command.help == (
            "Drain pending outbox entries to the ticket tracker through gh."
        )
        assert helps == {
            "client": (
                "Drain one client (default: every configured client that has an"
                " outbox directory)."
            ),
            "limit": (
                "Maximum entries to claim per client this run (default: the"
                " client's outbox.max_entries_per_run; 1 to 100)."
            ),
            "force": (
                "Operator override: also drain a client with outbox.enabled:"
                " false, and first clear the held-entry list and every"
                " unreachable marker so those entries are retried."
            ),
            "as_json": "Output the drain envelope as JSON.",
        }
        assert group.help == drain_cli.GROUP_HELP

    def test_help_output_mentions_drain(self) -> None:
        group_help = CliRunner().invoke(main, ["outbox", "--help"])
        drain_help = _invoke("--help")

        assert "drain" in group_help.output
        assert "Drain pending outbox entries" in drain_help.output


class TestOutboxConfig:
    def test_defaults(self) -> None:
        cfg = OutboxConfig()

        assert cfg.max_entries_per_run == _DEFAULT_CAP
        assert cfg.enabled is True
        assert ClientConfig(name="x", workspace_path="/tmp/x").outbox == cfg

    @pytest.mark.parametrize("value", [0, 101])
    def test_cap_bounds(self, value: int) -> None:
        with pytest.raises(ValidationError):
            OutboxConfig(max_entries_per_run=value)

    @pytest.mark.parametrize("value", [True, False])
    def test_enabled_accepts_a_real_bool(self, value: bool) -> None:
        assert OutboxConfig.model_validate({"enabled": value}).enabled is value

    @pytest.mark.parametrize(
        "value", ["sometimes", "yes", "no", "true", "false", "1", 0, 1, None, []]
    )
    def test_enabled_rejects_a_non_bool_instead_of_coercing(
        self, value: object
    ) -> None:
        with pytest.raises(ValidationError):
            OutboxConfig.model_validate({"enabled": value})

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            OutboxConfig.model_validate({"bogus": 1})

    def test_disabled_round_trips(self) -> None:
        cfg = ClientConfig(
            name="x", workspace_path="/tmp/x", outbox=OutboxConfig(enabled=False)
        )

        again = ClientConfig.model_validate(cfg.model_dump())

        assert again.outbox.enabled is False

    def test_schema_includes_both_fields(self) -> None:
        schema = ClientConfig.model_json_schema()
        outbox = schema["$defs"]["OutboxConfig"]["properties"]

        assert set(outbox) == {"enabled", "max_entries_per_run"}
        assert "outbox" in schema["properties"]

    def test_cli_ceiling_equals_the_model_ceiling(self) -> None:
        command = TestHelp()._drain_command()
        (limit,) = [p for p in command.params if p.name == "limit"]

        assert isinstance(limit.type, click.IntRange)
        assert limit.type.max == OUTBOX_MAX_ENTRIES_PER_RUN_CEILING == _CEILING
        assert limit.type.min == 1
