"""Parse family and version out of a Claude model id (#3626).

Pure, stdlib-only leaf module: no ``cw.*`` imports and no I/O. It is the one
place that reads the ``claude-<family>-<major>[-<minor>]`` shape, so callers
that need a version rule (``cw.native_daemon.model_supports_auto``) and
callers that need a family rule (``cw.worker_effort``) cannot disagree about
how an id is split. Each caller keeps its own policy.

Id grammar accepted:

- family-first ids only, start-anchored: ``claude-<family>-<major>``, with an
  optional ``-<minor>``. Everything after the version is ignored, so dated
  (``-20260101``) and suffixed ids parse.
- major and minor are one or two digits, and each must not be followed by
  another digit. A date directly after the family (``claude-sonnet-20250101``)
  therefore does not parse as a version, and a date after the major
  (``claude-sonnet-5-20260101``) does not parse as a minor.

Returns ``None`` for: an empty or missing id, bare aliases (``opus``),
legacy ``claude-3-…`` ids, provider-prefixed ids (``us.anthropic.…``), and
anything else that does not match the shape above.
"""

from __future__ import annotations

import re
from typing import Final, NamedTuple

_MODEL_ID_RE: Final = re.compile(
    r"^claude-(?P<family>[a-z]+)-(?P<major>\d{1,2})(?!\d)"
    r"(?:-(?P<minor>\d{1,2})(?!\d))?"
)


class ModelVersion(NamedTuple):
    """Family and version of a parsed Claude model id."""

    family: str
    major: int
    minor: int | None


def parse_model_id(model: str | None) -> ModelVersion | None:
    """Return the family and version of *model*, or ``None`` if unparseable.

    Input is stripped and lower-cased before matching, so case and surrounding
    whitespace never change the result.
    """
    if not model:
        return None
    match = _MODEL_ID_RE.match(model.strip().lower())
    if match is None:
        return None
    minor = match.group("minor")
    return ModelVersion(
        family=match.group("family"),
        major=int(match.group("major")),
        minor=int(minor) if minor is not None else None,
    )
