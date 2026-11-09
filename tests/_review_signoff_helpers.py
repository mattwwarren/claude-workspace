"""Shared builders for the ``cw review signoff render`` tests (#3043, RFC 0017 II2).

Plain functions, no fixtures (the sibling ``_*_helpers.py`` shape). PR-body
builders compose the sections Step 4d writes (adjudication, operator override)
and the invoker drives ``cw review signoff render`` through the attested setup
from ``tests._review_attestation_helpers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_adjudication import render_deferred_findings_md

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

    from cw.review_adjudication import Adjudication
    from tests._review_attestation_helpers import AttestedSetup


def adjudication_section(*entries: Adjudication) -> str:
    """The ``## Review adjudication`` block with the DEFERRED-REVIEW-FINDINGS list."""
    return "## Review adjudication\n\n" + render_deferred_findings_md(list(entries))


def override_section(actor: str, reason: str) -> str:
    """The Step 4d ``## Operator override`` block."""
    return f"## Operator override\n\n- actor: {actor}\n- reason: {reason}\n"


def compose_pr_body(*sections: str, signoff: str | None = None) -> str:
    """A PR body: the given sections, then an optional sign-off block last."""
    parts = ["# Summary\n\nWhat changed.\n", *sections]
    if signoff is not None:
        parts.append(signoff)
    return "\n\n".join(part.rstrip("\n") for part in parts) + "\n"


def pr_body_with_signoff(section: str, *tail: str) -> str:
    """A body whose sign-off section sits in the middle, followed by ``tail``."""
    return compose_pr_body(section, *tail)


def write_body(setup: AttestedSetup, name: str, text: str) -> Path:
    """Write a body file under the worktree's ``.cw`` and return its path."""
    path = setup.worktree / ".cw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def render_signoff(
    setup: AttestedSetup,
    body_file: Path,
    pr_head: str,
    *,
    current_body_file: Path | None = None,
    extra: tuple[str, ...] = (),
) -> Result:
    """Invoke ``cw review signoff render`` for ``setup``'s worktree."""
    args: list[str] = ["--body-file", str(body_file), "--pr-head", pr_head]
    if current_body_file is not None:
        args += ["--current-body-file", str(current_body_file)]
    return setup.invoke(("review", "signoff", "render"), *args, *extra)
