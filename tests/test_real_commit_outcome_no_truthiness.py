"""Forbid truthiness reads of ``Review.had_real_commit`` under ``src/cw/`` (#1745).

``Review.had_real_commit`` is the named tri-state ``RealCommitOutcome``
(``"committed"`` / ``"no_op"`` / ``"unknown"``), not a bool. Every value is a
non-empty string, so ``if review.had_real_commit:`` is always true and
``review.had_real_commit is False`` is always false: both silently reintroduce
the bug the retype exists to remove. Consumers must compare against a named
value (``== "no_op"``).

The scan is scoped to a ``Review`` receiver -- a bare ``review`` name or any
``<expr>.review`` attribute -- because the ``codex_fix_loop`` tracker and its
``had_real_commit: bool`` parameters are legitimately boolean while the loop
runs. A ``.had_real_commit`` read on such a receiver fails the scan when it is:

- the test of an ``if`` / ``while`` / ternary / ``assert``;
- an operand of ``not``, ``and`` or ``or``;
- compared with ``is`` / ``is not`` against ``None`` / ``True`` / ``False``.
"""

from __future__ import annotations

import ast

from tests.conftest import _SRC_ROOT, _files_under

_FIELD = "had_real_commit"
_RECEIVER = "review"
_SCAN_PREFIX = "cw/"
# The one production consumer; the scan must see it or it proves nothing.
_KNOWN_CONSUMER = "cw/codex_review/_verdict/_render.py"
_LEGACY_SINGLETONS = (None, True, False)


def _is_review_outcome(node: ast.expr) -> bool:
    """True for ``review.had_real_commit`` and ``<expr>.review.had_real_commit``."""
    if not isinstance(node, ast.Attribute) or node.attr != _FIELD:
        return False
    receiver = node.value
    if isinstance(receiver, ast.Name):
        return receiver.id == _RECEIVER
    return isinstance(receiver, ast.Attribute) and receiver.attr == _RECEIVER


def _is_legacy_singleton(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and any(
        node.value is singleton for singleton in _LEGACY_SINGLETONS
    )


def _identity_offenders(node: ast.Compare) -> list[ast.expr]:
    """Operands of an ``is``/``is not`` comparison against a legacy singleton."""
    operands = [node.left, *node.comparators]
    offenders: list[ast.expr] = []
    for index, op in enumerate(node.ops):
        if not isinstance(op, (ast.Is, ast.IsNot)):
            continue
        left, right = operands[index], operands[index + 1]
        if _is_review_outcome(left) and _is_legacy_singleton(right):
            offenders.append(left)
        if _is_review_outcome(right) and _is_legacy_singleton(left):
            offenders.append(right)
    return offenders


def _truthiness_operands(node: ast.AST) -> list[ast.expr]:
    """The expressions *node* evaluates for truthiness, if any."""
    if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
        return [node.test]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return [node.operand]
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    return []


def _offending_lines(source: str) -> list[int]:
    """Line numbers of every truthiness/identity read of the outcome field."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        candidates = [
            operand
            for operand in _truthiness_operands(node)
            if _is_review_outcome(operand)
        ]
        if isinstance(node, ast.Compare):
            candidates.extend(_identity_offenders(node))
        lines.extend(candidate.lineno for candidate in candidates)
    return sorted(lines)


def _scan() -> dict[str, list[int]]:
    """Map each scanned ``src/`` relpath to its offending line numbers."""
    return {
        path.relative_to(_SRC_ROOT).as_posix(): _offending_lines(
            path.read_text(encoding="utf-8")
        )
        for path in _files_under(_SCAN_PREFIX)
    }


def test_no_truthiness_read_of_review_had_real_commit() -> None:
    offenders = {relpath: lines for relpath, lines in _scan().items() if lines}
    assert offenders == {}


def test_scan_covers_the_known_consumer() -> None:
    """The scan must reach the renderer, and the renderer must still read the
    field through a ``review`` receiver, or the guard is vacuous."""
    path = _SRC_ROOT / _KNOWN_CONSUMER
    assert _KNOWN_CONSUMER in _scan()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and _is_review_outcome(node)
    ]
    assert reads


def test_self_test_flags_every_offending_shape() -> None:
    snippet = "\n".join(
        [
            "if review.had_real_commit:",  # 1
            "    pass",
            "while out.review.had_real_commit:",  # 3
            "    pass",
            "x = 1 if review.had_real_commit else 2",  # 5
            "assert review.had_real_commit",  # 6
            "y = not review.had_real_commit",  # 7
            "z = flag and review.had_real_commit",  # 8
            "w = review.had_real_commit or flag",  # 9
            "a = review.had_real_commit is None",  # 10
            "b = verdict.review.had_real_commit is True",  # 11
            "c = review.had_real_commit is not False",  # 12
            "d = False is review.had_real_commit",  # 13
        ]
    )
    assert _offending_lines(snippet) == [1, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13]


def test_self_test_allows_named_comparisons_and_internal_bools() -> None:
    snippet = "\n".join(
        [
            'if review.had_real_commit == "no_op":',
            "    pass",
            'ok = review.had_real_commit != "unknown"',
            "if had_real_commit:",  # the fix loop's internal bool
            "    pass",
            "if tracker.had_real_commit:",  # not a Review receiver
            "    pass",
            "if make().had_real_commit:",  # call receiver, not a Review name
            "    pass",
            "e = review.had_real_commit is other",  # not a legacy singleton
            "f = review.had_real_commit < 1",  # not an identity op
        ]
    )
    assert _offending_lines(snippet) == []
