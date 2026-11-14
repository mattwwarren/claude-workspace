"""Probe whether the Claude account is still usage-limited (#3547).

Runs the manual probe ``docs/dispatch-runbook.md`` documents ("Quota walls:
probe before pausing"): ``claude -p "Reply with exactly: WORKER-OK"``, with
``--model <worker_model>`` when the client pins one. The reply is classified
with the single usage-limit detection seam, ``USAGE_LIMIT_RE``. Used by
``cw dev-queue usage-limit clear`` before it lifts a window; anything this
cannot classify is ERROR, and the caller fails closed on it. One attempt, no
retry, no lock held.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum

from cw.claude_review._runner import CLAUDE_BINARY
from cw.exceptions import USAGE_LIMIT_RE

PROBE_PROMPT = "Reply with exactly: WORKER-OK"
_PROBE_OK = "WORKER-OK"
_PROBE_TIMEOUT_SECONDS = 120
# Bound on the output quoted back in a verdict's detail.
_DETAIL_MAX_CHARS = 200


class ProbeVerdict(StrEnum):
    """What the account probe concluded."""

    LIMITED = "limited"
    AVAILABLE = "available"
    ERROR = "error"


@dataclass(frozen=True)
class ProbeResult:
    """The probe's verdict plus a short human-readable reason."""

    verdict: ProbeVerdict
    detail: str


def _snippet(text: str) -> str:
    """One whitespace-collapsed line of *text*, bounded for a message."""
    return " ".join(text.split())[:_DETAIL_MAX_CHARS]


def _classify(completed: subprocess.CompletedProcess[str]) -> ProbeResult:
    """LIMITED on the usage-limit wording anywhere, AVAILABLE on a clean reply."""
    output = f"{completed.stdout}\n{completed.stderr}"
    match = USAGE_LIMIT_RE.search(output)
    if match is not None:
        return ProbeResult(ProbeVerdict.LIMITED, _snippet(match.group(0)))
    if completed.returncode == 0 and _PROBE_OK in completed.stdout:
        return ProbeResult(ProbeVerdict.AVAILABLE, _PROBE_OK)
    reply = _snippet(output) or "no output"
    detail = f"unrecognised reply (exit {completed.returncode}): {reply}"
    return ProbeResult(ProbeVerdict.ERROR, detail)


def probe_account_usage_limit(
    model: str | None, *, timeout_seconds: int = _PROBE_TIMEOUT_SECONDS
) -> ProbeResult:
    """Ask ``claude`` for a trivial reply and classify the answer.

    *model* is the client's ``worker_model``; ``--model`` is passed only when
    it is set. A missing binary, any other ``OSError`` and a timeout are all
    ERROR with a short detail.
    """
    argv = [CLAUDE_BINARY, "-p", PROBE_PROMPT]
    if model:
        argv += ["--model", model]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return ProbeResult(ProbeVerdict.ERROR, f"{CLAUDE_BINARY} binary not found")
    except subprocess.TimeoutExpired:
        detail = f"{CLAUDE_BINARY} probe timed out after {timeout_seconds}s"
        return ProbeResult(ProbeVerdict.ERROR, detail)
    except OSError as exc:
        detail = f"could not run {CLAUDE_BINARY}: {exc.strerror or type(exc).__name__}"
        return ProbeResult(ProbeVerdict.ERROR, detail)
    return _classify(completed)
