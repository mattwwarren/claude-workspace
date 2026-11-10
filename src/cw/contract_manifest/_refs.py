"""Resolve manifest references against a repo tree, without importing cw.

``code`` refs are found by AST, so a cited module is never imported. A
``prompt`` ref is resolved through an injected ``prompt_for_stage`` callable,
which keeps this package free of an import of the opencode prompt builder.
``ticket`` refs are format-checked by the checker and never looked up here.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from cw.contract_manifest._models import (
    CodeRef,
    DocRef,
    ElementRef,
    PromptRef,
    TextRef,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Resolver", "make_resolver"]

#: ``None`` means the ref resolves; a string is the reason it does not.
Resolver = Callable[[ElementRef], str | None]

_PYTHON_SUFFIX = ".py"


class _RefError(Exception):
    """Internal: carries the detail of an unresolved ref."""


def _repo_file(repo_root: Path, relative: str) -> Path:
    """The existing file *relative* names under *repo_root*."""
    if Path(relative).is_absolute():
        msg = f"path {relative!r} is absolute; refs are repo-relative"
        raise _RefError(msg)
    root = repo_root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        msg = f"path {relative!r} escapes the repo root"
        raise _RefError(msg)
    if not target.is_file():
        msg = f"file {relative!r} does not exist"
        raise _RefError(msg)
    return target


def _defined_names(node: ast.stmt) -> tuple[str, ...]:
    """The names a top-level or class-body statement binds."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return (node.name,)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return (node.target.id,)
    if isinstance(node, ast.Assign):
        return tuple(t.id for t in node.targets if isinstance(t, ast.Name))
    return ()


def _find_symbol(body: Sequence[ast.stmt], parts: Sequence[str]) -> bool:
    """Whether the dotted *parts* resolve in *body*, descending into classes."""
    head, rest = parts[0], parts[1:]
    for node in body:
        if head not in _defined_names(node):
            continue
        if not rest:
            return True
        if isinstance(node, ast.ClassDef):
            return _find_symbol(node.body, rest)
    return False


def _resolve_code(repo_root: Path, ref: CodeRef) -> None:
    if not ref.path.endswith(_PYTHON_SUFFIX):
        msg = f"code ref path {ref.path!r} is not a .py file"
        raise _RefError(msg)
    target = _repo_file(repo_root, ref.path)
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=ref.path)
    except SyntaxError as exc:
        msg = f"{ref.path} does not parse: {exc.msg}"
        raise _RefError(msg) from exc
    if not _find_symbol(tree.body, ref.symbol.split(".")):
        msg = f"symbol {ref.symbol!r} not defined in {ref.path}"
        raise _RefError(msg)


def _resolve_text(repo_root: Path, ref: TextRef) -> None:
    target = _repo_file(repo_root, ref.path)
    if ref.needle not in target.read_text(encoding="utf-8"):
        msg = f"needle {ref.needle!r} not found in {ref.path}"
        raise _RefError(msg)


def make_resolver(repo_root: Path, prompt_for_stage: Callable[[str], str]) -> Resolver:
    """A resolver for refs under *repo_root*, prompts via *prompt_for_stage*.

    A ``KeyError`` from *prompt_for_stage* (an unsupported stage) is an
    unresolved ref. Each stage's prompt is built at most once.
    """
    prompts: dict[str, str] = {}

    def _resolve_prompt(ref: PromptRef) -> None:
        if ref.stage not in prompts:
            try:
                prompts[ref.stage] = prompt_for_stage(ref.stage)
            except KeyError as exc:
                msg = f"no prompt for stage {ref.stage!r}: {exc}"
                raise _RefError(msg) from exc
        if ref.needle not in prompts[ref.stage]:
            msg = f"needle {ref.needle!r} not in the {ref.stage} prompt"
            raise _RefError(msg)

    def resolve(ref: ElementRef) -> str | None:
        try:
            if isinstance(ref, CodeRef):
                _resolve_code(repo_root, ref)
            elif isinstance(ref, TextRef):
                _resolve_text(repo_root, ref)
            elif isinstance(ref, DocRef):
                _repo_file(repo_root, ref.path)
            elif isinstance(ref, PromptRef):
                _resolve_prompt(ref)
            # A TicketRef is format-checked by the checker, never looked up.
        except _RefError as exc:
            return str(exc)
        return None

    return resolve
