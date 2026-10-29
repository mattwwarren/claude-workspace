"""Guard tests: the clean-mode review convergence prose agrees across the
command files, contract docs, skills and runbook (#2787).

Pure-markdown assertions in the style of
``test_reviewed_sha_capture_contract_docs.py``. The state machine itself is
tested in Python (``tests/test_review_convergence_*.py``,
``tests/test_cli_review_converge.py``); this file pins only what the prose
promises the CLI, so a later edit cannot drop a fail-closed branch, reorder a
step the engine depends on, or let a reason list go stale.

The load-bearing contracts:

- ``check-voided`` runs before ``converge suppress`` and ``record-round``;
- exit 0 from ``cw review confirm`` is a verdict, never a clean signal, and
  ``result_blocker_reason`` is read before the first confirm ``record-round``;
- only confirm exit 3 maps to the uncharged ``confirm_backend_unavailable``;
- the confirm call is a foreground Bash call at the 600000 ms cap, with an
  overrun mapped to ``agent_block``;
- finalize Step 4.0b stays outside the slice ``tests/test_stage.py`` reads;
- every reason list that enumerates the operator-unavailable reasons names
  ``confirm_backend_unavailable``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).resolve().parent.parent

REVIEW = "auto-dev-review.md"
APPENDIX = "auto-dev-review-appendix.md"
PREP_PR = "prep-pr.md"
FINALIZE = "auto-dev-finalize.md"
AUTO_DEV = "auto-dev.md"

ERROR_RULE = "`converge config` error rule"
BLOCKED_TOKENS = (
    "stage3_review",
    '"agent_block"',
    "`dispatch_refusal`",
)
NEW_REASONS = ("review_not_converging", "confirm_backend_unavailable")
NEW_REVIEW_FIELDS = (
    "convergence_mode",
    "rounds_run",
    "converged",
    "final_round_clean_sha",
    "confirm_backend",
)
STOPS = ("no_progress", "oscillation", "max_cycles", "operator_action")
FORBIDDEN_TOKENS = (
    "confirm_round_skipped",
    "convergence_config_fallback",
    "fallback:no_ticket_id",
)
DOC_FILES = (
    ".claude/commands/auto-dev-review.md",
    ".claude/commands/auto-dev-review-appendix.md",
    ".claude/commands/prep-pr.md",
    ".claude/commands/auto-dev-finalize.md",
    ".claude/commands/auto-dev.md",
    ".claude/skills/cw-orchestration/SKILL.md",
    ".claude/skills/cw-queue-peek/SKILL.md",
    ".claude/skills/orchestrate-sprint/SKILL.md",
    "docs/headless-contract.md",
    "docs/dispatch-runbook.md",
    "docs/events.md",
    "config/CONFIG_REFERENCE.md",
    "src/cw/data/GUIDE.md",
)
#: What the codex-lane Claude confirm prompt carries besides the diff (#2791),
#: as ``_prepare_review_pass`` builds it. The CHANGELOG entry, the
#: CONFIG_REFERENCE ``review`` row and the runbook's "Which backend runs"
#: bullet all state it word for word.
CONFIRM_PROMPT_PAYLOAD = (
    "the approved plan (`.cw/plan.md`), the ticket title and body, the ticket's "
    "comments (every comment the pipeline did not post itself, fetched live from "
    "the tracker), the finding dispositions an operator already settled, and the "
    "repo's review configuration (reviewer agent specs, `.claude/review-extras.md`, "
    "the `CLAUDE.md` quality gates and ruff settings)"
)
BACKEND_UNAVAILABLE_HEADLINE = (
    "`confirm_backend_unavailable` is uncharged, so verify the backend is restored "
    "before requeueing or draining."
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _confirm_section() -> str:
    content = _cmd(REVIEW)
    start = content.index("### Clean-mode confirm round and exits")
    end = content.index("### Step 3c: Verify the `fixed` claims")
    return content[start:end]


def _bullet(section: str, lead: str) -> str:
    """The ``- **<lead>**`` bullet of *section*, continuation lines included."""
    start = section.index(f"- **{lead}")
    nxt = section.find("\n- ", start + 1)
    return section[start : nxt if nxt != -1 else len(section)]


def _step_40b() -> str:
    content = _cmd(FINALIZE)
    start = content.index("### Step 4.0b:")
    end = content.index("### Step 4a: Merge Gate Check")
    return content[start:end]


def _step_40() -> str:
    content = _cmd(FINALIZE)
    start = content.index("### Step 4.0: Large-Tier Review Approval Check")
    end = content.index("### Step 4.0b:")
    return content[start:end]


def _fences(section: str, lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", section, flags=re.DOTALL)


# --- core doc: auto-dev-review.md ---------------------------------------------


def test_core_names_converge_and_sparse_skip_never_fires_in_clean_mode() -> None:
    content = _cmd(REVIEW)
    assert "cw review converge" in content
    assert "the sparse-feedback skip never fires" in content
    assert "rereview_skipped_sparse" in content
    window = _after(content, "the sparse-feedback skip never fires", span=900)
    assert "full re-review" in window
    assert "git diff $DIFF_BASE...origin/<branch-name>" in window


def test_core_pins_the_diff_base_before_the_confirm_call() -> None:
    """#2800: the confirm fence re-pins the diff base before it runs the round."""
    [fence] = [
        b for b in _fences(_confirm_section(), "bash") if "cw review confirm" in b
    ]
    pin = fence.index(
        "cw review converge diff-base >.cw/diff-base.json 2>.cw/diff-base-stderr.txt"
    )
    call = fence.index("timeout 600 cw review confirm")
    status = fence.index("DIFF_BASE_RC=$?")
    surfaced = fence.index('echo "DIFF_BASE_RC=$DIFF_BASE_RC"')
    assert pin < status < surfaced < call
    assert fence.index("""CLIENT="$(jq -r '.client' "$CW_CTX")\"""") < pin
    [else_branch] = re.findall(r"\nelse\n(.*?)\nfi\n", fence, flags=re.DOTALL)
    assert "tail -n 20 .cw/diff-base-stderr.txt" in else_branch
    assert fence.index("\nelse\n") > call
    assert "DIFF_BASE_RC" in _confirm_section().split(fence, 1)[1]


def test_core_loop_round_pins_the_diff_base_without_a_pipeline() -> None:
    """#2800: the pin's exit status is read directly, never through ``jq``."""
    content = _cmd(REVIEW)
    bullet = _after(content, "- `loop_round`:", span=900)
    [fence] = _fences(bullet, "bash")
    lines = [line.strip() for line in fence.splitlines()]
    pin = lines.index("cw review converge diff-base >.cw/diff-base.json")
    assert lines[pin + 1] == "DIFF_BASE_RC=$?"
    assert lines[pin + 2] == 'echo "DIFF_BASE_RC=$DIFF_BASE_RC"'
    assert lines[pin + 3] == 'DIFF_BASE="$(jq -r .diff_base .cw/diff-base.json)"'
    assert "prints `DIFF_BASE_RC=<n>`" in bullet
    assert "use `$DIFF_BASE`, not `$FORK_POINT`" in bullet


def test_core_clean_mode_consolidate_passes_the_diff_base() -> None:
    content = _cmd(REVIEW)
    clean_line = (
        "cw review consolidate --documents-from .cw/review-findings/ "
        '--base "$DIFF_BASE" --plan .cw/plan.md -'
    )
    assert clean_line in content
    assert "`git diff $DIFF_BASE...$CHECKPOINT_3A_SHA` in clean mode" in content
    assert "`git diff $FORK_POINT...$CHECKPOINT_3A_SHA` in blocking mode" in content


def test_core_step_3a_clean_fence_diffs_from_the_pinned_base() -> None:
    """#2800: the Step 3a clean-mode diff fence re-derives and uses ``DIFF_BASE``."""
    content = _cmd(REVIEW)
    window = _after(content, "Clean mode (#2800) diffs from the pinned base", span=500)
    [fence] = _fences(window, "bash")
    fetch = fence.index("git fetch origin <branch-name>")
    derive = fence.index('DIFF_BASE="$(jq -r .diff_base .cw/diff-base.json)"')
    diff = fence.index("git diff $DIFF_BASE...origin/<branch-name>")
    assert fetch < derive < diff
    assert "$FORK_POINT" not in fence


def test_core_fix_loop_repins_the_diff_base_before_the_clean_re_review() -> None:
    """#2800: a fix cycle may merge ``origin/<default>``, so the re-review re-pins."""
    content = _cmd(REVIEW)
    window = _after(content, "the sparse-feedback skip never fires", span=1300)
    assert "Re-run `cw review converge diff-base` first" in window
    assert "running the `loop_round` bullet's pin fence again" in window
    assert "its redirect into `.cw/diff-base.json`" in window
    assert (
        "`.cw/diff-base.json`, not only the ledger, is what must be refreshed" in window
    )


def test_core_orders_check_voided_before_suppress_before_record_round() -> None:
    content = _cmd(REVIEW)
    voided = content.index("cw review check-voided")
    suppress = content.index("cw review converge suppress")
    record = content.index("cw review converge record-round")
    assert voided < suppress < record
    confirm = _bullet(_confirm_section(), "0**")
    assert (
        confirm.index("check-voided")
        < confirm.index("converge suppress")
        < confirm.index("converge record-round --kind confirm")
    )


def test_core_sources_reviewed_sha_from_converge_finalize() -> None:
    content = _cmd(REVIEW)
    window = _after(content, "**Clean-mode `review` fields (#2787).**", span=700)
    assert "`review.reviewed_sha` is `final_round_clean_sha` in clean mode" in window
    assert (
        "`REVIEWED_SHA_FOR_SENTINEL` comes from its `final_round_clean_sha`" in content
    )


def test_core_head_not_origin_tip_is_not_cured_by_another_round() -> None:
    content = _cmd(REVIEW)
    window = _after(content, "If the `reason` is `head_not_origin_tip`", span=1300)
    assert "another review round cannot cure it" in window
    assert "re-run `converge finalize` once" in window
    assert '`blocker.reason: "agent_block"`' in window


def test_core_names_the_new_reasons_and_status_stopped_branch() -> None:
    content = _cmd(REVIEW)
    for reason in NEW_REASONS:
        assert reason in content
    assert "- `stopped`:" in content
    assert "run no reviewer and record no round" in content


def test_confirm_step_derives_ticket_and_client_before_the_call() -> None:
    section = _confirm_section()
    jq_client = section.index("""CLIENT="$(jq -r '.client' "$CW_CTX")\"""")
    call = section.index('cw review confirm --ticket-id "$TICKET" -c "$CLIENT"')
    assert jq_client < call


def test_confirm_exit_code_table_lists_every_exit() -> None:
    section = _confirm_section()
    for row in (
        "| 0 |",
        "| 3 |",
        "| 4 |",
        "| 2 |",
        "| 1 |",
        "| 124, or no exit status |",
    ):
        assert row in section, row


def test_confirm_exit_zero_is_not_a_clean_signal() -> None:
    bullet = _bullet(_confirm_section(), "0**")
    assert "NEVER that it is clean" in bullet
    assert bullet.index("result_blocker_reason") < bullet.index(
        "record-round --kind confirm"
    )
    for reason in (
        "codex_must_fix_findings",
        "scope_tier_unresolvable",
        "empty_diff_no_commits",
    ):
        assert reason in bullet
    mech = bullet[bullet.index("`codex_must_fix_mechanically_rejected`") :]
    assert "`review_blocked`" in mech
    assert "before `record-round`" in mech
    assert "Any other non-null `result_blocker_reason`" in bullet
    assert '"agent_block"' in bullet


def test_confirm_exit_three_alone_maps_to_backend_unavailable() -> None:
    section = _confirm_section()
    bullet = _bullet(section, "3**")
    assert "`confirm_backend_unavailable`" in bullet
    assert "Only exit 3 maps to `confirm_backend_unavailable`" in bullet
    assert "`stop_confirm_escalated`" in bullet
    assert "outside the uncharged class" in bullet
    assert "claim evidence" in bullet
    for needle in ("overrun", "124", "timeout"):
        assert needle not in bullet.lower(), needle
    assert not re.search(r"any non-zero exit[^.]*confirm_backend_unavailable", section)


def test_confirm_exit_four_is_an_agent_block_not_an_outage() -> None:
    bullet = _bullet(_confirm_section(), "4**")
    assert '"agent_block"' in bullet
    assert "discarded_findings" in bullet
    assert 'NOT "restore the backend"' in bullet
    assert "Do not run `converge confirm-failed`" in bullet


def test_confirm_exit_two_is_never_recorded_as_an_outage() -> None:
    bullet = _bullet(_confirm_section(), "2**")
    assert "never report it as `confirm_backend_unavailable`" in bullet
    assert "Record nothing on the ledger" in bullet


def test_confirm_anything_else_blocks_and_never_continues_as_clean() -> None:
    bullet = _bullet(_confirm_section(), "Anything else**")
    assert "Do not continue as clean" in bullet
    assert '"agent_block"' in bullet
    assert "confirm_invocation_error" in bullet


def test_record_round_failures_take_agent_block_and_name_friction_lines() -> None:
    content = _cmd(REVIEW)
    window = _after(content, "- **Any other non-zero `record-round` exit**", span=2200)
    assert '"agent_block"' in window
    assert "do not continue as clean" in window
    assert "nothing" in window
    for friction in (
        "convergence_unread_findings",
        "convergence_failed_role",
        "convergence_record_round_failed",
    ):
        assert friction in window


def test_confirm_bash_call_is_foreground_at_the_600000_ms_cap() -> None:
    section = _confirm_section()
    guard = section[section.index("**Bash call guard (R23).**") :]
    guard = guard[: guard.index("\n\n")]
    assert "`600000`" in guard
    assert "120000" in guard
    assert "`timeout 600`" in guard
    assert "Worker Execution Discipline rule 3" in guard
    assert "never pass `run_in_background`" in guard
    assert "never accept a harness-offered background continuation" in guard
    bash_blocks = [b for b in _fences(section, "bash") if "cw review confirm" in b]
    assert len(bash_blocks) == 1
    assert "timeout 600 cw review confirm" in bash_blocks[0]
    assert "run_in_background" not in bash_blocks[0]


def test_confirm_overrun_maps_to_agent_block_never_to_an_outage() -> None:
    bullet = _bullet(_confirm_section(), "Overrun")
    assert '"agent_block"' in bullet
    assert "600s" in bullet
    assert "Last captured output" in bullet
    assert "Do not run `converge confirm-failed`" in bullet
    assert "never use `confirm_backend_unavailable`" in bullet
    assert "diff too large for one foreground round" in bullet
    assert 'NOT "restore the backend"' in bullet


def test_core_states_the_config_error_rule_once_and_others_cite_it() -> None:
    content = _cmd(REVIEW)
    assert content.count("**The `converge config` error rule (#2787).**") == 1
    assert "`convergence_config_error: exit=<n>`" in content
    assert "treated as blocking" in content
    assert "`jq -r '.review.convergence // empty' .claude/cw-context.json" in content
    for name in (AUTO_DEV, PREP_PR, FINALIZE):
        assert ERROR_RULE in _cmd(name), name
    assert ERROR_RULE in _step_40b()


def test_core_points_to_the_appendix_section_from_a_trigger_sentence() -> None:
    content = _cmd(REVIEW)
    window = _after(content, "**Clean-mode head (#2787).**", span=2800)
    assert "auto-dev-review-appendix.md" in window
    assert "Clean-mode convergence: ledger, rounds, and stop conditions" in window
    assert "Read it now" in window


# --- appendix and other command files -----------------------------------------


def test_appendix_has_the_section_and_the_stop_release_table() -> None:
    content = _cmd(APPENDIX)
    heading = "## Clean-mode convergence: ledger, rounds, and stop conditions"
    section = content[content.index(heading) :]
    assert "**Stop-release table.**" in section
    for stop in STOPS:
        assert f"| `{stop}` |" in section, stop
    assert "cw review converge operator-settle" in section
    row = section[section.index("| `no_progress` |") :].split("\n", 1)[0]
    assert "terminal, no other release" in row
    assert "Only a commit releases `no_progress`" in section


def test_appendix_states_the_no_progress_noise_floor() -> None:
    content = _cmd(APPENDIX)
    assert "`STALL_NOISE_FLOOR`" in content
    assert "is a loop still fixing small items, not a stall" in content
    models = (ROOT / "src/cw/review_convergence/_models.py").read_text(encoding="utf-8")
    assert "STALL_NOISE_FLOOR = 1" in models


def test_prep_pr_states_clean_precedence_and_no_confirm_round() -> None:
    content = _cmd(PREP_PR)
    assert (
        "otherwise `cw review converge config`'s clean-mode `max_cycles`, otherwise 3"
        in content
    )
    assert "`/prep-pr` runs none" in content
    assert "`/prep-pr` ignores `converge config`'s `dispatch_refusal`" in content
    assert "init --no-confirm" in content
    assert ERROR_RULE in content


def test_finalize_step_40b_is_strict_and_fetches_through_verify_tip() -> None:
    section = _step_40b()
    assert "cw review converge verify-tip" in section
    assert "any non-merge commit after the last clean round" in section
    assert (
        "a merge of `origin/<default-branch>` that touches a file of the "
        "branch's own diff"
    ) in section
    assert "passes no `--default-branch`" in section
    assert "`verify-tip` runs the fetch of `origin/<branch>` itself" in section
    assert "fails closed" in section
    assert "`review_tip_check: stale`" in section
    assert "git fetch" not in section
    assert "converge config" in section


def test_finalize_step_40b_states_the_default_merge_tolerance() -> None:
    """#2815: a non-touching merge of the default branch no longer parks."""
    section = _step_40b()
    for needle in (
        "`CHANGELOG.md`",
        "`merge_gate_ignore_paths`",
        "`review.default_merge_tolerance`",
        "`default_merge_tolerated: true`",
        "`review_tip_check: default_merge_tolerated`",
    ):
        assert needle in section, needle
    cost = _after(section, "**Accepted cost.**", span=900)
    assert "Step 4c.5 rebase" in cost
    assert "`/prep-pr` commit" in cost
    assert "a merge of `origin/<default-branch>` included" not in section
    assert "are commits after the last clean round" not in section


def test_the_tip_tolerance_reaches_the_other_docs() -> None:
    appendix = _cmd(APPENDIX)
    stale = _after(appendix, "**Stale ledgers and the final tip.**", span=1200)
    assert "any non-merge commit after the last clean round" in stale
    assert "`review.default_merge_tolerance: false`" in stale
    assert "a merge of `origin/<default>` included" not in stale
    assert "review_tip_check: default_merge_tolerated" in _cmd(REVIEW)
    runbook = _read("docs/dispatch-runbook.md")
    note = _after(runbook, "#### Clean-mode tip tolerance (#2815)", span=1600)
    for needle in (
        "default_merge_tolerance",
        "default_merge_tolerated",
        "CHANGELOG.md",
        "merge_gate_ignore_paths",
    ):
        assert needle in note, needle
    reference = _read("config/CONFIG_REFERENCE.md")
    row = reference[reference.index("| `review` | ReviewConfig |") :].split("\n", 1)[0]
    assert "default_merge_tolerance" in row
    ignore_row = reference[reference.index("| `merge_gate_ignore_paths` |") :].split(
        "\n", 1
    )[0]
    assert "not counted as touched by the tip-rule tolerance" in ignore_row
    assert "`review.default_merge_tolerance: false` avoids that" in ignore_row


def test_finalize_step_40b_respects_the_stage_test_slice() -> None:
    step40 = _step_40()
    assert len(_fences(step40, "json")) >= 1
    section = _step_40b()
    assert _fences(section, "json") == []
    for block in _fences(section, "bash"):
        assert "REVIEW_APPROVAL_RECORDED" not in block
    assert _cmd(FINALIZE).index("### Step 4.0b:") > _cmd(FINALIZE).index(
        "### Step 4.0: Large"
    )


def test_finalize_blocking_mode_tolerance_sentence_is_byte_identical() -> None:
    content = _cmd(FINALIZE)
    assert (
        "no non-merge commit landed after it (other than commits already on "
        "`origin/<default-branch>`), and every merge commit after it has its "
        "non-first parents on `origin/<default-branch>`."
    ) in content
    assert "only when `cw review converge config` reports `clean`" in _step_40b()


def test_finalize_clean_mode_reaches_the_delegated_child_and_rechecks() -> None:
    content = _cmd(FINALIZE)
    child = _after(content, "**Headless delegated fence (mandatory):**", span=1800)
    assert "Step 4.0b `verify-tip`" in child
    recheck = _after(content, "**Headless re-check before auto-merge:**", span=1600)
    assert "Step 4.0b" in recheck
    assert "`review_tip_check: stale`" in recheck
    sensitive = _after(content, "Because this decision is HEAD-sensitive", span=900)
    assert "Step 4.0 and then Step 4.0b" in sensitive
    assert '"the exact fence"' in sensitive
    assert "  git merge origin/main --no-edit\n" in content


# --- contract docs ------------------------------------------------------------


def test_contract_docs_list_both_reasons_and_the_five_fields() -> None:
    contract = _read("docs/headless-contract.md")
    auto_dev = _cmd(AUTO_DEV)
    for text in (contract, auto_dev):
        for reason in NEW_REASONS:
            assert f"| `{reason}` |" in text, reason
        for field in NEW_REVIEW_FIELDS:
            assert field in text, field
    for field in NEW_REVIEW_FIELDS:
        assert f"| `review.{field}` |" in contract, field


def test_headless_contract_has_note_a17() -> None:
    contract = _read("docs/headless-contract.md")
    assert contract.count("**Note (A17, #2787):**") == 1
    note = _after(contract, "**Note (A17, #2787):**", span=2200)
    assert "`null` means not reported" in note
    assert "self-consistency check, not proof of convergence" in note
    assert "open-enum additions" in note
    assert "**No version bump**" in note


def test_config_reference_documents_review_and_the_max_cycles_release() -> None:
    content = _read("config/CONFIG_REFERENCE.md")
    row = content[content.index("| `review` | ReviewConfig |") :].split("\n", 1)[0]
    assert (
        "Raising `review.max_cycles` releases a `max_cycles` stop on a same-tip requeue"
        in row
    )
    assert "`confirm_backend`" in row
    assert "the opencode and `local` REVIEW lanes write no convergence ledger" in row
    assert "blocked before any agent runs (`clean_review_unsupported_backend`" in row
    assert "ignores it" not in row
    assert "and to the codex REVIEW lane (#2791" in row
    assert "`claude -p`" in row
    assert "`none`" in row
    assert "runs on `client.worker_model` when that is set" in row
    worker_row = content[content.index("| `worker_model` |") :].split("\n", 1)[0]
    assert "codex-lane Claude confirm" in worker_row


def test_config_reference_states_the_codex_lane_default_on_egress() -> None:
    """#2791 G6: the unset default, both opt-outs and the reachable set."""
    content = _read("config/CONFIG_REFERENCE.md")
    row = content[content.index("| `review` | ReviewConfig |") :].split("\n", 1)[0]
    egress = _after(row, "**Default-on egress on codex lanes:**", span=2000)
    assert (
        "with `confirm_backend` unset sends its diff, plan and ticket context "
        "to Claude" in egress
    )
    assert f"The prompt carries the diff, {CONFIRM_PROMPT_PAYLOAD}." in egress
    assert (
        "`confirm_backend: none` or `confirm_backend: codex` is the opt-out" in egress
    )
    assert "empty settings sources (no operator or project hooks)" in egress
    assert "untracked and gitignored files included" in egress


def test_codex_lane_confirm_prompt_payload_is_stated_identically() -> None:
    """#2791: all three egress statements name the full confirm prompt payload."""
    changelog = _read("CHANGELOG.md")
    entry = _after(changelog, "**Codex-lane clean clients now send", span=1600)
    config = _read("config/CONFIG_REFERENCE.md")
    row = config[config.index("| `review` | ReviewConfig |") :].split("\n", 1)[0]
    runbook = _read("docs/dispatch-runbook.md")
    bullet = _after(runbook, "- **Which backend runs.**", span=1800)
    for name, text in (("CHANGELOG", entry), ("CONFIG", row), ("runbook", bullet)):
        assert CONFIRM_PROMPT_PAYLOAD in " ".join(text.split()), name


def test_codex_lane_clean_mode_is_documented() -> None:
    """#2791: the runbook subsection and the headless-contract rows (G2)."""
    runbook = _read("docs/dispatch-runbook.md")
    assert runbook.count("#### Codex-lane clean mode (#2791)") == 1
    section = _after(runbook, "#### Codex-lane clean mode (#2791)", span=4600)
    assert "`review.confirm_backend: none` or `codex`" in section
    assert "`worker_model` (`--model <id>`) when that is set" in section
    assert "skips the codex loop and runs only the Claude confirm" in section
    assert "`codex_convergence_ledger_error`" in section
    assert "`--override-must-fix` is refused" in section
    assert BACKEND_UNAVAILABLE_HEADLINE not in section

    contract = _read("docs/headless-contract.md")
    backend_row = _row(contract, "| `review.confirm_backend` |")
    assert 'an unset `review.confirm_backend` resolves to `"claude"`' in backend_row
    unavailable_row = _row(contract, "| `confirm_backend_unavailable` |")
    assert "`blocker.details` then starts with `backend=claude;`" in unavailable_row

    assert contract.count("| `codex_convergence_ledger_error` |") == 1
    ledger_row = _row(contract, "| `codex_convergence_ledger_error` |")
    assert '`blocker.stage: "stage3_review"`, `retry_eligible: true`' in ledger_row
    assert "BLOCKED_ON_USER" in ledger_row
    assert "dispatch never reads `retry_eligible`" in ledger_row
    assert "nothing auto-retries" in ledger_row
    assert "a failed save is transient" in ledger_row
    assert "repeats identically" in ledger_row
    assert "The park is charged" in ledger_row

    assert contract.count("| `codex_review_worktree_mutated` |") == 1
    mutated_row = _row(contract, "| `codex_review_worktree_mutated` |")
    assert '`blocker.stage: "stage3_review"`, `retry_eligible: false`' in mutated_row
    assert "BLOCKED_ON_USER" in mutated_row
    assert "nothing auto-retries" in mutated_row
    assert "cw dev-queue requeue <T> -c <C> --stage review" in mutated_row
    for token in FORBIDDEN_TOKENS:
        assert token not in ledger_row + mutated_row, token


def _row(text: str, prefix: str) -> str:
    return text[text.index(prefix) :].split("\n", 1)[0]


_GUARD_ROW = "| `clean_review_unsupported_backend` |"


def test_clean_review_unsupported_backend_is_documented() -> None:
    """#2797: the guard's reason row, the runbook block and the CHANGELOG fix."""
    contract = _read("docs/headless-contract.md")
    assert contract.count(_GUARD_ROW) == 1
    row = _row(contract, _GUARD_ROW)
    assert '`blocker.stage: "stage3_review"`, `retry_eligible: false`' in row
    assert "BLOCKED_ON_USER" in row
    assert "A bare `cw dev-queue requeue` does not clear it" in row
    assert "`opencode`" in row
    assert "`local`" in row

    runbook = _read("docs/dispatch-runbook.md")
    assert "still ignores clean mode" not in runbook
    heading = "#### Opencode and local REVIEW lanes under clean mode (#2797)"
    assert runbook.count(heading) == 1
    section = " ".join(_after(runbook, heading, span=1000).split())
    assert "blocked before any agent runs" in section
    assert "`clean_review_unsupported_backend`" in section
    assert "A bare `cw dev-queue requeue` re-dispatches into the same block" in section

    changelog = _read("CHANGELOG.md")
    entry = _after(changelog, "**Claude-side confirm round for codex-lane", span=4000)
    entry = entry.split("\n- **", 1)[0]
    assert "still ignores clean mode" not in entry
    assert "`clean_review_unsupported_backend`" in entry


# --- interactive refusal (R24) ------------------------------------------------


@pytest.mark.parametrize("name", [AUTO_DEV, REVIEW])
def test_interactive_refusal_reads_dispatch_refusal_with_a_headless_branch(
    name: str,
) -> None:
    content = _cmd(name)
    assert "`dispatch_refusal`" in content
    assert "no fallback to `blocking`" in content or "never falls back" in content
    for token in BLOCKED_TOKENS:
        assert token in content, token
    assert "retry_eligible: false" in content
    assert "never print and stop" in content
    assert "not reachable in practice" not in content


def test_auto_dev_step_zero_sits_before_intake() -> None:
    content = _cmd(AUTO_DEV)
    flow = content[content.index("## Chained Pipeline Flow") :]
    assert flow.index("0. **Clean-mode dispatch check") < flow.index("1. **Intake**")
    assert "`done` event" in flow[: flow.index("1. **Intake**")]


def test_no_doc_names_the_withdrawn_tokens() -> None:
    for rel in DOC_FILES:
        text = _read(rel)
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{rel}: {token}"


def test_core_never_has_a_bare_print_and_stop_without_a_headless_branch() -> None:
    for name in (AUTO_DEV, REVIEW):
        content = _cmd(name)
        for match in re.finditer(r"print `dispatch_refusal` and stop", content):
            window = content[match.start() : match.start() + 1200]
            assert "Headless" in window, name


# --- skills and runbook -------------------------------------------------------


def test_orchestration_skill_and_runbook_carry_the_dispatch_guidance() -> None:
    skill = _read(".claude/skills/cw-orchestration/SKILL.md")
    runbook = _read("docs/dispatch-runbook.md")
    for text in (skill, runbook):
        assert "refuses" in text
        assert "`cw dev-queue add <T> -c <C> --lane <lane>`" in text or (
            "cw dev-queue add <T> -c <C>" in text
        )
        assert "cw dev-queue" in text


def test_skills_and_runbook_give_each_stop_its_release_command() -> None:
    for rel in (
        ".claude/skills/cw-orchestration/SKILL.md",
        "docs/dispatch-runbook.md",
    ):
        text = _read(rel)
        assert "cw review converge operator-settle" in text, rel
        assert "`review_not_converging`" in text, rel
        assert "no_progress" in text, rel
    runbook = _read("docs/dispatch-runbook.md")
    for stop in STOPS:
        assert f"| `{stop}` |" in runbook, stop
    row = runbook[runbook.index("| `no_progress` |") :].split("\n", 1)[0]
    assert "terminal, no other release" in row
    skill = _read(".claude/skills/cw-orchestration/SKILL.md")
    assert "A `no_progress` stop is terminal until a commit lands" in skill
    sprint = _read(".claude/skills/orchestrate-sprint/SKILL.md")
    for reason in (*NEW_REASONS, "review_blocked", "review_operator_actionable"):
        assert f"`{reason}`" in sprint, reason


def test_runbook_carries_the_single_uncharged_sentence() -> None:
    runbook = _read("docs/dispatch-runbook.md")
    assert runbook.count(BACKEND_UNAVAILABLE_HEADLINE) == 1


def test_queue_peek_skill_carries_the_clean_mode_exemption() -> None:
    skill = _read(".claude/skills/cw-queue-peek/SKILL.md")
    window = _after(skill, "**Clean-mode convergence exemption (#2787).**", span=1500)
    assert "`queue_peek_convergence`" in window
    assert "age rules (3 and 7)" in window
    assert "liveness gate" in window


def test_events_doc_names_operator_settle_as_a_second_settled_emitter() -> None:
    events = _read("docs/events.md")
    window = _after(events, "### `review.finding_settled`", span=900)
    assert "cw review converge operator-settle" in window
    assert "`N/A`" in window
    assert "not a new event type" in window


# --- stale reason lists (R17) -------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "anchor"),
    [
        ("src/cw/data/GUIDE.md", "`dependency_unmerged`"),
        ("docs/events.md", '`"awaiting_operator_availability"`'),
        ("docs/dispatch-runbook.md", "`dependency_unmerged`"),
        ("config/CONFIG_REFERENCE.md", "# dependency_unmerged"),
        ("src/cw/dev_queue/dispositions.py", '"dependency_unmerged"'),
    ],
)
def test_operator_unavailable_reason_lists_name_confirm_backend_unavailable(
    rel: str, anchor: str
) -> None:
    window = _after(_read(rel), anchor, span=300)
    assert "confirm_backend_unavailable" in window


# --- clean-mode invariants ----------------------------------------------------


def test_sparse_skip_token_is_never_appended_in_clean_mode() -> None:
    content = _cmd(REVIEW)
    window = _after(
        content,
        "**Clean mode (#2787): the sparse-feedback skip never fires.**",
        span=900,
    )
    assert "`rereview_skipped_sparse` is never appended in clean mode" in window
