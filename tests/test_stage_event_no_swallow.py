"""Doc guard: ``cw event record stage.*`` calls are never swallowed (#2429).

``cw event record`` rejects a ``stage``/``prev_stage`` outside the closed
headless-contract §10.2 enum. A trailing ``|| true`` on the invocation would
discard that rejection, so a typo'd stage would vanish silently instead of
surfacing in the calling fence. Every ``/auto-dev*`` stage-event invocation
must therefore end without one.

The invocation is read forward from its ``cw event record stage.`` token
across ``\\``-continued lines only (forward-only, like the
``_marker_windows`` idiom in ``tests/test_not_main_checkout_docs.py``, but
bounded by the command itself rather than a fixed span), so unrelated
``|| true`` text after the command cannot trip the guard.
"""

from __future__ import annotations

import re

from tests.conftest import _COMMANDS_ROOT

_COMMAND_PREFIX = "cw event record stage."
_SWALLOW = "|| true"
_PREFIX = re.compile(r"[ \t>]*")

# Every stage doc that emits a stage event today. A floor, not an exact set:
# it only guards against the glob silently matching nothing.
_EMITTING_DOCS = frozenset(
    {
        "auto-dev.md",
        "auto-dev-intake.md",
        "auto-dev-plan.md",
        "auto-dev-plan-appendix.md",
        "auto-dev-impl.md",
        "auto-dev-review.md",
        "auto-dev-finalize.md",
    }
)


def _invocation(text: str, idx: int) -> str:
    """The shell command starting at *idx*, through its last continued line."""
    lines: list[str] = []
    pos = idx
    while True:
        end = text.find("\n", pos)
        line = text[pos:] if end == -1 else text[pos:end]
        lines.append(line)
        if end == -1 or not line.rstrip().endswith("\\"):
            return "\n".join(lines)
        pos = end + 1


def _starts_command_line(text: str, idx: int) -> bool:
    """True when only indentation / blockquote ``>`` precede *idx* on its line.

    Separates a fenced shell invocation from a prose mention of the command
    (e.g. the ``Stage event rule (#2429)`` section naming it in backticks).
    """
    line_start = text.rfind("\n", 0, idx) + 1
    return _PREFIX.fullmatch(text[line_start:idx]) is not None


def _stage_event_invocations() -> dict[str, list[str]]:
    """``{doc name: [invocation, ...]}`` across every ``auto-dev*.md`` doc."""
    found: dict[str, list[str]] = {}
    for path in sorted(_COMMANDS_ROOT.glob("auto-dev*.md")):
        text = path.read_text(encoding="utf-8")
        idx = text.find(_COMMAND_PREFIX)
        while idx != -1:
            if _starts_command_line(text, idx):
                found.setdefault(path.name, []).append(_invocation(text, idx))
            idx = text.find(_COMMAND_PREFIX, idx + 1)
    return found


def test_every_emitting_doc_is_scanned() -> None:
    assert set(_stage_event_invocations()) >= _EMITTING_DOCS


def test_invocation_reads_through_the_payload_line() -> None:
    """The extractor reaches the ``--payload`` line, where ``|| true`` sat."""
    for invocations in _stage_event_invocations().values():
        for invocation in invocations:
            assert "--payload" in invocation, invocation


def test_no_stage_event_invocation_swallows_its_exit_status() -> None:
    offenders = [
        f"{name}: {invocation.splitlines()[0].strip()}"
        for name, invocations in _stage_event_invocations().items()
        for invocation in invocations
        if _SWALLOW in invocation
    ]
    assert offenders == []
