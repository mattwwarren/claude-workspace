"""Pin every ``feature_branch_prefix`` read under ``src/`` to the branch seam (#2948).

A ticket's branch is resolved in one place,
:func:`cw.models.resolve_feature_branch`: the row's recorded ``branch`` when
set, else ``<feature_branch_prefix>/<ticket_id>``. A site that reads the prefix
itself skips the row's branch, so a ticket on any other branch (an adopted PR)
would be invisible to it. Only the resolver and the field's own home may read
the attribute. ``tests/`` is out of scope: tests build prefixed names on
purpose.

The deleted ``feature_branch_key`` helper must not come back either, under any
spelling of a call or alias, so its name is banned from ``src/`` outright.
"""

from __future__ import annotations

import ast

from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files

_PREFIX_ATTR = "feature_branch_prefix"
_DELETED_HELPER = "feature_branch_key"
_RESOLVER_MODULE = "cw/models/branch.py"
_PREFIX_ATTR_ALLOWLIST = frozenset({_RESOLVER_MODULE, "cw/models/client.py"})


def _prefix_attribute_lines(source: str, relpath: str) -> list[int]:
    """Line numbers of every ``<expr>.feature_branch_prefix`` in *source*."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source, filename=relpath))
        if isinstance(node, ast.Attribute) and node.attr == _PREFIX_ATTR
    )


def _scan_prefix_attributes() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _iter_src_files():
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        lines = _prefix_attribute_lines(path.read_text(encoding="utf-8"), relpath)
        if lines:
            found[relpath] = lines
    return found


def _violations(found: dict[str, list[int]]) -> dict[str, list[int]]:
    return {
        relpath: lines
        for relpath, lines in found.items()
        if relpath not in _PREFIX_ATTR_ALLOWLIST
    }


def test_prefix_is_read_only_by_the_resolver() -> None:
    found = _scan_prefix_attributes()

    assert _violations(found) == {}, (
        f"{_PREFIX_ATTR} read outside the branch seam: resolve the branch with "
        "cw.models.resolve_feature_branch(ticket_id, client, task) instead, "
        f"passing the row when it is in scope. Found: {_violations(found)}"
    )


def test_allowlist_is_not_vacuous() -> None:
    """The resolver itself reads the prefix, so a scan that finds nothing there
    means the scan is broken, not that the code is clean."""
    assert _RESOLVER_MODULE in _scan_prefix_attributes()


def test_collector_flags_a_prefix_read_outside_the_allowlist() -> None:
    source = (
        "x = client.feature_branch_prefix\n"
        "y = f'{cfg.feature_branch_prefix}/{tid}'\n"
        "z = 'feature_branch_prefix'\n"
    )
    relpath = "cw/dispatch/new_site.py"

    lines = _prefix_attribute_lines(source, relpath)

    assert lines == [1, 2]
    assert _violations({relpath: lines}) == {relpath: [1, 2]}
    assert _violations({_RESOLVER_MODULE: lines}) == {}


def test_deleted_helper_name_is_gone_from_src() -> None:
    offenders = [
        path.relative_to(_SRC_ROOT).as_posix()
        for path in _iter_src_files()
        if _DELETED_HELPER in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
