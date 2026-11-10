"""The pure contract checker: manifest x doc scan x vocabularies -> violations.

``check_contract`` never touches the filesystem and imports no cw constant.
The caller supplies the backend set, the ``Status`` values, the known
``blocker.reason`` values and a :data:`~cw.contract_manifest._refs.Resolver`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.contract_manifest._models import (
    NATIVE_BACKEND,
    CodeRef,
    ContractViolation,
    FailsClosedDisposition,
    ImplementedDisposition,
    PromptRef,
    TicketRef,
)
from cw.contract_manifest._tags import ELEMENT_ID, SCOPES

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Sequence

    from cw.contract_manifest._models import (
        Cell,
        ContractElement,
        DocScan,
        ElementRef,
        Manifest,
        ViolationKind,
    )
    from cw.contract_manifest._refs import Resolver

__all__ = ["AUTHORING_RULE", "check_contract"]

#: Stated in every coverage failure, and in docs/headless-contract.md §4.5.
AUTHORING_RULE = (
    "tag every new contract rule: add a tagged element and list it in covers"
)


def _violation(
    kind: ViolationKind,
    detail: str,
    *,
    element_id: str | None = None,
    backend: str | None = None,
) -> ContractViolation:
    return ContractViolation(
        kind=kind, element_id=element_id, backend=backend, detail=detail
    )


def _ref_of(cell: Cell) -> ElementRef | None:
    if isinstance(cell, str):
        return None
    return getattr(cell, "ref", None)


def _cell_rule_violations(
    element_id: str, backend: str, cell: Cell
) -> Iterator[ContractViolation]:
    """The disposition-specific rules for one cell (refs are checked apart)."""
    if isinstance(cell, str):
        yield _violation(
            "todo_cell",
            "skeleton TODO cell; give it a disposition",
            element_id=element_id,
            backend=backend,
        )
    elif isinstance(cell, ImplementedDisposition):
        if backend != NATIVE_BACKEND and not isinstance(cell.ref, CodeRef | PromptRef):
            yield _violation(
                "implemented_needs_ref",
                "an implemented cell off claude-native needs a code or prompt ref",
                element_id=element_id,
                backend=backend,
            )
    elif isinstance(cell, FailsClosedDisposition):
        is_ticket = isinstance(cell.ref, TicketRef)
        ticket_needs_pending = is_ticket and not cell.guard_pending
        code_with_pending = isinstance(cell.ref, CodeRef) and cell.guard_pending
        if ticket_needs_pending or code_with_pending:
            yield _violation(
                "fails_closed_needs_guard",
                "fails-closed needs a code guard ref, or a ticket ref with "
                "guard_pending: true",
                element_id=element_id,
                backend=backend,
            )


def _ref_violations(
    element_id: str, backend: str, ref: ElementRef | None, resolver: Resolver
) -> Iterator[ContractViolation]:
    if ref is None:
        return
    if isinstance(ref, TicketRef):
        if ref.number < 1:
            yield _violation(
                "ticket_invalid",
                f"ticket number {ref.number} is not a positive integer",
                element_id=element_id,
                backend=backend,
            )
        return
    detail = resolver(ref)
    if detail is not None:
        yield _violation(
            "ref_unresolved", detail, element_id=element_id, backend=backend
        )


def _id_violations(element_id: str) -> Iterator[ContractViolation]:
    if ELEMENT_ID.fullmatch(element_id) is None:
        yield _violation(
            "id_malformed",
            "ids are <scope>.<kebab-slug>",
            element_id=element_id,
        )
        return
    scope = element_id.split(".", 1)[0]
    if scope not in SCOPES:
        yield _violation(
            "unknown_scope",
            f"scope {scope!r} is not one of {SCOPES}",
            element_id=element_id,
        )


def _element_violations(
    element_id: str,
    element: ContractElement,
    backends: Sequence[str],
    resolver: Resolver,
) -> Iterator[ContractViolation]:
    yield from _id_violations(element_id)
    for backend in backends:
        if backend not in element.cells:
            yield _violation(
                "missing_cell",
                "every element lists every backend",
                element_id=element_id,
                backend=backend,
            )
    for backend, cell in element.cells.items():
        if backend not in backends:
            yield _violation(
                "unknown_backend",
                f"backend {backend!r} is not one of {tuple(backends)}",
                element_id=element_id,
                backend=backend,
            )
        yield from _cell_rule_violations(element_id, backend, cell)
        yield from _ref_violations(element_id, backend, _ref_of(cell), resolver)


def _doc_violations(
    manifest: Manifest, scan: DocScan, backends: Sequence[str]
) -> Iterator[ContractViolation]:
    tagged = {tag.element_id for tag in scan.tags}
    for tag in scan.tags:
        if tag.element_id in manifest.elements:
            continue
        for backend in backends:
            yield ContractViolation(
                kind="missing_cell",
                element_id=tag.element_id,
                backend=backend,
                doc=tag.doc,
                line=tag.line,
                detail="doc tag has no manifest element",
            )
    for element_id in manifest.elements:
        if element_id not in tagged:
            yield _violation(
                "orphan_element",
                "manifest element has no tag in any doc",
                element_id=element_id,
            )


def _coverage_violations(
    manifest: Manifest, statuses: Collection[str], blocker_reasons: Collection[str]
) -> Iterator[ContractViolation]:
    covered: set[str] = set()
    for element_id, element in manifest.elements.items():
        for entry in element.covers:
            covered.add(entry)
            if entry not in statuses and entry not in blocker_reasons:
                yield _violation(
                    "covers_unknown",
                    f"covers entry {entry!r} is neither a Status nor a "
                    "known blocker.reason",
                    element_id=element_id,
                )
    for status in sorted(set(statuses) - covered):
        yield _violation(
            "status_not_covered", f"Status {status!r} is uncovered; {AUTHORING_RULE}"
        )
    for reason in sorted(set(blocker_reasons) - covered):
        yield _violation(
            "blocker_reason_not_covered",
            f"blocker.reason {reason!r} is uncovered; {AUTHORING_RULE}",
        )


def _sort_key(item: ContractViolation) -> tuple[str, str, str, str, int]:
    return (
        item.kind,
        item.element_id or "",
        item.backend or "",
        item.doc or "",
        item.line or 0,
    )


def check_contract(
    manifest: Manifest,
    scan: DocScan,
    *,
    backends: Sequence[str],
    statuses: Collection[str],
    blocker_reasons: Collection[str],
    resolver: Resolver,
) -> list[ContractViolation]:
    """Every contract gap between *manifest*, *scan* and the vocabularies.

    Scan-time violations pass through. The result is sorted by
    ``(kind, element_id, backend, doc, line)``, so it is deterministic.
    """
    found: list[ContractViolation] = list(scan.violations)
    if set(manifest.backends) != set(backends):
        found.append(
            _violation(
                "backend_set_drift",
                f"manifest backends {manifest.backends} != {tuple(backends)}",
            )
        )
    for element_id, element in manifest.elements.items():
        found.extend(_element_violations(element_id, element, backends, resolver))
    found.extend(_doc_violations(manifest, scan, backends))
    found.extend(_coverage_violations(manifest, statuses, blocker_reasons))
    return sorted(found, key=_sort_key)
