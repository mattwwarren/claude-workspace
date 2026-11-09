"""The ``review.engine`` block of ``.claude/cw-context.json`` (RFC 0017 I1).

The engine reaches a worker only through this file, stamped at spawn from the
client's ``review.engine``. A context without the block reads as no engine,
and :func:`read_engine_context` fails closed on it: an engine round never falls
back to the built-in roster.

This module does not import ``cw.cli``. The JSON reader is a deliberate small
duplicate of the CLI's, accepted for layering (RFC 0017 R1).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.exceptions import CwError

if TYPE_CHECKING:
    from pathlib import Path

#: Branch used when neither the flag nor the context names one.
FALLBACK_DEFAULT_BRANCH = "main"


@dataclass(frozen=True)
class EngineContext:
    """What an engine round needs from the worker's context file."""

    skill: str
    guarded_paths: tuple[str, ...]
    ticket_id: str | None
    client: str | None
    default_branch: str


def engine_from_review_obj(
    review_obj: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """``(skill_name, error)`` from a context's ``review`` object.

    Absent or null ``engine`` is ``(None, None)``. A present but malformed one
    returns ``(None, reason)`` so the caller can report it, never a guess.
    """
    engine = review_obj.get("engine")
    if engine is None:
        return None, None
    if not isinstance(engine, Mapping):
        return None, "review.engine is not an object"
    skill = engine.get("skill")
    if not isinstance(skill, str) or not skill:
        return None, "review.engine.skill is missing or not a string"
    return skill, None


def load_context_object(context_path: Path) -> dict[str, object]:
    """The parsed context object, or ``{}`` when unreadable or not an object."""
    try:
        data = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _guarded(engine: object) -> tuple[str, ...]:
    if not isinstance(engine, Mapping):
        return ()
    raw = engine.get("guarded_paths", [])
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _default_branch(data: Mapping[str, object], explicit: str | None) -> str:
    if explicit:
        return explicit
    snapshot = data.get("world_state_snapshot")
    if isinstance(snapshot, Mapping):
        branch = snapshot.get("origin_main_branch")
        if isinstance(branch, str) and branch:
            return branch
    return FALLBACK_DEFAULT_BRANCH


def read_engine_context(
    context_path: Path, *, default_branch: str | None = None
) -> EngineContext:
    """The engine context at *context_path*, or :class:`CwError` when absent.

    Fail closed: an unreadable file, a missing ``review.engine`` or a malformed
    one all raise, so the caller parks or exits instead of running the
    built-in roster.
    """
    data = load_context_object(context_path)
    review = data.get("review")
    if not isinstance(review, Mapping):
        msg = (
            f"no review.engine in {context_path} (context unreadable or below "
            "schema 19)"
        )
        raise CwError(msg)
    skill, error = engine_from_review_obj(review)
    if skill is None:
        msg = error or f"no review.engine in {context_path}"
        raise CwError(msg)
    ticket = data.get("ticket_id")
    client = data.get("client")
    return EngineContext(
        skill=skill,
        guarded_paths=_guarded(review.get("engine")),
        ticket_id=ticket if isinstance(ticket, str) else None,
        client=client if isinstance(client, str) else None,
        default_branch=_default_branch(data, default_branch),
    )
