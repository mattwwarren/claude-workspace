"""Pydantic models and the YAML loader for the contract element manifest.

Shape rules live here: a malformed cell stops at load with
:class:`ManifestLoadError`. Rules that need context (the backend set, the
docs, a resolver, the ``Status`` vocabulary) live in ``_check.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND as NATIVE_BACKEND,
)

if TYPE_CHECKING:
    from collections.abc import Hashable

__all__ = [
    "DISPOSITIONS",
    "NATIVE_BACKEND",
    "TODO_CELL",
    "BackendDisposition",
    "Cell",
    "CodeRef",
    "ContractElement",
    "ContractViolation",
    "DeferredDisposition",
    "DocRef",
    "DocScan",
    "DocTag",
    "ElementRef",
    "FailsClosedDisposition",
    "ImplementedDisposition",
    "Manifest",
    "ManifestLoadError",
    "NotApplicableDisposition",
    "PromptRef",
    "TextRef",
    "TicketRef",
    "ViolationKind",
    "parse_manifest",
]

#: The skeleton-only cell value (RFC 0018 S1 seeding step 2). It parses, and
#: ``check_contract`` flags it as ``todo_cell``.
TODO_CELL = "TODO"

Disposition = Literal["implemented", "fails-closed", "deferred", "not-applicable"]
DISPOSITIONS: tuple[str, ...] = get_args(Disposition)

ViolationKind = Literal[
    # Tags
    "tag_malformed",
    "tag_in_fence",
    "tag_in_table",
    "tag_reserved_range",
    "tag_mid_paragraph",
    "tag_bad_anchor",
    "tag_duplicate",
    "tag_wrong_scope",
    "unknown_scope",
    # Manifest vs docs
    "missing_cell",
    "orphan_element",
    "unknown_backend",
    "backend_set_drift",
    "todo_cell",
    "id_malformed",
    # Cell rules
    "implemented_needs_ref",
    "fails_closed_needs_guard",
    "ref_unresolved",
    "ticket_invalid",
    # Coverage
    "status_not_covered",
    "blocker_reason_not_covered",
    "covers_unknown",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ManifestLoadError(ValueError):
    """The manifest text is not valid YAML or violates a model shape rule."""


class ContractViolation(BaseModel):
    """One contract gap found by the scanner or the checker."""

    model_config = _STRICT

    kind: ViolationKind
    element_id: str | None = None
    backend: str | None = None
    doc: str | None = None
    line: int | None = None
    detail: str

    def __str__(self) -> str:
        where = f"{self.doc}:{self.line}" if self.doc is not None else "-"
        return (
            f"{self.kind}: {self.element_id or '-'} [{self.backend or '-'}] "
            f"{where} -- {self.detail}"
        )


class DocTag(BaseModel):
    """One strict ``<!-- el: <id> -->`` tag found in a command doc."""

    model_config = _STRICT

    element_id: str
    scope: str
    doc: str
    line: int


class DocScan(BaseModel):
    """Every tag the scanner collected, plus its scan-time violations."""

    model_config = _STRICT

    tags: tuple[DocTag, ...]
    violations: tuple[ContractViolation, ...]


class CodeRef(BaseModel):
    """A top-level or dotted symbol in a repo Python file, resolved by AST."""

    model_config = _STRICT

    kind: Literal["code"]
    path: str = Field(min_length=1)
    symbol: str = Field(min_length=1)


class TextRef(BaseModel):
    """A literal substring of a repo file."""

    model_config = _STRICT

    kind: Literal["text"]
    path: str = Field(min_length=1)
    needle: str = Field(min_length=1)


class PromptRef(BaseModel):
    """A literal substring of the opencode prompt for one stage."""

    model_config = _STRICT

    kind: Literal["prompt"]
    stage: Literal["plan", "impl", "review", "finalize"]
    needle: str = Field(min_length=1)


class TicketRef(BaseModel):
    """A tracker ticket number. Format-checked only: tests cannot call ``gh``."""

    model_config = _STRICT

    kind: Literal["ticket"]
    number: StrictInt


class DocRef(BaseModel):
    """A repo file that must exist."""

    model_config = _STRICT

    kind: Literal["doc"]
    path: str = Field(min_length=1)


ElementRef = Annotated[
    CodeRef | TextRef | PromptRef | TicketRef | DocRef,
    Field(discriminator="kind"),
]
_GuardRef = Annotated[CodeRef | TicketRef, Field(discriminator="kind")]


class ImplementedDisposition(BaseModel):
    """The backend carries the whole obligation."""

    model_config = _STRICT

    disposition: Literal["implemented"]
    ref: ElementRef | None = None
    note: str | None = None


class FailsClosedDisposition(BaseModel):
    """A guard blocks every run that would otherwise go wrong silently."""

    model_config = _STRICT

    disposition: Literal["fails-closed"]
    ref: _GuardRef
    guard_pending: bool = False
    note: str | None = None


class DeferredDisposition(BaseModel):
    """Not carried yet; the cited ticket completes it."""

    model_config = _STRICT

    disposition: Literal["deferred"]
    ref: TicketRef
    note: str | None = None


class NotApplicableDisposition(BaseModel):
    """The obligation cannot arise on this backend."""

    model_config = _STRICT

    disposition: Literal["not-applicable"]
    reason: str = Field(min_length=1)
    note: str | None = None


BackendDisposition = Annotated[
    ImplementedDisposition
    | FailsClosedDisposition
    | DeferredDisposition
    | NotApplicableDisposition,
    Field(discriminator="disposition"),
]
Cell = BackendDisposition | Literal["TODO"]


class ContractElement(BaseModel):
    """One tagged contract element: the vocabulary it covers and its cells."""

    model_config = _STRICT

    covers: tuple[str, ...] = ()
    cells: dict[str, Cell]


class Manifest(BaseModel):
    """The whole manifest file."""

    model_config = _STRICT

    schema_version: Literal[1]
    backends: tuple[str, ...]
    elements: dict[str, ContractElement]

    def ids_with(self, backend: str, disposition: str) -> tuple[str, ...]:
        """Sorted ids whose *backend* cell has *disposition*.

        An unknown backend yields an empty tuple; an unknown disposition
        raises ``ValueError``.
        """
        if disposition not in DISPOSITIONS:
            msg = f"unknown disposition {disposition!r}; expected one of {DISPOSITIONS}"
            raise ValueError(msg)
        return tuple(
            sorted(
                element_id
                for element_id, element in self.elements.items()
                if _disposition_of(element.cells.get(backend)) == disposition
            )
        )


def _disposition_of(cell: Cell | None) -> str | None:
    if cell is None or isinstance(cell, str):
        return None
    return cell.disposition


class _UniqueKeyLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that rejects duplicate mapping keys.

    Plain ``safe_load`` keeps the last duplicate silently, which would hide a
    duplicated element id.
    """


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode
) -> dict[Hashable, object]:
    loader.flatten_mapping(node)
    seen: set[str] = set()
    for key_node, _value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        key = str(key_node.value)
        if key in seen:
            msg = f"duplicate key {key!r}"
            raise yaml.constructor.ConstructorError(
                None, None, msg, key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_manifest(text: str) -> Manifest:
    """Parse and validate manifest *text*; raise :class:`ManifestLoadError`."""
    loader = _UniqueKeyLoader(text)
    try:
        raw = loader.get_single_data()
    except yaml.YAMLError as exc:
        msg = f"contract manifest is not valid YAML: {exc}"
        raise ManifestLoadError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"contract manifest root must be a mapping, got {type(raw).__name__}"
        raise ManifestLoadError(msg)
    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        msg = f"contract manifest failed validation: {exc}"
        raise ManifestLoadError(msg) from exc
