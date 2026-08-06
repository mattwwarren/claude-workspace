"""The session-scoped focus pointer store (#1644).

``~/.local/share/cw/focus.json`` answers "what is this session working on?" —
a bare ``{session_id: FocusEntry}`` map (the root IS the map; the shape is
pinned by the ticket, not chosen here) written by ``cw focus set/clear`` and
read by ``cw statusline render``.

Two contracts shape this module:

* **R3 — never crash a reader.** :func:`load_focus_store` returns ``{}`` for an
  absent, unreadable, malformed, or schema-invalid file, mirroring
  ``cw.config._load_concurrency_overrides``. ``statusline render`` is invoked on
  every assistant message; a corrupt store must degrade, not raise.
* **R4 — reuse the dev-queue lock discipline.** :func:`_lock` is structurally
  the same ``fcntl.flock`` over a dedicated sibling lock file as
  ``cw.dev_queue.storage._lock``; no new locking primitive is invented here.

R6 pins no expiry, no TTL, and no pruning: :func:`clear_focus` is the only
deletion path, and nothing inspects ``FocusEntry.set_at`` for staleness.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from cw.atomic import atomic_write_text
from cw.config import focus_file, focus_lock_file, refuse_real_state_write
from cw.models import FocusEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

# The on-disk root is the map itself, so validation goes through a TypeAdapter
# rather than a wrapping BaseModel — see the module docstring.
_STORE_ADAPTER: TypeAdapter[dict[str, FocusEntry]] = TypeAdapter(dict[str, FocusEntry])


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the focus store."""
    focus_file().parent.mkdir(parents=True, exist_ok=True)
    fd = focus_lock_file().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# Public alias, mirroring ``cw.dev_queue.storage.dev_queue_lock``. Prefer the
# higher-level helpers below; reach for this only to wrap a load → mutate →
# save sequence this module does not already provide.
focus_lock = _lock


def load_focus_store() -> dict[str, FocusEntry]:
    """Load the focus map from disk; return ``{}`` if absent or unusable.

    Fail-safe by design (R3) — see the module docstring.
    """
    path = focus_file()
    if not path.exists():
        return {}
    try:
        return _STORE_ADAPTER.validate_json(path.read_text())
    except (ValidationError, ValueError, OSError):
        return {}


def save_focus_store(store: dict[str, FocusEntry]) -> None:
    """Persist the focus map to disk atomically (caller should hold the lock)."""
    path = focus_file()
    refuse_real_state_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(_STORE_ADAPTER.dump_python(store, mode="json")))


def get_focus(session_id: str) -> FocusEntry | None:
    """Return *session_id*'s focus entry, or None when it has none."""
    return load_focus_store().get(session_id)


def set_focus(session_id: str, client: str, lane: str | None = None) -> FocusEntry:
    """Point *session_id* at *client* (optionally a specific *lane*).

    Replaces any prior entry for the session and returns the new one. Callers
    are responsible for validating *client*/*lane* against ``clients.yaml``
    first — this layer only persists.
    """
    entry = FocusEntry(client=client, lane=lane)
    with _lock():
        store = load_focus_store()
        store[session_id] = entry
        save_focus_store(store)
    return entry


def clear_focus(session_id: str) -> None:
    """Drop *session_id*'s focus entry. Idempotent on an unknown session."""
    with _lock():
        store = load_focus_store()
        if store.pop(session_id, None) is None:
            return
        save_focus_store(store)
