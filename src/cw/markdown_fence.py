"""The one backtick-fence widening helper, shared by every markdown renderer.

A fenced code block breaks if its body holds a backtick run as long as the
fence, so each renderer that fences text it does not control sizes the fence
from the body. Leaf module: imports nothing from :mod:`cw`.
"""

from __future__ import annotations

import re

__all__ = ["fence_for"]

_MIN_FENCE_LENGTH = 3
_BACKTICK_RUN = re.compile(r"`+")


def fence_for(text: str) -> str:
    """A backtick fence one longer than the longest run in *text*, at least 3."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    return "`" * max(_MIN_FENCE_LENGTH, longest + 1)
