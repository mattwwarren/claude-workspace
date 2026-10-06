#!/usr/bin/env python3
"""
Post-ship validation: verify a PR was actually created and emit a structured
ship summary.

Used by /prep-pr (Step 8/10) and /auto-dev (Step 4c) to enforce that the project
ship-it command actually produced a PR. Without this, Claude can claim "shipped"
while having skipped /ship-it entirely.

Subcommands:
  verify     Run all checks, exit non-zero if any required check fails.
             Emits a markdown ship summary to stdout, or JSON with --json.

Checks (all required unless flagged optional):
  - Current branch is not main/master
  - Branch is pushed to origin and origin SHA matches local HEAD
  - PR exists for this branch (gh pr view succeeds)
  - PR head SHA matches local HEAD
  - Auto-merge is enabled, or the PR is already MERGED (optional,
    --require-automerge; downgraded to optional regardless when
    .claude/project-config.yaml sets pr.auto_merge: false — see
    check-automerge-allowed below)
  - Monitor is registered (optional, --require-monitor)

Subcommands:
  verify                   Run all checks above, exit non-zero if any
                            required check fails.
  arm-automerge            Arm `gh pr merge --auto --squash` pinned to a
                            verified `--head-sha` (`--match-head-commit`)
                            with a bounded retry, reading `autoMergeRequest`
                            back instead of trusting gh's exit code (#2576,
                            #2581). Prints one JSON result object; consults
                            the pr.auto_merge seam itself and makes no gh
                            call when it is false or undeterminable.
  check-automerge-allowed  Print "true"/"false" for whether
                            .claude/project-config.yaml's pr.auto_merge
                            permits `gh pr merge --auto`: exit 0 allowed, 1
                            explicit pr.auto_merge: false, 2 undeterminable
                            (fail closed, #2581). The shared seam every
                            markdown arm site (ship-it.md,
                            auto-dev-finalize.md, review-monitor.md,
                            cw-session-watch/SKILL.md) shells out to
                            before arming auto-merge (#2046).

Exit codes:
  0  all checks passed (arm-automerge: armed, or the PR is already MERGED)
  1  a required check failed (arm-automerge: arming failed after all retries)
  2  invocation error (not in a git repo, gh missing, etc.); also, for
     check-automerge-allowed and arm-automerge, an existing
     .claude/project-config.yaml whose pr.auto_merge cannot be determined
     (fail closed, #2581)
  3  arm-automerge only: refused because pr.auto_merge is false (no gh call)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

sys.path.insert(0, str(Path(__file__).parent))

from utils.runtime_paths import review_monitor_script_path

try:
    yaml: ModuleType | None = import_module("yaml")
except ImportError:  # pragma: no cover - downstream repo without PyYAML
    yaml = None


def _load_project_config_module() -> ModuleType | None:
    """Load the shared config reader from source or an installed package."""
    source = Path(__file__).resolve().parents[2] / "src" / "cw" / "project_config.py"
    if source.exists():
        spec = importlib.util.spec_from_file_location("cw_project_config", source)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except ImportError:
                pass
            else:
                return module
    try:
        return import_module("cw.project_config")
    except ImportError:
        return None


_project_config = _load_project_config_module()


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROTECTED_BRANCHES = {"main", "master"}
MONITOR_SCRIPT = review_monitor_script_path()
PROJECT_CONFIG_PATH = Path(".claude") / "project-config.yaml"
# gh `state` of a merged PR. Kept local (not imported from cw.gh) because this
# script runs under the shebang interpreter, where `cw` is not importable; a
# test pins it to cw.gh._GH_PR_STATE_MERGED so the two cannot drift.
PR_STATE_MERGED = "MERGED"
# arm-automerge (#2576): 4 attempts with linear 3s/6s/9s backoff (18s worst case).
ARM_MAX_ATTEMPTS = 4
ARM_BACKOFF_SECONDS = 3.0
GH_STDERR_LIMIT = 1000
EXIT_ARM_DISALLOWED = 3
ARM_STATUS_ARMED = "armed"
ARM_STATUS_MERGED = "merged"
ARM_STATUS_FAILED = "failed"
ARM_STATUS_SKIPPED = "skipped"
# arm-automerge --head-sha (#2581): a full SHA only, never a branch or prefix.
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


# --- Data Models ---


@dataclass
class CheckResult:
    """Result of a single validation check."""

    name: str
    passed: bool
    detail: str = ""
    required: bool = True


@dataclass
class ShipSummary:
    """Structured ship summary."""

    status: str = "unknown"  # ok | failed
    branch: str = ""
    head_sha: str = ""
    origin_sha: str = ""
    pr_number: int | None = None
    pr_url: str = ""
    pr_head_sha: str = ""
    pr_state: str = ""
    pr_title: str = ""
    automerge_enabled: bool = False
    automerge_method: str = ""
    monitor_registered: bool = False
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ArmResult:
    """Structured result of an arm-automerge run (#2576)."""

    pr_number: int
    max_attempts: int
    status: str = ARM_STATUS_FAILED  # armed | merged | failed | skipped
    attempts: int = 0  # `gh pr merge` calls actually made
    pr_state: str = ""
    gh_exit_code: int | None = None
    gh_stderr: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# --- Shell helpers ---


def run(
    cmd: list[str],
    check: bool = False,
    capture: bool = True,
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command. Default: capture, do not raise, inherit cwd."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        cwd=cwd,
    )


def git(*args: str) -> str:
    """Run a git command, return stripped stdout. Returns empty string on failure."""
    result = run(["git", *args])
    return result.stdout.strip() if result.returncode == 0 else ""


# --- Checks ---


def detect_branch() -> str:
    branch = git("branch", "--show-current")
    if not branch:
        sys.stderr.write("ERROR: not on a branch (detached HEAD?)\n")
        sys.exit(2)
    return branch


def check_not_protected(branch: str, _summary: ShipSummary) -> CheckResult:
    if branch in PROTECTED_BRANCHES:
        return CheckResult(
            name="not-protected-branch",
            passed=False,
            detail=(
                f"on protected branch '{branch}'"
                " — finalize must run from a feature branch"
            ),
        )
    return CheckResult(name="not-protected-branch", passed=True, detail=branch)


def check_branch_pushed(branch: str, summary: ShipSummary) -> CheckResult:
    summary.head_sha = git("rev-parse", "HEAD")
    summary.origin_sha = git("rev-parse", f"origin/{branch}")

    if not summary.origin_sha:
        return CheckResult(
            name="branch-pushed",
            passed=False,
            detail=f"origin/{branch} does not exist — branch was never pushed",
        )
    if summary.origin_sha != summary.head_sha:
        return CheckResult(
            name="branch-pushed",
            passed=False,
            detail=(
                f"origin/{branch} SHA ({summary.origin_sha[:8]}) does not match "
                f"local HEAD ({summary.head_sha[:8]}) — push is stale"
            ),
        )
    return CheckResult(name="branch-pushed", passed=True, detail=summary.head_sha[:8])


def check_pr_exists(_branch: str, summary: ShipSummary) -> CheckResult:
    fields = "number,url,headRefOid,state,title,autoMergeRequest"
    result = run(["gh", "pr", "view", "--json", fields])
    if result.returncode != 0:
        return CheckResult(
            name="pr-exists",
            passed=False,
            detail=(
                "no PR found for current branch — `gh pr view` failed "
                f"(stderr: {result.stderr.strip()[:200]})"
            ),
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return CheckResult(
            name="pr-exists",
            passed=False,
            detail=f"could not parse `gh pr view` output: {e}",
        )

    summary.pr_number = data.get("number")
    summary.pr_url = data.get("url", "")
    summary.pr_head_sha = data.get("headRefOid", "")
    summary.pr_state = data.get("state", "")
    summary.pr_title = data.get("title", "")
    auto_merge = data.get("autoMergeRequest")
    if auto_merge:
        summary.automerge_enabled = True
        summary.automerge_method = auto_merge.get("mergeMethod", "")

    return CheckResult(name="pr-exists", passed=True, detail=f"#{summary.pr_number}")


def check_pr_sha_matches(summary: ShipSummary) -> CheckResult:
    if not summary.pr_head_sha:
        return CheckResult(
            name="pr-sha-matches",
            passed=False,
            detail="PR head SHA missing from response",
        )
    if summary.pr_head_sha != summary.head_sha:
        return CheckResult(
            name="pr-sha-matches",
            passed=False,
            detail=(
                f"PR head ({summary.pr_head_sha[:8]}) does not match "
                f"local HEAD ({summary.head_sha[:8]}) — push and PR are out of sync"
            ),
        )
    return CheckResult(
        name="pr-sha-matches", passed=True, detail=summary.pr_head_sha[:8]
    )


def resolve_project_config_auto_merge(
    config_path: Path = PROJECT_CONFIG_PATH,
) -> bool | None:
    """Read pr.auto_merge from .claude/project-config.yaml, or None on any failure.

    Mirrors cw.tracker.load_project_config_dict's safe-degrade shape (absent
    file, unparseable YAML, non-dict root, absent/non-bool key -> None) via
    the shared project-config reader. If neither the checked-out source tree
    nor an installed cw package is available, `pr.auto_merge` is read
    directly from `config_path` (PyYAML is already confirmed importable at
    that point) rather than silently treated as unknown (#2373, follow-up to
    #2046).
    """
    if yaml is None:
        return None
    project_config = _project_config or _load_project_config_module()
    root = config_path.parent.parent
    if project_config is not None and root / PROJECT_CONFIG_PATH == config_path:
        raw = project_config.load_project_config_dict(root, yaml_module=yaml)
    elif config_path.exists():
        # Shared cw.project_config module unavailable (neither the source
        # tree nor an installed cw package loaded), or config_path doesn't
        # have the shape the shared reader's root-based lookup assumes
        # (#2373, follow-up to #2046's fail-open). PyYAML is confirmed
        # importable at this point (see the `yaml is None` guard above), so
        # read config_path directly instead of silently reporting "unknown".
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
    else:
        return None
    if not isinstance(raw, dict):
        return None
    pr_block = raw.get("pr")
    if not isinstance(pr_block, dict):
        return None
    auto_merge = pr_block.get("auto_merge")
    return auto_merge if isinstance(auto_merge, bool) else None


AUTOMERGE_KEY = "auto_merge"
AUTOMERGE_SECTION = "pr"
# One canonical line shape, read without PyYAML. fullmatch only, so `true#x`
# (no space before `#`) is not a trailing comment and refuses.
_TRAILING_COMMENT = r"(?:[ ]+#.*)?[ ]*"
_AUTOMERGE_LINE_RE = re.compile(
    rf"[ ]+{AUTOMERGE_KEY}:[ ]+"
    rf"(?P<value>true|True|TRUE|false|False|FALSE){_TRAILING_COMMENT}"
)
_PR_SECTION_RE = re.compile(rf"{AUTOMERGE_SECTION}:{_TRAILING_COMMENT}")
NO_YAML_REASON = "PyYAML unavailable and pr.auto_merge could not be read without it"
UNDETERMINABLE_TEMPLATE = (
    "cannot determine pr.auto_merge ({reason}); "
    "refusing to arm auto-merge (fail closed, #2046/#2581)"
)


@dataclass(frozen=True)
class AutomergeGate:
    """Verdict of the pr.auto_merge seam; `reason` is set only when undeterminable."""

    allowed: bool
    reason: str | None


_GATE_ALLOWED = AutomergeGate(allowed=True, reason=None)


def _gate_refused(reason: str) -> AutomergeGate:
    return AutomergeGate(allowed=False, reason=reason)


def _gate_from_pr_block(
    pr_block: dict[object, object], config_path: Path
) -> AutomergeGate:
    """Absent key allows; a bool decides; anything else refuses."""
    if AUTOMERGE_KEY not in pr_block:
        return _GATE_ALLOWED
    value = pr_block[AUTOMERGE_KEY]
    if isinstance(value, bool):
        return AutomergeGate(allowed=value, reason=None)
    return _gate_refused(f"pr.auto_merge in {config_path} is not a boolean: {value!r}")


def _gate_from_parsed(raw: object, config_path: Path) -> AutomergeGate:
    """Gate verdict for a parsed YAML document (None is an empty file)."""
    if raw is None:
        return _GATE_ALLOWED
    if not isinstance(raw, dict):
        return _gate_refused(f"{config_path} is not a YAML mapping")
    pr_block = raw.get(AUTOMERGE_SECTION)
    if pr_block is None:
        return _GATE_ALLOWED
    if not isinstance(pr_block, dict):
        return _gate_refused(f"{AUTOMERGE_SECTION} in {config_path} is not a mapping")
    return _gate_from_pr_block(pr_block, config_path)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parent_is_pr_section(lines: list[str], index: int) -> bool:
    """True when the nearest less-indented line above ``index`` is ``pr:``."""
    indent = _indent(lines[index])
    for line in reversed(lines[:index]):
        if _indent(line) < indent:
            return _PR_SECTION_RE.fullmatch(line) is not None
    return False


def _scan_automerge_gate(text: str) -> AutomergeGate:
    """Read pr.auto_merge without PyYAML; any non-canonical shape refuses."""
    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    hits = [index for index, line in enumerate(lines) if AUTOMERGE_KEY in line]
    if not hits:
        return _GATE_ALLOWED
    match = _AUTOMERGE_LINE_RE.fullmatch(lines[hits[0]]) if len(hits) == 1 else None
    if match is None or not _parent_is_pr_section(lines, hits[0]):
        return _gate_refused(NO_YAML_REASON)
    return AutomergeGate(allowed=match["value"].lower() == "true", reason=None)


def read_automerge_gate(config_path: Path = PROJECT_CONFIG_PATH) -> AutomergeGate:
    """Fail-closed pr.auto_merge verdict for `config_path` (#2581).

    A missing file allows. Any other read failure, unparseable YAML or
    wrong-shaped value refuses with a single-line reason. Without PyYAML a
    dependency-free scan reads only the canonical `pr:` / `auto_merge:` shape.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return _GATE_ALLOWED
    except (OSError, ValueError) as err:
        return _gate_refused(f"cannot read {config_path}: {err}")
    if yaml is None:
        return _scan_automerge_gate(text)
    try:
        raw = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError):
        return _gate_refused(f"invalid YAML in {config_path}")
    return _gate_from_parsed(raw, config_path)


def automerge_allowed(config_path: Path = PROJECT_CONFIG_PATH) -> bool:
    """True unless pr.auto_merge is explicitly false or cannot be determined.

    Fail closed for an existing config (#2581); a missing config file is
    allowed. Thin wrapper over read_automerge_gate; the markdown arm sites
    reach the same verdict through check-automerge-allowed (#2046).
    """
    return read_automerge_gate(config_path).allowed


def resolve_effective_automerge_required(base_required: bool) -> bool:
    """Downgrade a caller's --require-automerge when pr.auto_merge is false.

    pr.auto_merge: false in .claude/project-config.yaml means this repo has
    declared it cannot rely on `gh pr merge --auto` (commonly: no branch
    protection to gate a pending merge on, so the command merges
    immediately instead of arming one). Treating the resulting
    automerge-enabled failure as required led headless /prep-pr Step 9 to
    retry `gh pr merge --auto`, forcing an unreviewed, CI-unconfirmed merge
    (#2046). Any other config state (absent file/key, auto_merge: true,
    unparseable YAML, no PyYAML) leaves the caller's own flag untouched.
    It deliberately does not use the fail-closed read_automerge_gate: verify
    must never downgrade --require-automerge on a config it cannot read
    (#2581).
    """
    if not base_required:
        return False
    return resolve_project_config_auto_merge() is not False


def check_automerge(summary: ShipSummary, required: bool) -> CheckResult:
    """Pass when auto-merge is armed, or the PR is already MERGED.

    A synchronous squash-merge flow (a repo with no required status checks)
    leaves the PR MERGED with `autoMergeRequest` null; a merged PR satisfies
    the intent of the check. A CLOSED-unmerged or un-armed OPEN PR still
    fails (#2163).
    """
    if summary.automerge_enabled:
        return CheckResult(
            name="automerge-enabled",
            passed=True,
            detail=summary.automerge_method or "enabled",
            required=required,
        )
    if summary.pr_state == PR_STATE_MERGED:
        return CheckResult(
            name="automerge-enabled",
            passed=True,
            detail="PR already merged — auto-merge not needed",
            required=required,
        )
    return CheckResult(
        name="automerge-enabled",
        passed=False,
        detail="auto-merge is not enabled on the PR",
        required=required,
    )


def check_monitor_registered(summary: ShipSummary, required: bool) -> CheckResult:
    if not MONITOR_SCRIPT.exists():
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=f"{MONITOR_SCRIPT} not found",
            required=required,
        )
    if summary.pr_number is None:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail="no PR number to check monitor against",
            required=required,
        )
    repo_result = run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    repo = repo_result.stdout.strip()
    if repo_result.returncode != 0 or not repo:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=f"could not resolve repo via gh: {repo_result.stderr.strip()[:200]}",
            required=required,
        )
    try:
        result = run(
            [sys.executable, str(MONITOR_SCRIPT), "status", "--repo", repo, "--json"]
        )
    except OSError as e:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=f"could not invoke review_monitor.py: {e}",
            required=required,
        )
    if result.returncode != 0:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=(
                f"review_monitor.py status returned {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            ),
            required=required,
        )
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=f"could not parse review_monitor.py status output: {e}",
            required=required,
        )
    monitored = state.get("monitored", {}) if isinstance(state, dict) else {}
    key = f"{repo}#{summary.pr_number}"
    if key not in monitored:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=f"PR {key} not found in monitored state",
            required=required,
        )
    summary.monitor_registered = True
    return CheckResult(
        name="monitor-registered",
        passed=True,
        detail="registered",
        required=required,
    )


def _read_back_automerge(
    pr_number: int, *, cwd: Path | None = None
) -> tuple[str, bool, str]:
    """Read `state` and `autoMergeRequest` back from gh, run in ``cwd``.

    Returns ``(state, armed, error)``. Any read failure (non-zero exit, bad
    JSON, non-object payload) is reported as not armed with ``error`` set and
    ``state`` empty, so the caller treats it like any other un-armed read.
    """
    result = run(
        ["gh", "pr", "view", str(pr_number), "--json", "state,autoMergeRequest"],
        cwd=cwd,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or f"gh pr view exited {result.returncode}"
        return "", False, error
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return "", False, f"could not parse gh pr view output: {e}"
    if not isinstance(data, dict):
        return "", False, "gh pr view output was not a JSON object"
    state = data.get("state")
    return (
        state if isinstance(state, str) else "",
        bool(data.get("autoMergeRequest")),
        "",
    )


def _poll_armed(result: ArmResult, *, cwd: Path | None = None) -> tuple[bool, str]:
    """Read back the PR; record success on `result` when armed or MERGED.

    Returns ``(done, read_error)``. A non-null `autoMergeRequest` or a MERGED
    state is success regardless of how the preceding `gh pr merge` exited.
    """
    state, armed, error = _read_back_automerge(result.pr_number, cwd=cwd)
    if state:
        result.pr_state = state
    if state == PR_STATE_MERGED:
        result.status = ARM_STATUS_MERGED
        result.detail = "PR already merged -- auto-merge not needed"
        return True, ""
    if armed:
        result.status = ARM_STATUS_ARMED
        result.detail = "autoMergeRequest read back non-null"
        return True, ""
    return False, error


def _finish_failed(result: ArmResult, read_error: str) -> ArmResult:
    """Fill in the failure detail once every attempt has been spent."""
    result.status = ARM_STATUS_FAILED
    if result.gh_exit_code == 0:
        if read_error:
            result.gh_stderr = read_error[:GH_STDERR_LIMIT]
            result.detail = (
                "gh pr merge exited 0 but the autoMergeRequest read-back failed"
            )
        else:
            result.detail = (
                "gh pr merge exited 0 but autoMergeRequest read back null (#1140)"
            )
    else:
        result.detail = f"gh pr merge --auto exited {result.gh_exit_code}"
        if read_error:
            result.detail += f"; read-back failed: {read_error}"
    return result


def arm_automerge(
    pr_number: int,
    *,
    head_sha: str,
    attempts: int = ARM_MAX_ATTEMPTS,
    backoff_seconds: float = ARM_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    cwd: Path | None = None,
) -> ArmResult:
    """Arm auto-merge with bounded retry, trusting the read-back over gh's exit.

    Each iteration reads the PR first, so an already-armed (or MERGED) PR is a
    no-op success and a retry after a stale read never re-arms needlessly.
    After a `gh pr merge --auto --squash` it reads back again: a non-null
    `autoMergeRequest` or MERGED state is success even when gh exited
    non-zero ("already queued"), while exit 0 with a null read-back is a
    failure to retry (#1140). Sleeps ``backoff_seconds * n`` after the n-th
    failed attempt, never after the last one. Every gh call runs in ``cwd`` (the
    process cwd when ``None``) so gh acts on the repo the caller named.

    The arm is pinned to ``head_sha`` with ``--match-head-commit``, tying it
    to the verified SHA at arm time (closing the verify-to-arm window). The
    pin applies only to arms this call issues: an already-armed or MERGED PR
    is a no-op success and is not re-pinned.
    """
    result = ArmResult(pr_number=pr_number, max_attempts=attempts)
    merge_cmd = [
        "gh",
        "pr",
        "merge",
        str(pr_number),
        "--auto",
        "--squash",
        "--match-head-commit",
        head_sha,
    ]
    while True:
        done, read_error = _poll_armed(result, cwd=cwd)
        if done:
            return result
        if result.attempts >= attempts:
            return _finish_failed(result, read_error)
        merge = run(merge_cmd, cwd=cwd)
        result.attempts += 1
        result.gh_exit_code = merge.returncode
        result.gh_stderr = merge.stderr.strip()[:GH_STDERR_LIMIT]
        done, read_error = _poll_armed(result, cwd=cwd)
        if done:
            return result
        if result.attempts < attempts:
            sleep(backoff_seconds * result.attempts)


def collect_diff_stats(summary: ShipSummary, base: str) -> None:
    """Best-effort diff metrics vs base branch. Non-fatal if base is unknown."""
    base_ref = f"origin/{base}"
    merge_base = git("merge-base", base_ref, "HEAD")
    if not merge_base:
        summary.warnings.append(f"could not compute merge-base against {base_ref}")
        return
    numstat = git("diff", "--numstat", f"{merge_base}...HEAD")
    if not numstat:
        return
    files = 0
    adds = 0
    dels = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        try:
            adds += int(parts[0]) if parts[0] != "-" else 0
            dels += int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            continue
    summary.files_changed = files
    summary.additions = adds
    summary.deletions = dels


# --- Output ---


def render_markdown(summary: ShipSummary) -> str:
    lines = ["## Ship Summary", ""]
    if summary.status == "ok":
        lines.append("**Status:** OK")
    else:
        lines.append("**Status:** FAILED")
    lines.append("")

    if summary.pr_number is not None:
        lines.append(
            f"- **PR:** [#{summary.pr_number}]({summary.pr_url}) — {summary.pr_title}"
        )
        lines.append(f"- **State:** {summary.pr_state}")
    else:
        lines.append("- **PR:** (none — see failed checks below)")
    lines.append(f"- **Branch:** `{summary.branch}` @ `{summary.head_sha[:8]}`")
    if summary.origin_sha:
        lines.append(
            f"- **Origin SHA:** `{summary.origin_sha[:8]}`"
            f" (matches HEAD: {summary.origin_sha == summary.head_sha})"
        )
    if summary.automerge_enabled:
        auto_merge_text = f"enabled ({summary.automerge_method})"
    elif summary.pr_state == PR_STATE_MERGED:
        auto_merge_text = "n/a (PR already merged)"
    else:
        auto_merge_text = "disabled"
    lines.append("- **Auto-merge:** " + auto_merge_text)
    lines.append(
        "- **Monitor:** "
        + ("registered" if summary.monitor_registered else "not registered")
    )
    if summary.files_changed:
        lines.append(
            f"- **Diff:** {summary.files_changed} files,"
            f" +{summary.additions} / -{summary.deletions}"
        )

    lines.append("")
    lines.append("### Checks")
    for check in summary.checks:
        marker = "✓" if check.passed else ("✗" if check.required else "○")
        req = "" if check.required else " (optional)"
        detail = f" — {check.detail}" if check.detail else ""
        lines.append(f"- {marker} `{check.name}`{req}{detail}")

    if summary.warnings:
        lines.append("")
        lines.append("### Warnings")
        lines.extend(f"- {w}" for w in summary.warnings)

    return "\n".join(lines) + "\n"


# --- Main ---


def cmd_verify(args: argparse.Namespace) -> int:
    if not shutil.which("gh"):
        sys.stderr.write("ERROR: `gh` CLI not found on PATH\n")
        return 2
    if not Path(".git").exists() and not git("rev-parse", "--git-dir"):
        sys.stderr.write("ERROR: not in a git repository\n")
        return 2

    summary = ShipSummary()
    summary.branch = args.branch or detect_branch()

    effective_require_automerge = resolve_effective_automerge_required(
        args.require_automerge
    )
    if effective_require_automerge != args.require_automerge:
        summary.warnings.append(
            "pr.auto_merge: false in .claude/project-config.yaml — "
            "automerge-enabled check treated as optional, not required (#2046)"
        )
    elif yaml is None and PROJECT_CONFIG_PATH.exists():
        summary.warnings.append(
            "could not read pr.auto_merge: PyYAML unavailable; "
            "treating auto-merge as required"
        )

    summary.checks.append(check_not_protected(summary.branch, summary))
    summary.checks.append(check_branch_pushed(summary.branch, summary))

    pr_check = check_pr_exists(summary.branch, summary)
    summary.checks.append(pr_check)
    if pr_check.passed:
        summary.checks.append(check_pr_sha_matches(summary))
        summary.checks.append(
            check_automerge(summary, required=effective_require_automerge)
        )
        summary.checks.append(
            check_monitor_registered(summary, required=args.require_monitor)
        )
    else:
        # No PR means downstream checks are all skipped/failed.
        for name, required in [
            ("pr-sha-matches", True),
            ("automerge-enabled", effective_require_automerge),
            ("monitor-registered", args.require_monitor),
        ]:
            summary.checks.append(
                CheckResult(
                    name=name, passed=False, detail="skipped — no PR", required=required
                )
            )

    collect_diff_stats(summary, args.base)

    failed_required = [c for c in summary.checks if c.required and not c.passed]
    summary.status = "failed" if failed_required else "ok"

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        sys.stdout.write(render_markdown(summary))

    return 1 if failed_required else 0


def _arm_invocation_error(message: str) -> int:
    sys.stderr.write(f"ERROR: {message}\n")
    return 2


def _refuse_undeterminable(reason: str) -> int:
    """Exit 2 for a config whose pr.auto_merge cannot be determined (#2581)."""
    return _arm_invocation_error(UNDETERMINABLE_TEMPLATE.format(reason=reason))


def _git_toplevel(path: Path) -> Path | None:
    """The git toplevel containing ``path``, or None when it is not in a work tree."""
    toplevel = git("-C", str(path), "rev-parse", "--show-toplevel")
    return Path(toplevel) if toplevel else None


def _normalize_repo_path(repo_path: Path) -> tuple[Path | None, str]:
    """Resolve an explicit --repo-path to its git toplevel.

    Returns ``(root, "")`` or ``(None, error)``.
    """
    if not repo_path.is_dir():
        return None, f"--repo-path is not a directory: {repo_path}"
    toplevel = _git_toplevel(repo_path)
    if toplevel is None:
        return None, f"--repo-path is not inside a git work tree: {repo_path}"
    return toplevel, ""


def cmd_check_automerge_allowed(args: argparse.Namespace) -> int:
    """Print "true"/"false" for whether pr.auto_merge permits `gh pr merge --auto`.

    The shared seam every markdown arm site shells out to before its own
    `gh pr merge --auto` call, so .claude/project-config.yaml is read once,
    not independently by each of ship-it.md, auto-dev-finalize.md,
    review-monitor.md, and cw-session-watch/SKILL.md (#2046). Exit 0 +
    "true" means arming is allowed; exit 1 + "false" means an explicit
    pr.auto_merge: false disallows it and the caller must skip the arm and
    leave the PR open. Exit 2 means an existing config's pr.auto_merge cannot
    be determined (prints "false", reason on stderr; fail closed, #2581) or
    --repo-path is not a directory or not inside a git work tree (prints
    nothing). A missing config file is still "true".
    """
    config_path = PROJECT_CONFIG_PATH
    if args.repo_path is not None:
        root, error = _normalize_repo_path(args.repo_path)
        if root is None:
            return _arm_invocation_error(error)
        config_path = root / PROJECT_CONFIG_PATH
    gate = read_automerge_gate(config_path)
    print("true" if gate.allowed else "false")
    if gate.reason is not None:
        return _refuse_undeterminable(gate.reason)
    return 0 if gate.allowed else 1


def _resolve_arm_repo_path(repo_path: Path | None) -> tuple[Path | None, str]:
    """The repo whose seam applies: --repo-path's toplevel, else the git toplevel.

    Returns ``(root, "")`` or ``(None, error)``. Never falls back to a
    cwd-relative or as-given path: gh acts on the repo it runs in, so the
    seam must be read from that same repo's root.
    """
    if repo_path is not None:
        return _normalize_repo_path(repo_path)
    toplevel = git("rev-parse", "--show-toplevel")
    if not toplevel:
        return None, "not in a git repository and --repo-path was not given"
    return Path(toplevel), ""


def _full_sha(value: str) -> str:
    """argparse type for --head-sha: a full 40-character hex SHA, lower-cased."""
    if _FULL_SHA_RE.fullmatch(value) is None:
        message = f"expected a full 40-character hex commit SHA, got {value!r}"
        raise argparse.ArgumentTypeError(message)
    return value.lower()


def cmd_arm_automerge(args: argparse.Namespace) -> int:
    """Arm auto-merge on a PR with bounded retry and read-back (#2576).

    Re-checks the pr.auto_merge seam exactly as `check-automerge-allowed`
    does, BEFORE any gh call: when it disallows arming this makes zero gh
    calls and exits 3 (#2046). Prints one JSON result object on stdout; a
    persistent failure also writes gh's last stderr to stderr and exits 1.
    An existing config whose pr.auto_merge cannot be determined also makes
    zero gh calls and exits 2 with no JSON (fail closed, #2581).
    """
    if not shutil.which("gh"):
        return _arm_invocation_error("`gh` CLI not found on PATH")
    if args.attempts < 1:
        return _arm_invocation_error("--attempts must be >= 1")
    if args.backoff_seconds < 0:
        return _arm_invocation_error("--backoff-seconds must be >= 0")
    repo_path, error = _resolve_arm_repo_path(args.repo_path)
    if repo_path is None:
        return _arm_invocation_error(error)

    gate = read_automerge_gate(repo_path / PROJECT_CONFIG_PATH)
    if gate.reason is not None:
        return _refuse_undeterminable(gate.reason)
    if not gate.allowed:
        skipped = ArmResult(
            pr_number=args.pr_number,
            max_attempts=args.attempts,
            status=ARM_STATUS_SKIPPED,
            detail=(
                "pr.auto_merge: false in .claude/project-config.yaml (#2046) "
                "-- no gh call made"
            ),
        )
        print(json.dumps(skipped.to_dict(), indent=2))
        return EXIT_ARM_DISALLOWED

    result = arm_automerge(
        args.pr_number,
        head_sha=args.head_sha,
        attempts=args.attempts,
        backoff_seconds=args.backoff_seconds,
        cwd=repo_path,
    )
    print(json.dumps(result.to_dict(), indent=2))
    if result.status == ARM_STATUS_FAILED:
        sys.stderr.write(
            f"ERROR: gh pr merge --auto failed after {result.attempts}/"
            f"{result.max_attempts} attempts (exit {result.gh_exit_code}): "
            f"{result.gh_stderr}\n"
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prep_pr_finalize.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    verify = sub.add_parser("verify", help="Verify PR exists and emit ship summary")
    verify.add_argument("--branch", help="Branch to check (default: current branch)")
    verify.add_argument(
        "--base", default="main", help="Base branch for diff stats (default: main)"
    )
    verify.add_argument(
        "--require-automerge",
        action="store_true",
        help=(
            "Treat auto-merge-not-enabled as a required failure; an "
            "already-MERGED PR satisfies it "
            "(downgraded to optional when .claude/project-config.yaml "
            "sets pr.auto_merge: false)"
        ),
    )
    verify.add_argument(
        "--require-monitor",
        action="store_true",
        help="Treat monitor-not-registered as a required failure",
    )
    verify.add_argument(
        "--json", action="store_true", help="Emit JSON instead of markdown"
    )
    verify.set_defaults(func=cmd_verify)

    check_allowed = sub.add_parser(
        "check-automerge-allowed",
        help=(
            "Print true/false for whether pr.auto_merge permits "
            "`gh pr merge --auto`: exit 0 allowed, exit 1 explicit "
            "pr.auto_merge: false, exit 2 undeterminable config or invalid "
            "--repo-path (fail closed)"
        ),
    )
    check_allowed.add_argument(
        "--repo-path",
        type=Path,
        help=(
            "Path inside the repository whose .claude/project-config.yaml "
            "should be read (normalized to the git toplevel)"
        ),
    )
    check_allowed.set_defaults(func=cmd_check_automerge_allowed)

    arm = sub.add_parser(
        "arm-automerge",
        help=(
            "Arm auto-merge on a PR, pinned to a verified head SHA, with "
            "bounded retry and read-back (#2576, #2581)"
        ),
    )
    arm.add_argument("pr_number", type=int, help="Pull request number to arm")
    arm.add_argument(
        "--attempts",
        type=int,
        default=ARM_MAX_ATTEMPTS,
        help=f"Max gh pr merge --auto calls (default: {ARM_MAX_ATTEMPTS})",
    )
    arm.add_argument(
        "--backoff-seconds",
        type=float,
        default=ARM_BACKOFF_SECONDS,
        help=(
            "Linear backoff base; sleeps backoff * n after the n-th failed "
            f"attempt (default: {ARM_BACKOFF_SECONDS})"
        ),
    )
    arm.add_argument(
        "--repo-path",
        type=Path,
        help=(
            "Path inside the repository (normalized to its git toplevel) whose "
            ".claude/project-config.yaml should be read (default: the git "
            "toplevel of the current directory)"
        ),
    )
    arm.add_argument(
        "--head-sha",
        required=True,
        type=_full_sha,
        help=(
            "Full 40-character SHA of the verified local HEAD. The arm is "
            "pinned to it with `gh pr merge --match-head-commit`, so it applies "
            "only if the PR head still matches at arm time (closes the "
            "verify-to-arm window); a mismatch fails the arm and gh's stderr "
            "is reported."
        ),
    )
    arm.set_defaults(func=cmd_arm_automerge)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return cast("int", args.func(args))


if __name__ == "__main__":
    sys.exit(main())
