"""Guard tests: markdown arm sites branch on ``arm-automerge`` exit status (#2585).

``.claude/commands/review-monitor.md`` (the Re-arm auto-merge block) and
``.claude/skills/cw-session-watch/SKILL.md`` (the PR-open recovery block) used
to gate a bare ``gh pr merge --auto`` on ``check-automerge-allowed`` and print
"disabled via .claude/project-config.yaml" on *any* non-zero exit, including
exit 2 (``pr.auto_merge`` undeterminable, #2581). Both now arm through
``prep_pr_finalize.py arm-automerge`` and branch on its exit status, so only
exit 3 claims the repo opted out. These tests pin the fenced bash of both docs
and execute the ``case "$arm_status" in`` ladder for each exit code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    _bash_fences,
    _cmd,
)

_SKILL_PATH = (
    Path(__file__).parent.parent
    / ".claude"
    / "skills"
    / "cw-session-watch"
    / "SKILL.md"
)
_CASE_HEADER = 'case "$arm_status" in'
_ARM_LABEL_RE = re.compile(r"^\s*(0|1|3|\*)\)", re.MULTILINE)
_REFUSAL_PHRASE = "NOT arming"
_DISABLED_WORD = "disable"
_BASH_TIMEOUT_SECONDS = 10
_EXIT_ARM_FAILED = 1
_EXIT_DISALLOWED = 3
_EXIT_UNKNOWN = 127

_REVIEW_MONITOR = "review-monitor"
_SESSION_WATCH = "cw-session-watch"
_DOC_NAMES = [_REVIEW_MONITOR, _SESSION_WATCH]


def _doc(name: str) -> str:
    if name == _REVIEW_MONITOR:
        return _cmd("review-monitor.md")
    return _SKILL_PATH.read_text(encoding="utf-8")


def _fences(name: str) -> str:
    return "\n".join(_bash_fences(_doc(name)))


def _arm_case(fences: str) -> str:
    """Return the ``case "$arm_status" in`` .. ``esac`` block from *fences*."""
    lines = fences.splitlines()
    start = next(i for i, line in enumerate(lines) if _CASE_HEADER in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "esac")
    return "\n".join(lines[start : end + 1])


def _arms(case_text: str) -> dict[str, str]:
    """Split *case_text* into arms keyed by label (``0``/``1``/``3``/``*``).

    Labels are matched at line start only: arm bodies contain ``)`` in prose
    such as ``(pr.auto_merge: false)``.
    """
    matches = list(_ARM_LABEL_RE.finditer(case_text))
    arms: dict[str, str] = {}
    for position, match in enumerate(matches):
        stop = (
            matches[position + 1].start()
            if position + 1 < len(matches)
            else case_text.rindex("esac")
        )
        arms[match.group(1)] = case_text[match.start() : stop]
    return arms


def _lower_contains_disable(text: str) -> bool:
    return _DISABLED_WORD in text.lower()


@pytest.mark.parametrize("name", _DOC_NAMES)
def test_arm_site_uses_arm_automerge_with_pin(name: str) -> None:
    fences = _fences(name)
    assert "prep_pr_finalize.py arm-automerge" in fences
    assert "--repo-path" in fences
    assert '--head-sha "$HEAD_SHA"' in fences
    assert re.search(r"HEAD_SHA=\$\(git[^\n]*rev-parse HEAD\)", fences)


@pytest.mark.parametrize("name", _DOC_NAMES)
def test_no_bare_auto_merge_in_fenced_bash(name: str) -> None:
    assert re.search(r"gh pr merge[^\n]*--auto", _fences(name)) is None


@pytest.mark.parametrize("name", _DOC_NAMES)
def test_disabled_message_only_in_exit_3_arm(name: str) -> None:
    arms = _arms(_arm_case(_fences(name)))
    assert set(arms) == {"0", "1", "3", "*"}
    mentioning = {
        label for label, text in arms.items() if _lower_contains_disable(text)
    }
    assert mentioning == {"3"}
    assert "disabled via .claude/project-config.yaml" in arms["3"]
    assert "leaving PR open for manual merge" in arms["3"]
    assert _REFUSAL_PHRASE in arms["*"]
    assert "stderr" in arms["*"]


@pytest.mark.parametrize("name", _DOC_NAMES)
def test_exit_1_arm_does_not_claim_disabled(name: str) -> None:
    arm = _arms(_arm_case(_fences(name)))["1"]
    assert "arm failed" in arm
    assert not _lower_contains_disable(arm)


@pytest.mark.parametrize("name", _DOC_NAMES)
@pytest.mark.parametrize("code", [_EXIT_ARM_FAILED, 2, _EXIT_DISALLOWED, _EXIT_UNKNOWN])
def test_exit_code_behavior(name: str, code: int) -> None:
    """Run the extracted ``case`` for *code*; "disable" appears iff code == 3.

    The ``0)`` arm is dropped before execution: cw-session-watch's ``0)`` arm
    holds ``cw dev-queue remove <ticket> -c <client>;`` which bash parses as a
    redirection (a parse-time syntax error even when the arm is not taken).
    """
    arms = _arms(_arm_case(_fences(name)))
    body = "".join(text for label, text in arms.items() if label != "0")
    script = f"arm_status={code}\n{_CASE_HEADER}\n{body}esac\n"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=_BASH_TIMEOUT_SECONDS,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert output.strip(), "case ladder printed nothing"
    assert _lower_contains_disable(output) is (code == _EXIT_DISALLOWED)
    if code not in {_EXIT_ARM_FAILED, _EXIT_DISALLOWED}:
        assert _REFUSAL_PHRASE in output


def test_review_monitor_hold_rules_unchanged() -> None:
    doc = _doc(_REVIEW_MONITOR)
    assert "deliberate hold, NOT re-arming" in doc
    assert "unresolved human threads" in doc
    assert 'case "$DIS_ACTOR" in' in doc


def test_exit_2_wording_pinned_in_review_monitor_prose() -> None:
    doc = _doc(_REVIEW_MONITOR)
    for phrase in (
        "arm-automerge",
        "#2581",
        "undeterminable",
        "fails closed",
        "Only exit 3",
    ):
        assert phrase in doc, phrase


@pytest.mark.parametrize("name", _DOC_NAMES)
def test_exit_2_wording_pinned_in_fenced_refusal_arm(name: str) -> None:
    arm = _arms(_arm_case(_fences(name)))["*"]
    assert _REFUSAL_PHRASE in arm
    assert "stderr" in arm
