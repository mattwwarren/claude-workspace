"""Round-trip load/dump of ``clients.yaml`` for the lane-editing commands.

``cw lane add``, ``cw lane rm`` and ``cw lane set`` (#2697) all edit
``clients.yaml`` in place with ruamel.yaml's round-trip mode so operator
comments survive. This module holds the one copy of that load/dump
boilerplate. Locking stays with each caller, inside its own command function,
so the bounded-lock audit in ``tests/test_config.py`` still sees every
``clients_lock(bounded=True)`` site where it was.

Round-tripping is byte-for-byte only for a file already in ruamel.yaml's
default style (2-space mapping indent, sequence dashes at their parent key's
indent, lines within 80 columns); any other layout is rewritten in that style.

:func:`to_plain` converts the round-trip types (``CommentedMap``,
``CommentedSeq`` and the ruamel scalar subclasses) to builtins, for
validating an edited entry with pydantic or putting its values in an event.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarbool import ScalarBoolean

from cw.atomic import atomic_write_text
from cw.exceptions import CwError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["dump_clients_doc", "load_clients_doc", "to_plain"]

# The document an absent ``clients.yaml`` is read as.
_EMPTY_CLIENTS_DOC = "clients:\n"


def load_clients_doc(path: Path) -> tuple[YAML, CommentedMap]:
    """Load *path* in round-trip mode; return the loader and the document.

    An absent file reads as an empty ``clients:`` map. A document with no
    top-level ``clients`` key raises :class:`CwError`. The caller must hold
    ``clients_lock`` across this load and the matching
    :func:`dump_clients_doc`.
    """
    rt = YAML(typ="rt")
    rt.default_flow_style = False
    content = path.read_text() if path.exists() else _EMPTY_CLIENTS_DOC
    doc = rt.load(content)
    if not isinstance(doc, CommentedMap) or "clients" not in doc:
        msg = f"{path} has no 'clients:' key."
        raise CwError(msg)
    return rt, doc


def dump_clients_doc(rt: YAML, doc: CommentedMap, path: Path) -> None:
    """Serialize *doc* with *rt* and atomically replace *path* with it."""
    buf = StringIO()
    rt.dump(doc, buf)
    atomic_write_text(path, buf.getvalue())


def to_plain(value: object) -> object:
    """Recursively convert ruamel round-trip values to builtin types.

    Mappings become ``dict``, sequences ``list``, and ruamel's ``str`` /
    ``int`` / ``float`` / ``bool`` subclasses their builtin base. Any other
    value (``None``, a timestamp) is returned unchanged.
    """
    if isinstance(value, dict):
        return {to_plain(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    return _plain_scalar(value)


def _plain_scalar(value: object) -> object:
    # ScalarBoolean (an anchored bool) subclasses int, not bool, so it is
    # tested first; a plain bool must not fall through to int().
    if isinstance(value, ScalarBoolean):
        return bool(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value
