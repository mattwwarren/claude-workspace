"""Doc pin for the Stage 2 operator-directions section (#3761).

The section in ``auto-dev-impl.md`` and the codex prompt note say the same
pinned things; both read their phrases from ``impl_operator_directions`` so
they cannot drift.
"""

from __future__ import annotations

from cw.stage_verification.impl_operator_directions import PINNED_PHRASES
from tests._shared.workflow_text import _cmd, _norm

_TAG = "<!-- el: impl.operator-directions -->"
_HEADING = "### Operator directions (#3761)"


def _directions_section() -> str:
    content = _cmd("auto-dev-impl.md", raw=True)
    start = content.index(_HEADING)
    end = content.index("\n<!-- el", start)
    return _norm(content[start:end])


def test_section_carries_its_element_tag() -> None:
    content = _cmd("auto-dev-impl.md", raw=True)

    assert _TAG in content
    assert content.index(_TAG) < content.index(_HEADING)


def test_section_names_the_key_and_the_skip_rule() -> None:
    section = _directions_section()

    assert "queue_metadata.operator_directions" in section
    assert "null or absent" in section


def test_section_contains_every_pinned_phrase() -> None:
    section = _directions_section()

    for phrase in PINNED_PHRASES:
        assert phrase in section


def test_section_sits_between_regress_and_dispatch_detection() -> None:
    content = _cmd("auto-dev-impl.md", raw=True)

    regress = content.index("<!-- el: impl.regress-review-findings -->")
    directions = content.index(_TAG)
    dispatch = content.index("<!-- el-begin: impl.dispatch-detection -->")
    assert regress < directions < dispatch
