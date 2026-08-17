"""Tests for .claude/scripts/classify_merge_conflict.py (#1850).

Uses importlib to load the script directly (it lives outside the src/ tree),
mirroring ``tests/test_check_plan_scope_conformance.py``'s loader pattern.
All fixtures are deterministic string literals built in ``tmp_path`` — no
real merge is ever performed and no repo file is ever rewritten.

The behaviour under test is deliberately fail-closed: the resolver may only
write when *every* conflict block in *every* named file classifies into one
of the three enumerated safe categories. Half of this file is therefore
refusal coverage, not happy-path coverage.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Script loader
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "classify_merge_conflict.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("classify_merge_conflict", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("classify_merge_conflict", mod)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _conflict(ours: list[str], theirs: list[str]) -> str:
    """Render one conflict-marker block exactly as git leaves it on disk."""
    lines = ["<<<<<<< HEAD", *ours, "=======", *theirs, ">>>>>>> origin/main"]
    return "\n".join(lines) + "\n"


def _changelog_conflict() -> str:
    return (
        "# Changelog\n\n"
        + _conflict(
            ["## Added", "- ours: a thing"],
            ["## Fixed", "- theirs: another thing"],
        )
        + "\n## Older\n"
    )


def _import_conflict() -> str:
    return (
        "from __future__ import annotations\n\n"
        + _conflict(
            ["import json", "import sys"],
            ["import json", "import os"],
        )
        + "\n\ndef main() -> None:\n    return None\n"
    )


def _write(tmp_path: Path, name: str, content: str) -> Path:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _list_file(tmp_path: Path, paths: list[Path]) -> Path:
    listing = tmp_path / "conflicted.txt"
    listing.write_text(
        "\n".join(str(p) for p in paths) + "\n",
        encoding="utf-8",
    )
    return listing


def _run(
    tmp_path: Path,
    paths: list[Path],
    *,
    json_flag: bool = True,
) -> tuple[int, str]:
    argv = ["resolve", "--conflicted-files", str(_list_file(tmp_path, paths))]
    if json_flag:
        argv.append("--json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _mod.main(argv)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Happy path — the three enumerated safe categories
# ---------------------------------------------------------------------------


def test_doc_append_resolves_both_sections(tmp_path: Path) -> None:
    target = _write(tmp_path, "CHANGELOG.md", _changelog_conflict())
    code, out = _run(tmp_path, [target])
    assert code == 0
    verdict = json.loads(out)
    assert verdict["safe"] is True
    assert verdict["categories"]["doc_append"] == 1
    resolved = target.read_text(encoding="utf-8")
    assert "## Added" in resolved
    assert "## Fixed" in resolved
    assert "<<<<<<<" not in resolved
    assert "=======" not in resolved
    assert ">>>>>>>" not in resolved


def test_import_union_dedupes_and_unions(tmp_path: Path) -> None:
    target = _write(tmp_path, "src/pkg/mod.py", _import_conflict())
    code, out = _run(tmp_path, [target])
    assert code == 0
    assert json.loads(out)["categories"]["import_union"] == 1
    resolved = target.read_text(encoding="utf-8").splitlines()
    assert resolved.count("import json") == 1
    union = [line for line in resolved if line.startswith("import ")]
    assert union == ["import json", "import sys", "import os"]


def test_one_sided_insert_keeps_non_empty_side(tmp_path: Path) -> None:
    content = "alpha\n" + _conflict([], ["def added() -> None:", "    return None"])
    target = _write(tmp_path, "src/pkg/other.py", content)
    code, out = _run(tmp_path, [target])
    assert code == 0
    assert json.loads(out)["categories"]["one_sided_insert"] == 1
    resolved = target.read_text(encoding="utf-8")
    assert "def added() -> None:" in resolved
    assert "<<<<<<<" not in resolved


def test_multi_file_all_safe_resolves_all(tmp_path: Path) -> None:
    doc = _write(tmp_path, "CHANGELOG.md", _changelog_conflict())
    src = _write(tmp_path, "src/pkg/mod.py", _import_conflict())
    code, out = _run(tmp_path, [doc, src])
    assert code == 0
    verdict = json.loads(out)
    assert sorted(verdict["resolved_files"]) == sorted([str(doc), str(src)])
    assert verdict["categories"] == {"doc_append": 1, "import_union": 1}
    assert "<<<<<<<" not in doc.read_text(encoding="utf-8")
    assert "<<<<<<<" not in src.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Refuse-and-park
# ---------------------------------------------------------------------------


def test_overlapping_logic_conflict_is_unsafe(tmp_path: Path) -> None:
    content = "def f() -> int:\n" + _conflict(["    value = 1"], ["    value = 2"])
    target = _write(tmp_path, "src/pkg/mod.py", content)
    code, out = _run(tmp_path, [target])
    assert code == 1
    verdict = json.loads(out)
    assert verdict["safe"] is False
    assert verdict["files"][0]["path"] == str(target)
    assert verdict["files"][0]["unsafe_blocks"]
    assert target.read_text(encoding="utf-8") == content


def test_doc_category_gated_by_path_not_content_shape(tmp_path: Path) -> None:
    """A CHANGELOG-shaped disjoint append in a source file must still refuse."""
    target = _write(tmp_path, "src/pkg/mod.py", _changelog_conflict())
    code, out = _run(tmp_path, [target])
    assert code == 1
    assert json.loads(out)["safe"] is False
    assert "<<<<<<<" in target.read_text(encoding="utf-8")


def test_atomicity_one_unsafe_file_blocks_all_writes(tmp_path: Path) -> None:
    safe_content = _changelog_conflict()
    unsafe_content = "def f() -> int:\n" + _conflict(["    v = 1"], ["    v = 2"])
    doc = _write(tmp_path, "CHANGELOG.md", safe_content)
    src = _write(tmp_path, "src/pkg/mod.py", unsafe_content)
    code, out = _run(tmp_path, [doc, src])
    assert code == 1
    assert json.loads(out)["safe"] is False
    assert doc.read_text(encoding="utf-8") == safe_content
    assert src.read_text(encoding="utf-8") == unsafe_content


def test_no_conflict_markers_is_unsafe(tmp_path: Path) -> None:
    """A listed file with no markers (delete/modify, binary) is not resolvable."""
    target = _write(tmp_path, "CHANGELOG.md", "# Changelog\n\nno markers here\n")
    code, out = _run(tmp_path, [target])
    assert code == 1
    assert "no_conflict_markers" in json.dumps(json.loads(out))


def test_malformed_conflict_markers_treated_as_unsafe(tmp_path: Path) -> None:
    truncated = "# Changelog\n<<<<<<< HEAD\nours\n=======\ntheirs\n"
    target = _write(tmp_path, "CHANGELOG.md", truncated)
    code, out = _run(tmp_path, [target])
    assert code in (1, 2)
    assert target.read_text(encoding="utf-8") == truncated
    if code == 1:
        assert json.loads(out)["safe"] is False


def test_diff3_base_marker_treated_as_unsafe(tmp_path: Path) -> None:
    diff3 = (
        "# Changelog\n"
        "<<<<<<< HEAD\nours\n||||||| base\nbase\n=======\ntheirs\n>>>>>>> origin/main\n"
    )
    target = _write(tmp_path, "CHANGELOG.md", diff3)
    code, _ = _run(tmp_path, [target])
    assert code == 1
    assert target.read_text(encoding="utf-8") == diff3


def test_nested_open_marker_treated_as_unsafe(tmp_path: Path) -> None:
    nested = "<<<<<<< HEAD\nours\n<<<<<<< HEAD\n=======\ntheirs\n>>>>>>> origin/main\n"
    target = _write(tmp_path, "CHANGELOG.md", nested)
    code, _ = _run(tmp_path, [target])
    assert code == 1


def test_stray_close_marker_treated_as_unsafe(tmp_path: Path) -> None:
    stray = "# Changelog\n>>>>>>> origin/main\n"
    target = _write(tmp_path, "CHANGELOG.md", stray)
    code, _ = _run(tmp_path, [target])
    assert code == 1


def test_unreadable_conflicted_files_list_exits_2_no_writes(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    code = _mod.main(["resolve", "--conflicted-files", str(missing), "--json"])
    assert code == 2


def test_empty_conflicted_files_list_exits_2(tmp_path: Path) -> None:
    listing = tmp_path / "empty.txt"
    listing.write_text("\n  \n", encoding="utf-8")
    code = _mod.main(["resolve", "--conflicted-files", str(listing), "--json"])
    assert code == 2


def test_unreadable_conflicted_file_exits_2_no_writes(tmp_path: Path) -> None:
    doc = _write(tmp_path, "CHANGELOG.md", _changelog_conflict())
    missing = tmp_path / "src" / "gone.py"
    code, _ = _run(tmp_path, [doc, missing])
    assert code == 2
    assert "<<<<<<<" in doc.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit-level classification
# ---------------------------------------------------------------------------


def test_classify_block_categories() -> None:
    assert _mod.classify_block("CHANGELOG.md", [], ["x"]) == "one_sided_insert"
    assert _mod.classify_block("a.py", ["import os"], ["import sys"]) == "import_union"
    assert _mod.classify_block("docs/a.txt", ["a"], ["b"]) == "doc_append"
    assert _mod.classify_block("a.py", ["a = 1"], ["a = 2"]) == "unsafe"


def test_is_doc_path_allowlist() -> None:
    assert _mod.is_doc_path("CHANGELOG.md")
    assert _mod.is_doc_path("CHANGELOG")
    assert _mod.is_doc_path("docs/guide/thing.txt")
    assert not _mod.is_doc_path("src/cw/cli.py")


def test_is_doc_path_excludes_non_docs_markdown() -> None:
    """A bare .md suffix outside docs/ is NOT doc-safe -- this repo's own
    orchestration prose lives exactly there, and the binding operator
    directive scopes doc_append to 'the docs/CHANGELOG allowlist' only."""
    assert not _mod.is_doc_path("README.md")
    assert not _mod.is_doc_path(".claude/commands/auto-dev-finalize.md")
    assert not _mod.is_doc_path(".claude/skills/some-skill/SKILL.md")
    assert not _mod.is_doc_path("CLAUDE.md")


def test_is_doc_path_docs_check_is_root_anchored() -> None:
    """The general invariant, not just the reported counterexample: `docs`
    must be the FIRST path component, not merely present anywhere in the
    tree. A `docs`-named directory nested elsewhere (this repo genuinely has
    one at .claude/docs/coding/, holding reviewer-agent-consumed prose) is
    NOT the project's documentation tree."""
    assert not _mod.is_doc_path(".claude/docs/coding/output-formats.md")
    assert not _mod.is_doc_path("src/pkg/docs/notes.md")
    assert not _mod.is_doc_path("a/b/docs/c/d.md")
    # Root-anchored docs/ still resolves at any depth beneath it.
    assert _mod.is_doc_path("docs/a.md")
    assert _mod.is_doc_path("docs/guide/nested/thing.md")


def test_import_union_recognizes_non_python_import_forms() -> None:
    ours = ["import type { A } from 'a';"]
    theirs = ["import { B } from 'b';"]
    assert _mod.classify_block("web/app.ts", ours, theirs) == "import_union"


def test_blank_lines_do_not_defeat_import_union() -> None:
    ours = ["import os", ""]
    theirs = ["", "import sys"]
    assert _mod.classify_block("a.py", ours, theirs) == "import_union"


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def _run_cli(
    listing: Path, *, json_flag: bool = True
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(_SCRIPT),
        "resolve",
        "--conflicted-files",
        str(listing),
    ]
    if json_flag:
        argv.append("--json")
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_cli_exit_code_and_json_contract(tmp_path: Path) -> None:
    doc = _write(tmp_path, "CHANGELOG.md", _changelog_conflict())
    ok = _run_cli(_list_file(tmp_path, [doc]))
    assert ok.returncode == 0
    payload = json.loads(ok.stdout)
    assert payload["safe"] is True
    assert payload["resolved_files"] == [str(doc)]
    assert payload["categories"]["doc_append"] == 1

    bad = _write(tmp_path, "src/pkg/mod.py", "x\n" + _conflict(["a = 1"], ["a = 2"]))
    refused = _run_cli(_list_file(tmp_path, [bad]))
    assert refused.returncode == 1
    refused_payload = json.loads(refused.stdout)
    assert refused_payload["safe"] is False
    assert refused_payload["files"][0]["path"] == str(bad)

    usage = _run_cli(tmp_path / "absent.txt")
    assert usage.returncode == 2
    assert usage.stdout.strip() == ""
    assert "classify_merge_conflict" in usage.stderr


def test_cli_human_summary_without_json_flag(tmp_path: Path) -> None:
    doc = _write(tmp_path, "CHANGELOG.md", _changelog_conflict())
    result = _run_cli(_list_file(tmp_path, [doc]), json_flag=False)
    assert result.returncode == 0
    assert result.stdout.startswith("classify_merge_conflict: resolved")
    assert "doc_append" in result.stdout


def test_cli_human_summary_on_refusal(tmp_path: Path) -> None:
    bad = _write(tmp_path, "src/pkg/mod.py", "x\n" + _conflict(["a = 1"], ["a = 2"]))
    result = _run_cli(_list_file(tmp_path, [bad]), json_flag=False)
    assert result.returncode == 1
    assert result.stdout.startswith("classify_merge_conflict: refused")
