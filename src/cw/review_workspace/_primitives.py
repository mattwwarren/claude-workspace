"""Filesystem and identifier primitives every other submodule builds on.

Error construction, identifier and role-slug validation, the ``.cw``
directory guard, ``lstat``-based path classification, and JSON object
read/atomic write. Imports ``_constants`` only.
"""

from __future__ import annotations

import json
import re
import stat
from typing import TYPE_CHECKING, Literal

from cw.atomic import atomic_write_text
from cw.exceptions import CwError
from cw.review_workspace._constants import _ID_RE, _SLUG_RE

if TYPE_CHECKING:
    from pathlib import Path


def _error(message: str) -> CwError:
    return CwError(message)


def _safe_component(value: str, label: str) -> str:
    if not _ID_RE.fullmatch(value) or value in {".", ".."}:
        _error_message = f"invalid review workspace {label}: {value!r}"
        raise _error(_error_message)
    return value


def _role_slug(role: str) -> str:
    normalized = role.strip().lower().replace(" ", "-").replace("/", "-")
    normalized = re.sub(r"-+", "-", normalized)
    if not role.strip() or not _SLUG_RE.fullmatch(normalized):
        _error_message = f"invalid reviewer role name: {role!r}"
        raise _error(_error_message)
    return normalized


def _ensure_cw(root: Path) -> Path:
    cw_dir = root / ".cw"
    if cw_dir.is_symlink():
        _error_message = f"review workspace directory must not be a symlink: {cw_dir}"
        raise _error(_error_message)
    try:
        cw_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _error_message = f"cannot create review workspace directory {cw_dir}: {exc}"
        raise _error(_error_message) from exc
    if not cw_dir.is_dir():
        _error_message = f"review workspace path is not a directory: {cw_dir}"
        raise _error(_error_message)
    return cw_dir


def _kind(path: Path) -> Literal["absent", "directory", "file", "symlink", "other"]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _read_object(path: Path, label: str) -> dict[str, object]:
    if _kind(path) != "file":
        _error_message = f"{label} is not a regular file: {path}"
        raise _error(_error_message)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _error_message = f"cannot read {label} at {path}: {exc}"
        raise _error(_error_message) from exc
    if not isinstance(value, dict):
        _error_message = f"{label} at {path} must contain a JSON object"
        raise _error(_error_message)
    return value


def _write_object(path: Path, value: dict[str, object], label: str) -> None:
    try:
        atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        _error_message = f"cannot atomically write {label} at {path}: {exc}"
        raise _error(_error_message) from exc
