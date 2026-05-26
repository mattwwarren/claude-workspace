#!/usr/bin/env python3
"""Pre-flight checks for /cw-smoke-test.

Verifies the environment is healthy enough to dispatch ``/auto-dev`` against
a single ticket. Emits one JSON object on stdout with ``ok`` (bool) and
``checks`` (list of ``{name, passed, severity, detail}``). Exits 0 when no
``severity=hard`` check failed (soft warnings still report ``ok=true``);
exits 1 when at least one hard check failed.

Each check distinguishes ``severity``:
- ``hard``  — must pass; failure aborts the smoke test.
- ``soft``  — warning only; surfaces in the report but does not abort.

Checks performed:
- ``agents_present``        (hard)  plan-reviewer + plan-soundness-reviewer
                                   exist under one of ``~/.claude/agents/``
                                   or the repo-local ``.claude/agents/``.
- ``cw_backend_healthy``    (hard)  ``cw doctor`` exits 0 OR fails only on
                                   non-backend categories (linkage drift,
                                   stale state).
- ``cw_doctor_clean``       (soft)  ``cw doctor`` reports zero issues.
- ``ticket_open``           (hard)  ``gh issue view <id>`` returns
                                   ``state=OPEN``.
- ``no_open_pr_for_ticket`` (hard)  ``gh pr list --search "<id>"`` returns
                                   no PR whose title or body references the
                                   ticket as already in flight.
- ``not_already_queued``    (hard)  the ticket is not already RUNNING or
                                   PENDING in ``cw dev-queue status``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_GLOBAL_AGENTS = Path.home() / ".claude" / "agents"
_REQUIRED_AGENTS = ("plan-reviewer.md", "plan-soundness-reviewer.md")
_DEV_QUEUE_BLOCKING_STATES = {"PENDING", "RUNNING", "CLAIMED"}


def _check_agents(repo_root: Path) -> dict[str, Any]:
    local_agents = repo_root / ".claude" / "agents"
    missing: list[str] = []
    locations: list[str] = []
    for name in _REQUIRED_AGENTS:
        found_in: list[str] = []
        if (_GLOBAL_AGENTS / name).is_file():
            found_in.append(str(_GLOBAL_AGENTS))
        if (local_agents / name).is_file():
            found_in.append(str(local_agents))
        if not found_in:
            missing.append(name)
        else:
            locations.append(f"{name}@{found_in[0]}")
    if missing:
        detail = (
            f"missing: {', '.join(missing)} "
            f"(looked in {_GLOBAL_AGENTS}, {local_agents})"
        )
        return {
            "name": "agents_present",
            "passed": False,
            "severity": "hard",
            "detail": detail,
        }
    return {
        "name": "agents_present",
        "passed": True,
        "severity": "hard",
        "detail": "; ".join(locations),
    }


def _check_cw_doctor() -> tuple[dict[str, Any], dict[str, Any]]:
    cw = shutil.which("cw")
    if cw is None:
        hard = {
            "name": "cw_backend_healthy",
            "passed": False,
            "severity": "hard",
            "detail": "cw binary not on PATH",
        }
        soft = {
            "name": "cw_doctor_clean",
            "passed": False,
            "severity": "soft",
            "detail": "skipped — cw not on PATH",
        }
        return hard, soft
    proc = subprocess.run(
        [cw, "doctor"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    # cw doctor exits 0 when everything passes. Backend-related failures are
    # the only category that should hard-block a smoke test — linkage drift
    # and stale-session warnings are normal on a working system.
    hard_keywords = ("backend", "binary", "config", "parse")
    output_lower = output.lower()
    hard_failed = proc.returncode != 0 and any(k in output_lower for k in hard_keywords)
    hard = {
        "name": "cw_backend_healthy",
        "passed": not hard_failed,
        "severity": "hard",
        "detail": output if hard_failed else "backend reachable, config parseable",
    }
    soft = {
        "name": "cw_doctor_clean",
        "passed": proc.returncode == 0,
        "severity": "soft",
        "detail": "no issues"
        if proc.returncode == 0
        else f"cw doctor reported issues:\n{output}",
    }
    return hard, soft


def _check_ticket_open(ticket: str, repo: str) -> dict[str, Any]:
    gh = shutil.which("gh")
    if gh is None:
        return {
            "name": "ticket_open",
            "passed": False,
            "severity": "hard",
            "detail": "gh CLI not on PATH",
        }
    proc = subprocess.run(
        [gh, "issue", "view", ticket, "-R", repo, "--json", "state,title,number"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "name": "ticket_open",
            "passed": False,
            "severity": "hard",
            "detail": (
                f"gh issue view failed: {proc.stderr.strip() or proc.stdout.strip()}"
            ),
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": "ticket_open",
            "passed": False,
            "severity": "hard",
            "detail": f"gh output not valid JSON: {exc}",
        }
    state = payload.get("state", "UNKNOWN")
    title = payload.get("title", "")
    return {
        "name": "ticket_open",
        "passed": state == "OPEN",
        "severity": "hard",
        "detail": f"#{payload.get('number')} state={state} title={title!r}",
    }


def _check_no_open_pr(ticket: str, repo: str) -> dict[str, Any]:
    gh = shutil.which("gh")
    if gh is None:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": False,
            "severity": "hard",
            "detail": "gh CLI not on PATH",
        }
    # Search both title and body for the ticket reference. The smoke test
    # should not dispatch onto a ticket that already has work in flight.
    search = f"#{ticket} in:title,body is:pr is:open"
    proc = subprocess.run(
        [
            gh,
            "pr",
            "list",
            "-R",
            repo,
            "--search",
            search,
            "--json",
            "number,title,url",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": False,
            "severity": "hard",
            "detail": (
                f"gh pr list failed: {proc.stderr.strip() or proc.stdout.strip()}"
            ),
        }
    try:
        prs = json.loads(proc.stdout) or []
    except json.JSONDecodeError as exc:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": False,
            "severity": "hard",
            "detail": f"gh output not valid JSON: {exc}",
        }
    # gh's free-text search is fuzzy — filter to PRs whose title actually
    # references the ticket (avoids false-positives from unrelated PRs that
    # mention the number in passing).
    pattern = re.compile(rf"(^|\D){re.escape(ticket)}(\D|$)")
    matching = [pr for pr in prs if pattern.search(pr.get("title", ""))]
    if not matching:
        return {
            "name": "no_open_pr_for_ticket",
            "passed": True,
            "severity": "hard",
            "detail": "no open PR references the ticket",
        }
    summary = ", ".join(f"#{pr['number']} {pr['title']}" for pr in matching)
    return {
        "name": "no_open_pr_for_ticket",
        "passed": False,
        "severity": "hard",
        "detail": f"open PRs reference ticket: {summary}",
    }


def _check_not_queued(ticket: str, client: str) -> dict[str, Any]:
    cw = shutil.which("cw")
    if cw is None:
        return {
            "name": "not_already_queued",
            "passed": False,
            "severity": "hard",
            "detail": "cw binary not on PATH",
        }
    proc = subprocess.run(
        [cw, "dev-queue", "status", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Older builds may not support --json. Fall back to a plain run and
        # grep for the ticket; the smoke-test is best-effort here.
        proc = subprocess.run(
            [cw, "dev-queue", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "name": "not_already_queued",
                "passed": False,
                "severity": "hard",
                "detail": (
                    f"cw dev-queue status failed: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                ),
            }
        # Plain-text fallback: look for the ticket id alongside a blocking state.
        text = proc.stdout
        for state in _DEV_QUEUE_BLOCKING_STATES:
            if state in text and ticket in text:
                return {
                    "name": "not_already_queued",
                    "passed": False,
                    "severity": "hard",
                    "detail": (
                        f"ticket {ticket} appears with state {state} "
                        f"in dev-queue status output"
                    ),
                }
        return {
            "name": "not_already_queued",
            "passed": True,
            "severity": "hard",
            "detail": "ticket not seen in dev-queue status",
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": "not_already_queued",
            "passed": False,
            "severity": "hard",
            "detail": f"cw dev-queue status JSON parse failed: {exc}",
        }
    by_client = payload if isinstance(payload, dict) else {}
    entries = (
        by_client.get(client, []) if isinstance(by_client.get(client), list) else []
    )
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            str(entry.get("ticket_id")) == ticket
            and entry.get("state") in _DEV_QUEUE_BLOCKING_STATES
        ):
            return {
                "name": "not_already_queued",
                "passed": False,
                "severity": "hard",
                "detail": (
                    f"ticket {ticket} is {entry.get('state')} "
                    f"in dev-queue for client {client}"
                ),
            }
    return {
        "name": "not_already_queued",
        "passed": True,
        "severity": "hard",
        "detail": f"ticket {ticket} not queued or running for client {client}",
    }


def _resolve_repo_root() -> Path:
    # The script lives at <repo>/.claude/skills/cw-smoke-test/scripts/preflight.py
    # — climb four parents to land on the repo root.
    return Path(__file__).resolve().parents[4]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight checks for /cw-smoke-test."
    )
    parser.add_argument(
        "--ticket-id", required=True, help="GitHub ticket number (no '#')."
    )
    parser.add_argument(
        "--repo",
        default="mattwwarren/claude-workspace",
        help="GitHub repo in OWNER/NAME form (default: mattwwarren/claude-workspace).",
    )
    parser.add_argument(
        "--client",
        default="claude-workspace",
        help="cw client name for the dev-queue lookup.",
    )
    args = parser.parse_args()
    ticket = args.ticket_id.lstrip("#")
    repo_root = _resolve_repo_root()

    checks: list[dict[str, Any]] = []
    checks.append(_check_agents(repo_root))
    hard_doctor, soft_doctor = _check_cw_doctor()
    checks.append(hard_doctor)
    checks.append(soft_doctor)
    checks.append(_check_ticket_open(ticket, args.repo))
    checks.append(_check_no_open_pr(ticket, args.repo))
    checks.append(_check_not_queued(ticket, args.client))

    hard_failed = any(
        check["severity"] == "hard" and not check["passed"] for check in checks
    )
    report = {
        "ok": not hard_failed,
        "ticket_id": ticket,
        "client": args.client,
        "repo": args.repo,
        "checks": checks,
    }
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")
    return 1 if hard_failed else 0


if __name__ == "__main__":
    sys.exit(main())
