"""``cw lane set`` / ``cw config lane set`` (GitHub #2697).

The setter is the narrow, agent-reachable way to change a lane's
``max_parallel`` and ``attempt_ceiling`` without hand-editing ``clients.yaml``.
Everything else (the human opt-in gates, ``reap_policy``, the guard toggles and
every other key) is refused before any file access, the command is refused
inside a dispatch worker, and every call that changes something records one
``lane.config_changed`` audit event.

The bounded-lock (held ``clients_lock``) test for ``lane set`` lives with the
other bounded-lock tests in ``tests/test_cli_config_cmds.py``.

The ``clients.yaml`` text is written by hand rather than with
``write_clients_yaml`` (which serializes with ``yaml.safe_dump`` and drops
comments), and in ruamel.yaml's default style, the only layout the
byte-for-byte guarantee covers.
"""

from __future__ import annotations

import json
import logging
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

import cw.config
from cw.cli import main
from cw.cli._clients_doc import dump_clients_doc, load_clients_doc, to_plain
from cw.cli.lane_set import _SETTABLE_KEYS
from cw.cli.queues import _VALID_EVENT_TYPES
from cw.config import load_clients
from cw.events import read_events
from cw.models import OrchestratorEventType
from tests._cli_review_helpers import _enter_operator_machine, _write_session_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from tests.conftest import CapturedEvent

# Short literal workspace paths (never created; nothing here checks them): a
# tmp_path-length value pushes a line past ruamel's 80-column default width,
# which rewraps it and is exactly the style limit the docstring states.
_CLIENTS_YAML = """\
# top comment
clients:
  acme:
    workspace_path: /tmp/ws-acme
    lanes:
    - name: default
      max_parallel: 2
    - name: fast
      max_parallel: 1  # keep me
      priority: 5
  other:
    workspace_path: /tmp/ws-other
    lanes:
    - name: fast
      max_parallel: 1
  bare:
    workspace_path: /tmp/ws-bare
"""

_SUPPORTED = "Supported: attempt_ceiling, max_parallel"
_ADR_0020 = "docs/adr/0020-the-operator-is-the-orchestrator-session.md"
_FAST_HEADER = "Lane 'fast' updated for client 'acme':"


@pytest.fixture(autouse=True)
def _operator_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enter_operator_machine(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def clients_yaml(tmp_config_dir: Path) -> Path:
    path = cw.config.clients_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CLIENTS_YAML)
    return path


@pytest.fixture
def events(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events(
        "cw.cli.config_cmds", OrchestratorEventType.LANE_CONFIG_CHANGED
    )


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, list(args))


def _changed(changes: dict[str, list[object]], lane: str = "fast") -> CapturedEvent:
    return (
        OrchestratorEventType.LANE_CONFIG_CHANGED,
        {"client": "acme", "lane": lane, "changes": changes},
        None,
    )


def _lane(client: str, name: str) -> dict[str, object]:
    """The lane's parsed ``clients.yaml`` entry, loaded through ``cw.config``."""
    lanes = load_clients()[client].effective_lanes
    return next(ln for ln in lanes if ln.name == name).model_dump()


class TestHappyPath:
    def test_set_rewrites_only_the_changed_value(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        before = clients_yaml.read_text()

        result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")

        assert result.exit_code == 0, result.output
        assert result.output == f"{_FAST_HEADER}\n  max_parallel: 1 -> 3\n"
        assert clients_yaml.read_text() == before.replace(
            "max_parallel: 1  # keep me", "max_parallel: 3  # keep me"
        )
        assert events == [_changed({"max_parallel": [1, 3]})]
        assert _lane("acme", "fast")["max_parallel"] == 3

    @pytest.mark.parametrize(
        "args",
        [
            ["lane", "set", "fast", "max_parallel=3", "-c", "acme"],
            ["lane", "set", "-c", "acme", "fast", "max_parallel=3"],
            ["config", "lane", "set", "acme", "fast", "max_parallel=3"],
            ["config", "lane", "set", "fast", "max_parallel=3", "--client", "acme"],
        ],
        ids=" ".join,
    )
    def test_dash_c_and_config_alias_forms_match(
        self, clients_yaml: Path, events: list[CapturedEvent], args: list[str]
    ) -> None:
        before = clients_yaml.read_text()

        result = _invoke(*args)

        assert result.exit_code == 0, result.output
        assert result.output == f"{_FAST_HEADER}\n  max_parallel: 1 -> 3\n"
        assert clients_yaml.read_text() == before.replace(
            "max_parallel: 1  # keep me", "max_parallel: 3  # keep me"
        )
        assert events == [_changed({"max_parallel": [1, 3]})]

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (
                ["lane", "set", "acme", "fast", "max_parallel=3", "-c", "acme"],
                "With -c/--client, pass exactly one positional argument (NAME).",
            ),
            (
                ["lane", "set", "fast", "max_parallel=3"],
                "Pass CLIENT NAME positionally, or NAME with -c/--client.",
            ),
        ],
        ids=["both-forms", "missing-client"],
    )
    def test_client_resolution_errors_come_from_the_shared_helper(
        self,
        clients_yaml: Path,
        events: list[CapturedEvent],
        args: list[str],
        message: str,
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke(*args)

        assert result.exit_code != 0
        assert message in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    def test_other_client_lane_of_the_same_name_is_untouched(
        self, events: list[CapturedEvent]
    ) -> None:
        result = _invoke("lane", "set", "other", "fast", "max_parallel=4")

        assert result.exit_code == 0, result.output
        assert _lane("other", "fast")["max_parallel"] == 4
        assert _lane("acme", "fast")["max_parallel"] == 1
        changes = {"max_parallel": [1, 4]}
        assert events == [
            (
                OrchestratorEventType.LANE_CONFIG_CHANGED,
                {"client": "other", "lane": "fast", "changes": changes},
                None,
            )
        ]

    def test_help_shows_the_operator_facing_contract(self) -> None:
        result = _invoke("lane", "set", "--help")

        assert result.exit_code == 0, result.output
        text = " ".join(result.output.split())
        assert "Set max_parallel or attempt_ceiling on a lane." in text
        assert "[CLIENT] NAME KEY=VALUE..." in text
        assert "Refused inside a dispatch worker." in text
        assert "ruamel.yaml's default style" in text
        assert "KEY=null" in text


class TestValueParsing:
    @pytest.mark.parametrize("token", ["false", "False", "FALSE"])
    def test_attempt_ceiling_false_disables_the_ceiling(
        self, clients_yaml: Path, events: list[CapturedEvent], token: str
    ) -> None:
        result = _invoke("lane", "set", "acme", "fast", f"attempt_ceiling={token}")

        assert result.exit_code == 0, result.output
        assert result.output == f"{_FAST_HEADER}\n  attempt_ceiling: (unset) -> false\n"
        assert "attempt_ceiling: false\n" in clients_yaml.read_text()
        assert _lane("acme", "fast")["attempt_ceiling"] is False
        assert events == [_changed({"attempt_ceiling": [None, False]})]

    def test_attempt_ceiling_integer(self, events: list[CapturedEvent]) -> None:
        result = _invoke("lane", "set", "acme", "fast", "attempt_ceiling=3")

        assert result.exit_code == 0, result.output
        assert "  attempt_ceiling: (unset) -> 3\n" in result.output
        ceiling = _lane("acme", "fast")["attempt_ceiling"]
        assert ceiling == 3
        assert type(ceiling) is int
        assert events == [_changed({"attempt_ceiling": [None, 3]})]

    @pytest.mark.parametrize(
        ("token", "message"),
        [
            ("0", "lane attempt_ceiling must be a positive integer (got 0)"),
            ("-2", "lane attempt_ceiling must be a positive integer (got -2)"),
            ("true", "lane attempt_ceiling does not accept true"),
            ("abc", "attempt_ceiling"),
        ],
    )
    def test_attempt_ceiling_rejections(
        self,
        clients_yaml: Path,
        events: list[CapturedEvent],
        token: str,
        message: str,
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke("lane", "set", "acme", "fast", f"attempt_ceiling={token}")

        assert result.exit_code != 0
        assert message in result.output
        assert "Invalid settings for lane 'fast' of client 'acme'" in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    @pytest.mark.parametrize("token", ["0", "-1", "true", "abc", "1.5"])
    def test_max_parallel_must_be_a_positive_integer(
        self, clients_yaml: Path, events: list[CapturedEvent], token: str
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke("lane", "set", "acme", "fast", f"max_parallel={token}")

        assert result.exit_code != 0
        assert "max_parallel must be a positive integer" in result.output
        assert "cw lane pause" in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    @pytest.mark.parametrize("token", ["null", "NULL", "~", ""])
    def test_unset_removes_the_key_so_the_lane_inherits(
        self, clients_yaml: Path, events: list[CapturedEvent], token: str
    ) -> None:
        before = clients_yaml.read_text()

        result = _invoke("lane", "set", "acme", "default", f"max_parallel={token}")

        assert result.exit_code == 0, result.output
        assert result.output == (
            "Lane 'default' updated for client 'acme':\n  max_parallel: 2 -> (unset)\n"
        )
        assert clients_yaml.read_text() == before.replace(
            "    - name: default\n      max_parallel: 2\n", "    - name: default\n"
        )
        # The model default applies again.
        assert _lane("acme", "default")["max_parallel"] == 1
        assert events == [_changed({"max_parallel": [2, None]}, lane="default")]

    def test_unset_of_an_absent_key_is_a_no_op(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke("lane", "set", "acme", "fast", "attempt_ceiling=null")

        assert result.exit_code == 0, result.output
        assert "already has these values; nothing written." in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []


class TestAtomicity:
    def test_one_invalid_assignment_writes_nothing(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke(
            "lane", "set", "acme", "fast", "max_parallel=3", "attempt_ceiling=0"
        )

        assert result.exit_code != 0
        assert "attempt_ceiling" in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    def test_several_changes_apply_together(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        result = _invoke(
            "lane", "set", "acme", "fast", "max_parallel=3", "attempt_ceiling=4"
        )

        assert result.exit_code == 0, result.output
        assert result.output == (
            f"{_FAST_HEADER}\n  max_parallel: 1 -> 3\n  attempt_ceiling: (unset) -> 4\n"
        )
        lane = _lane("acme", "fast")
        assert (lane["max_parallel"], lane["attempt_ceiling"]) == (3, 4)
        assert events == [
            _changed({"max_parallel": [1, 3], "attempt_ceiling": [None, 4]})
        ]

    def test_unchanged_keys_are_omitted(self, events: list[CapturedEvent]) -> None:
        result = _invoke(
            "lane", "set", "acme", "fast", "max_parallel=1", "attempt_ceiling=4"
        )

        assert result.exit_code == 0, result.output
        assert result.output == f"{_FAST_HEADER}\n  attempt_ceiling: (unset) -> 4\n"
        assert events == [_changed({"attempt_ceiling": [None, 4]})]

    def test_a_key_assigned_twice_is_refused(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke(
            "lane", "set", "acme", "fast", "max_parallel=3", "max_parallel=4"
        )

        assert result.exit_code != 0
        assert "'max_parallel' is assigned more than once" in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []


class TestNoOp:
    def test_current_value_writes_nothing(
        self,
        clients_yaml: Path,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _no_dump(*_args: object) -> None:
            pytest.fail("a no-op must not rewrite clients.yaml")

        monkeypatch.setattr("cw.cli.lane_set.dump_clients_doc", _no_dump)
        before = clients_yaml.read_bytes()

        result = _invoke("lane", "set", "acme", "fast", "max_parallel=1")

        assert result.exit_code == 0, result.output
        assert result.output == (
            "Lane 'fast' of client 'acme' already has these values; nothing written.\n"
        )
        assert clients_yaml.read_bytes() == before
        assert events == []


class TestRefusals:
    @pytest.mark.parametrize(
        "assignment",
        ["signoff=operator", "signoff=null", "finalize_gate=manual", "finalize_gate="],
    )
    def test_gate_keys_are_refused_naming_the_gate(
        self, clients_yaml: Path, events: list[CapturedEvent], assignment: str
    ) -> None:
        before = clients_yaml.read_bytes()
        key = assignment.partition("=")[0]

        result = _invoke("lane", "set", "acme", "fast", assignment)

        assert result.exit_code != 0
        assert f"`{key}` is a human opt-in gate ({_ADR_0020})" in result.output
        assert "never sets, clears or releases it" in result.output
        assert _SUPPORTED in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    @pytest.mark.parametrize(
        "assignment",
        [
            "reap_policy=auto",
            "codex_fix_loop_enabled=true",
            "busy_wait_guard_enabled=false",
            "subagent_spawn_guard_enabled=false",
            "background_tool_guard_enabled=false",
            "disposition_drift_check_enabled=false",
            "priority=2",
            "description=x",
            "name=x",
            "paused=true",
            "pipeline=x",
            "gate_recipes=x",
            "bogus=1",
        ],
    )
    def test_every_other_key_is_refused(
        self, clients_yaml: Path, events: list[CapturedEvent], assignment: str
    ) -> None:
        before = clients_yaml.read_bytes()
        key = assignment.partition("=")[0]

        result = _invoke("lane", "set", "acme", "fast", assignment)

        assert result.exit_code != 0
        assert f"Unknown or unsettable lane key '{key}'. {_SUPPORTED}" in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    def test_paused_points_at_pause_and_resume(self) -> None:
        result = _invoke("lane", "set", "acme", "fast", "paused=true")

        assert result.exit_code != 0
        assert "`cw lane pause`" in result.output
        assert "`cw lane resume`" in result.output

    def test_key_refusal_precedes_any_file_access(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        clients_yaml.unlink()

        result = _invoke("lane", "set", "acme", "fast", "signoff=operator")

        assert result.exit_code != 0
        assert "is a human opt-in gate" in result.output
        assert not clients_yaml.exists()
        assert events == []

    def test_settable_keys_are_pinned(self) -> None:
        assert frozenset({"max_parallel", "attempt_ceiling"}) == _SETTABLE_KEYS
        assert "signoff" not in _SETTABLE_KEYS
        assert "finalize_gate" not in _SETTABLE_KEYS


class TestLookupFailures:
    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["nope", "fast", "max_parallel=3"], "Client 'nope' not found in "),
            (["acme", "slow", "max_parallel=3"], "Lane 'slow' not found for client"),
            (["bare", "fast", "max_parallel=3"], "Client 'bare' has no lanes declared"),
            (["acme", "fast"], "Pass at least one KEY=VALUE assignment"),
        ],
        ids=["unknown-client", "unknown-lane", "no-lanes", "no-assignments"],
    )
    def test_fails_cleanly(
        self,
        clients_yaml: Path,
        events: list[CapturedEvent],
        args: list[str],
        message: str,
    ) -> None:
        before = clients_yaml.read_bytes()

        result = _invoke("lane", "set", *args)

        assert result.exit_code != 0
        assert message in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    def test_missing_clients_file_reports_the_client(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        clients_yaml.unlink()

        result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")

        assert result.exit_code != 0
        assert "Client 'acme' not found in " in result.output
        assert not clients_yaml.exists()
        assert events == []

    def test_file_without_a_clients_key(
        self, clients_yaml: Path, events: list[CapturedEvent]
    ) -> None:
        clients_yaml.write_text("other: 1\n")

        result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")

        assert result.exit_code != 0
        assert "has no 'clients:' key." in result.output
        assert clients_yaml.read_text() == "other: 1\n"
        assert events == []


class TestWorkerRefusal:
    @pytest.mark.parametrize(
        "prefix", [["lane", "set"], ["config", "lane", "set"]], ids=" ".join
    )
    def test_headless_context_refuses(
        self,
        tmp_path: Path,
        clients_yaml: Path,
        events: list[CapturedEvent],
        prefix: list[str],
    ) -> None:
        _write_session_context(tmp_path, headless=True)
        before = clients_yaml.read_bytes()

        result = _invoke(*prefix, "acme", "fast", "max_parallel=3")

        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert "Run `cw lane set` from the orchestrator session." in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []

    def test_worker_tmpdir_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clients_yaml: Path,
        events: list[CapturedEvent],
    ) -> None:
        worker = tmp_path / "worker-wt"
        _write_session_context(worker, headless=True)
        scratch = worker / ".cw" / "tmp"
        scratch.mkdir(parents=True)
        monkeypatch.setenv("TMPDIR", str(scratch))
        before = clients_yaml.read_bytes()

        result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")

        assert result.exit_code != 0
        assert "dispatch worker" in result.output
        assert clients_yaml.read_bytes() == before
        assert events == []


class TestAuditEvent:
    def test_event_reaches_the_bus_and_round_trips_as_json(self) -> None:
        result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")
        assert result.exit_code == 0, result.output

        changed = [
            e
            for e in read_events()
            if e.type == OrchestratorEventType.LANE_CONFIG_CHANGED
        ]

        assert len(changed) == 1
        payload = changed[0].payload
        assert payload == {
            "client": "acme",
            "lane": "fast",
            "changes": {"max_parallel": [1, 3]},
        }
        assert json.loads(json.dumps(payload)) == payload

    def test_event_type_is_registered(self) -> None:
        assert OrchestratorEventType.LANE_CONFIG_CHANGED.value == "lane.config_changed"
        assert "lane.config_changed" in _VALID_EVENT_TYPES

    def test_emit_failure_says_the_change_was_applied(
        self,
        clients_yaml: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _fail(*_args: object, **_kwargs: object) -> None:
            msg = "inbox full"
            raise OSError(msg)

        monkeypatch.setattr("cw.cli.config_cmds.record_event", _fail)
        before = clients_yaml.read_text()

        with caplog.at_level(logging.WARNING, logger="cw.cli.config_cmds"):
            result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")

        assert result.exit_code != 0
        text = " ".join(result.output.split())
        assert (
            "The change to lane 'fast' of client 'acme' WAS applied"
            " (max_parallel: 1 -> 3)"
        ) in text
        assert "lane.config_changed audit event failed (OSError: inbox full)" in text
        assert clients_yaml.read_text() == before.replace(
            "max_parallel: 1  # keep me", "max_parallel: 3  # keep me"
        )
        warnings = [
            r
            for r in caplog.records
            if r.name == "cw.cli.config_cmds" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "client=acme lane=fast" in warnings[0].getMessage()


class TestClientsDoc:
    def test_to_plain_returns_builtin_types(self) -> None:
        rt = YAML(typ="rt")
        doc = rt.load(
            "a: &flag true\n"
            "b: *flag\n"
            "c: 0x1f\n"
            "d: 1.5\n"
            'e: "quoted"\n'
            "f:\n- 1\n- {g: x}\n"
            "h: null\n"
        )

        plain = to_plain(doc)

        assert plain == {
            "a": True,
            "b": True,
            "c": 31,
            "d": 1.5,
            "e": "quoted",
            "f": [1, {"g": "x"}],
            "h": None,
        }
        assert isinstance(plain, dict)
        assert type(plain) is dict
        assert type(plain["a"]) is bool
        assert type(plain["c"]) is int
        assert type(plain["d"]) is float
        assert type(plain["e"]) is str
        assert type(plain["f"]) is list
        assert type(plain["f"][1]) is dict

    def test_load_missing_file_defaults_to_an_empty_clients_map(
        self, tmp_path: Path
    ) -> None:
        _rt, doc = load_clients_doc(tmp_path / "absent.yaml")

        assert "clients" in doc
        assert doc["clients"] is None

    def test_dump_round_trips_default_style_byte_for_byte(
        self, clients_yaml: Path
    ) -> None:
        before = clients_yaml.read_text()
        rt, doc = load_clients_doc(clients_yaml)

        dump_clients_doc(rt, doc, clients_yaml)

        assert clients_yaml.read_text() == before
        buf = StringIO()
        rt.dump(doc, buf)
        assert buf.getvalue() == before

    def test_non_default_style_is_rewritten_in_default_style(
        self, clients_yaml: Path
    ) -> None:
        # The documented limit of the byte-for-byte guarantee: a 4-space,
        # indented-dash layout keeps its comment (at its old column) and values
        # but is re-emitted in ruamel's default style.
        clients_yaml.write_text(
            "clients:\n"
            "    acme:\n"
            "        workspace_path: /tmp/ws-acme\n"
            "        lanes:\n"
            "          - name: fast  # keep me\n"
            "            max_parallel: 1\n"
        )

        result = _invoke("lane", "set", "acme", "fast", "max_parallel=3")

        assert result.exit_code == 0, result.output
        assert clients_yaml.read_text() == (
            "clients:\n"
            "  acme:\n"
            "    workspace_path: /tmp/ws-acme\n"
            "    lanes:\n"
            "    - name: fast        # keep me\n"
            "      max_parallel: 3\n"
        )
