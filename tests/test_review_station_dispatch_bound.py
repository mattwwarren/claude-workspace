"""Doc-structure guards for #2156: review stations must dispatch exclusively
through the Agent tool's subagent spawn shape, never through a teammate/
agent-team message — a teammate reply has no completion-notification path
(an idle named teammate does stay in the Stop hook's `background_tasks`,
#2688, but nothing resumes the coordinator when it replies), so a coordinator
that ends its turn awaiting a second teammate message can never be woken
(the same no-notification-path shape #2157's
Worker Execution Discipline rule 3 already forbids for a raw backgrounded Bash
call).

Also pins the "losing the round" fix: each reviewer's findings are now written
to disk immediately after that reviewer returns, before the next is
dispatched, rather than batched at Checkpoint 3a — so a wedged reviewer no
longer discards every already-completed sibling's findings — plus the sibling
`truncated_response` failure reason and the Output contract's degrade-not-
truncate guidance.
"""

from tests.conftest import _appendix, _cmd


def _step3a_dispatch_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("Dispatch shape depends on mode")
    end = content.index("**Sandbox warning**")
    return content[start:end]


def _output_contract_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("**Output contract.**")
    end = content.index("### Checkpoint 3a")
    return content[start:end]


def _frontmatter() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("---")
    end = content.index("---", start + 3) + 3
    return content[start:end]


def _appendix_parent_subagent_section() -> str:
    content = _appendix("review")
    start = content.index("## Parent turns and subagent turns are not symmetric")
    end = content.index("## Blocking-findings comment rule")
    return content[start:end]


def _worker_execution_discipline_section() -> str:
    content = _cmd("auto-dev.md")
    start = content.index("## Worker Execution Discipline")
    end = content.index("## Comment provenance rule")
    return content[start:end]


def test_review_stations_forbid_teammate_dispatch() -> None:
    """Step 3a's dispatch-shape section must explicitly forbid teammate/
    agent-team dispatch for review stations, citing the no-completion-
    notification rationale."""
    section = _step3a_dispatch_section()
    assert "teammate" in section
    assert "SendMessage" in section
    assert "background_tasks" in section or "Worker Execution Discipline" in section


def test_review_stations_allowed_tools_excludes_sendmessage() -> None:
    """Regression-pin: the command's own `allowed-tools` frontmatter must not
    grant `SendMessage` — reviewer dispatch has no teammate-messaging tool
    available to begin with."""
    assert "SendMessage" not in _frontmatter()


def test_truncated_response_reason_documented() -> None:
    """The Output contract must document the sibling `truncated_response`
    failure reason next to `unparseable_response`, distinguishing a response
    cut off mid-JSON from one that never parsed at all."""
    section = _output_contract_section()
    assert '"reason": "unparseable_response"' in section
    assert '"reason": "truncated_response"' in section
    assert "cut off mid-JSON" in section
    assert "never parsed at all" in section


def test_reviewer_failure_appends_friction_highlight() -> None:
    """Recording a ReviewerRunFailure (either reason) must append a
    friction_highlights entry, documented near the construction sites."""
    section = _output_contract_section()
    assert "reviewer_failed:" in section
    assert "ReviewerRunFailure" in section


def test_output_contract_has_degrade_not_truncate_guidance() -> None:
    """The Output contract must instruct a reviewer at risk of an oversized
    payload to self-check and emit status: "degraded" rather than risk a
    cut-off block, with any numeric guidance explicitly marked provisional."""
    section = _output_contract_section()
    assert 'status: "degraded"' in section
    assert "provisional" in section


def test_appendix_extends_parent_subagent_symmetry_with_teammate_rationale() -> None:
    """The appendix's parent/subagent-symmetry section must additionally
    state the teammate-dispatch prohibition's fuller rationale."""
    section = _appendix_parent_subagent_section()
    assert "teammate" in section
    assert "background_tasks" in section


def test_appendix_no_longer_claims_teammates_are_untracked() -> None:
    """#2688: an idle named teammate DOES keep ``background_tasks`` non-empty
    (that is what strands the session), so the old "not tracked ... at all"
    claim is gone; the prohibition itself stands."""
    content = _appendix("review")
    section = _appendix_parent_subagent_section()
    assert "is not tracked in the Stop hook's" not in content
    assert "#2688" in section
    assert "SendMessage" in section


def test_step3a_no_longer_claims_teammates_are_untracked() -> None:
    """The Step 3a dispatch-shape section mirrors the appendix correction."""
    section = _step3a_dispatch_section()
    assert "has no Stop-hook `background_tasks` tracking" not in section
    assert "#2688" in section


def test_worker_execution_discipline_cross_references_review_station_rule() -> None:
    """auto-dev.md's Worker Execution Discipline section must name
    auto-dev-review.md as carrying a sibling instance of rule 3's
    principle for teammate/agent-team dispatch."""
    section = _worker_execution_discipline_section()
    assert "auto-dev-review.md" in section
    assert "sibling" in section
