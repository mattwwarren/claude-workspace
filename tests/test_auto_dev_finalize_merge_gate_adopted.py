"""Guard tests: adopted PRs join the Stage 4a sibling inventory (#2949).

``auto-dev-finalize.md`` Step 4a's fence selects other tickets' pipeline PRs by
branch prefix. #2949 adds PRs carried by a live dev-queue row
(``cw dev-queue adopted-prs -c <client>``, tagged ``adopted`` so the loop's
prefix filter admits them), fails closed when that inventory cannot be built,
and de-dupes against the ``--author @me`` list.

Split from ``test_auto_dev_finalize_merge_gate_content_aware.py`` to keep that
module under the ~1000-line ceiling; it reuses that module's real-git
``_GateRepo`` harness (now with ``cw`` and unfiltered ``gh pr list`` stubs) and
fence-slicing helpers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from cw.dispatch.routing.pr_refs import _extract_blocked_on_pr
from tests.test_auto_dev_finalize_merge_gate_content_aware import (
    _ABSENT_LOG,
    _BOTTOM,
    _CLEAR_MARKER,
    _DISABLED_MARKER,
    _PR_REF,
    _SHARED,
    _SIBLING_NOTE,
    _TOP,
    _conflicting_pr,
    _details,
    _gate_fence,
    _GateRepo,
    _headless_block,
    _loop_body,
    _shared_with,
    _step4a,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture
def gate(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> _GateRepo:
    return _GateRepo(tmp_path, make_git_repo)


_CLIENT = "acme"
_WITH_CLIENT: dict[str, object] = {"client": _CLIENT}
_ADOPTED_ARGV = f"dev-queue adopted-prs -c {_CLIENT}"
_BLOCKED_STEP = 'STEP4A_BLOCKED: blocker.reason: "agent_block" — Step 4a '
_CW_OUTDATED = "cw is missing or older than this command; upgrade cw"
_FOREIGN_REF = "feat/x"
_THEIRS = {_SHARED: _shared_with(_TOP, 'name = "theirs"')}
_OURS = {_SHARED: _shared_with(_TOP, 'name = "ours"')}


def _pr_lists(gate: _GateRepo) -> list[str]:
    return [call for call in gate.gh_calls() if call.startswith("pr list")]


def test_fence_reads_client_from_cw_context_before_the_adopted_inventory() -> None:
    fence = _gate_fence()
    loop_start, _ = _loop_body(fence)
    assert "jq -r '.client // empty'" in fence
    assert fence.index(".client // empty") < fence.index("cw dev-queue adopted-prs")
    assert (
        fence.index(".merge_gate_enabled")
        < fence.index(_DISABLED_MARKER)
        < fence.index("exit 3")
        < fence.index("gh pr list")
        < fence.index("cw dev-queue adopted-prs")
        < loop_start
    )


def test_sibling_inventory_comment_still_opens_its_comment_block() -> None:
    fence = _gate_fence()
    loop_start, _ = _loop_body(fence)
    lines = fence[:loop_start].splitlines()
    first = len(lines)
    while first > 0 and lines[first - 1].startswith("#"):
        first -= 1
    assert lines[first].startswith(_SIBLING_NOTE)
    assert "SEEN_PRS" in lines[first - 1]


def test_loop_admits_the_adopted_tag_and_dedupes_by_pr_number() -> None:
    fence = _gate_fence()
    loop_start, loop_end = _loop_body(fence)
    body = fence[loop_start:loop_end]
    assert 'SEEN_PRS=""' in fence[:loop_start]
    assert '[ "$PR_TAG" = adopted ] || continue' in body
    assert 'case " $SEEN_PRS " in *" $PR_NUMBER "*) continue ;; esac' in body
    assert 'SEEN_PRS="$SEEN_PRS $PR_NUMBER"' in body
    assert "break" not in body
    assert re.search(r"\bexit\b", body) is None


def test_adopted_inventory_failures_do_not_append_to_the_conflict_list() -> None:
    fence = _gate_fence()
    loop_start, _ = _loop_body(fence)
    inventory = fence[fence.index("# Adopted-PR inventory (#2949)") : loop_start]
    assert "MERGE_GATE_CONFLICTS+=(" not in inventory
    assert inventory.count("exit 4") == 3


def test_adopted_inventory_failure_bullet_documents_both_messages() -> None:
    bullets = [
        line
        for line in _headless_block().splitlines()
        if line.startswith("- **Adopted-PR inventory fails**")
    ]
    assert len(bullets) == 1
    assert 'blocker.reason: "agent_block"' in bullets[0]
    assert "exits 4" in bullets[0]
    assert _CW_OUTDATED in bullets[0]
    assert "127" in bullets[0]
    assert "no prefix-only fallback" in bullets[0]
    assert "HEADLESS BLOCK" not in bullets[0]
    block = _headless_block()
    assert block.index("**Open-PR inventory fails**") < block.index(
        "**Adopted-PR inventory fails**"
    )


def test_filter_prose_names_live_row_adopted_prs_not_prefix_only() -> None:
    step = _step4a()
    assert (
        "This filter selects other tickets' pipeline PRs and stays prefix-based"
        not in (step)
    )
    prose = step[: step.index("**If open PR found from this pipeline:**")]
    assert "live dev-queue row" in prose
    assert "cw dev-queue adopted-prs" in prose
    assert "adopted_pr_url" in prose


def test_e2e_adopted_foreign_non_prefix_pr_with_a_conflict_blocks(
    gate: _GateRepo,
) -> None:
    gate.add_adopted(7, _FOREIGN_REF, _THEIRS)
    gate.checkout_ours(_OURS)
    result = gate.run(context_extra=_WITH_CLIENT)
    details = _details(result)
    assert details is not None, result.stdout
    assert details.startswith(f"PR #7 ({_FOREIGN_REF}) is open and conflicts textually")
    assert _PR_REF.findall(details) == ["7"]
    assert _extract_blocked_on_pr(details) == 7
    assert gate.cw_calls() == [_ADOPTED_ARGV]


def test_e2e_adopted_pr_with_disjoint_files_clears(gate: _GateRepo) -> None:
    gate.add_adopted(7, _FOREIGN_REF, {"src/other.py": "y = 2\n"}, push=False)
    gate.checkout_ours({"src/mine.py": "x = 1\n"})
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 0, result.stderr
    assert "PR #7: no file overlap" in result.stdout
    assert _CLEAR_MARKER in result.stdout


def test_e2e_adopted_pr_overlap_that_merges_clean_proceeds(gate: _GateRepo) -> None:
    gate.add_adopted(
        7, _FOREIGN_REF, {_SHARED: _shared_with(_BOTTOM, "strict = false")}
    )
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "renamed"')})
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 0, result.stderr
    assert "merge-tree clean despite file overlap with PR #7 — proceeding" in (
        result.stdout
    )
    assert _CLEAR_MARKER in result.stdout


def test_e2e_non_adopted_non_prefix_author_pr_is_still_ignored(
    gate: _GateRepo,
) -> None:
    """Regression: the adopted tag, not the author list, widens the filter."""
    gate.add_adopted(8, "feat/y", {"src/other.py": "y = 2\n"}, push=False)
    gate.add_pr(101, _THEIRS, head_ref="feature/x")
    gate.all_open_prs.append("101 feature/x")
    gate.checkout_ours(_OURS)
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 0, result.stderr
    assert _CLEAR_MARKER in result.stdout
    assert "PR #101" not in result.stdout
    assert not any(call.startswith("pr diff 101") for call in gate.gh_calls())


def test_e2e_client_with_no_adopted_rows_is_identical_to_no_client(
    gate: _GateRepo,
) -> None:
    _conflicting_pr(gate)
    baseline = gate.run()
    assert gate.cw_calls() == []
    with_client = gate.run(context_extra=_WITH_CLIENT)
    assert gate.cw_calls() == [_ADOPTED_ARGV]
    assert (with_client.returncode, with_client.stdout) == (
        baseline.returncode,
        baseline.stdout,
    )
    assert _details(with_client) is not None
    assert len(_pr_lists(gate)) == 1, gate.gh_calls()


def test_e2e_adopted_pr_also_in_author_list_is_evaluated_once(
    gate: _GateRepo,
) -> None:
    gate.add_adopted(9, "dev/9", _THEIRS, author_is_me=True)
    gate.checkout_ours(_OURS)
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.stdout.count("PR #9") == 1, result.stdout
    assert _details(result) is not None
    assert [c for c in gate.gh_calls() if c.startswith("pr diff 9")] == [
        "pr diff 9 --name-only"
    ]


def test_e2e_adopted_row_for_the_current_ticket_is_skipped(gate: _GateRepo) -> None:
    """The ref is never pushed, so evaluating this PR would fail closed."""
    gate.add_adopted(7, _FOREIGN_REF, _THEIRS, push=False)
    gate.checkout_ours(_OURS)
    result = gate.run(
        context_extra={**_WITH_CLIENT, "queue_metadata": {"branch": _FOREIGN_REF}}
    )
    assert result.returncode == 0, result.stderr
    assert _CLEAR_MARKER in result.stdout
    assert not any(call.startswith("pr diff") for call in gate.gh_calls())


def test_e2e_closed_adopted_pr_absent_from_the_open_list_is_not_evaluated(
    gate: _GateRepo,
) -> None:
    gate.add_adopted(7, _FOREIGN_REF, _THEIRS, listed=False)
    gate.checkout_ours(_OURS)
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 0, result.stderr
    assert _CLEAR_MARKER in result.stdout
    assert not any(call.startswith("pr diff") for call in gate.gh_calls())


def test_e2e_cw_failure_fails_closed_with_the_generic_message(
    gate: _GateRepo,
) -> None:
    _conflicting_pr(gate)
    gate.cw_exit = 1
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 4, result.stdout
    assert result.stdout.startswith(
        f"{_BLOCKED_STEP}adopted-PR inventory failed "
        f"(cw dev-queue adopted-prs exited 1), tooling error, failing closed: "
        "cw: stub failure"
    )
    assert _CW_OUTDATED not in result.stdout
    assert _CLEAR_MARKER not in result.stdout


@pytest.mark.parametrize("cw_exit", [127, 2])
def test_e2e_cw_missing_or_outdated_names_the_upgrade(
    gate: _GateRepo, cw_exit: int
) -> None:
    _conflicting_pr(gate)
    gate.cw_exit = cw_exit
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 4, result.stdout
    assert result.stdout.startswith(
        f"{_BLOCKED_STEP}adopted-PR inventory unavailable "
        f"(cw dev-queue adopted-prs exited {cw_exit}: {_CW_OUTDATED}), "
        "tooling error, failing closed: cw: stub failure"
    )
    assert "inventory failed" not in result.stdout


def test_e2e_all_open_pr_list_failure_fails_closed(gate: _GateRepo) -> None:
    gate.add_adopted(7, _FOREIGN_REF, _THEIRS)
    gate.checkout_ours(_OURS)
    gate.all_open_fails = True
    result = gate.run(context_extra=_WITH_CLIENT)
    assert result.returncode == 4, result.stdout
    assert result.stdout.startswith(
        f"{_BLOCKED_STEP}open-PR inventory for adopted rows failed "
        "(gh pr list exited 1), tooling error, failing closed: gh: all-open boom"
    )
    assert _CLEAR_MARKER not in result.stdout


def test_e2e_disabled_gate_never_calls_cw(gate: _GateRepo) -> None:
    gate.add_adopted(7, _FOREIGN_REF, _THEIRS)
    gate.checkout_ours(_OURS)
    gate.cw_exit = 1
    result = gate.run(context_extra={**_WITH_CLIENT, "merge_gate_enabled": False})
    assert result.returncode == 0, result.stderr
    assert _DISABLED_MARKER in result.stdout
    assert gate.cw_calls() == []
    assert gate.gh_calls() == []


def test_e2e_branch_only_row_matches_by_head_ref(gate: _GateRepo) -> None:
    """An unparseable ``adopted_pr_url`` yields ``- <branch>``: join on the ref."""
    gate.add_adopted(7, _FOREIGN_REF, _THEIRS, row=f"- {_FOREIGN_REF}")
    gate.checkout_ours(_OURS)
    details = _details(gate.run(context_extra=_WITH_CLIENT))
    assert details is not None
    assert details.startswith(f"PR #7 ({_FOREIGN_REF}) is open and conflicts")


def test_e2e_absent_script_blocks_adopted_pr_on_raw_intersection(
    gate: _GateRepo,
) -> None:
    gate.add_adopted(
        7, _FOREIGN_REF, {_SHARED: _shared_with(_BOTTOM, "strict = false")}
    )
    gate.checkout_ours({_SHARED: _shared_with(_TOP, 'name = "renamed"')})
    result = gate.run(plant_script=False, context_extra=_WITH_CLIENT)
    assert _ABSENT_LOG in result.stdout
    assert _details(result) == (
        f"PR #7 ({_FOREIGN_REF}) is open and shares files with this branch: {_SHARED}"
    )
