"""CLI tests for ``cw outbox list`` (#3705, RFC 0019 O1).

``list`` is a pure read: it never reads a body, never recovers a claim and never
writes. Entries are seeded through the public ``enqueue`` / ``claim`` API only.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import click
import pytest
from click.testing import CliRunner
from freezegun import freeze_time

from cw.cli import main
from cw.config import state_dir
from cw.outbox import (
    OUTBOX_LIST_SCHEMA_VERSION,
    OutboxEntry,
    OutboxReason,
    OutboxRole,
    OutboxState,
    OutboxTransition,
    claim,
    enqueue,
)
from tests._reconcile_helpers import dead_launch_marker, live_launch_marker

if TYPE_CHECKING:
    from cw.models import LaunchMarker

_EMPTY_JSON = '{"schema_version":1,"entries":[],"limit":50,"truncated":false}'
_ROW_KEYS = {
    "id",
    "client",
    "role",
    "ticket",
    "marker",
    "producer",
    "state",
    "reason",
    "detail",
    "created_at",
    "updated_at",
    "claimed_at",
    "claimant_verdict",
    "part_count",
    "body_bytes",
}
_ENVELOPE_KEYS = {"schema_version", "entries", "limit", "truncated"}
_DEFAULT_LIMIT = 50


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(main, ["outbox", *args])


def _seed(
    client: str = "acme",
    *,
    ticket: str = "T-1",
    marker: str = "m1",
    at: str = "2026-11-01T10:00:00Z",
    parts: tuple[str, ...] = ("body",),
) -> OutboxEntry:
    with freeze_time(at):
        return enqueue(
            client,
            role=OutboxRole.TICKETS,
            ticket=ticket,
            marker=marker,
            producer="codex-review",
            parts=parts,
            redacted=False,
        ).entry


def _claim_as(
    monkeypatch: pytest.MonkeyPatch, marker: LaunchMarker | None, client: str
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr("cw.outbox._store.current_launch_marker", lambda: marker)
        assert len(claim(client)) == 1


def _json(*args: str) -> dict[str, Any]:
    result = _invoke("list", "--json", *args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    return payload


def _force_delivered(entry: OutboxEntry) -> None:
    """Rewrite *entry*'s record as delivered (seeding only; the store is bypassed).

    The record is built through the model, so its last transition describes the
    delivered state and the model's consistency check accepts it.
    """
    path = state_dir() / "outbox" / entry.client / f"{entry.id}.json"
    delivered = OutboxTransition(
        from_state=entry.state,
        to_state=OutboxState.DELIVERED,
        reason=OutboxReason.POSTED,
        at=entry.updated_at,
        initiator="settle",
    )
    seeded = OutboxEntry.model_validate(
        {
            **entry.model_dump(),
            "state": OutboxState.DELIVERED,
            "reason": OutboxReason.POSTED,
            "transitions": [*entry.transitions, delivered],
        }
    )
    path.write_text(seeded.model_dump_json(), encoding="utf-8")


def _hour(n: int) -> str:
    return f"2026-11-01T{n:02d}:00:00Z"


class TestListEmpty:
    def test_list_empty_text(self) -> None:
        result = _invoke("list")

        assert result.exit_code == 0, result.output
        assert result.output.strip() == "No outbox entries."

    def test_list_empty_json(self) -> None:
        result = _invoke("list", "--json")

        assert result.exit_code == 0, result.output
        assert result.output.strip() == _EMPTY_JSON

    def test_list_does_not_create_the_store(self) -> None:
        _invoke("list")
        _invoke("list", "-c", "acme")

        assert not (state_dir() / "outbox").exists()


class TestListText:
    def test_list_text_columns_per_client(self) -> None:
        a = _seed("acme", ticket="T-A", marker="ma")
        b = _seed("beta", ticket="T-B", marker="mb", at=_hour(11))

        result = _invoke("list")

        assert result.exit_code == 0, result.output
        header = result.output.splitlines()[0].split()
        assert header == [
            "id",
            "client",
            "state",
            "reason",
            "ticket",
            "producer",
            "created",
        ]
        assert a.id in result.output
        assert b.id in result.output
        assert "T-A" in result.output
        assert "beta" in result.output
        assert "codex-review" in result.output
        assert "2026-11-01T10:00:00Z" in result.output

    def test_list_dash_c_filters(self) -> None:
        _seed("acme", ticket="T-A", marker="ma")
        _seed("beta", ticket="T-B", marker="mb")

        result = _invoke("list", "-c", "acme")

        assert "T-A" in result.output
        assert "T-B" not in result.output

    def test_client_option_accepts_dash_c(self) -> None:
        _seed("acme")

        short = _invoke("list", "-c", "acme", "--json")
        long = _invoke("list", "--client", "acme", "--json")

        assert short.exit_code == long.exit_code == 0
        assert short.output == long.output

    def test_list_all_clients_when_omitted(self) -> None:
        _seed("acme", marker="ma")
        _seed("orphan-dir", marker="mb")

        payload = _json()

        assert {row["client"] for row in payload["entries"]} == {
            "acme",
            "orphan-dir",
        }

    def test_list_annotates_claimed_with_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The dead claimant goes last: any later ``claim`` would recover it.
        cases: list[tuple[str, LaunchMarker | None, str, str | None]] = [
            ("T-LIVE", live_launch_marker(), "in_flight", "claimed (in_flight)"),
            ("T-NONE", None, "unknown", "claimed (unknown)"),
            ("T-DEAD", dead_launch_marker(), "owner_gone", "claimed (owner_gone)"),
        ]
        for index, (ticket, marker, _verdict, _label) in enumerate(cases):
            _seed("acme", ticket=ticket, marker=f"m{index}", at=_hour(10 + index))
            _claim_as(monkeypatch, marker, "acme")
        _seed("acme", ticket="T-PEND", marker="mp", at=_hour(20))

        payload = _json()
        text = _invoke("list").output

        verdicts = {
            row["ticket"]: row["claimant_verdict"] for row in payload["entries"]
        }
        assert verdicts == {
            "T-LIVE": "in_flight",
            "T-DEAD": "owner_gone",
            "T-NONE": "unknown",
            "T-PEND": None,
        }
        for _ticket, _marker, _verdict, label in cases:
            assert label in text
        assert "claimed (" in text
        pending_line = next(ln for ln in text.splitlines() if "T-PEND" in ln)
        assert "claimed" not in pending_line

    def test_long_cells_are_truncated_with_a_marker(self) -> None:
        long_ticket = "T" * 40
        _seed(ticket=long_ticket)

        text = _invoke("list").output

        assert long_ticket not in text
        assert "T" * 15 + "~" in text
        assert _json()["entries"][0]["ticket"] == long_ticket

    def test_list_does_not_recover_a_dead_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        _claim_as(monkeypatch, dead_launch_marker(), "acme")

        _invoke("list")
        payload = _json()

        assert payload["entries"][0]["state"] == "claimed"
        assert payload["entries"][0]["claimant_verdict"] == "owner_gone"

    def test_list_never_reads_body_files(self) -> None:
        entry = _seed()
        (state_dir() / "outbox" / "acme" / f"{entry.id}.body").unlink()

        result = _invoke("list")

        assert result.exit_code == 0, result.output
        assert entry.id in result.output


class TestListFilters:
    def test_list_state_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(ticket="T-OLD", marker="a", at=_hour(10))
        _claim_as(monkeypatch, live_launch_marker(), "acme")
        _seed(ticket="T-NEW", marker="b", at=_hour(11))

        pending = _json("--state", "pending")
        claimed = _json("--state", "claimed")

        assert [r["ticket"] for r in pending["entries"]] == ["T-NEW"]
        assert [r["ticket"] for r in claimed["entries"]] == ["T-OLD"]

    def test_list_state_filter_applies_before_limit(self) -> None:
        # Newer delivered entries sit above older pending ones: a limit applied
        # before the state filter would return an empty page.
        _seed(ticket="P-1", marker="p1", at=_hour(10))
        _seed(ticket="P-2", marker="p2", at=_hour(11))
        for index in range(3):
            entry = _seed(ticket=f"D-{index}", marker=f"d{index}", at=_hour(12 + index))
            _force_delivered(entry)

        payload = _json("--state", "pending", "--limit", "2")

        assert [r["ticket"] for r in payload["entries"]] == ["P-2", "P-1"]
        assert payload["truncated"] is False

    def test_list_limit_is_global_across_clients_and_newest_first(self) -> None:
        stamps = {
            ("acme", "T-1"): _hour(10),
            ("beta", "T-2"): _hour(11),
            ("gamma", "T-3"): _hour(12),
            ("acme", "T-4"): _hour(13),
            ("beta", "T-5"): _hour(14),
            ("gamma", "T-6"): _hour(15),
        }
        for (client, ticket), at in stamps.items():
            _seed(client, ticket=ticket, marker=ticket.lower(), at=at)

        payload = _json("--limit", "4")

        assert [r["ticket"] for r in payload["entries"]] == [
            "T-6",
            "T-5",
            "T-4",
            "T-3",
        ]
        assert payload["limit"] == 4
        assert payload["truncated"] is True

    def test_same_instant_orders_by_id_then_client(self) -> None:
        for client in ("acme", "beta"):
            _seed(client, ticket="T-1", marker="same")
        _seed("acme", ticket="T-2", marker="other")

        rows = _json()["entries"]
        order = [(r["created_at"], r["id"], r["client"]) for r in rows]

        assert order == sorted(order, reverse=True)

    def test_list_truncated_false_when_exactly_limit_matches(self) -> None:
        for index in range(3):
            _seed(ticket=f"T-{index}", marker=f"m{index}", at=_hour(10 + index))

        payload = _json("--limit", "3")

        assert len(payload["entries"]) == 3
        assert payload["truncated"] is False

    def test_list_truncated_true_when_more_than_limit_match(self) -> None:
        for index in range(4):
            _seed(ticket=f"T-{index}", marker=f"m{index}", at=_hour(10 + index))

        payload = _json("--limit", "3")

        assert len(payload["entries"]) == 3
        assert payload["truncated"] is True

    def test_truncated_when_one_client_alone_overflows(self) -> None:
        for index in range(3):
            _seed("acme", ticket=f"A-{index}", marker=f"a{index}", at=_hour(10 + index))
        _seed("beta", ticket="B-1", marker="b1", at=_hour(20))

        payload = _json("--limit", "3")

        assert [r["ticket"] for r in payload["entries"]] == ["B-1", "A-2", "A-1"]
        assert payload["truncated"] is True

    def test_list_json_echoes_effective_limit(self) -> None:
        _seed()

        assert _json()["limit"] == _DEFAULT_LIMIT
        assert _json("--limit", "3")["limit"] == 3

    @pytest.mark.parametrize("limit", ["0", "-1", "abc"])
    def test_limit_below_one_is_a_usage_error(self, limit: str) -> None:
        result = _invoke("list", "--limit", limit)

        assert result.exit_code != 0

    def test_state_rejects_unknown_value(self) -> None:
        assert _invoke("list", "--state", "nonsense").exit_code != 0


class TestListJson:
    def test_list_json_golden_shape(self) -> None:
        entry = _seed(parts=("one", "two"))

        payload = _json()

        assert set(payload) == _ENVELOPE_KEYS
        assert payload["schema_version"] == 1
        assert isinstance(payload["schema_version"], int)
        assert OUTBOX_LIST_SCHEMA_VERSION == 1
        assert payload["limit"] == _DEFAULT_LIMIT
        assert payload["truncated"] is False
        (row,) = payload["entries"]
        assert set(row) == _ROW_KEYS
        assert "claim_token" not in row
        assert row["id"] == entry.id
        assert row["client"] == "acme"
        assert row["role"] == "tickets"
        assert row["ticket"] == "T-1"
        assert row["marker"] == "m1"
        assert row["producer"] == "codex-review"
        assert row["state"] == "pending"
        assert row["reason"] is None
        assert row["detail"] is None
        assert row["claimed_at"] is None
        assert row["claimant_verdict"] is None
        assert row["part_count"] == 2
        assert row["body_bytes"] == len("onetwo")
        assert row["created_at"].startswith("2026-11-01T10:00:00")
        assert row["updated_at"].startswith("2026-11-01T10:00:00")

    def test_json_never_exposes_the_claim_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()
        with monkeypatch.context() as patch:
            patch.setattr("cw.outbox._store.current_launch_marker", live_launch_marker)
            (claimed,) = claim("acme")
        assert claimed.claim_token is not None

        result = _invoke("list", "--json")

        assert claimed.claim_token not in result.output
        assert "claim_token" not in result.output
        assert OutboxState.CLAIMED.value in result.output


class TestErrorsAndHelp:
    def test_bad_client_name_exits_nonzero(self) -> None:
        result = _invoke("list", "-c", "../escape")

        assert result.exit_code != 0

    def test_help_lists_drain(self) -> None:
        """#3706 (RFC 0019 O2) added ``drain``; ``list`` still exists."""
        ctx = click.Context(main)
        group = main.get_command(ctx, "outbox")
        assert isinstance(group, click.Group)
        drain_cmd = group.commands["drain"]
        assert "list" in group.commands
        option_helps = [p.help for p in drain_cmd.params if isinstance(p, click.Option)]

        assert group.help is not None
        assert "drain" in group.help.lower()
        assert drain_cmd.help
        assert option_helps
        assert all(option_helps)
        assert "drain" in _invoke("--help").output.lower()
        assert "drain" in _invoke("drain", "--help").output.lower()

    def test_group_lists_list_and_drain(self) -> None:
        group = main.get_command(click.Context(main), "outbox")
        assert isinstance(group, click.Group)

        assert sorted(group.commands) == ["drain", "list"]
