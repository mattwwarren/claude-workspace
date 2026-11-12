"""Shared test helpers for the ``cw.codex_review`` per-submodule test suite.

Cross-module doubles, payload builders, and git helpers used by two or more of
the split ``test_codex_review_*.py`` files (and by ``test_codex_fix_loop.py``).
This module has no ``test_`` prefix, so pytest does not collect it (same
convention as ``tests/conftest.py``); it is imported explicitly by the test
modules that use each helper.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from cw.auto_dev_result import Review
from cw.codex_fix_loop.baseline import CycleBaseline, cycle_touched_paths
from cw.codex_fix_loop.duplicate_reader import check_duplicate_reader
from cw.codex_review import _prepare_review_pass, run_review
from cw.codex_review._shard import plan_prompt_shards, split_diff_by_file
from cw.codex_runner import CodexRunResult, FakeCodexRunner
from cw.config import save_state
from cw.dev_queue import save_dev_queue
from cw.local_runner import make_blocked
from cw.models import CwState, DevQueueStore, QueueItemStatus, Stage, TicketTask
from cw.review_findings import AcceptedFinding, CapturedDiff, ReviewVerdict
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._shared.git_repos import add_bare_origin, commit_tracked_file, git_in
from tests._shared.review_builders import _make_diff
from tests._shared.session_seeding import _make_daemon_session, _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_fix_loop.fence import FenceBreach
    from cw.codex_fix_loop.growth import LoopGrowthBudget
    from cw.codex_review import _ReviewPassInputs
    from cw.codex_review._shard import _ShardPlan
    from cw.review_findings import Finding

# The redacted observed `gh issue view 2633 --json comments` payload (#2633).
_OBSERVED_COMMENTS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "gh_issue_comments_2633.json"
)


def _is_fix_argv(argv: list[str]) -> bool:
    """True when *argv* is a fix-cycle ``codex exec`` invocation (#1767).

    Keyed on flags, never on a sandbox value: a reviewer argv always carries
    ``--output-schema`` and a capability-probe argv always carries
    ``--skip-git-repo-check``, while the fix argv carries neither. A sandbox
    literal stops telling them apart once ``reviewer_sandbox_mode`` or
    ``fix_sandbox_mode`` leaves its default. A negative predicate: a future
    fourth builder would classify as a fix, which the discriminator pin in
    ``tests/test_codex_sandbox_argv_defaults.py`` exists to catch.
    """
    return "--output-schema" not in argv and "--skip-git-repo-check" not in argv


def _mk_codex_proc(
    stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """A ``codex --version`` :class:`subprocess.CompletedProcess` double.

    Shared by ``test_codex_executor.py``'s ``TestCodexCapabilityDiagnosis``
    (binary presence/version probe) and ``test_codex_capability.py``'s
    fingerprint tests (#1709) — the two probes are different subsystems but
    build the identical ``CompletedProcess`` shape, so the builder lives here
    rather than being copied a third time.
    """
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _finding_payload(
    *,
    severity: str = "MUST_FIX",
    file: str = "src/cw/foo.py",
    line_start: int | None = None,
    line_end: int | None = None,
    summary: str = "Bug here",
    consequence: str = "It breaks",
    suggested_fix: str = "Fix it",
    evidence: str = "def broken():",
    confidence: str = "HIGH",
) -> dict[str, object]:
    """Minimal valid Finding payload (JSON-dict shape).

    line_start/line_end default to None (a file-level finding, no line
    anchor) -- pass both when the caller's document must survive
    diff-based evidence validation (review_findings._classify_finding),
    which schema-only callers (_parse_reviewer_document, run_codex_roles)
    never reach.
    """
    payload: dict[str, object] = {
        "severity": severity,
        "file": file,
        "summary": summary,
        "consequence": consequence,
        "suggested_fix": suggested_fix,
        "evidence": evidence,
        "confidence": confidence,
    }
    if line_start is not None:
        payload["line_start"] = line_start
    if line_end is not None:
        payload["line_end"] = line_end
    return payload


def _doc_json(
    *,
    role: str = "Code Quality Reviewer",
    status: str = "ok",
    findings: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "reviewer_role": role,
            "status": status,
            "detail": "reviewed; no issues found.",
            "findings": findings if findings is not None else [],
        }
    )


def _ok_result(
    role: str = "Code Quality Reviewer",
    *,
    findings: list[dict[str, object]] | None = None,
    stdout: str = "",
) -> CodexRunResult:
    """A successful CodexRunResult whose ``-o`` file holds a valid document.

    *stdout* defaults to "" (no ``codex exec --json`` audit stream); pass a
    JSONL body to drive :func:`cw.codex_review._audit_events._parse_codex_audit_events`
    through the real ``_run_codex_role`` path (#1710).
    """
    return CodexRunResult(
        returncode=0,
        stdout=stdout,
        stderr="",
        output_file_content=_doc_json(role=role, findings=findings),
    )


class _SequencedRunner:
    """CodexRunner double returning queued results, recording each call."""

    def __init__(self, results: list[CodexRunResult]) -> None:
        self._results = results
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {"argv": list(argv), "timeout": timeout_seconds, "stdin": stdin}
        )
        return self._results[len(self.calls) - 1]


class _Clock:
    """Deterministic monotonic() stand-in stepping through *values*."""

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        value = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return value


def _shard_limit_inputs(prepared: _ReviewPassInputs) -> tuple[int, dict[str, int]]:
    """``(overhead, cost_by_file)`` of *prepared* for deriving a test limit (#2864).

    *overhead* is the longest role prompt over an empty diff; each file's cost
    is its diff chunk plus its file-list line, the planner's own accounting.
    Tests derive their prompt limit from these, never hard-code one.
    """
    empty = prepared.render_prompts(CapturedDiff(text=""), [])
    overhead = max(len(prompt) for prompt in empty.values())
    cost_by_file = {
        path: len(chunk) + len(path) + 1
        for path, chunk in split_diff_by_file(prepared.diff.text).items()
    }
    return overhead, cost_by_file


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _populate_global_agents_dir(path: Path, **role_to_content: str) -> None:
    """Write fake global agent-spec files into *path* (#1773).

    Keyword names are the agent-spec FILE stems under
    ``_REVIEWER_ROLE_AGENT_FILES`` with ``-`` written as ``_`` (a Python
    keyword cannot carry a dash), e.g. ``code_reviewer="SPEC"`` writes
    ``<path>/code-reviewer.md``. Lets ``_resolve_agent_spec``'s
    global-fallback cases populate a controlled directory without repeating
    the mkdir/write boilerplate.
    """
    for stem, content in role_to_content.items():
        _write(path / f"{stem.replace('_', '-')}.md", content)


def _task(*, scope_hint: str | None = "small") -> TicketTask:
    """REVIEW-stage task; ``scope_hint`` defaults to ``"small"`` (#2767).

    Most codex tests run over an origin-less ``make_git_repo`` worktree, whose
    diff cannot be measured, so the codex clean tail needs a tier source to
    complete. The hint is that source; pass ``scope_hint=None`` to exercise
    the ``scope_tier_unresolvable`` block.
    """
    return _make_ticket_task(
        ticket_id="T-1", client="test", stage=Stage.REVIEW, scope_hint=scope_hint
    )


def _seed_default_branch_config(repo: Path, files: dict[str, str]) -> Path:
    """Commit *files* on the current branch (``main``), then push to a bare origin.

    Puts review configuration (``.claude/review-extras.md`` and friends) on the
    default branch and fixes ``origin/main`` at seed time (#3038). The caller
    stays on ``main`` and branches afterwards: later feature-branch edits model
    "the branch edits its own review configuration", and later unpushed
    ``main`` commits model "local main ahead of origin/main". Not
    ``make_git_repo_with_origin``/``make_worktree_with_change``: both branch to
    ``feature`` before pushing, so ``origin/main`` would hold no config files.
    Returns the bare origin's path.
    """
    for relpath, content in files.items():
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git_in(repo, "add", relpath)
    git_in(repo, "commit", "--allow-empty", "-m", "seed default-branch review config")
    return add_bare_origin(repo)


# ---------------------------------------------------------------------------
# Sharded-review fixtures (#2864, hoisted for #2867): a four-file diff and a
# prompt limit derived from the pass, so the planner splits it into shards.
# ---------------------------------------------------------------------------

_SHARD_FILES = ("pkg_a/a1.py", "pkg_a/a2.py", "pkg_b/b1.py", "pkg_c/c1.py")
_CHANGED_FILES_HEADER = "## Changed Files\n"


def _stem_content(relpath: str, lines: int = 20) -> str:
    stem = Path(relpath).stem
    return "".join(f"{stem}_{n} = {n}\n" for n in range(lines))


def _large_diff_repo(
    make_git_repo: Callable[[str], Path], name: str, *, lines: int = 20
) -> Path:
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", "feature")
    for relpath in _SHARD_FILES:
        commit_tracked_file(repo, relpath, _stem_content(relpath, lines))
    return repo


class _Sharded(NamedTuple):
    prepared: _ReviewPassInputs
    limit: int
    plans: list[_ShardPlan]


def _sharded_setup(repo: Path, session_id: str) -> _Sharded:
    """Warm the capability cache and derive a limit that shards *repo*'s diff."""
    prepared = _prepare_review_pass(
        _task(),
        repo,
        "main",
        config_branch="main",
        runner=FakeCodexRunner(),
        session_id=session_id,
    )
    overhead, costs = _shard_limit_inputs(prepared)
    limit = overhead + 2 * max(costs.values())
    assert max(len(p) for p in prepared.prompts_by_role.values()) > limit
    assert limit >= overhead + max(costs.values())
    plans = plan_prompt_shards(prepared, limit)
    assert len(plans) > 1
    return _Sharded(prepared, limit, plans)


def _prompt_files(prompt: object) -> list[str]:
    section = str(prompt).split(_CHANGED_FILES_HEADER, 1)[1].split("\n\n", 1)[0]
    return section.splitlines()


def _run_limited(
    repo: Path,
    runner: _SequencedRunner,
    *,
    limit: int | None,
    session_id: str,
    budget: int | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    return run_review(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=budget,
        session_id=session_id,
        fix_loop_enabled=False,
        max_prompt_chars=limit,
    )


def _must_fix_in(path: str) -> list[dict[str, object]]:
    stem = Path(path).stem
    return [
        _finding_payload(
            severity="MUST_FIX",
            file=path,
            line_start=1,
            line_end=1,
            evidence=f"{stem}_0 = 0",
        )
    ]


def _all_ok(roles: list[str], shards: int) -> list[CodexRunResult]:
    return [_ok_result(role) for _shard in range(shards) for role in roles]


# A fix invocation that changes nothing, and a reviewer role run that fails
# outright (hoisted for #2877 from the #2880 coverage-gap tests).
_FIX_NOOP = CodexRunResult(returncode=0, stdout="", stderr="")
_FAILED_RUN = CodexRunResult(returncode=1, stdout="", stderr="boom")


class _EditingRunner:
    """CodexRunner double whose fix calls edit the worktree (#2877).

    A fix call (argv carries ``workspace-write``) runs the next of *fixes*
    against the worktree and succeeds; it never consumes *reviews*. Every other
    call pops the next scripted review result. ``calls`` records each call.
    """

    def __init__(
        self, reviews: list[CodexRunResult], fixes: list[Callable[[Path], None]]
    ) -> None:
        self._reviews = list(reviews)
        self._fixes = list(fixes)
        self.calls: list[dict[str, object]] = []
        self.fix_calls = 0

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {"argv": list(argv), "timeout": timeout_seconds, "stdin": stdin}
        )
        if "workspace-write" in argv:
            self._fixes[self.fix_calls](worktree)
            self.fix_calls += 1
            return _FIX_NOOP
        return self._reviews.pop(0)

    @property
    def review_calls(self) -> list[dict[str, object]]:
        """Every non-fix call, in order."""
        return [call for call in self.calls if "workspace-write" not in call["argv"]]


def _install_pre_commit_hook(repo: Path, script: str) -> None:
    """Install *script* as *repo*'s ``pre-commit`` hook, made executable.

    Used to simulate a repo-local hook (e.g. ruff-format) that rewrites files
    and exits non-zero on the run where it changes something — the scenario
    ``_commit_fix_cycle``'s retry-once exists to survive — or a lint hook that
    keeps failing (#2633's ``codex_fix_hook_failed``).
    """
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(script, encoding="utf-8")
    hook_path.chmod(0o755)


def exclude_cw_dir(repo: Path) -> None:
    """Git-exclude ``.cw/`` in *repo*, as a real cw worktree does.

    Mirrors ``worktree/_lifecycle.py::_CW_EXCLUDE_PATTERNS``: a worktree made
    by ``make_git_repo`` does not exclude ``.cw/``, so cw's own state would be
    staged by the cycle's ``git add -A`` (#2967).
    """
    exclude = repo / git_in(repo, "rev-parse", "--git-path", "info/exclude")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(".cw/\n")


def assert_clean_after_refusal(worktree: Path, start_sha: str) -> str:
    """Assert a refused cycle left a clean tree plus a pinned edit ref.

    Empty ``git status --porcelain``, ``HEAD`` still *start_sha*, and a commit
    pinned under ``refs/cw/refused-cycles/``. Returns that ref name. Git runs
    through ``tests/_git_sandbox``.
    """
    assert git_in(worktree, "status", "--porcelain") == ""
    assert git_in(worktree, "rev-parse", "HEAD") == start_sha
    refs = git_in(
        worktree, "for-each-ref", "--format=%(refname)", "refs/cw/refused-cycles/"
    ).split()
    assert refs, "no edit ref pinned under refs/cw/refused-cycles/"
    ref = refs[-1]
    assert git_in(worktree, "cat-file", "-t", ref) == "commit"
    return ref


def _stage_merge_from_other_branch(
    repo: Path, files: dict[str, str], *, squash: bool = False
) -> None:
    """Leave *repo* with an uncommitted merge of a branch carrying *files* (#2633).

    Creates ``other-staged`` from ``main`` with *files* committed, returns to
    the original branch, then runs ``git merge --no-commit --no-ff`` (default:
    ``MERGE_HEAD`` set, merge result staged) or ``git merge --squash``
    (``squash=True``: the same content staged, no ``MERGE_HEAD``). Callers
    pass files that do not conflict with the current branch.
    """
    branch = git_in(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git_in(repo, "checkout", "-b", "other-staged", "main")
    for rel_path, text in files.items():
        _write(repo / rel_path, text)
    git_in(repo, "add", *files)
    git_in(repo, "commit", "-m", "other branch work")
    git_in(repo, "checkout", branch)
    if squash:
        git_in(repo, "merge", "--squash", "other-staged")
    else:
        git_in(repo, "merge", "--no-commit", "--no-ff", "other-staged")


def _head_baseline(repo: Path) -> CycleBaseline:
    """A ``CycleBaseline`` built straight from *repo*'s ``HEAD`` (#2633).

    Deliberately not :func:`capture_cycle_baseline`, which refuses a tree a
    test has already edited or pre-staged.
    """
    return CycleBaseline(
        head_sha=git_in(repo, "rev-parse", "HEAD"),
        tree_sha=git_in(repo, "rev-parse", "HEAD^{tree}"),
    )


def _measured_from_head(repo: Path) -> frozenset[str]:
    """The paths a cycle touched since *repo*'s ``HEAD`` (stages them, #2633)."""
    return frozenset(cycle_touched_paths(repo, _head_baseline(repo)))


def _seed_conflicting_cherry_pick(repo: Path) -> None:
    """Leave *repo* mid-cherry-pick on a ``pyproject.toml`` conflict (#2633).

    Commits a base ``pyproject.toml`` on the current branch, edits it on a
    side branch, edits the same line differently on the current branch, then
    cherry-picks the side commit: three unmerged stages, ``CHERRY_PICK_HEAD``
    set and no ``MERGE_HEAD``.
    """
    branch = git_in(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _write(repo / "pyproject.toml", "version = 'base'\n")
    git_in(repo, "add", "pyproject.toml")
    git_in(repo, "commit", "-m", "base pyproject")
    git_in(repo, "checkout", "-b", "pick-side")
    _write(repo / "pyproject.toml", "version = 'side'\n")
    git_in(repo, "commit", "-am", "side pyproject")
    git_in(repo, "checkout", branch)
    _write(repo / "pyproject.toml", "version = 'branch'\n")
    git_in(repo, "commit", "-am", "branch pyproject")
    with pytest.raises(subprocess.CalledProcessError):
        git_in(repo, "cherry-pick", "pick-side")


def _observed_comments() -> list[dict[str, object]]:
    """The observed #2633 comment list, unwrapped as ``gh.fetch_issue_comments`` does.

    A capture of ``gh issue view 2633 --json comments`` with only values
    redacted (login, ids, urls) and the two agent-authored scan comments cut
    to their opening lines plus the marker-quoting and agent-marker lines.
    """
    payload = json.loads(_OBSERVED_COMMENTS_FIXTURE.read_text(encoding="utf-8"))
    comments: list[dict[str, object]] = payload["comments"]
    return comments


# ---------------------------------------------------------------------------
# Scripted fix-loop harness (#1837, promoted for #2816): drives the real
# ``run_review_with_fix_loop`` with cycle 0 and every re-review scripted.
# ---------------------------------------------------------------------------

FAKE_REREVIEW_REASON = "fake_rereview_result"
_SCRIPTED_TICKET = "1837"


def _producer_delta() -> CapturedDiff:
    """A delta touching only ``src/cw/producer.py``."""
    return _make_diff("def changed(x, y):", files={"src/cw/producer.py": [3]})


def _loop_verdict(*accepted: AcceptedFinding, **overrides: object) -> ReviewVerdict:
    """A verdict over *accepted*: blocking iff it carries a MUST_FIX."""
    must_fix = [af.finding for af in accepted if af.finding.severity == "MUST_FIX"]
    payload: dict[str, object] = {
        "blocking": bool(must_fix),
        "must_fix": must_fix,
        "reviewed_sha": "sha",
        "accepted": list(accepted),
        "review": Review(
            must_fix_initial=len(must_fix),
            should_fix=0,
            fix_cycles_used=0,
            deferred=0,
            agents_run=1,
        ),
    }
    payload.update(overrides)
    return ReviewVerdict.model_validate(payload)


def _apply_suppression(verdict: ReviewVerdict, kwargs: dict[str, Any]) -> ReviewVerdict:
    """Apply a forwarded ``settled_suppression`` the way synthesis does."""
    suppression = kwargs.get("settled_suppression")
    return verdict if suppression is None else suppression(verdict)


class _FakePrepared(NamedTuple):
    """Only the two fields the loop reads off ``_rereview``'s prepared inputs."""

    delta_diff: CapturedDiff | None
    delta_changed_files: frozenset[str] | None


class _FakeRunner:
    """Every codex invocation succeeds.

    By default a fix invocation changes nothing (a tolerated no-op cycle).
    With *edit*, each fix invocation writes a new file, so the cycle commits
    and moves ``HEAD``. *extra_writes* (``relpath -> content``) is written on
    every fix invocation too, after the *edit* file.
    """

    def __init__(
        self, *, edit: bool = False, extra_writes: Mapping[str, str] | None = None
    ) -> None:
        self._edit = edit
        self._extra_writes = dict(extra_writes or {})
        self.fix_calls = 0

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        if "workspace-write" in argv:
            self.fix_calls += 1
            if self._edit:
                path = worktree / f"fix_cycle_{self.fix_calls}.py"
                path.write_text(f"cycle = {self.fix_calls}\n", encoding="utf-8")
            for relpath, content in self._extra_writes.items():
                (worktree / relpath).write_text(content, encoding="utf-8")
        return CodexRunResult(returncode=0, stdout="", stderr="")


class _FakeRereview:
    """Replays a scripted list of per-cycle accepted-finding sets.

    *reviewed_sha* names each re-review's sha from its 0-based index; the
    default is ``sha<index + 1>``. A forwarded ``settled_suppression`` is
    applied to each scripted verdict, as synthesis applies it. A ``None``
    entry scripts an unparseable re-review (no verdict).
    """

    def __init__(
        self,
        cycles: list[list[AcceptedFinding] | None],
        worktree: Path,
        *,
        reviewed_sha: Callable[[int], str] | None = None,
    ) -> None:
        self._cycles = cycles
        self._worktree = worktree
        self._reviewed_sha = reviewed_sha or (lambda index: f"sha{index + 1}")
        self.previous_shas: list[str] = []
        self.prior_summaries: list[list[str]] = []
        self.suppression: list[object] = []
        self.calls = 0

    def __call__(
        self, **kwargs: Any
    ) -> tuple[Any, ReviewVerdict | None, _FakePrepared]:
        self.previous_shas.append(kwargs["previous_reviewed_sha"])
        self.prior_summaries.append([f.summary for f in kwargs["prior_open_findings"]])
        self.suppression.append(kwargs.get("settled_suppression"))
        index = min(self.calls, len(self._cycles) - 1)
        self.calls += 1
        scripted = self._cycles[index]
        result = make_blocked(
            ticket_id=_SCRIPTED_TICKET,
            worktree=self._worktree,
            reason=FAKE_REREVIEW_REASON,
            stage_reached="stage3_review",
        )
        prepared = _FakePrepared(_producer_delta(), frozenset({"src/cw/producer.py"}))
        if scripted is None:
            return result, None, prepared
        verdict = _loop_verdict(*scripted, reviewed_sha=self._reviewed_sha(index))
        return result, _apply_suppression(verdict, kwargs), prepared


def _drive_loop(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    cycle0: list[AcceptedFinding],
    cycles: list[list[AcceptedFinding] | None],
    *,
    cycle0_sha: str = "sha0",
    reviewed_sha: Callable[[int], str] | None = None,
    runner: _FakeRunner | None = None,
    task: TicketTask | None = None,
    fix_loop_enabled: bool = True,
    **loop_kwargs: Any,
) -> tuple[Any, ReviewVerdict | None, _FakeRereview]:
    """Run the real loop over a scripted cycle 0 and scripted re-reviews.

    *loop_kwargs* (``cycle_observer``, ``run_growth``, ``seed_findings``...)
    are forwarded to :func:`run_review_with_fix_loop` only when given.
    """
    from cw.codex_fix_loop import _driver, review_pass, run_review_with_fix_loop

    cycle0_verdict = _loop_verdict(*cycle0, reviewed_sha=cycle0_sha)
    cycle0_result = make_blocked(
        ticket_id=_SCRIPTED_TICKET,
        worktree=repo,
        reason=FAKE_REREVIEW_REASON,
        stage_reached="stage3_review",
    )
    monkeypatch.setattr(
        review_pass,
        "run_review",
        lambda **kwargs: (cycle0_result, _apply_suppression(cycle0_verdict, kwargs)),
    )
    fake = _FakeRereview(cycles, repo, reviewed_sha=reviewed_sha)
    monkeypatch.setattr(_driver, "_rereview", fake)

    result, verdict = run_review_with_fix_loop(
        runner=runner or _FakeRunner(),
        task=task or _task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id="s-convergence",
        fix_loop_enabled=fix_loop_enabled,
        **loop_kwargs,
    )
    return result, verdict, fake


_DUPLICATE_READER_NATIVE = (
    "def _read_roster_workers(roster):\n    return list(roster)\n"
)


def _duplicate_reader_repo(make_git_repo: Callable[..., Path]) -> Path:
    """A repo whose tracked source already holds one reader (#2667).

    ``src/native.py`` carries the existing ``_read_roster_workers`` that a
    duplication finding names; ``src/hold.py`` is the file a cycle edits.
    """
    repo = make_git_repo("duplicate-reader")
    _write(repo / "src" / "native.py", _DUPLICATE_READER_NATIVE)
    _write(repo / "src" / "hold.py", "HOLD = 1\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "base")
    return repo


def _check_duplicate_reader(
    repo: Path,
    findings: list[Finding],
    *,
    loop_budget: LoopGrowthBudget | None = None,
    cycle: int = 2,
) -> FenceBreach | None:
    """Stage the cycle against *repo*'s HEAD, then run the check (#2667)."""
    baseline = _head_baseline(repo)
    cycle_touched_paths(repo, baseline)
    return check_duplicate_reader(
        repo, baseline, findings, cycle=cycle, loop_budget=loop_budget
    )


def seed_codex_stage_run(
    *,
    worktree: Path,
    stage: Stage = Stage.REVIEW,
    client_name: str = "test",
    ticket_id: str = "T-1",
    session_id: str = "sess-1",
    lane: str | None = None,
    task_updates: Mapping[str, object] | None = None,
) -> None:
    """Seed a Session + a matching RUNNING dev-queue row at *stage*, plus clients.yaml.

    The ``cw codex run`` driver re-derives its run from exactly these. Callers
    still request ``tmp_config_dir`` (the isolation the writes land in).
    *task_updates* are applied to the row with ``model_copy`` (e.g. a PLAN
    flow stamping ``plan_approved_at`` / ``plan_approved_fingerprint``).
    """
    write_clients_yaml(ClientSpec(client_name, str(worktree)))
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id=session_id,
                    client=client_name,
                    worktree_path=worktree,
                )
            ]
        )
    )
    task = TicketTask(
        ticket_id=ticket_id,
        client=client_name,
        stage=stage,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
    )
    updates: dict[str, object] = dict(task_updates or {})
    if lane is not None:
        updates["lane"] = lane
    if updates:
        task = task.model_copy(update=updates)
    save_dev_queue(DevQueueStore(tasks=[task]))


# --- prep_pr_state stub (hoisted from test_codex_fix_loop_gates, #3061) ---------

_PREP_PR_STATE_STUB_TEMPLATE = '''\
"""prep_pr_state stub: understands gate-timeout."""
import json
import sys
import time

SIDE = __SIDE__
CALLS = __CALLS__
cfg = json.load(open(SIDE))
with open(CALLS, "a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "detect-gates":
    time.sleep(cfg["detect_delay"])
    raw = cfg["detect_raw"]
    print(raw if raw is not None else json.dumps(
        {"gates": cfg["gates"], "detected_from": []}))
    sys.exit(cfg["detect_exit"])
if sys.argv[1] == "gate-timeout":
    time.sleep(cfg["ceiling_delay"])
    raw = cfg["ceiling_raw"]
    print(raw if raw is not None else json.dumps(
        {"gate": sys.argv[2], "foreground_ceiling_s": cfg["ceilings"][sys.argv[2]]}))
    sys.exit(cfg["ceiling_exit"])
sys.exit(2)
'''


class PrepPrStateStub:
    """A fake ``prep_pr_state.py`` plus the call log it appends to."""

    def __init__(self, scripts_dir: Path, state: Path) -> None:
        self.script = scripts_dir / "prep_pr_state.py"
        self.calls_path = state / "calls.log"
        self.side = state / "stub.json"
        self.config: dict[str, object] = {
            "gates": [],
            "ceilings": {},
            "detect_delay": 0,
            "ceiling_delay": 0,
            "detect_raw": None,
            "detect_exit": 0,
            "ceiling_raw": None,
            "ceiling_exit": 0,
        }
        scripts_dir.mkdir(parents=True, exist_ok=True)
        text = _PREP_PR_STATE_STUB_TEMPLATE.replace(
            "__SIDE__", repr(str(self.side))
        ).replace("__CALLS__", repr(str(self.calls_path)))
        self.script.write_text(text, encoding="utf-8")
        self.set()

    def set(self, **overrides: object) -> None:
        self.config.update(overrides)
        self.side.write_text(json.dumps(self.config), encoding="utf-8")

    def gates(self, *entries: tuple[str, str, float]) -> None:
        self.set(
            gates=[{"name": n, "command": c} for n, c, _ in entries],
            ceilings={n: ceiling for n, _, ceiling in entries},
        )

    def calls(self) -> list[str]:
        if not self.calls_path.exists():
            return []
        return self.calls_path.read_text(encoding="utf-8").splitlines()


def install_home_prep_pr_state(
    state_dir: Path, *gates: tuple[str, str, float]
) -> PrepPrStateStub:
    """Install a ``prep_pr_state.py`` stub under the per-test HOME with *gates*.

    The IMPL executor's quality gates resolve the script repo-local first, then
    ``~/.claude/scripts``; a worktree-local stub would be an untracked file.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    stub = PrepPrStateStub(Path.home() / ".claude" / "scripts", state_dir)
    stub.gates(*gates)
    return stub


def install_home_scope_script(
    exit_code: int, stdout: str, stderr: str, version: int = 3
) -> Path:
    """Write a stub ``check_plan_scope_conformance.py`` under the per-test HOME.

    It ignores its arguments and prints *stdout* / *stderr* then exits with
    *exit_code*. The ``# cw-script-version: N`` marker is the second line, as in
    the real script (the first five lines are read).
    """
    path = Path.home() / ".claude" / "scripts" / "check_plan_scope_conformance.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        f"# cw-script-version: {version}\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return path


def fresh_impl_repo(
    make_git_repo: Callable[..., Path], name: str, branch: str
) -> tuple[Path, Path]:
    """A fresh-row IMPL worktree on *branch*, pushed to a bare origin.

    Adds no commit beyond ``main``, so ``check_existing_branch_work`` does not
    fire (``make_git_repo_with_origin`` commits ``feature.py``, which a fresh
    row would read as prior work). Returns ``(worktree, origin)``.
    """
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", branch)
    origin = add_bare_origin(repo)
    return repo, origin


def seed_codex_impl_run(
    *,
    worktree: Path,
    ticket_id: str,
    session_id: str,
    task_updates: Mapping[str, object] | None = None,
) -> None:
    """Seed an IMPL session and RUNNING row, and git-exclude ``.cw/``.

    Builds no repo: a fresh row gets its worktree from :func:`fresh_impl_repo`
    and a repair row from the ``make_git_repo_with_origin`` fixture.
    """
    seed_codex_stage_run(
        worktree=worktree,
        stage=Stage.IMPL,
        ticket_id=ticket_id,
        session_id=session_id,
        task_updates=task_updates,
    )
    exclude_cw_dir(worktree)


def fresh_finalize_repo(
    make_git_repo: Callable[..., Path],
    name: str,
    branch: str,
    *,
    unpushed_commits: int = 0,
) -> tuple[Path, Path]:
    """A FINALIZE worktree on *branch*: one implementation commit, pushed.

    ``main`` and *branch* sit on a bare origin, so the branch is a real
    non-empty one (the review-complete state a FINALIZE row starts from).
    *unpushed_commits* adds that many commits after the push, leaving the
    branch ahead of ``origin/<branch>`` for the ahead-push path. Returns
    ``(worktree, origin)``.
    """
    repo = make_git_repo(name)
    git_in(repo, "checkout", "-b", branch)
    commit_tracked_file(repo, "feature.py")
    origin = add_bare_origin(repo)
    for index in range(unpushed_commits):
        commit_tracked_file(repo, f"ahead_{index}.py")
    return repo, origin


def seed_codex_finalize_run(
    *,
    worktree: Path,
    ticket_id: str,
    session_id: str,
    task_updates: Mapping[str, object] | None = None,
) -> None:
    """Seed a FINALIZE session and RUNNING row, and git-exclude ``.cw/``.

    Builds no repo: pair it with :func:`fresh_finalize_repo`.
    """
    seed_codex_stage_run(
        worktree=worktree,
        stage=Stage.FINALIZE,
        ticket_id=ticket_id,
        session_id=session_id,
        task_updates=task_updates,
    )
    exclude_cw_dir(worktree)
