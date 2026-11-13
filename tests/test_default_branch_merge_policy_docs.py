"""Doc conformance for the default-branch merge policy (#2979, ADR-0022).

Three rules: a branch that is merely behind the default branch is never acted
on; ``origin/<default>`` is merged at most once and only on a conflict; a clean
merge never causes a review. These tests pin the prose every site carries:
finalize Step 4c.5, the probe sites (each fetches before it probes), the probe
contract defined once in the runbook, the Step 4.0 re-check pin, the Step 4.0b
rule, ``review-monitor``, the runbook and ADR-0022.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from cw.merge_probe import PROBE_TIMEOUT_SECONDS
from cw.review_convergence._merge_tolerance import TIP_CHECK_BUDGET_SECONDS
from tests._shared.git_repos import _clean_git_env
from tests._shared.guard_fences import substitute_fence_placeholders
from tests._shared.workflow_text import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after

_ROOT = Path(__file__).resolve().parent.parent
_PROBE = "cw worktree merge-probe"

# Skip only off CI: a CI runner without jq must fail, not silently pass unrun.
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not installed",
)

#: Every source file that really spawns ``git merge-tree``: path ->
#: (the name ADR-0022 must carry, why it is allowlisted).
_MERGE_TREE_SPAWNERS = {
    "src/cw/merge_probe.py": (
        "`cw.merge_probe.merge_tree`",
        "the one spawner for the probe, the tip rule and the REVIEW gate",
    ),
    "src/cw/pr_feedback.py": (
        "`cw.pr_feedback`",
        "pre-existing conflict listing for an adopted PR, own NUL parsing",
    ),
    "src/cw/reconcile/review_recipes/repair_git.py": (
        "`repair_git._conflict_set`",
        "pre-existing conflict set for a mechanical repair, own parsing",
    ),
}


def _merge_tree_spawner_files() -> set[str]:
    """Source files passing the literal ``"merge-tree"`` as argv material."""
    found: set[str] = set()
    for path in sorted((_ROOT / "src" / "cw").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.List | ast.Tuple):
                items: list[ast.expr] = node.elts
            elif isinstance(node, ast.Call):
                items = node.args
            else:
                continue
            if any(
                isinstance(item, ast.Constant) and item.value == "merge-tree"
                for item in items
            ):
                found.add(path.relative_to(_ROOT).as_posix())
    return found


def _read(relpath: str) -> str:
    return (_ROOT / relpath).read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    begin = content.index(start)
    return content[begin : content.index(end, begin)]


def _step_4c5() -> str:
    return _between(_cmd("auto-dev-finalize.md"), "### Step 4c.5", "### Step 4d")


def _step_40b() -> str:
    return _between(_cmd("auto-dev-finalize.md"), "### Step 4.0b:", "### Step 4a:")


def _pre_pr_sync_fence() -> str:
    content = _cmd("auto-dev-finalize.md")
    prompt = _after(content, "**Agent prompt must include:**", span=6000)
    return _between(prompt, "git fetch origin\n", "```")


def _prep_pr_step_1() -> str:
    return _between(
        _cmd("prep-pr.md"), "## Step 1: Sync with Base Branch", "## Step 2:"
    )


def _impl_freshness() -> str:
    return _between(
        _cmd("auto-dev-impl.md"),
        "- **Branch freshness:**",
        "- **Record the fork point**",
    )


def _review_fallback_section() -> str:
    """The fallback's fence and the prose that follows it, up to the next heading."""
    return _between(
        _appendix("review"),
        "## Fallback — direct execution from the main session's worktree",
        "## Clean-mode convergence",
    )


def _review_fallback_fence() -> str:
    section = _review_fallback_section()
    return _between(section, "# From the main session's worktree", "```")


def _first_bash_fence(section: str) -> str:
    """The body of the first ```` ```bash ```` fence in *section*."""
    return section.split("```bash\n", 1)[1].split("```", 1)[0]


def _probe_part_of_review_fallback() -> str:
    """The fallback fence up to the edit steps (the rest needs a real repo)."""
    return _review_fallback_fence().split("# apply edits", 1)[0]


# Logs every invocation so a test can prove the probe never ran.
_STUB_CW = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_CW_LOG"
printf '%s' "$STUB_PROBE_STDOUT"
printf '%s' "$STUB_PROBE_STDERR" >&2
exit "$STUB_PROBE_RC"
"""

# Logs every invocation; the fence's ahead-count must read 0 so the push
# branch is skipped; fetch exits $STUB_FETCH_RC, everything else (checkout,
# merge, push) succeeds. When $STUB_FETCH_FAIL_ARG is set, only a fetch whose
# argv contains that exact token exits $STUB_FETCH_RC; every other fetch exits
# 0, so a fence with several fetches can fail one of them alone.
_STUB_GIT = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_GIT_LOG"
if [ "$1" = "rev-list" ]; then
  echo 0
fi
if [ "$1" = "fetch" ]; then
  if [ -z "$STUB_FETCH_FAIL_ARG" ]; then
    exit "$STUB_FETCH_RC"
  fi
  for arg in "$@"; do
    if [ "$arg" = "$STUB_FETCH_FAIL_ARG" ]; then
      exit "$STUB_FETCH_RC"
    fi
  done
  exit 0
fi
exit 0
"""


class _FenceRun(NamedTuple):
    """What a fence did under stub ``cw`` and ``git``."""

    git_calls: list[str]
    cw_calls: list[str]
    stdout: str


def _run_fence(
    tmp_path: Path,
    fence: str,
    *,
    stdout: str,
    stderr: str,
    rc: int,
    fetch_rc: int = 0,
    fetch_fail_arg: str = "",
) -> _FenceRun:
    """Run *fence* under bash with stub ``cw`` and ``git`` (one argv per call).

    *fetch_fail_arg* narrows *fetch_rc* to fetches whose argv holds that token.
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("cw", _STUB_CW), ("git", _STUB_GIT)):
        stub = stubs / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    git_log = tmp_path / "git.log"
    git_log.touch()
    cw_log = tmp_path / "cw.log"
    cw_log.touch()
    env = {
        **_clean_git_env(),
        "HOME": str(home),
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "STUB_GIT_LOG": str(git_log),
        "STUB_CW_LOG": str(cw_log),
        "STUB_FETCH_RC": str(fetch_rc),
        "STUB_FETCH_FAIL_ARG": fetch_fail_arg,
        "STUB_PROBE_STDOUT": stdout,
        "STUB_PROBE_STDERR": stderr,
        "STUB_PROBE_RC": str(rc),
    }
    fence = substitute_fence_placeholders(
        fence, {"branch-name": "dev/2979", "base": "main"}
    )
    result = subprocess.run(
        ["bash", "-c", fence],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return _FenceRun(
        git_log.read_text(encoding="utf-8").splitlines(),
        cw_log.read_text(encoding="utf-8").splitlines(),
        result.stdout,
    )


def _run_pre_pr_sync_fence(
    tmp_path: Path, *, stdout: str, stderr: str, rc: int, fetch_rc: int = 0
) -> tuple[list[str], list[str]]:
    """Run the finalize pre-PR sync fence; returns its git and cw invocations."""
    run = _run_fence(
        tmp_path,
        _pre_pr_sync_fence(),
        stdout=stdout,
        stderr=stderr,
        rc=rc,
        fetch_rc=fetch_rc,
    )
    return run.git_calls, run.cw_calls


@requires_jq
class TestPrePrSyncFenceBehaviour:
    """The merge guard is executed, not just grepped (#2979).

    The fence pipes the probe JSON through ``jq``, so these need the real binary.
    """

    _MERGE = "merge origin/main --no-edit"

    def test_a_crashed_probe_exiting_1_never_merges(self, tmp_path: Path) -> None:
        git_calls, _ = _run_pre_pr_sync_fence(
            tmp_path,
            stdout="",
            stderr="Traceback (most recent call last):\n  boom\n",
            rc=1,
        )
        assert not any(call.startswith("merge") for call in git_calls)

    def test_exit_1_with_a_conflict_state_merges_exactly_once(
        self, tmp_path: Path
    ) -> None:
        git_calls, _ = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"conflict"}', stderr="", rc=1
        )
        assert git_calls.count(self._MERGE) == 1

    def test_a_clean_probe_does_not_merge(self, tmp_path: Path) -> None:
        git_calls, _ = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"clean"}', stderr="", rc=0
        )
        assert not any(call.startswith("merge") for call in git_calls)

    def test_an_unavailable_probe_does_not_merge(self, tmp_path: Path) -> None:
        git_calls, _ = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"unavailable"}', stderr="", rc=3
        )
        assert not any(call.startswith("merge") for call in git_calls)

    def test_a_failed_fetch_neither_probes_nor_merges(self, tmp_path: Path) -> None:
        # A stale origin/main must not drive a merge: even a would-be conflict
        # verdict is ignored because the probe is never run.
        git_calls, cw_calls = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"conflict"}', stderr="", rc=1, fetch_rc=128
        )
        assert not any(call.startswith("merge") for call in git_calls)
        assert not any("merge-probe" in call for call in cw_calls)

    def test_a_failed_fetch_prints_the_skipped_marker(self) -> None:
        fence = substitute_fence_placeholders(
            _pre_pr_sync_fence(), {"branch-name": "dev/2979"}
        )
        assert 'echo "PROBE_RC=skipped PROBE_STATE=skipped"' in fence
        assert "`PROBE_RC=skipped` means the fetch failed" in _cmd(
            "auto-dev-finalize.md"
        )


def _prep_pr_probe_fence() -> str:
    return _first_bash_fence(_prep_pr_step_1())


def _impl_probe_fence() -> str:
    return _first_bash_fence(_impl_freshness())


#: The sites whose fence only probes (the merge is a separate step the agent
#: takes). The finalize site, whose fence also merges, is covered above.
_PROBE_ONLY_SITES = [
    pytest.param(_prep_pr_probe_fence, id="prep-pr-step-1"),
    pytest.param(_impl_probe_fence, id="impl-stage-2"),
    pytest.param(_probe_part_of_review_fallback, id="review-fallback"),
]


@requires_jq
class TestProbeOnlySiteFenceBehaviour:
    """A failed fetch skips the probe at every site, not only finalize's (#2979).

    The probe never fetches, so probing after a failed fetch reads a stale
    ``origin/main``. Each fence enforces this itself: the agent cannot obey a
    prose rule once the probe has already run in the same Bash call.
    """

    @pytest.mark.parametrize("fence", _PROBE_ONLY_SITES)
    def test_a_failed_fetch_never_runs_the_probe(
        self, fence: object, tmp_path: Path
    ) -> None:
        assert callable(fence)
        run = _run_fence(
            tmp_path,
            str(fence()),
            stdout='{"state":"conflict"}',
            stderr="",
            rc=1,
            fetch_rc=128,
        )
        assert not any("merge-probe" in call for call in run.cw_calls)
        assert not any(call.startswith("merge") for call in run.git_calls)
        assert "PROBE_RC=skipped PROBE_STATE=skipped" in run.stdout

    @pytest.mark.parametrize("fence", _PROBE_ONLY_SITES)
    def test_a_good_fetch_probes_once_and_prints_the_outcome(
        self, fence: object, tmp_path: Path
    ) -> None:
        assert callable(fence)
        run = _run_fence(
            tmp_path, str(fence()), stdout='{"state":"conflict"}', stderr="", rc=1
        )
        assert run.cw_calls == ["worktree merge-probe --base main"]
        assert "PROBE_RC=1 PROBE_STATE=conflict" in run.stdout
        assert "skipped" not in run.stdout
        assert not any(call.startswith("merge") for call in run.git_calls)


@requires_jq
class TestReviewFallbackFetchGuard:
    """``FETCH_RC`` in the review fallback captures the ``main`` fetch only (#2979).

    The fence runs two fetches, ``<branch-name>`` then ``main``. A stub that fails
    both together cannot tell which one the guard reads, so these fail one alone.
    """

    def test_a_failed_main_fetch_skips_the_probe(self, tmp_path: Path) -> None:
        run = _run_fence(
            tmp_path,
            _probe_part_of_review_fallback(),
            stdout='{"state":"conflict"}',
            stderr="",
            rc=1,
            fetch_rc=128,
            fetch_fail_arg="main",
        )
        assert "fetch origin dev/2979" in run.git_calls
        assert "fetch origin main" in run.git_calls
        assert not any("merge-probe" in call for call in run.cw_calls)
        assert "PROBE_RC=skipped PROBE_STATE=skipped" in run.stdout

    def test_a_failed_branch_fetch_alone_still_probes(self, tmp_path: Path) -> None:
        # The branch fetch sits outside the guard by design: only the fetch of
        # the default branch decides whether the probe may read origin/main.
        run = _run_fence(
            tmp_path,
            _probe_part_of_review_fallback(),
            stdout='{"state":"conflict"}',
            stderr="",
            rc=1,
            fetch_rc=128,
            fetch_fail_arg="dev/2979",
        )
        assert run.cw_calls == ["worktree merge-probe --base main"]
        assert "PROBE_RC=1 PROBE_STATE=conflict" in run.stdout
        assert "skipped" not in run.stdout


class TestStep4c5:
    def test_never_rebases_or_force_pushes(self) -> None:
        section = _step_4c5()
        for retired in (
            "git rebase origin/main",
            "--force-with-lease",
            "auto-rebase",
            "rebase-retry",
            "git rebase --abort",
        ):
            assert retired not in section, retired

    def test_behind_is_no_action(self) -> None:
        section = _step_4c5()
        row = section[section.index("| `MERGEABLE` | `BEHIND` |") :].split("\n", 1)[0]
        assert "**No action.**" in row
        assert "`behind_default_no_action`" in row

    def test_conflicting_runs_one_merge(self) -> None:
        section = _step_4c5()
        row = section[section.index("| `CONFLICTING` | `DIRTY` |") :].split("\n", 1)[0]
        assert "One merge of `origin/main`" in row
        assert section.count("git merge origin/main\n") == 1
        assert "no loops" in section
        assert "plain push" in section
        assert "PRE_MERGE_SHA=$(git rev-parse HEAD)" in section

    def test_a_conflict_resolution_merge_is_reviewed(self) -> None:
        section = _step_4c5()
        assert "normal full review round" in section
        assert "#2980" in section
        assert "delta-confirm" in section
        assert "delta_confirm_eligible" in section


class TestProbeSites:
    @pytest.mark.parametrize(
        "fence",
        [
            pytest.param(_pre_pr_sync_fence, id="finalize-pre-pr-sync"),
            pytest.param(_prep_pr_step_1, id="prep-pr-step-1"),
            pytest.param(_impl_freshness, id="impl-stage-2"),
            pytest.param(_review_fallback_fence, id="review-fallback"),
        ],
    )
    def test_each_site_fetches_before_it_probes(self, fence: object) -> None:
        assert callable(fence)
        text = str(fence())
        assert _PROBE in text
        probe = text.index(_PROBE)
        fetch = text.find("git fetch origin")
        assert 0 <= fetch < probe

    @pytest.mark.parametrize(
        "section",
        [
            pytest.param(_prep_pr_step_1, id="prep-pr-step-1"),
            pytest.param(_impl_freshness, id="impl-stage-2"),
            pytest.param(_review_fallback_section, id="review-fallback"),
        ],
    )
    def test_a_failed_fetch_means_no_probe(self, section: object) -> None:
        assert callable(section)
        text = str(section())
        assert "If the fetch fails, do NOT probe" in text
        assert "`merge_probe_unavailable`" in text
        assert "`PROBE_RC=skipped PROBE_STATE=skipped`" in text

    @pytest.mark.parametrize(
        "fence",
        [
            pytest.param(_prep_pr_step_1, id="prep-pr-step-1"),
            pytest.param(_impl_freshness, id="impl-stage-2"),
            pytest.param(_review_fallback_fence, id="review-fallback"),
        ],
    )
    def test_the_probe_sits_inside_the_fetch_guard(self, fence: object) -> None:
        """The probe is reachable only through ``FETCH_RC`` (same shape as finalize)."""
        assert callable(fence)
        text = str(fence())
        fetch_rc = text.index("FETCH_RC=$?")
        guard = text.index('if [ "$FETCH_RC" -eq 0 ]; then')
        probe = text.index(_PROBE)
        skipped = text.index('echo "PROBE_RC=skipped PROBE_STATE=skipped"')
        assert text.index("git fetch origin") < fetch_rc < guard < probe < skipped

    def test_the_finalize_sync_prose_states_the_failed_fetch_rule(self) -> None:
        prompt = _after(
            _cmd("auto-dev-finalize.md"), "**Agent prompt must include:**", span=8000
        )
        assert "If the fetch fails, do NOT probe" in prompt
        assert "`merge_probe_unavailable`" in prompt

    def test_the_review_fallback_merges_only_on_a_proven_conflict(self) -> None:
        fence = _review_fallback_fence()
        assert "git fetch origin main" in fence
        assert (
            "exit 1 AND JSON state conflict (both) only: "
            "git merge origin/main --no-edit" in fence
        )
        assert "merge_probe_unavailable" in fence

    def test_the_finalize_sync_merges_only_on_a_conflict(self) -> None:
        fence = _pre_pr_sync_fence()
        probe = fence.index(_PROBE)
        merge = fence.index("git merge origin/main --no-edit")
        assert probe < merge
        assert fence.count("git merge origin/main") == 1
        # Exit 1 alone is not a conflict: a cw crash exits 1 too (#2979).
        guard = 'if [ "$PROBE_RC" -eq 1 ] && [ "$PROBE_STATE" = "conflict" ]; then'
        assert guard in fence[probe:merge]
        assert 'PROBE_STATE="$(printf' in fence[probe:merge]
        assert "BOTH `PROBE_RC` is 1 AND the JSON `state` is `conflict`" in (
            _cmd("auto-dev-finalize.md")
        )

    @pytest.mark.parametrize(
        "fence",
        [
            pytest.param(_pre_pr_sync_fence, id="finalize-pre-pr-sync"),
            pytest.param(_prep_pr_step_1, id="prep-pr-step-1"),
            pytest.param(_impl_freshness, id="impl-stage-2"),
            pytest.param(_review_fallback_fence, id="review-fallback"),
        ],
    )
    def test_each_site_prints_the_probe_result(self, fence: object) -> None:
        """A worker's Bash calls share no shell state: the outcome must be printed."""
        assert callable(fence)
        text = str(fence())
        capture = text.index('PROBE_JSON="$(cw worktree merge-probe')
        echo = text.index('echo "PROBE_RC=$PROBE_RC PROBE_STATE=$PROBE_STATE"')
        dump = text.index("printf '%s\\n' \"$PROBE_JSON\"")
        assert capture < echo < dump

    @pytest.mark.parametrize(
        "section",
        [
            pytest.param(_prep_pr_step_1, id="prep-pr-step-1"),
            pytest.param(_impl_freshness, id="impl-stage-2"),
        ],
    )
    def test_split_fence_sites_say_to_read_the_printed_line(
        self, section: object
    ) -> None:
        assert callable(section)
        assert "from the printed `PROBE_RC=… PROBE_STATE=…` line" in str(section())

    def test_the_finalize_prose_points_at_the_printed_outcome(self) -> None:
        content = _cmd("auto-dev-finalize.md")
        assert "The fence prints a `PROBE_RC=… PROBE_STATE=…` line" in content

    def test_no_unconditional_merge_in_prep_pr_or_impl(self) -> None:
        for text in (_prep_pr_step_1(), _impl_freshness()):
            merge = text.index("git merge origin/")
            gate = text.index("`PROBE_RC` 1 AND `PROBE_STATE` `conflict`")
            assert text.index('PROBE_STATE="$(printf') < gate < merge
            assert "(both, never the exit code alone)" in text[gate:merge]
            assert "merge_probe_unavailable" in text[:gate]


class TestProbeContract:
    def test_the_runbook_defines_it_once(self) -> None:
        runbook = _read("docs/dispatch-runbook.md")
        assert runbook.count("**The probe contract**") == 1
        contract = _after(runbook, "**The probe contract**", span=3200)
        for needle in (
            "`--worktree` defaults to the current directory",
            "exit **0**",
            "exits **1**",
            "exits **3**",
            "**the probe never fetches**",
            "`base_sha`",
            "BOTH the exit code is 1 AND the JSON `state` is `conflict`",
        ):
            assert needle in contract, needle


class TestStep40:
    def test_the_headless_recheck_keeps_the_step_4c5_pin(self) -> None:
        content = _cmd("auto-dev-finalize.md")
        recheck = content.index("**Headless re-check before auto-merge:**")
        arm = content.index("3. **Enable auto-merge:**", recheck)
        span = content[recheck:arm]
        assert "Step 4c.5" in span
        assert "review_pending_approval" in span
        assert "after any Step 4c.5 merge" in span
        assert "Step 4c.5 rebase" not in span

    def test_step_40b_has_no_overlap_text(self) -> None:
        section = _step_40b()
        for retired in (
            "overlaps branch files",
            "`merge_gate_ignore_paths`",
            "apart from `CHANGELOG.md`",
            "touches a file of the branch's own diff",
        ):
            assert retired not in section, retired
        assert "git itself would not write" in section
        assert "#2980" in section

    def test_step_40b_runs_the_conflict_resolution_round(self) -> None:
        """#2980: verify-tip, then delta-confirm, then verify-tip as the decider."""
        section = _step_40b()
        for needle in (
            "delta_confirm_eligible",
            "`refused`",
            "`backend_unavailable`",
            "`round_failed`",
            "`findings`",
            "`stopped`",
            "`still_stale`",
            'timeout 600 cw review converge delta-confirm --ticket-id "$TICKET" '
            '-c "$CLIENT"',
            "never run `converge finalize` in between",
            "`ledger_not_confirmable`",
            "`delta_confirm_eligible` is advisory",
            "re-reads the live client config",
            "that second `verify-tip` is the sole decider",
            'next_actions: ["requeue_stage_review"]',
        ):
            assert needle in section, needle

    def test_the_fence_scope_note_is_gone(self) -> None:
        assert "**Fence scope (#2918).**" not in _cmd("auto-dev-finalize.md")


class TestRunbook:
    def test_it_states_the_policy_and_its_limits(self) -> None:
        runbook = _read("docs/dispatch-runbook.md")
        section = _between(
            runbook,
            "#### Default-branch merge policy (#2979)",
            "#### Codex-lane clean mode (#2791)",
        )
        for needle in (
            "**Behind is not a reason to act.**",
            "**Merge at most once, and only on a conflict.**",
            "**A clean merge never causes a review.**",
            "#2980",
            "`delta-confirm`",
            "`behind_default_no_action`",
            "`merge_probe_unavailable`",
            "**Diagnosing a tip that reads stale despite a clean merge.**",
            "`not_textually_clean <sha7>`",
            "**Accepted limitations.**",
            "Strict-protection repos without a merge queue",
            "False conflicts are fail-closed",
        ):
            assert needle in section, needle

    def test_pr_attention_treats_behind_as_no_action(self) -> None:
        runbook = _read("docs/dispatch-runbook.md")
        section = _between(
            runbook,
            "#### Default-branch merge policy (#2979)",
            "#### Codex-lane clean mode (#2791)",
        )
        bullet = _after(section, "- **PR attention follows the same behind policy", 800)
        assert "`pr_hydrate` Row 1" in bullet
        assert "review-monitor's `merge_blocked` attention state" in bullet
        assert "does not trigger `escalate_merge_block`" in bullet
        assert "failing CI and change requests retain their" in bullet
        assert "#2990 tracks making it probe them" in runbook

    def test_review_monitor_preserves_ci_and_review_signals_while_behind(self) -> None:
        runbook = _read("docs/dispatch-runbook.md")
        section = _between(
            runbook,
            "#### Default-branch merge policy (#2979)",
            "#### Codex-lane clean mode (#2791)",
        )
        bullet = _after(section, "- **PR attention follows the same behind policy", 800)
        assert "failing CI and change requests retain their `ci_failing`" in bullet
        assert "Neither path merges or rebases" in bullet

    def test_the_documented_bounds_match_the_code_constants(self) -> None:
        assert TIP_CHECK_BUDGET_SECONDS == 30.0
        assert PROBE_TIMEOUT_SECONDS == 10.0

    def test_the_in_lock_tip_check_bound_matches_the_code(self) -> None:
        section = _between(
            _read("docs/dispatch-runbook.md"),
            "#### Default-branch merge policy (#2979)",
            "#### Codex-lane clean mode (#2791)",
        )
        flat = " ".join(section.split())
        assert (
            "every call (rev-parse, merge-base, rev-list, merge-tree) has a 10 s "
            "timeout, and the per-merge `merge-tree` loop runs under a 30 s "
            "wall-clock budget checked before each merge" in flat
        )
        assert "`verify-tip` and `finalize` (not `init` / `status`)" in flat
        assert "one bounded (10 s) `git fetch origin <default-branch>`" in flat
        assert "a failed fetch keeps the stale verdict" in flat

    def test_the_branch_behind_main_bullet_no_longer_says_rebase(self) -> None:
        runbook = _read("docs/dispatch-runbook.md")
        bullet = _after(runbook, "- `branch_behind_main`:", span=400).split("\n- ")[0]
        assert "rebase, then" not in bullet
        assert "never a rebase or force-push" in bullet


class TestCwOrchestrationSkill:
    def test_merge_blocked_takes_no_branch_action_for_behind_alone(self) -> None:
        skill = _read(".claude/skills/cw-orchestration/SKILL.md")
        bullet = _after(skill, "- **`merge_blocked`**:", span=2400).split("\n- ")[0]
        assert "BEHIND alone has no merge-blocked attention or branch action" in bullet
        assert (
            "handle CI failures and review requests through their own attention states"
            in bullet
        )
        assert "never a rebase or force-push" in bullet


class TestReviewMonitor:
    def test_behind_is_not_a_trigger_and_nothing_rebases(self) -> None:
        monitor = _cmd("review-monitor.md")
        trigger = _after(monitor, "**Step 4b: Auto-fix dispatch.**", span=700)
        assert '`merge_state_status == "DIRTY"`' in trigger
        assert '{"DIRTY", "BEHIND"}' not in trigger
        jq = _after(monitor, "gh pr list --repo <entry.repo> --author @me", span=600)
        assert '.mergeStateStatus == "BEHIND"' not in jq
        template = _between(monitor, "**Prompt template**", "**Re-arm auto-merge")
        assert "git rebase" not in template
        assert "--force-with-lease" not in template
        assert "dispatch a rebase agent" not in monitor

    def test_a_behind_pr_with_a_fixable_cause_is_still_fixed(self) -> None:
        monitor = _cmd("review-monitor.md")
        para = _after(
            monitor,
            "**BEHIND does not mask an actionable signal (#2996).**",
            span=900,
        ).split("\n\n")[0]
        assert "`ci_ok == false`" in para
        assert "reports `ci_failing`" in para
        assert "reports `changes_requested`" in para
        assert 'Skip merge-main work unless `merge_state_status == "DIRTY"`' in para
        template = _between(monitor, "**Prompt template**", "**Re-arm auto-merge")
        assert "State: merge_state_status=<DIRTY|BEHIND|UNKNOWN>," in template


class TestAdr:
    def test_adr_0022_exists_and_is_indexed(self) -> None:
        adr = _read(
            "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
        )
        assert "**Status:** Accepted" in adr
        decision = " ".join(adr.split())
        assert (
            "carries a conflict resolution is reviewed by `cw review converge "
            "delta-confirm` over the resolution alone"
        ) in decision
        assert "a normal full round whenever that review is refused" in decision
        assert "no per-client key" in decision
        assert "`review.default_merge_tolerance: false`" in decision
        index = _read("docs/adr/README.md")
        assert (
            "[0022](0022-a-clean-default-branch-merge-never-causes-a-review.md)"
            in index
        )

    def test_invariant_3_carves_out_the_review_gate_and_states_its_bound(
        self,
    ) -> None:
        adr = " ".join(
            _read(
                "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
            ).split()
        )
        assert "This does not cover the REVIEW staleness gate" in adr
        assert "Every one of those calls has a 10 s timeout" in adr
        assert "`PROBE_TIMEOUT_SECONDS`" in adr
        assert (
            "a 30 s wall-clock budget (`TIP_CHECK_BUDGET_SECONDS`) checked before "
            "each merge" in adr
        )
        assert "a timeout or a spent budget reads as stale (fail closed)" in adr
        assert "There is no per-tick cap." in adr

    def test_the_consequences_state_pr_attention_policy_and_ref_refresh(
        self,
    ) -> None:
        adr = " ".join(
            _read(
                "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
            ).split()
        )
        assert "PR attention also treats BEHIND as no-action" in adr
        assert "BEHIND alone does not escalate" in adr
        assert "CI failures or review requests retain their own attention states" in adr
        assert "the gate is inert for dispatch-driven rows (#2990)" in adr
        assert "tracked in #2990" in " ".join(
            _read("src/cw/dispatch/branch_freshness.py").split()
        )
        assert "`verify-tip` and `finalize` refresh the default branch once." in adr
        assert (
            "`non_merge_commit`, `second_parent_not_on_default` or `no_default_ref`"
            in adr
        )
        assert "`converge init` / `status` and the in-lock gate never fetch it" in adr

    def test_the_probe_sweep_budget_is_documented_as_checked_before_each_probe(
        self,
    ) -> None:
        """The sweep's 30 s is a budget checked before each probe, not a bound (#2979).

        One probe is up to three 10 s git calls, so the worst case is the budget
        plus one probe; every surface that states the figure says so.
        """
        worst_case = "the worst case is the budget plus one probe"
        adr = " ".join(
            _read(
                "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
            ).split()
        )
        runbook = _after(
            " ".join(_read("docs/dispatch-runbook.md").split()),
            "**The `branch_behind_main` REVIEW gate (#1823).**",
            span=900,
        )
        source = " ".join(_read("src/cw/dispatch/branch_freshness.py").split())
        for name, text in (("adr", adr), ("runbook", runbook), ("source", source)):
            assert worst_case in text, name
        assert "a 30 s budget checked before each probe" in adr
        assert "under a 30 s budget checked before each probe" in runbook
        assert "within a 30 s budget" not in runbook
        assert "(20 probes, 30 s, 10 s per git call)" not in adr
        assert "checked before each probe, not during one" in source

    def test_the_in_lock_tip_check_budget_is_documented_as_checked_before_each_merge(
        self,
    ) -> None:
        """The tip check's 30 s is checked before each merge, not a bound (#2979).

        One merge is up to three 10 s git calls and four more git calls run
        before the loop, so the worst case is those four calls plus the budget
        plus one merge; every surface that states the figure says so.
        """
        worst_case = (
            "the worst case is the four git calls before the loop plus the budget "
            "plus one merge"
        )
        adr = " ".join(
            _read(
                "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
            ).split()
        )
        runbook = _after(
            " ".join(_read("docs/dispatch-runbook.md").split()),
            "The gate's tip check runs git under `dev_queue_lock`",
            span=700,
        )
        source = " ".join(
            _read("src/cw/review_convergence/_merge_tolerance.py")
            .replace("#:", "")
            .split()
        )
        for name, text in (("adr", adr), ("runbook", runbook)):
            assert worst_case in text, name
        assert (
            "the worst case is the four prelude git calls plus the budget plus one "
            "merge, not the budget" in source
        )
        assert (
            "The worst case is the four git calls before the loop, plus the budget, "
            "plus one merge" in source
        )
        assert "checked before each merge, not during one" in source
        assert "up to three git calls of 10 s each" in source
        retired = (
            "shares one 30 s wall-clock budget",
            "shared 30 s budget",
            "30 s shared budget across the merge loop",
            "one shared 30 s",
        )
        for name, text in (
            ("adr", adr),
            ("runbook", " ".join(_read("docs/dispatch-runbook.md").split())),
            ("source", source),
        ):
            for phrase in retired:
                assert phrase not in text, (name, phrase)

    def test_the_one_spawner_claim_names_every_other_merge_tree_caller(self) -> None:
        """Every real ``git merge-tree`` spawner is allowlisted and named (#2979).

        A spawner is a source file that passes the literal ``"merge-tree"`` as a
        list/tuple element or a call argument (the argv forms in use). Comments
        and docstrings that merely mention it are not spawners. A new spawner
        fails this test until it is allowlisted here and named in the ADR.
        """
        assert _merge_tree_spawner_files() == set(_MERGE_TREE_SPAWNERS)
        adr = " ".join(
            _read(
                "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
            ).split()
        )
        assert "the only spawner for the probe, the tip rule and the REVIEW gate" in adr
        assert "deliberately left alone" in adr
        for relpath, (adr_name, _why) in _MERGE_TREE_SPAWNERS.items():
            if relpath != "src/cw/merge_probe.py":
                assert adr_name in adr, relpath
        probe = " ".join(_read("src/cw/merge_probe.py").split())
        assert "only spawner for the probe, the tip rule and the REVIEW gate" in probe
        assert "``repair_git._conflict_set``" in probe
        assert "``cw.pr_feedback``" in probe
        architecture = " ".join(_read("ARCHITECTURE.md").split())
        assert "only spawner for the probe, the tip rule and the REVIEW gate" in (
            architecture
        )
        assert "`repair_git._conflict_set`" in architecture
        assert "`cw.pr_feedback`'s conflict listing" in architecture


class TestFailedFetchDocsMatchTheCode:
    """The docs state what each site does on a failed fetch, Python site included."""

    def test_invariant_1_names_the_fix_dispatch_exception(self) -> None:
        adr = " ".join(
            _read(
                "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
            ).split()
        )
        assert "Each shell fence" in adr
        assert "does not probe: it prints `PROBE_RC=skipped PROBE_STATE=skipped`" in adr
        assert (
            "The Python fix dispatch (`dispatch_fix_agent`) is the one exception"
            in (adr)
        )
        assert "still probes the local tracking ref" in adr
        assert "merges nothing" in adr
        # The retired blanket claim: every caller skips the probe on a failed fetch.
        assert (
            "every caller fetches `origin/<default>` first in the same fence"
            not in (adr)
        )

    def test_the_runbook_contract_agrees_with_the_adr(self) -> None:
        runbook = " ".join(_read("docs/dispatch-runbook.md").split())
        contract = _after(runbook, "**The probe contract**", span=4200)
        assert "so every shell site runs" in contract
        assert "prints `PROBE_RC=skipped PROBE_STATE=skipped`" in contract
        assert "`dispatch_fix_agent`) is the one exception" in contract
        assert "still probes the local tracking ref" in contract

    def test_the_fix_dispatch_does_probe_after_a_degraded_fetch(self) -> None:
        """The docs above describe this code: probe after a degraded fetch, note it."""
        source = " ".join(_read("src/cw/reconcile/review_recipes/fix_agent.py").split())
        assert "the caller probes against the tracking ref it already has" in source
        assert "used the possibly stale" in source

    def test_architecture_keeps_the_two_staleness_gates_distinct(self) -> None:
        architecture = " ".join(_read("ARCHITECTURE.md").split())
        paragraph = _after(
            architecture, "**ADR-0022 — a clean default-branch merge", 2200
        )
        behind = paragraph.index("`branch_behind_main` gate parks only on")
        review = paragraph.index("review-staleness gate (`review_artifacts_stale`)")
        assert behind < review
        # The in-lock tip check belongs to the review gate, not branch_behind_main.
        assert "its tip check" not in paragraph
        assert "still runs a bounded tip check of git in-lock" in paragraph[review:]
        assert "in-lock" not in paragraph[behind:review]


# --- the release-cut CHANGELOG exception (#3190) -------------------------------

_ADR_0022 = "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"

#: Scripts outside ``src/`` that really spawn ``git merge-tree`` (the
#: ``_merge_tree_spawner_files`` scan walks ``src/cw`` only).
_SCRIPT_MERGE_TREE_SPAWNERS = {
    ".claude/scripts/check_changelog_frozen.py": (
        "`.claude/scripts/check_changelog_frozen.py",
        "stdlib-only freeze gate; cannot import cw (#3190)",
    ),
}

_ADR_EXCEPTION = (
    "One exception: a merge of `origin/<default>` into a clean PR whose only"
    " purpose is to re-home a CHANGELOG hunk that a release cut misplaced (`cw"
    " worktree changelog-sync`, #3190). Its tree is not the one git writes"
    " (CHANGELOG.md is rebuilt), so the tip rule reads it as a hand edit of a"
    " clean merge and offers it to `cw review converge delta-confirm`; two such"
    " merges after the same clean round are refused as `multiple_resolutions` and"
    " get a normal full round. That cost is accepted: the alternative is a"
    " corrupted released section on the default branch."
)
_ADR_INVARIANT_1 = (
    "`.claude/scripts/check_changelog_frozen.py --merge-into` (#3190) is a third,"
    " outside `src/`: it is stdlib-only, cannot import `cw`, and is deliberately"
    " left alone as well."
)
_ADR_HEADER = (
    "amended by #2980 (the conflict-resolution round) and #3190 (the release-cut"
    " CHANGELOG exception)"
)
_ARCH_EXCEPTION = (
    "One exception (#3190): `cw worktree changelog-sync` merges `origin/<default>`"
    " into a clean PR solely to re-home a CHANGELOG hunk that a release cut"
    " misplaced; the tip rule reads that merge as a hand edit of a clean merge and"
    " offers it to `delta-confirm`."
)
_ARCH_SPAWNERS = (
    "`repair_git._conflict_set`, `cw.pr_feedback`'s conflict listing and"
    " `.claude/scripts/check_changelog_frozen.py` (a stdlib-only script that"
    " cannot import `cw`; its `--merge-into` mode runs its own `git merge-tree"
    " --write-tree`, #3190) are separate `git merge-tree` callers left alone."
)
_RUNBOOK_RULE_2 = (
    "One exception (#3190): `cw worktree changelog-sync` merges"
    " `origin/<default-branch>` into a `MERGEABLE`, probe-clean PR solely to"
    ' re-home a CHANGELOG hunk that a release cut misplaced (see "Release cut and'
    ' in-flight PRs" below).'
)
_RESIDUAL = (
    "Layer C re-checks only while `test (ubuntu-latest, 3.13)` is still running,"
    " and Layer B only when the PR is armed after the cut.",
    "A PR armed before the cut whose `test` job had already finished green is not"
    " re-checked by anything: when `test (macos-latest, 3.13)` or `gate-changelog`"
    " finishes after the cut, GitHub merges it with the pre-cut result",
    "The window runs from the end of `test`'s last step to the merge, which is"
    " minutes, not seconds, and it widens by however long the last required check"
    " finishes after `test`.",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _merge_policy_section() -> str:
    return _flat(
        _between(
            _read("docs/dispatch-runbook.md"),
            "#### Default-branch merge policy (#2979)",
            "#### Codex-lane clean mode (#2791)",
        )
    )


def _playbook_section() -> str:
    return _flat(
        _between(
            _read("docs/release-playbook.md"),
            "### Release cut and in-flight PRs (#3190)",
            "### Commit-subject contract",
        )
    )


class TestReleaseCutException:
    def test_the_script_spawner_really_spawns_merge_tree(self) -> None:
        for relpath in _SCRIPT_MERGE_TREE_SPAWNERS:
            tree = ast.parse(_read(relpath))
            assert any(
                isinstance(node, ast.Constant) and node.value == "merge-tree"
                for node in ast.walk(tree)
            ), relpath

    def test_adr_names_the_exception_the_script_and_the_amendment(self) -> None:
        adr = _flat(_read(_ADR_0022))

        assert _ADR_HEADER in adr
        assert _ADR_EXCEPTION in adr
        assert _ADR_INVARIANT_1 in adr
        assert adr.index("deliberately left alone.") < adr.index(_ADR_INVARIANT_1)
        for adr_name, _why in _SCRIPT_MERGE_TREE_SPAWNERS.values():
            assert adr_name in adr

    def test_architecture_names_the_exception_and_the_script(self) -> None:
        architecture = _flat(_read("ARCHITECTURE.md"))

        assert _ARCH_EXCEPTION in architecture
        assert _ARCH_SPAWNERS in architecture
        assert architecture.index("reports a conflict.") < architecture.index(
            _ARCH_EXCEPTION
        )
        for adr_name, _why in _SCRIPT_MERGE_TREE_SPAWNERS.values():
            assert adr_name in architecture

    def test_runbook_states_the_exception_the_layers_and_the_limit(self) -> None:
        section = _merge_policy_section()

        assert _RUNBOOK_RULE_2 in section
        paragraph = _after(
            section, "**Release cut and in-flight PRs (#3190).**", span=3400
        )
        for needle in (
            "GitHub's squash ignores the `CHANGELOG.md merge=union` driver",
            "`check_changelog_frozen.py --merge-into origin/<default-branch>"
            " --head <sha>`",
            "makes no `gh pr merge` call",
            "`cw worktree changelog-sync --base <default-branch>`",
            "`delta_confirm_eligible: true`",
            "`multiple_resolutions`",
            "`shipped_pr_checks_failed`",
            "`cw dev-queue recover-red-pr`",
        ):
            assert needle in paragraph, needle
        assert section.index("**Release cut and in-flight PRs (#3190).**") < (
            section.index("**Friction tokens.**")
        )
        limits = section[section.index("**Accepted limitations.**") :]
        assert "**A release cut after the merge-time check (#3190).**" in limits
        for sentence in _RESIDUAL:
            assert sentence in limits, sentence
        assert "`gh pr merge --auto --squash`" in limits
        assert "`strict_required_status_checks_policy`" in limits

    def test_step_4c5_carries_no_changelog_sync(self) -> None:
        section = _step_4c5()

        assert "changelog-sync" not in section
        assert "changelog_merge_freeze" not in section

    def test_release_playbook_defines_the_layers_and_the_residual(self) -> None:
        section = _playbook_section()

        assert "**At arm time (Layer B).**" in section
        assert "**At merge time (Layer C).**" in section
        assert "**What neither covers.**" in section
        assert section.index("**At arm time (Layer B).**") < section.index(_RESIDUAL[0])
        for sentence in _RESIDUAL:
            assert sentence in section, sentence
        assert "`cw worktree changelog-sync --base main`" in section
        assert "as #3279 did" in section

    def test_the_residual_sentences_match_in_runbook_and_playbook(self) -> None:
        runbook = _merge_policy_section()
        playbook = _playbook_section()
        for sentence in _RESIDUAL:
            assert (sentence in runbook, sentence in playbook) == (True, True)

    def test_readme_and_workflow_readme_rows(self) -> None:
        readme = _read("README.md")
        assert (
            "| `cw worktree changelog-sync --base <b>` | After a release cut, merge"
            " `origin/<b>` into HEAD and re-home the branch's CHANGELOG bullets under"
            " `[Unreleased]`; JSON plus exit 0 (clean or moved) / 1 (conflict, or"
            " refused for a rebuild refusal, a branch that already contains"
            " `origin/<b>`, an in-progress merge or a dirty tree: read `state`) / 3"
            " (unavailable); never fetches |"
        ) in readme
        assert readme.index("`cw worktree merge-probe") < readme.index(
            "`cw worktree changelog-sync --base <b>`"
        )
        workflows = _flat(_read(".github/workflows/README.md"))
        assert (
            "Lint, type check, workflow lint (actionlint), unit tests, integration"
            " tests, diff coverage, and on PRs a merge-time CHANGELOG freeze"
            " re-check against the latest base (#3190)"
        ) in workflows
