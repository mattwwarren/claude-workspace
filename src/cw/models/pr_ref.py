"""``PrRef``: a typed reference to the pull request a dev-queue row waits on.

GitHub #3634 (follow-up to #2321). ``TicketTask.blocked_on_pr`` used to be a
bare int, which only names a PR inside the row's own client repo. A ``PrRef``
carries an optional ``repo``:

- ``repo=None`` means "the row's own client repo", exactly what every legacy
  persisted int means, and is written back as that bare int, so same-repo
  ``dev_queue.json`` bytes are unchanged.
- an explicit ``owner/repo`` names a PR in another repo and is written back as
  ``{"repo", "number"}``.

Merge evidence is keyed ``(repo.lower(), number)``; a same-repo ref looks up
``(None, number)``, which only same-client task rows and ``cross_repo=False``
watches emit (see :func:`pr_evidence_keys`).

Imports only pydantic, the stdlib and the dependency-free ``cw._predicates``,
so it is a DAG root of ``cw.models``. Not re-exported from ``cw.models``:
import it by submodule path.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, field_validator

from cw._predicates import is_positive_real_int, is_repo_slug, parse_repo_pr_ref

if TYPE_CHECKING:
    from pydantic.config import JsonDict

_BARE_NUMBER_RE: Final = re.compile(r"[1-9][0-9]*")

# The persisted-row JSON-schema hint for ``TicketTask.blocked_on_pr`` (#3634,
# R10). A ``dev_queue.json`` row accepts the legacy or same-repo bare int, the
# cross-repo ``{repo, number}`` object, or null, and ``cw schema show
# ticket-task`` must advertise exactly those shapes. Keep it in step with
# ``PrRef`` and ``dump_blocked_on`` (tests/test_models_pr_ref.py pins that).
# The object arm is inlined rather than a ``$ref``: a hand-written
# ``{"$ref": "#/$defs/PrRef"}`` makes pydantic 2.12.5 raise ``KeyError`` while
# counting refs. The defensive load tolerance (0, negative and bool load as
# ``None``) is deliberately not advertised (``minimum: 1``).
BLOCKED_ON_PR_JSON_SCHEMA: Final[JsonDict] = {
    "anyOf": [
        {"type": "integer", "minimum": 1},
        {
            "type": "object",
            "properties": {
                "repo": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                },
                "number": {"type": "integer", "minimum": 1},
            },
            "required": ["number"],
        },
        {"type": "null"},
    ]
}

PrRefKey = tuple[str | None, int]
"""A merge-evidence key: ``(repo lowercased, number)``, or ``(None, number)``."""


class PrRef(BaseModel):
    """One PR reference: a positive ``number`` and an optional ``owner/repo``."""

    model_config = ConfigDict(frozen=True)

    number: int
    repo: str | None = None

    @field_validator("number", mode="before")
    @classmethod
    def _check_number(cls, value: object) -> int:
        if not is_positive_real_int(value):
            msg = f"PR number must be a positive integer, got {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str | None) -> str | None:
        if value is not None and not is_repo_slug(value):
            msg = f"repo must be an owner/repo slug, got {value!r}"
            raise ValueError(msg)
        return value

    @classmethod
    def parse(cls, value: object) -> PrRef | None:
        """Parse ``N``, ``"N"`` or ``"owner/repo#N"`` into a ref, or None."""
        if isinstance(value, int):
            return cls.from_number(value)
        if not isinstance(value, str):
            return None
        if _BARE_NUMBER_RE.fullmatch(value) is not None:
            return cls(number=int(value))
        parsed = parse_repo_pr_ref(value)
        if parsed is None:
            return None
        return cls(repo=parsed[0], number=parsed[1])

    @classmethod
    def from_number(cls, number: object) -> PrRef | None:
        """Return a same-repo ref for a positive non-bool int, else None."""
        if not is_positive_real_int(number):
            return None
        return cls(number=number)

    def label(self) -> str:
        """Render ``#9`` for a same-repo ref, ``owner/repo#9`` for an explicit one."""
        if self.repo is None:
            return f"#{self.number}"
        return f"{self.repo}#{self.number}"

    def key(self) -> PrRefKey:
        """Return the merge-evidence key, the repo lowercased."""
        return (None if self.repo is None else self.repo.lower(), self.number)

    def normalized(self, own_slug: str | None) -> PrRef:
        """Drop a repo equal to *own_slug* (case-insensitively); keep any other."""
        if (
            self.repo is not None
            and own_slug is not None
            and self.repo.lower() == own_slug.lower()
        ):
            return PrRef(number=self.number)
        return self


def pr_evidence_keys(repo: str, number: int, *, cross_repo: bool) -> set[PrRefKey]:
    """Return the evidence keys one observed PR ``repo#number`` satisfies.

    An explicit ref always matches ``(repo.lower(), number)``. Only an own-repo
    observation (``cross_repo=False``) also feeds a same-repo ref's
    ``(None, number)`` lookup, so a merged ``other/repo#5`` never releases a
    same-repo hold on ``#5``.
    """
    keys: set[PrRefKey] = {(repo.lower(), number)}
    if not cross_repo:
        keys.add((None, number))
    return keys


def coerce_blocked_on(value: object) -> object:
    """Before-validator for ``TicketTask.blocked_on_pr``.

    A positive int (legacy, or a same-repo write-back) becomes
    ``PrRef(number=n)``. A non-positive or bool legacy int becomes ``None``
    rather than failing the whole store load. A dict or ``PrRef`` passes
    through to normal validation.
    """
    if isinstance(value, int):  # bool too: from_number rejects it
        return PrRef.from_number(value)
    return value


def dump_blocked_on(ref: PrRef | None) -> int | dict[str, object] | None:
    """Serializer for ``TicketTask.blocked_on_pr``: same-repo as a bare int."""
    if ref is None:
        return None
    if ref.repo is None:
        return ref.number
    return {"repo": ref.repo, "number": ref.number}
