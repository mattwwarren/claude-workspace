"""Doc-structure guards for #2156: review stations must dispatch exclusively
through the Agent tool's subagent spawn shape, never through a teammate/
agent-team message — a teammate reply carries no Stop-hook `background_tasks`
tracking, so a coordinator that ends its turn awaiting a second teammate
message can never be woken (the same no-notification-path shape #2157's
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


def _step3a_spawned_roles_paragraph() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("**Track `SPAWNED_ROLES`:**")
    end = content.index("Dispatch shape depends on mode")
    return content[start:end]


def _output_contract_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("**Output contract.**")
    end = content.index("### Checkpoint 3a")
    return content[start:end]


def _checkpoint_3a_step1() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("### Checkpoint 3a: Adjudicate every finding")
    end = content.index("2. Assemble the consolidate-input envelope")
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


def test_findings_round_initialization_is_recoverable() -> None:
    """A new round must archive prior findings, while a resume retains them."""
    content = _cmd("auto-dev-review.md")
    startup = content[content.index("### Step 3a") : content.index("**Small scope:")]
    assert "review-findings-round.json" in startup
    assert 'status: "in_progress"' in startup
    assert "resume" in startup.lower()
    assert "retain the active directory" in startup
    assert "review-findings-archive" in startup
    assert "Never use `rm`" in startup
    assert "restore" in startup.lower()


def test_new_round_promotes_verified_staging_after_archiving_old_round() -> None:
    """Round archival and marker promotion must be explicitly ordered."""
    content = _cmd("auto-dev-review.md")
    startup = content[content.index("### Step 3a") : content.index("**Small scope:")]
    archive_dir = ".cw/review-findings-archive/<ticket>-<round-id>/review-findings/"
    archive_marker = (
        ".cw/review-findings-archive/<ticket>-<round-id>/review-findings-round.json"
    )
    staging_dir = ".cw/review-findings-staging/<ticket>-<round-id>/review-findings/"
    staging_marker = (
        ".cw/review-findings-staging/<ticket>-<round-id>/review-findings-round.json"
    )
    assert archive_dir in startup
    assert archive_marker in startup
    assert staging_dir in startup
    assert staging_marker in startup
    assert startup.index("First archive the old active directory") < startup.index(
        "only after both renames succeed"
    )
    assert startup.index("only after both renames succeed") < startup.index(
        "atomically rename the verified staging findings directory"
    )
    assert "final commit point" in startup


def test_roster_is_persisted_before_spawn_and_after_return() -> None:
    """The complete pending roster and each return state use atomic writes."""
    content = _cmd("auto-dev-review.md")
    section = content[
        content.index("### Step 3a") : content.index("**Sandbox warning**")
    ]
    assert 'every role marked `status: "pending"`' in section
    assert "before the first Agent spawn" in section
    assert ".cw/review-findings-round.json.tmp" in section
    assert "immediately before each subsequent spawn" in section
    assert 'status: "completed"' in section
    assert "failure record" in section
    assert "atomically" in section


def test_each_reviewer_write_happens_before_next_dispatch() -> None:
    """The SPAWNED_ROLES paragraph must state the per-reviewer findings write
    happens immediately after that reviewer returns, before the next is
    dispatched — and no longer forward-reference Checkpoint 3a for the write
    itself."""
    paragraph = _step3a_spawned_roles_paragraph()
    assert "immediately after each reviewer returns" in paragraph
    assert "before dispatching the next" in paragraph
    assert "per Checkpoint 3a step 1" not in paragraph


def test_checkpoint_3a_step1_is_a_verification_not_a_fresh_write() -> None:
    """Checkpoint 3a's former step 1 must no longer perform a batched
    `rm -f`/Write cycle over the full SPAWNED_ROLES set — it verifies the
    on-disk set instead."""
    step1 = _checkpoint_3a_step1()
    assert "rm -f" not in step1
    assert "SPAWNED_ROLES" in step1
    assert "verif" in step1.lower()


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


def test_worker_execution_discipline_cross_references_review_station_rule() -> None:
    """auto-dev.md's Worker Execution Discipline section must name
    auto-dev-review.md as carrying a sibling instance of rule 3's
    principle for teammate/agent-team dispatch."""
    section = _worker_execution_discipline_section()
    assert "auto-dev-review.md" in section
    assert "sibling" in section


def test_recovery_matrix_post_commit_bullet_names_all_three_manifest_arms() -> None:
    """The recovery matrix's 'active+active present, archive+archive present'
    bullet must name all three reachable manifest phases (`committed`,
    `staging_marker_promoted`, `staging_directory_promoted`), in that order,
    with the third arm advancing the manifest and falling through to the
    second rather than requiring an unreachable `committed` state (#2156 —
    codex cycle-5 MUST_FIX: recovery could not finish a crash immediately
    after the staging-marker rename succeeded but before the manifest
    recorded it)."""
    content = _cmd("auto-dev-review.md")
    start = content.index(
        "active directory **and** active marker present, "
        "archive directory **and** archive marker present"
    )
    end = content.index("active directory present, active marker absent:", start)
    bullet = content[start:end]
    assert bullet.index("`committed`") < bullet.index("`staging_marker_promoted`")
    assert bullet.index("`staging_marker_promoted`") < bullet.index(
        "`staging_directory_promoted`"
    )
    assert "continue with arm 2" in bullet
    assert "recovery_inconsistent" in bullet
    assert "never require `committed`" in bullet
