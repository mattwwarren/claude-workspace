#!/usr/bin/env python3
"""
Post-ship validation: verify a PR was actually created and emit a structured ship summary.

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
  - Auto-merge is enabled (optional, --require-automerge)
  - Monitor is registered (optional, --require-monitor)

Exit codes:
  0  all checks passed
  1  a required check failed
  2  invocation error (not in a git repo, gh missing, etc.)
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.runtime_paths import review_monitor_script_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PROTECTED_BRANCHES = {"main", "master"}
MONITOR_SCRIPT = review_monitor_script_path()


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

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --- Shell helpers ---


def run(cmd: list[str], check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command. Default: capture, do not raise."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
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


def check_not_protected(branch: str, summary: ShipSummary) -> CheckResult:
    if branch in PROTECTED_BRANCHES:
        return CheckResult(
            name="not-protected-branch",
            passed=False,
            detail=f"on protected branch '{branch}' — finalize must run from a feature branch",
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


def check_pr_exists(branch: str, summary: ShipSummary) -> CheckResult:
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
    return CheckResult(name="pr-sha-matches", passed=True, detail=summary.pr_head_sha[:8])


def check_automerge(summary: ShipSummary, required: bool) -> CheckResult:
    if summary.automerge_enabled:
        return CheckResult(
            name="automerge-enabled",
            passed=True,
            detail=summary.automerge_method or "enabled",
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
    repo_result = run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    repo = repo_result.stdout.strip()
    if repo_result.returncode != 0 or not repo:
        return CheckResult(
            name="monitor-registered",
            passed=False,
            detail=f"could not resolve repo via gh: {repo_result.stderr.strip()[:200]}",
            required=required,
        )
    result = run([str(MONITOR_SCRIPT), "status", "--repo", repo, "--json"])
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
        lines.append(f"- **PR:** [#{summary.pr_number}]({summary.pr_url}) — {summary.pr_title}")
        lines.append(f"- **State:** {summary.pr_state}")
    else:
        lines.append("- **PR:** (none — see failed checks below)")
    lines.append(f"- **Branch:** `{summary.branch}` @ `{summary.head_sha[:8]}`")
    if summary.origin_sha:
        lines.append(f"- **Origin SHA:** `{summary.origin_sha[:8]}` (matches HEAD: {summary.origin_sha == summary.head_sha})")
    lines.append(
        "- **Auto-merge:** "
        + (f"enabled ({summary.automerge_method})" if summary.automerge_enabled else "disabled")
    )
    lines.append(
        "- **Monitor:** " + ("registered" if summary.monitor_registered else "not registered")
    )
    if summary.files_changed:
        lines.append(
            f"- **Diff:** {summary.files_changed} files, +{summary.additions} / -{summary.deletions}"
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
        for w in summary.warnings:
            lines.append(f"- {w}")

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

    summary.checks.append(check_not_protected(summary.branch, summary))
    summary.checks.append(check_branch_pushed(summary.branch, summary))

    pr_check = check_pr_exists(summary.branch, summary)
    summary.checks.append(pr_check)
    if pr_check.passed:
        summary.checks.append(check_pr_sha_matches(summary))
        summary.checks.append(check_automerge(summary, required=args.require_automerge))
        summary.checks.append(check_monitor_registered(summary, required=args.require_monitor))
    else:
        # No PR means downstream checks are all skipped/failed.
        for name, required in [
            ("pr-sha-matches", True),
            ("automerge-enabled", args.require_automerge),
            ("monitor-registered", args.require_monitor),
        ]:
            summary.checks.append(
                CheckResult(name=name, passed=False, detail="skipped — no PR", required=required)
            )

    collect_diff_stats(summary, args.base)

    failed_required = [c for c in summary.checks if c.required and not c.passed]
    summary.status = "failed" if failed_required else "ok"

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        sys.stdout.write(render_markdown(summary))

    return 1 if failed_required else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prep_pr_finalize.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    verify = sub.add_parser("verify", help="Verify PR exists and emit ship summary")
    verify.add_argument("--branch", help="Branch to check (default: current branch)")
    verify.add_argument("--base", default="main", help="Base branch for diff stats (default: main)")
    verify.add_argument(
        "--require-automerge",
        action="store_true",
        help="Treat auto-merge-not-enabled as a required failure",
    )
    verify.add_argument(
        "--require-monitor",
        action="store_true",
        help="Treat monitor-not-registered as a required failure",
    )
    verify.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
