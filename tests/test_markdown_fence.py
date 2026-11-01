"""Tests for ``cw.markdown_fence``, the shared backtick-fence widener."""

from __future__ import annotations

import pytest

from cw.markdown_fence import fence_for


@pytest.mark.parametrize(
    ("text", "fence"),
    [
        ("", "```"),
        ("plain", "```"),
        ("one ` tick", "```"),
        ("two `` ticks", "```"),
        ("three ``` ticks", "````"),
        ("five ````` ticks", "``````"),
        ("five ````` ticks and ``` three", "``````"),
        ("`` then ```` then `", "`````"),
    ],
)
def test_fence_is_one_longer_than_the_longest_backtick_run(
    text: str, fence: str
) -> None:
    assert fence_for(text) == fence
