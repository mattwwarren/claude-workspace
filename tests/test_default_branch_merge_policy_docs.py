"""Doc conformance for the default-branch merge policy (#2979, ADR-0022).

Three rules: a branch that is merely behind the default branch is never acted
on; ``origin/<default>`` is merged at most once and only on a conflict; a clean
merge never causes a review. These tests pin the prose every site carries:
finalize Step 4c.5, the probe sites (each fetches before it probes), the probe
contract defined once in the runbook, the Step 4.0 re-check pin, the Step 4.0b
rule, ``review-monitor``, the runbook and ADR-0022.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    _appendix,
    _clean_git_env,
    _cmd,
    substitute_fence_placeholders,
)
from tests.test_auto_dev_preflight_resolutions import _after

_ROOT = Path(__file__).resolve().parent.parent
_PROBE = "cw worktree merge-probe"


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


def _review_fallback_fence() -> str:
    appendix = _appendix("review")
    section = _after(
        appendix,
        "## Fallback — direct execution from the main session's worktree",
        3000,
    )
    return _between(section, "# From the main session's worktree", "```")


_STUB_CW = """#!/bin/sh
printf '%s' "$STUB_PROBE_STDOUT"
printf '%s' "$STUB_PROBE_STDERR" >&2
exit "$STUB_PROBE_RC"
"""

# Logs every invocation; the fence's ahead-count must read 0 so the push
# branch is skipped, everything else (fetch, checkout, merge, push) succeeds.
_STUB_GIT = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_GIT_LOG"
if [ "$1" = "rev-list" ]; then
  echo 0
fi
exit 0
"""


def _run_pre_pr_sync_fence(
    tmp_path: Path, *, stdout: str, stderr: str, rc: int
) -> list[str]:
    """Run the finalize pre-PR sync fence under bash with stub ``cw`` and ``git``.

    Returns the git invocations (one ``argv`` string each) the fence made.
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
    env = {
        **_clean_git_env(),
        "HOME": str(home),
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "STUB_GIT_LOG": str(git_log),
        "STUB_PROBE_STDOUT": stdout,
        "STUB_PROBE_STDERR": stderr,
        "STUB_PROBE_RC": str(rc),
    }
    fence = substitute_fence_placeholders(
        _pre_pr_sync_fence(), {"branch-name": "dev/2979"}
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
    return git_log.read_text(encoding="utf-8").splitlines()


class TestPrePrSyncFenceBehaviour:
    """The merge guard is executed, not just grepped (#2979)."""

    _MERGE = "merge origin/main --no-edit"

    def test_a_crashed_probe_exiting_1_never_merges(self, tmp_path: Path) -> None:
        git_calls = _run_pre_pr_sync_fence(
            tmp_path,
            stdout="",
            stderr="Traceback (most recent call last):\n  boom\n",
            rc=1,
        )
        assert not any(call.startswith("merge") for call in git_calls)

    def test_exit_1_with_a_conflict_state_merges_exactly_once(
        self, tmp_path: Path
    ) -> None:
        git_calls = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"conflict"}', stderr="", rc=1
        )
        assert git_calls.count(self._MERGE) == 1

    def test_a_clean_probe_does_not_merge(self, tmp_path: Path) -> None:
        git_calls = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"clean"}', stderr="", rc=0
        )
        assert not any(call.startswith("merge") for call in git_calls)

    def test_an_unavailable_probe_does_not_merge(self, tmp_path: Path) -> None:
        git_calls = _run_pre_pr_sync_fence(
            tmp_path, stdout='{"state":"unavailable"}', stderr="", rc=3
        )
        assert not any(call.startswith("merge") for call in git_calls)


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
        ],
    )
    def test_a_failed_fetch_means_no_probe(self, section: object) -> None:
        assert callable(section)
        text = str(section())
        assert "If the fetch fails, do NOT probe" in text
        assert "`merge_probe_unavailable`" in text

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

    def test_step_40b_has_no_overlap_or_delta_text(self) -> None:
        section = _step_40b()
        for retired in (
            "overlaps branch files",
            "delta confirm",
            "Delta confirm",
            "delta-confirm",
            "`merge_gate_ignore_paths`",
            "apart from `CHANGELOG.md`",
            "touches a file of the branch's own diff",
        ):
            assert retired not in section, retired
        assert "git itself would not write" in section
        assert "#2980" in section

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
            "`behind_default_no_action`",
            "`merge_probe_unavailable`",
            "**Diagnosing a tip that reads stale despite a clean merge.**",
            "`not_textually_clean <sha7>`",
            "**Accepted limitations.**",
            "Strict-protection repos without a merge queue",
            "False conflicts are fail-closed",
        ):
            assert needle in section, needle

    def test_the_branch_behind_main_bullet_no_longer_says_rebase(self) -> None:
        runbook = _read("docs/dispatch-runbook.md")
        bullet = _after(runbook, "- `branch_behind_main`:", span=400).split("\n- ")[0]
        assert "rebase, then" not in bullet
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


class TestAdr:
    def test_adr_0022_exists_and_is_indexed(self) -> None:
        adr = _read(
            "docs/adr/0022-a-clean-default-branch-merge-never-causes-a-review.md"
        )
        assert "**Status:** Accepted" in adr
        decision = " ".join(adr.split())
        assert "carries a conflict resolution is reviewed by a normal full round" in (
            decision
        )
        index = _read("docs/adr/README.md")
        assert (
            "[0022](0022-a-clean-default-branch-merge-never-causes-a-review.md)"
            in index
        )
