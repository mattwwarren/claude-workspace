#!/usr/bin/env python3
"""Render a ``## Decisions`` markdown section from a sentinel's friction reports.

Reads the JSON output of ``parse_sentinel.py`` on stdin (or from a file with
``--input``) and emits a markdown section that can be appended to a GitHub
issue body. Each ambiguity becomes a Q+A with the plan's default surfaced as
the accepted answer; each premise becomes a verification statement.

In ``--auto-accept-defaults`` mode every default is taken as-is — matches the
pattern used to clear #170 v2's five ambiguities in one pass. Without the
flag the script emits a stub that the human can edit.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_DECISIONS_HEADING = "## Decisions"


def _render_ambiguity(idx: int, item: dict[str, Any], auto_accept: bool) -> str:
    question = item.get("question", "(missing question)")
    default = item.get("plan_assumption", "(no default proposed)")
    alternatives = item.get("alternatives") or []
    why = item.get("why_it_matters", "")

    lines = [
        f"### A{idx}. {question}",
        "",
    ]
    if auto_accept:
        lines.append(f"**Decision:** accept plan default — _{default}_.")
    else:
        lines.append(f"**Plan default:** _{default}_.")
        lines.append("")
        lines.append("**Decision:** _<accept default | choose alternative | other>_")
        if alternatives:
            lines.append("")
            lines.append("Alternatives surfaced by the plan:")
            lines.extend(f"- {alt}" for alt in alternatives)
    if why:
        lines.append("")
        lines.append(f"_Why it matters: {why}_")
    return "\n".join(lines)


def _render_premise(idx: int, item: dict[str, Any], auto_accept: bool) -> str:
    # Producer emits two shapes today. Pre-verification (Plan Soundness output
    # surfaced before the human dispositions it) uses "premise" / "verify_by".
    # Post-verification (Plan Soundness's already-checked verdict) uses
    # "claim" / "verified" / "resolution". We render both.
    title = item.get("premise") or item.get("claim") or "(missing premise)"
    verified = item.get("verified")
    resolution = item.get("resolution", "")
    depends_on = item.get("plan_depends_on_it_for", "")
    evidence = item.get("evidence_in_ticket") or item.get("evidence", "")
    verify_by = item.get("verify_by", "")

    lines = [
        f"### P{idx}. {title}",
        "",
    ]
    if verified is not None:
        # Post-verification shape: surface the verdict + the proposed resolution.
        lines.append(f"**Verified:** {verified}")
        if resolution:
            lines.append("")
            lines.append(f"**Resolution:** _<{resolution}>_")
    elif auto_accept:
        lines.append(
            "**Verification:** accepted — premise treated as true for re-dispatch."
        )
    else:
        lines.append(
            "**Verification:** _<verified | falsified | partially-true | unknown>_"
        )
        if verify_by:
            lines.append("")
            lines.append(f"_Verify by: {verify_by}_")
    if depends_on:
        lines.append("")
        lines.append(f"_Plan depends on this for: {depends_on}_")
    if evidence:
        lines.append("")
        lines.append(f"_Evidence: {evidence}_")
    return "\n".join(lines)


def render(payload: dict[str, Any], *, auto_accept: bool) -> str:
    ambiguities = payload.get("ambiguities") or []
    premises = payload.get("premises") or []
    sections: list[str] = [_DECISIONS_HEADING, ""]

    if ambiguities:
        sections.append("### Ambiguities")
        sections.append("")
        for idx, item in enumerate(ambiguities, start=1):
            sections.append(_render_ambiguity(idx, item, auto_accept))
            sections.append("")

    if premises:
        sections.append("### Premises")
        sections.append("")
        for idx, item in enumerate(premises, start=1):
            sections.append(_render_premise(idx, item, auto_accept))
            sections.append("")

    if not ambiguities and not premises:
        sections.append(
            "_No ambiguities or premises in the sentinel — nothing to disposition._"
        )

    return "\n".join(sections).rstrip() + "\n"


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input:
        with open(args.input, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = json.load(sys.stdin)

    if not isinstance(data, dict):
        sys.stderr.write("expected a JSON object on input\n")
        raise SystemExit(2)

    # Accept either the parse_sentinel.py output wrapper or a bare raw payload.
    if "raw_payload" in data:
        raw = data["raw_payload"]
        if not isinstance(raw, dict):
            sys.stderr.write("raw_payload is missing or not an object\n")
            raise SystemExit(2)
        return raw
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        help="Path to JSON file (default: read from stdin).",
    )
    parser.add_argument(
        "--auto-accept-defaults",
        action="store_true",
        help="Auto-accept the plan default for every ambiguity / premise.",
    )
    args = parser.parse_args()

    payload = _load_payload(args)
    sys.stdout.write(render(payload, auto_accept=args.auto_accept_defaults))
    return 0


if __name__ == "__main__":
    sys.exit(main())
