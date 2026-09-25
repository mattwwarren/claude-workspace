"""Tests for .claude/scripts/check_must_fix_override.py (#2205).

The script is stdlib-only (it runs inside client worktrees where ``cw`` may not
be importable), so it is loaded by path through the shared conftest helpers.
Verdict fixtures are rendered by production's ``render_review_verdict_envelope``
via ``write_review_verdict_envelope``, so they have the real on-disk shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw import review_debt
from cw.models import MustFixOverride
from tests.conftest import (
    _make_finding,
    load_guard_script_module,
    run_guard_script_cli,
    write_review_verdict_envelope,
)

if TYPE_CHECKING:
    import subprocess

    from cw.review_findings import Finding

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "check_must_fix_override.py"
_mod = load_guard_script_module(_SCRIPT, "check_must_fix_override")

_TICKET = "GEN-2205"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_NEWER_SHA = "89abcdef0123456789abcdef0123456789abcdef"


def _findings() -> list[Finding]:
    return [
        _make_finding(file="src/a.py", summary="Bug at line 12"),
        _make_finding(file="src/b.py", summary="Leaks 3 handles"),
    ]


def _override(
    *,
    reviewed_sha: str = _SHA,
    finding_ids: list[tuple[str, str]] | None = None,
) -> dict[str, object]:
    ids = (
        finding_ids
        if finding_ids is not None
        else [("src/a.py", "bug"), ("src/b.py", "leaks N handles")]
    )
    return MustFixOverride(
        actor="octocat",
        reason="false positive; follow-up #77",
        reviewed_sha=reviewed_sha,
        finding_ids=ids,
        recorded_at=datetime(2026, 9, 25, tzinfo=UTC),
    ).model_dump(mode="json")


def _setup(
    tmp_path: Path,
    *,
    must_fix: list[Finding] | None = None,
    override: dict[str, object] | None = None,
    context_ticket: str = _TICKET,
    verdict_ticket: str = _TICKET,
    write_verdict: bool = True,
    write_context: bool = True,
) -> tuple[Path, Path]:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    verdict = worktree / ".claude" / "review-verdict.json"
    if write_verdict:
        verdict = write_review_verdict_envelope(
            worktree,
            ticket_id=verdict_ticket,
            reviewed_sha=_SHA,
            must_fix=_findings() if must_fix is None else must_fix,
        )
    context = worktree / ".claude" / "cw-context.json"
    if write_context:
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_text(
            json.dumps(
                {
                    "ticket_id": context_ticket,
                    "queue_metadata": {"must_fix_override": override},
                }
            ),
            encoding="utf-8",
        )
    return verdict, context


def _run(
    verdict: Path, context: Path, head: str = _SHA
) -> subprocess.CompletedProcess[str]:
    return run_guard_script_cli(
        _SCRIPT,
        ["--verdict", str(verdict), "--context", str(context), "--head", head],
    )


def _verdict_of(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


def test_script_declares_cw_script_version_header() -> None:
    lines = _SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env python3"
    assert lines[1] == "# cw-script-version: 1"


@pytest.mark.parametrize(
    ("file", "summary"),
    [
        ("src/a.py", "Bug at line 12"),
        ("src/a.py", "Bug   at lines 3-9 in `Foo.bar`"),
        ("src/b.py", "Leaks 3 handles:42"),
        ("src/c.py", "  Mixed CASE and 12 digits 345  "),
        ("N/A", "No diff anchor"),
    ],
)
def test_fingerprint_matches_cw_review_debt(file: str, summary: str) -> None:
    """The inline copy must stay byte-identical to the source of truth."""
    assert _mod.fingerprint_v1(file, summary) == review_debt.fingerprint_v1(
        file, summary
    )


def test_fingerprint_regexes_match_cw_review_debt() -> None:
    assert _mod._WHITESPACE_RE.pattern == review_debt._WHITESPACE_RE.pattern
    assert _mod._POSITION_RE.pattern == review_debt._POSITION_RE.pattern
    assert _mod._DIGIT_RUN_RE.pattern == review_debt._DIGIT_RUN_RE.pattern
    assert _mod._DIGIT_PLACEHOLDER == review_debt._DIGIT_PLACEHOLDER
    assert _mod._NO_ANCHOR_FILE == review_debt._NO_ANCHOR_FILE


def test_clean_verdict_is_clean(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, must_fix=[])
    result = _run(verdict, context)
    assert result.returncode == 0, result.stderr
    out = _verdict_of(result)
    assert out["status"] == "clean"
    assert out["findings"] == []


def test_absent_verdict_is_clean(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, write_verdict=False)
    result = _run(verdict, context)
    assert result.returncode == 0, result.stderr
    assert _verdict_of(result)["status"] == "clean"


def test_blocking_verdict_without_override_blocks(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path)
    result = _run(verdict, context)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert out["override"] is None
    assert out["reviewed_sha"] == _SHA
    assert out["findings"] == [
        {
            "file": "src/a.py",
            "summary": "Bug at line 12",
            "fingerprint": ["src/a.py", "bug"],
        },
        {
            "file": "src/b.py",
            "summary": "Leaks 3 handles",
            "fingerprint": ["src/b.py", "leaks N handles"],
        },
    ]
    assert "--override-must-fix" in str(out["detail"])


def test_matching_override_is_overridden(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, override=_override())
    result = _run(verdict, context)
    assert result.returncode == 0, result.stderr
    out = _verdict_of(result)
    assert out["status"] == "overridden"
    assert out["override"] == _override()


def test_override_for_an_older_review_round_is_stale(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, override=_override(reviewed_sha=_NEWER_SHA))
    result = _run(verdict, context)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert "stale override" in str(out["detail"])


def test_head_moved_past_reviewed_sha_blocks(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, override=_override())
    result = _run(verdict, context, head=_NEWER_SHA)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert "HEAD" in str(out["detail"])


def test_override_missing_a_live_finding_blocks(tmp_path: Path) -> None:
    verdict, context = _setup(
        tmp_path, override=_override(finding_ids=[("src/a.py", "bug")])
    )
    result = _run(verdict, context)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert "leaks N handles" in str(out["detail"])


def test_unfingerprintable_live_finding_blocks(tmp_path: Path) -> None:
    must_fix = [*_findings(), _make_finding(file="N/A", summary="Missing ticket")]
    verdict, context = _setup(tmp_path, must_fix=must_fix, override=_override())
    result = _run(verdict, context)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert "Missing ticket" in str(out["detail"])


def test_foreign_verdict_is_not_authoritative(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, verdict_ticket="GEN-9999")
    result = _run(verdict, context)
    assert result.returncode == 0, result.stderr
    out = _verdict_of(result)
    assert out["status"] == "clean"
    assert "GEN-9999" in str(out["detail"])


def test_missing_context_blocks_a_blocking_verdict(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, write_context=False)
    result = _run(verdict, context)
    assert result.returncode == 1
    assert _verdict_of(result)["status"] == "blocked"


def test_malformed_context_blocks_a_blocking_verdict(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, override=_override())
    context.write_text("{not json", encoding="utf-8")
    result = _run(verdict, context)
    assert result.returncode == 1
    assert _verdict_of(result)["status"] == "blocked"


def test_missing_context_leaves_a_clean_verdict_clean(tmp_path: Path) -> None:
    verdict, context = _setup(tmp_path, must_fix=[], write_context=False)
    result = _run(verdict, context)
    assert result.returncode == 0, result.stderr
    assert _verdict_of(result)["status"] == "clean"


@pytest.mark.parametrize(
    "override",
    [
        {"reviewed_sha": _SHA},
        {"reviewed_sha": 7, "finding_ids": []},
        {"reviewed_sha": _SHA, "finding_ids": [["only-one"]]},
        {"reviewed_sha": _SHA, "finding_ids": "src/a.py"},
        "not-a-dict",
    ],
    ids=["no_ids", "sha_not_str", "short_pair", "ids_not_list", "not_dict"],
)
def test_malformed_override_blocks(tmp_path: Path, override: object) -> None:
    verdict, context = _setup(tmp_path)
    context.write_text(
        json.dumps(
            {"ticket_id": _TICKET, "queue_metadata": {"must_fix_override": override}}
        ),
        encoding="utf-8",
    )
    result = _run(verdict, context)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert "malformed" in str(out["detail"])


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        json.dumps({"ticket_id": _TICKET}),
        json.dumps({"ticket_id": _TICKET, "verdict": {"blocking": True}}),
        json.dumps(
            {
                "ticket_id": _TICKET,
                "verdict": {"blocking": True, "must_fix": [{}], "reviewed_sha": _SHA},
            }
        ),
    ],
    ids=["not_json", "not_object", "no_verdict", "partial_verdict", "bad_finding"],
)
def test_unreadable_verdict_fails_closed(tmp_path: Path, body: str) -> None:
    verdict, context = _setup(tmp_path, override=_override())
    verdict.write_text(body, encoding="utf-8")
    result = _run(verdict, context)
    assert result.returncode == 1
    out = _verdict_of(result)
    assert out["status"] == "blocked"
    assert "verdict" in str(out["detail"])


def test_missing_required_argument_is_a_usage_error(tmp_path: Path) -> None:
    result = run_guard_script_cli(_SCRIPT, ["--verdict", str(tmp_path / "v.json")])
    assert result.returncode == 2
