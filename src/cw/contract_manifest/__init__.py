"""The contract element manifest (RFC 0018 S1, #3053).

The stage contracts live as prose in thirteen command docs
(:data:`DOC_SCOPES`). Each contract element carries a stable id in a
single-line HTML comment tag, and ``src/cw/data/contract_manifest.yaml``
gives every id one disposition per backend. ``tests/test_contract_manifest.py``
fails on any gap. This package is a cw-leaf: its only cw import is
:mod:`cw.markdown_fence`.

**Authoring rule: tag every new contract rule.** A new rule in a command doc
gets a tag and a manifest element with all four cells, and every ``Status``
value and known ``blocker.reason`` must sit in some element's ``covers``.

**Element unit.** An element is one of the following, matched on column-0
lines outside fences and tables:

1. A bold-lead or ``##``-``####`` heading line that carries an issue reference
   ``#\\d{3,5}``. A numbered lead may carry a one- or two-level number
   (``6.``, ``3.6.``).
2. A named sentinel field, marker or gate, tagged on the nearest column-0
   bold-lead or heading that defines it. A field that lives only in a fence or
   a table gets its tag on the enclosing bold-lead.
3. One whole table, such as ``orch.blocker-reason-table`` or
   ``orch.status-enum``. Rows are never enumerated.
4. An RFC 0017 member (below). A member on an indented bold-lead is tagged
   with a column-0 tag above it; other indented bold-leads are tagged only
   when rule 2 names them.

**RFC 0017 members.** An element is an RFC 0017 member when ``RFC 0017``
appears in its lead: the heading text, or the first bold run of a bold-lead
(up to the closing ``**``). A later mention in the same line's prose does not
count, and table rows never count.

**Tag grammar.** A tag is a whole column-0 line ``<!-- el: <id> -->``
(:data:`STRICT_TAG`). Any other line matching :data:`TAG_SHAPED` is
``tag_malformed``; ``el-begin`` / ``el-end`` are reserved for RFC 0018 A3
(#3058); a tag-shaped line inside a fence or a table is ``tag_in_fence`` /
``tag_in_table``. Placement is checked per tag, in this order, with at most
one placement violation:

1. ``tag_mid_paragraph``: the line above is prose (non-blank, column 0, not a
   heading or list item) and the anchor (the line below) is non-blank and not
   a block start (a heading, a list item, or a column-0 ``**`` line).
2. ``tag_bad_anchor``: otherwise the anchor must be a heading or a bold-lead
   (``**``, optionally indented and optionally after ``- ``, ``* `` or a
   ``1.`` / ``3.6.`` list marker).

**Id grammar.** ``<scope>.<kebab-slug>`` with the scope one of :data:`SCOPES`
and matching the doc. The slug is the kebab-case of the first five or fewer
meaningful words of the lead, dropping issue refs, parentheses, RFC tags, step
numbers and the stop words ``the`` / ``a``. A backtick code span is one word
(``_`` becomes ``-``). Named things take the prefixes ``sentinel-``,
``marker-`` and ``gate-``. A collision is suffixed with a more specific noun,
never a number, and ids never carry ticket numbers. Ids are global and each
is defined by exactly one tag.
"""

from __future__ import annotations

import importlib.resources

from cw.contract_manifest._check import AUTHORING_RULE, check_contract
from cw.contract_manifest._models import (
    DISPOSITIONS,
    NATIVE_BACKEND,
    TODO_CELL,
    BackendDisposition,
    Cell,
    CodeRef,
    ContractElement,
    ContractViolation,
    DeferredDisposition,
    DocRef,
    DocScan,
    DocTag,
    ElementRef,
    FailsClosedDisposition,
    ImplementedDisposition,
    Manifest,
    ManifestLoadError,
    NotApplicableDisposition,
    PromptRef,
    TextRef,
    TicketRef,
    ViolationKind,
    parse_manifest,
)
from cw.contract_manifest._refs import Resolver, make_resolver
from cw.contract_manifest._tags import (
    DOC_SCOPES,
    ELEMENT_ID,
    SCOPES,
    STRICT_TAG,
    TAG_SHAPED,
    scan_doc_tags,
    strip_doc_tags,
)

__all__ = [
    "AUTHORING_RULE",
    "DISPOSITIONS",
    "DOC_SCOPES",
    "ELEMENT_ID",
    "MANIFEST_RESOURCE",
    "NATIVE_BACKEND",
    "SCOPES",
    "STRICT_TAG",
    "TAG_SHAPED",
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
    "Resolver",
    "TextRef",
    "TicketRef",
    "ViolationKind",
    "check_contract",
    "elements_with",
    "load_manifest",
    "make_resolver",
    "parse_manifest",
    "scan_doc_tags",
    "strip_doc_tags",
]

#: The manifest's path inside the ``cw`` package, read like ``data/GUIDE.md``.
MANIFEST_RESOURCE = "data/contract_manifest.yaml"


def load_manifest() -> Manifest:
    """Parse the packaged manifest; raise :class:`ManifestLoadError`."""
    text = (
        importlib.resources.files("cw")
        .joinpath(MANIFEST_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return parse_manifest(text)


def elements_with(backend: str, disposition: str) -> tuple[str, ...]:
    """Sorted ids of the packaged manifest whose *backend* cell has *disposition*.

    An unknown backend yields an empty tuple; an unknown disposition raises
    ``ValueError``.
    """
    return load_manifest().ids_with(backend, disposition)
