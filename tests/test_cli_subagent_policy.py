"""Tests for the ``cw agent-spawn-pre`` spawn-shape policy (#2211).

#2017 established that cw must own the launch of any agent it is responsible
for monitoring: a harness subagent never enters the session roster, so cw can
neither see it start nor stop it. #2211 observed the uncovered path — an impl
worker forked a subagent for a read-only lookup, the fork inherited the
implementation mandate, and it committed and pushed before the parent's
stop message won the race.

:mod:`cw.cli._subagent_policy` is the enforcement surface. In a headless
dispatch worker it refuses an explicit ``fork``/blank ``subagent_type`` and,
on the same terms, a spawn that names no type at all. Denying that second case
waited on a complete spawn-site inventory (residual gap R7) — a refusal a
caller cannot correctly retry is an outage, not a guard — and shipped once
``review-sweep.md``'s six roles resolved to ``general-purpose``. Everything
else fails open.

Payload fixtures here are derived from the **real** captured ``PreToolUse``
payload in ``tests/test_cli_agent_spawn_stamp.py`` by mutating only
``tool_input.subagent_type`` — the one field under test. The base capture's
shape (including the keys the handler ignores) is preserved rather than
hand-authored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.cli._subagent_policy import classify_spawn
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests.conftest import (
    _headless_worktree,
    _write_global_toggle,
    _write_hook_context_file,
)
from tests.test_cli_agent_spawn_stamp import _PRE_PAYLOAD, _pre_tool_input

if TYPE_CHECKING:
    from pathlib import Path

_ABSENT = object()


def _spawn_payload(
    cwd: Path, subagent_type: object = _ABSENT, *, name: object = _ABSENT
) -> dict[str, object]:
    """Return the real capture rebased on *cwd*, with *subagent_type* applied.

    ``_ABSENT`` deletes the key entirely — the "spawned with no type named at
    all" shape, which is distinct from a present-but-blank value. Both are
    refused now (deny-on-omission shipped in #2211 round 2); the split
    survives only because ``_classify_subagent_type`` gives each shape its
    own reason string.

    *name* (#2688), when not ``_ABSENT``, sets the top-level
    ``tool_input["name"]`` an addressable (teammate) spawn carries. The base
    capture has no ``name`` key, so the default leaves the payload unchanged.
    The shape is the observed one: redacted operator transcripts show
    ``"subagent_type":"general-purpose","name":"impl-44"`` as siblings.
    """
    tool_input = _pre_tool_input()
    if subagent_type is _ABSENT:
        del tool_input["subagent_type"]
    else:
        tool_input["subagent_type"] = subagent_type
    if name is not _ABSENT:
        tool_input["name"] = name
    return {**_PRE_PAYLOAD, "cwd": str(cwd), "tool_input": tool_input}


class TestClassifySpawn:
    """The decision table, in a headless, guard-enabled worker."""

    @pytest.mark.parametrize("subagent_type", ["fork", "FORK", "Fork", "fOrK"])
    def test_explicit_fork_is_denied_case_insensitively(
        self, tmp_path: Path, subagent_type: str
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree, subagent_type))

        assert reason is not None
        assert "#2211" in reason

    def test_blank_subagent_type_is_denied(self, tmp_path: Path) -> None:
        """A present-but-blank value is a fork by another name, not an omission."""
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, "")) is not None

    def test_omitted_subagent_type_is_denied(self, tmp_path: Path) -> None:
        """Refused since the spawn-site inventory closed (was record-only, R7)."""
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree))

        assert reason is not None
        assert "#2211" in reason

    def test_omitted_type_refusal_names_what_to_retry_with(
        self, tmp_path: Path
    ) -> None:
        """The reason is the only channel the refused caller has.

        Denial is only defensible because a correct retry exists — the
        inventory made sure of that — so the message has to say what it is.
        """
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree))

        assert reason is not None
        assert "general-purpose" in reason
        assert "Read Only Helper" in reason
        assert "subagent_spawn_guard_enabled" in reason

    def test_null_subagent_type_is_denied(self, tmp_path: Path) -> None:
        """JSON ``null`` reads as "no type named", not as a blank string."""
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, None)) is not None

    @pytest.mark.parametrize(
        "subagent_type", ["general-purpose", "Explore", "Read Only Helper"]
    )
    def test_named_type_allows_silently(
        self, tmp_path: Path, subagent_type: str
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, subagent_type)) is None

    def test_non_agent_tool_name_yields_no_verdict(self, tmp_path: Path) -> None:
        """Belt-and-braces: the matcher already scopes this to Agent/Task."""
        worktree = _headless_worktree(tmp_path)
        payload = {**_spawn_payload(worktree, "fork"), "tool_name": "Bash"}

        assert classify_spawn(payload) is None

    def test_task_tool_name_is_covered(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)
        payload = {**_spawn_payload(worktree, "fork"), "tool_name": "Task"}

        assert classify_spawn(payload) is not None

    def test_malformed_tool_input_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unexpected payload shape fails open, loudly (#1946's idiom)."""
        worktree = _headless_worktree(tmp_path)
        payload = {**_spawn_payload(worktree, "fork"), "tool_input": "not-a-dict"}

        assert classify_spawn(payload) is None
        assert "#2211" in capsys.readouterr().err

    def test_non_string_subagent_type_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, 7)) is None
        assert "#2211" in capsys.readouterr().err

    def test_none_payload_yields_no_verdict(self) -> None:
        """Unreadable stdin upstream must not become a block."""
        assert classify_spawn(None) is None

    def test_disabled_guard_allows_an_explicit_fork(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        """The config gate is a real kill switch, not just a softener.

        Driven through the real config file rather than a patched resolver, so
        it also pins which toggle this classifier reads.
        """
        _write_global_toggle(tmp_config_dir, "subagent_spawn_guard_enabled", "false")
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, "fork")) is None

    def test_interactive_worker_allows_an_explicit_fork(self, tmp_path: Path) -> None:
        """An operator's deliberate interactive fork is never refused."""
        worktree = tmp_path / "interactive"
        worktree.mkdir()
        _write_hook_context_file(worktree, headless=False)

        assert classify_spawn(_spawn_payload(worktree, "fork")) is None

    def test_no_context_allows_an_explicit_fork(self, ancestor_free_dir: Path) -> None:
        bare = ancestor_free_dir / "bare"

        assert classify_spawn(_spawn_payload(bare, "fork")) is None


# The redacted rendering of the observed transcript shape
# ``"subagent_type":"general-purpose","name":"impl-44"`` (#2688).
_TEAMMATE_TYPE = "general-purpose"
_TEAMMATE_NAME = "impl-2688"


class TestClassifyNamedSpawn:
    """#2688: a spawn that sets ``name`` is an addressable in-process teammate.

    It goes idle instead of returning, stays registered in the Stop hook's
    ``background_tasks``, and strands the worker's session — so a headless
    worker refuses it, after the ``subagent_type`` check.
    """

    def test_named_spawn_is_denied(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(
            _spawn_payload(worktree, _TEAMMATE_TYPE, name=_TEAMMATE_NAME)
        )

        assert reason is not None
        assert "BLOCKED (#2688)" in reason
        assert "WITHOUT `name`" in reason
        assert "subagent_spawn_guard_enabled" in reason

    def test_named_reason_ends_with_the_shared_kill_switch_sentence(
        self, tmp_path: Path
    ) -> None:
        """The named and fork refusals end on the same kill-switch line."""
        worktree = _headless_worktree(tmp_path)

        named = classify_spawn(
            _spawn_payload(worktree, _TEAMMATE_TYPE, name=_TEAMMATE_NAME)
        )
        fork = classify_spawn(_spawn_payload(worktree, "fork"))

        assert named is not None
        assert fork is not None
        last_line = named.splitlines()[-1]
        assert last_line.startswith(
            "False positive? Disable via subagent_spawn_guard_enabled: false"
        )
        assert last_line.endswith("see CONFIG_REFERENCE.md.")
        assert fork.splitlines()[-1] == last_line

    def test_task_tool_name_with_name_is_denied(self, tmp_path: Path) -> None:
        worktree = _headless_worktree(tmp_path)
        payload = {
            **_spawn_payload(worktree, _TEAMMATE_TYPE, name=_TEAMMATE_NAME),
            "tool_name": "Task",
        }

        reason = classify_spawn(payload)

        assert reason is not None
        assert "#2688" in reason

    @pytest.mark.parametrize("name", ["", "   ", "\t\n", None])
    def test_blank_or_null_name_allows(self, tmp_path: Path, name: object) -> None:
        """Only a non-blank string names a teammate; anything emptier allows."""
        worktree = _headless_worktree(tmp_path)

        assert (
            classify_spawn(_spawn_payload(worktree, _TEAMMATE_TYPE, name=name)) is None
        )

    def test_non_string_name_warns_and_allows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unexpected ``name`` shape fails open, loudly."""
        worktree = _headless_worktree(tmp_path)

        assert classify_spawn(_spawn_payload(worktree, _TEAMMATE_TYPE, name=7)) is None
        assert "tool_input.name" in capsys.readouterr().err

    def test_fork_with_name_returns_the_fork_reason(self, tmp_path: Path) -> None:
        """The type is classified first: a named fork is refused as a fork."""
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree, "fork", name=_TEAMMATE_NAME))

        assert reason is not None
        assert "#2211" in reason
        assert "#2688" not in reason

    def test_omitted_type_with_name_returns_the_omitted_reason(
        self, tmp_path: Path
    ) -> None:
        """The observed ``{description, model, name, prompt}`` shape (#2211)."""
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree, name=_TEAMMATE_NAME))

        assert reason is not None
        assert "#2211" in reason
        assert "#2688" not in reason

    def test_type_shape_warning_still_falls_through_to_the_name_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-string type warns and allows; the chain then checks ``name``."""
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(_spawn_payload(worktree, 7, name=_TEAMMATE_NAME))

        assert reason is not None
        assert "#2688" in reason
        assert "tool_input.subagent_type" in capsys.readouterr().err

    def test_named_explore_spawn_is_denied(self, tmp_path: Path) -> None:
        """Any named type is refused, not only ``general-purpose``."""
        worktree = _headless_worktree(tmp_path)

        reason = classify_spawn(
            _spawn_payload(worktree, "Explore", name=_TEAMMATE_NAME)
        )

        assert reason is not None
        assert "#2688" in reason

    def test_disabled_guard_allows_a_named_spawn(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        _write_global_toggle(tmp_config_dir, "subagent_spawn_guard_enabled", "false")
        worktree = _headless_worktree(tmp_path)

        assert (
            classify_spawn(
                _spawn_payload(worktree, _TEAMMATE_TYPE, name=_TEAMMATE_NAME)
            )
            is None
        )

    def test_per_lane_override_allows_a_named_spawn(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        """The guard-off canary lane (#2710) is exactly this configuration."""
        write_clients_yaml(
            ClientSpec(
                "client-a",
                tmp_config_dir / "ws",
                lanes=[{"name": "canary", "subagent_spawn_guard_enabled": False}],
            ),
            ensure_workspaces=True,
        )
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _write_hook_context_file(worktree, lane="canary", headless=True)

        assert (
            classify_spawn(
                _spawn_payload(worktree, _TEAMMATE_TYPE, name=_TEAMMATE_NAME)
            )
            is None
        )

    def test_interactive_worker_allows_a_named_spawn(self, tmp_path: Path) -> None:
        worktree = tmp_path / "interactive"
        worktree.mkdir()
        _write_hook_context_file(worktree, headless=False)

        assert (
            classify_spawn(
                _spawn_payload(worktree, _TEAMMATE_TYPE, name=_TEAMMATE_NAME)
            )
            is None
        )

    def test_no_context_allows_a_named_spawn(self, ancestor_free_dir: Path) -> None:
        bare = ancestor_free_dir / "bare"

        assert (
            classify_spawn(_spawn_payload(bare, _TEAMMATE_TYPE, name=_TEAMMATE_NAME))
            is None
        )
