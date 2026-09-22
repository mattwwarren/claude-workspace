"""Doc-structure guards for the #2211 read-only-helper rule and spawn typing.

Pairs the code-level guard (``tests/test_cli_subagent_policy.py``) with the
prose half, mirroring ``tests/test_impl_guard_staleness_docs.py``'s pattern.
Two distinct things are pinned here:

1. the ``Read Only Helper`` agent's tool allowlist, which is the *capability*
   guarantee the ticket asked for — a helper that cannot write because it has
   no writing tool, not because it was told not to;
2. every ``.claude/commands/*.md`` spawn site this ticket typed, so a future
   edit cannot quietly drop back to an unnamed (unrostered) spawn.

``review-sweep.md`` and ``orchestrate-phase.md`` are deliberately unasserted:
the former names six roles that are not registered agent types (residual gap
R7), and the latter is outside the cw dispatch pipeline.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.conftest import _appendix, _cmd

_AGENTS_ROOT = Path(__file__).resolve().parents[1] / ".claude" / "agents"

_GENERAL_PURPOSE = 'subagent_type: "general-purpose"'


def _agent_frontmatter(name: str) -> dict[str, object]:
    """Return the parsed YAML frontmatter of ``.claude/agents/<name>``."""
    content = (_AGENTS_ROOT / name).read_text(encoding="utf-8")
    _, frontmatter, _ = content.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed


class TestReadOnlyHelperAgent:
    """The capability guarantee, asserted structurally rather than in prose."""

    def test_agent_is_registered_with_the_expected_name(self) -> None:
        assert _agent_frontmatter("read-only-helper.md")["name"] == "Read Only Helper"

    def test_agent_can_read(self) -> None:
        tools = _agent_frontmatter("read-only-helper.md")["tools"]

        assert "Read" in tools

    def test_agent_has_no_tool_that_can_write_or_push(self) -> None:
        """The #2211 regression test: adding Bash back defeats the whole point.

        The incident was a forked helper that edited, committed and pushed.
        Bash alone restores every one of those, so this asserts absence of the
        write-capable tools rather than presence of the read-only ones.
        """
        tools = _agent_frontmatter("read-only-helper.md")["tools"]

        for forbidden in ("Bash", "Edit", "Write", "NotebookEdit", "Agent", "Task"):
            assert forbidden not in tools


class TestImplDocSpawnRules:
    """The impl stage names its subagent type and knows about the helper."""

    def test_impl_spawn_names_a_subagent_type(self) -> None:
        assert _GENERAL_PURPOSE in _cmd("auto-dev-impl.md")

    def test_impl_spawn_keeps_its_pinned_model_substrings(self) -> None:
        """``test_auto_dev_model_pins.py`` asserts these — additive edits only."""
        content = _cmd("auto-dev-impl.md")

        assert '`isolation: "worktree"`, `model: $IMPL_MODEL`' in content
        assert "with `model: $IMPL_MODEL`" in content

    def test_impl_doc_routes_lookups_to_the_read_only_helper(self) -> None:
        content = _cmd("auto-dev-impl.md")

        assert 'subagent_type: "Read Only Helper"' in content
        assert "#2211" in content

    def test_impl_doc_forbids_forking(self) -> None:
        content = _cmd("auto-dev-impl.md")

        assert "never fork" in content.lower()

    def test_appendix_carries_the_rationale(self) -> None:
        content = _appendix("impl")

        assert "Read-only helper spawns: capability, not instruction (#2211)" in content

    def test_appendix_states_the_record_only_caveat(self) -> None:
        """An omitted type is warned about, not refused — say so, or it reads
        as a guarantee the guard does not make."""
        content = _appendix("impl")

        assert "record-only" in content

    def test_appendix_documents_the_config_kill_switch(self) -> None:
        assert "subagent_spawn_guard_enabled" in _appendix("impl")

    def test_appendix_states_the_residual_gaps(self) -> None:
        content = _appendix("impl")

        for gap in ("R1", "R4", "R7"):
            assert gap in content

    def test_appendix_points_at_the_deferred_half(self) -> None:
        assert "#2248" in _appendix("impl")


class TestAgentSpawnRule:
    """The cross-stage rule in auto-dev.md."""

    def test_spawn_rule_requires_a_named_subagent_type(self) -> None:
        content = _cmd("auto-dev.md")

        assert "subagent_type" in content
        assert "#2211" in content

    def test_spawn_rule_states_the_explicit_fork_refusal(self) -> None:
        content = _cmd("auto-dev.md")

        assert "fork" in content.lower()


class TestBareSpawnSitesAreTyped:
    """Each previously-untyped spawn site now names general-purpose."""

    def test_auto_dev_ci_failure_spawn_is_typed(self) -> None:
        content = _cmd("auto-dev.md")

        assert f"Spawn agent (`{_GENERAL_PURPOSE}`) in that PR's branch" in content

    def test_finalize_spawns_are_all_typed(self) -> None:
        """Four sites: fix-branch, UI capture, CI failure, review feedback."""
        content = _cmd("auto-dev-finalize.md")

        assert content.count(_GENERAL_PURPOSE) >= 4

    def test_prep_pr_parallel_spawn_is_typed(self) -> None:
        assert _GENERAL_PURPOSE in _cmd("prep-pr.md")

    def test_review_monitor_spawns_are_all_typed(self) -> None:
        """Two newly-typed sites plus the three that already complied."""
        content = _cmd("review-monitor.md")

        assert content.count(_GENERAL_PURPOSE) >= 5
