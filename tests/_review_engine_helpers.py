"""Shared builders for the review engine summary tests (#3036).

Imported by ``test_review_engine_summary.py`` (the model),
``test_review_engine_summary_io.py`` (the bounded loader) and
``test_cli_review_engine.py`` (``cw review engine validate``). This module has
no ``test_`` prefix, so pytest does not collect it (the
``tests/_cli_review_helpers.py`` convention).

``make_summary`` derives ``counts`` and ``verdict`` from its findings so a
valid summary is the default; any keyword override replaces the derived value,
which is how a test forces a counts mismatch or a PASS with a gating finding.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.models.review_engine_summary import REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION

if TYPE_CHECKING:
    from pathlib import Path

SHA = "a" * 40

_GATING = frozenset({"MUST_FIX", "SHOULD_FIX"})
_COUNT_KEYS = ("must_fix", "should_fix", "debt", "nit", "principle")


def make_finding(**overrides: object) -> dict[str, object]:
    """One valid finding; *overrides* replace individual keys."""
    finding: dict[str, object] = {
        "fingerprint": "fp-000001",
        "severity": "NIT",
        "rule_id": "style.naming",
        "file": "src/a.py",
        "line": 3,
        "summary": "Name does not follow the module convention.",
        "evidence": "def Foo(): ...",
        "consequence": None,
        "suggested_fix": None,
    }
    finding.update(overrides)
    return finding


def _tally(findings: list[dict[str, object]]) -> dict[str, int]:
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    for finding in findings:
        severity = finding.get("severity")
        if isinstance(severity, str) and severity.lower() in counts:
            counts[severity.lower()] += 1
    return counts


def make_summary(
    findings: list[dict[str, object]] | None = None, **overrides: object
) -> dict[str, object]:
    """A valid summary over *findings*; *overrides* replace derived keys."""
    rows = [] if findings is None else findings
    gating = any(finding.get("severity") in _GATING for finding in rows)
    summary: dict[str, object] = {
        "schema_version": REVIEW_ENGINE_SUMMARY_SCHEMA_VERSION,
        "head_sha": SHA,
        "verdict": "FAIL" if gating else "PASS",
        "coverage": "COMPLETE",
        "risk": "LOW",
        "risk_notes": [],
        "counts": _tally(rows),
        "findings": rows,
        "acceptance": [],
        "gate_signals": [],
    }
    summary.update(overrides)
    return summary


def summary_bytes(summary: dict[str, object]) -> bytes:
    """*summary* as compact UTF-8 JSON."""
    return json.dumps(summary, separators=(",", ":")).encode("utf-8")


def write_summary(
    directory: Path,
    summary: dict[str, object] | bytes,
    name: str = "summary.json",
) -> Path:
    """Write *summary* (a dict, or raw bytes) under *directory*; return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    raw = summary if isinstance(summary, bytes) else summary_bytes(summary)
    path = directory / name
    path.write_bytes(raw)
    return path


def pad_to(raw: bytes, size: int) -> bytes:
    """Pad *raw* with trailing JSON whitespace to exactly *size* bytes."""
    if len(raw) > size:
        msg = f"raw is {len(raw)} bytes, already over {size}"
        raise ValueError(msg)
    return raw + b" " * (size - len(raw))
