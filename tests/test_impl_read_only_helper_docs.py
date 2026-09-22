"""Doc-structure guards for the #2211 read-only-helper rule and spawn typing.

Pairs the code-level guard (``tests/test_cli_subagent_policy.py``) with the
prose half, mirroring ``tests/test_impl_guard_staleness_docs.py``'s pattern.
Two distinct things are pinned here:

1. the ``Read Only Helper`` agent's tool allowlist, which is the *capability*
   guarantee the ticket asked for — a helper that cannot write because it has
   no writing tool, not because it was told not to;
2. every ``.claude/commands/*.md`` spawn site this ticket typed, so a future
   edit cannot quietly drop back to an unnamed (unrostered) spawn.

``orchestrate-phase.md`` is deliberately unasserted: it is outside the cw
dispatch pipeline, and its three mentions are descriptive bullets about what
other commands do rather than spawn instructions of their own.

``review-sweep.md`` **is** asserted, per site. Round 1 left it out because its
six role names are not registered agent types (then-residual gap R7); the
resolution is that they never were types — they are prompt-defined roles that
already ran as implicitly-general-purpose agents, so naming ``general-purpose``
is behavior-preserving. That closed the inventory, which is what let
``classify_spawn`` flip from record-only to deny on an omitted type. Registering
any of the six as real agents is #2253's question, not this file's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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

    def test_appendix_states_the_omitted_type_refusal(self) -> None:
        """Round 1 shipped this case record-only; the appendix must not still
        say so, or it understates what the guard now refuses."""
        content = _appendix("impl")

        assert "`subagent_type` **not named at all** → **refused**" in content

    def test_appendix_explains_what_unblocked_the_refusal(self) -> None:
        """The inventory is the whole argument for denying — keep it on record."""
        content = _appendix("impl")

        assert "Why the omitted case was gated behind an inventory" in content
        assert "#2253" in content

    def test_appendix_documents_the_config_kill_switch(self) -> None:
        assert "subagent_spawn_guard_enabled" in _appendix("impl")

    def test_appendix_states_the_residual_gaps(self) -> None:
        content = _appendix("impl")

        for gap in ("R1", "R4", "R7"):
            assert gap in content

    def test_appendix_points_at_the_deferred_half(self) -> None:
        assert "#2248" in _appendix("impl")


class TestAgentSpawnRule:
    """The cross-stage rule in auto-dev.md.

    Asserted against the rule's own paragraph, not the whole file: ``fork``
    and ``subagent_type`` both occur incidentally elsewhere in a document
    this size, so a file-wide substring check would pass even if the rule
    were deleted outright.
    """

    def _rule(self) -> str:
        content = _cmd("auto-dev.md")
        start = content.index("**Agent spawn typing rule (#2211):**")
        return content[start : content.index("\n\n", start)]

    def test_spawn_rule_requires_a_named_subagent_type(self) -> None:
        rule = self._rule()

        assert "MUST name an explicit `subagent_type`" in rule
        assert "general-purpose" in rule

    def test_spawn_rule_names_the_read_only_helper_alternative(self) -> None:
        assert "Read Only Helper" in self._rule()

    def test_spawn_rule_states_the_explicit_fork_refusal(self) -> None:
        rule = self._rule()

        assert "Never fork" in rule
        assert "refuses an explicit fork" in rule

    def test_spawn_rule_states_deny_on_omission(self) -> None:
        """Round 1 could only promise a WARN here; the rule now promises a
        refusal, and must not be left describing the weaker behavior."""
        rule = self._rule()

        assert "refuses an omitted type" in rule
        assert "warns on an omitted type" not in rule


class TestBareSpawnSitesAreTyped:
    """Each previously-untyped spawn site now names general-purpose."""

    def test_auto_dev_ci_failure_spawn_is_typed(self) -> None:
        content = _cmd("auto-dev.md")

        assert f"Spawn agent (`{_GENERAL_PURPOSE}`) in that PR's branch" in content

    #: Each finalize spawn site, anchored on enough surrounding prose to
    #: identify *which* site it is. An occurrence count cannot do that: it
    #: passes whichever four of the five carry the type, so the one site that
    #: lost it is exactly the one the test does not notice.
    @pytest.mark.parametrize(
        "site",
        [
            # Step 5a, Fix branch — the CI-failing prior PR.
            f"to fix and push — pass `{_GENERAL_PURPOSE}` (#2211)",
            # Step 4c.2 Capture-now, inside a dispatch worktree (#766/#1047).
            f'(`{_GENERAL_PURPOSE}`, `model: "haiku"`, no `isolation` key)',
            # Step 4c.2 Capture-now, outside one.
            f'(`{_GENERAL_PURPOSE}`, `isolation: "worktree"`, `model: "haiku"`)',
            # Step 5a, CI-failure investigation.
            f"apply fix, push to branch. Pass `{_GENERAL_PURPOSE}` (#2211).",
            # Step 5b, addressing review feedback.
            f"summarizing the fix. Pass `{_GENERAL_PURPOSE}` (#2211).",
        ],
    )
    def test_finalize_spawns_are_all_typed(self, site: str) -> None:
        assert site in _cmd("auto-dev-finalize.md")

    def test_prep_pr_parallel_spawn_is_typed(self) -> None:
        assert f"Spawn parallel subagents via the Task tool (`{_GENERAL_PURPOSE}`)" in (
            _cmd("prep-pr.md")
        )

    @pytest.mark.parametrize(
        "site",
        [
            # Newly typed by #2211.
            f"Spawn ONE confirmation Task agent (`{_GENERAL_PURPOSE}`, sonnet model)",
            f"Spawn a bug-hunter Task agent (`{_GENERAL_PURPOSE}`, sonnet model)",
            # Already compliant before #2211 — pinned so they stay that way.
            f"Spawn ONE classifier Task agent per PR (`{_GENERAL_PURPOSE}`,",
            f"Use the Agent tool with `{_GENERAL_PURPOSE}`,",
            f"(Bash + Agent tool, `{_GENERAL_PURPOSE}`,",
        ],
    )
    def test_review_monitor_spawns_are_all_typed(self, site: str) -> None:
        assert site in _cmd("review-monitor.md")


#: The six ``review-sweep.md`` reviewer roles, each of which resolved to
#: ``general-purpose`` rather than to a new agent definition (#2253).
_REVIEW_SWEEP_ROLES = (
    "Bug Hunter",
    "CLAUDE.md Auditor",
    "Context Checker",
    "silent-failure-hunter",
    "pr-test-analyzer",
    "type-design-analyzer",
)


class TestReviewSweepSitesAreTyped:
    """The inventory gap that gated deny-on-omission, closed and pinned.

    Asserted per row rather than by occurrence count: a count passes no matter
    which rows carry the type, so it would not notice one role losing it.
    """

    def test_every_role_row_names_general_purpose(self) -> None:
        content = _cmd("review-sweep.md")

        for role in _REVIEW_SWEEP_ROLES:
            assert f"| **{role}** | `{_GENERAL_PURPOSE}` |" in content

    def test_confidence_scorer_spawn_is_typed(self) -> None:
        """The seventh site, in prose rather than a table row."""
        content = _cmd("review-sweep.md")

        assert f'(`{_GENERAL_PURPOSE}`, `model: "haiku"`)' in content

    def test_roles_are_documented_as_roles_not_registered_types(self) -> None:
        """Without this note the six names read as types someone forgot to
        register, which is exactly the misreading that stalled round 1."""
        content = _cmd("review-sweep.md")

        assert "The Agent column is a role, not a registered type (#2211)" in content
        assert "#2253" in content

    def test_no_agent_file_was_minted_for_any_role(self) -> None:
        """#2253 owns that decision — creating them here would pre-empt it."""
        registered = {path.stem for path in _AGENTS_ROOT.glob("*.md")}

        for role in _REVIEW_SWEEP_ROLES:
            assert role.lower().replace(" ", "-").replace(".", "") not in registered


class TestReviewMdWasAlreadyClean:
    """``review.md`` needed no edit — its table already names real types.

    Pinned rather than merely asserted in prose: this file is the record that
    the inventory behind deny-on-omission was actually checked here, so a
    later edit that drops the Agent Type column has to argue with a test.
    """

    def test_every_reviewer_row_names_a_registered_agent_type(self) -> None:
        content = _cmd("review.md")
        registered = {
            _agent_frontmatter(path.name)["name"] for path in _AGENTS_ROOT.glob("*.md")
        }

        for reviewer in (
            "Code Quality Reviewer",
            "Architecture Reviewer",
            "Test Reviewer",
            "Performance Reviewer",
            "API Contract Validator",
            "Deployment Reviewer",
            "SysAdmin Reviewer",
            "Data Safety Reviewer",
            "Product Manager Reviewer",
        ):
            assert f"`{reviewer}`" in content
            assert reviewer in registered
