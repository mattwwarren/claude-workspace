"""Tests for scripts/measure_skill_catalog.py (#1834)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from tests._queue_peek_helpers import make_transcript
from tests._shared.claude_home_files import (
    skill_listing_record,
    write_command,
    write_context_files,
    write_skill,
)
from tests._shared.guard_fences import load_guard_script_module
from tests._shared.stop_hook_context import _write_idle_transcript

if TYPE_CHECKING:
    from collections.abc import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "measure_skill_catalog.py"

_mod = load_guard_script_module(_SCRIPT, "measure_skill_catalog")

_LONG_DESC = "g" * 2000
_WARN = (
    "2026-11-10 WARN Skill listing over budget: 122 skills, 32039 chars > 8000 "
    "budget - descriptions will be truncated."
)
_LISTING = "\n".join(
    [
        "- rskill-a: alpha skill",
        "- rskill-b",
        "- gcmd: global command",
        "- slack:x: d",
        "- other: z",
    ]
)
_JSON_KEYS = {
    "delivered_chars",
    "delivered_skill_count",
    "delivered_entries",
    "budget_observed",
    "budget_formula_chars",
    "by_source",
    "demand_by_source",
    "demand_total_chars",
    "gap_components",
}
_GAP_KEYS = {
    "global_claude_md_chars",
    "claude_local_md_chars",
    "rules_chars",
    "listing_chars",
    "est_tokens_context_files",
    "est_tokens_listing",
    "gap_tokens",
}
_SOURCE_KEYS = {"entries", "name_only", "chars", "share_pct"}


@pytest.fixture
def tree(tmp_path: Path) -> SimpleNamespace:
    """A fake repo and home composed only from the shared writers."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    write_skill(repo / ".claude", "rskill-a", "alpha skill")
    write_skill(repo / ".claude", "rskill-b", None)
    write_command(repo / ".claude", "rcmd", None)
    write_skill(home / ".claude", "gskill", _LONG_DESC)
    write_command(home / ".claude", "gcmd", "global command")
    write_context_files(home, claude_md="c" * 40, local_md="l" * 8)
    return SimpleNamespace(repo=repo, home=home)


def _by_name(entries: Iterable[Any]) -> dict[str, Any]:
    return {e.name: e for e in entries}


def _table_rows(lines: list[str], title: str) -> list[str]:
    """Bucket names under ``title``: indented rows up to the next unindented line."""
    rows: list[str] = []
    for line in lines[lines.index(title) + 1 :]:
        if not line.startswith("  "):
            break
        rows.append(line.split()[0])
    return rows


def _report(tree: SimpleNamespace, content: str | None = _LISTING) -> Any:
    demand = _mod.enumerate_demand(tree.repo, tree.home)
    raw = None if content is None else _mod.RawListing(content, 6)
    gap = _mod.gap_components(tree.home, None if content is None else len(content))
    return _mod.summarize(demand, raw, _mod.known_names(demand), None, gap)


def test_loader_registers_module_in_sys_modules_before_exec() -> None:
    assert sys.modules["measure_skill_catalog"] is _mod
    assert _mod.Entry.__module__ == "measure_skill_catalog"
    assert _mod.Entry("a", True, 3).chars == 3


class TestParseFrontmatter:
    def test_plain_description(self) -> None:
        meta = _mod.parse_frontmatter("---\nname: a\ndescription: hello\n---\nbody\n")
        assert _mod.description_of(meta) == "hello"

    def test_folded_description(self) -> None:
        text = "---\ndescription: >\n  one\n  two\n---\nbody\n"
        assert _mod.description_of(_mod.parse_frontmatter(text)) == "one two"

    def test_quoted_description(self) -> None:
        text = "---\ndescription: 'a: b'\n---\nbody\n"
        assert _mod.description_of(_mod.parse_frontmatter(text)) == "a: b"

    def test_no_block_is_empty(self) -> None:
        assert _mod.parse_frontmatter("just a body\n") == {}

    def test_malformed_yaml_is_empty(self) -> None:
        assert _mod.parse_frontmatter("---\nkey: [unclosed\n---\nbody\n") == {}

    def test_list_block_is_empty(self) -> None:
        assert _mod.parse_frontmatter("---\n- a\n- b\n---\nbody\n") == {}

    def test_description_of_missing_or_non_string(self) -> None:
        assert _mod.description_of({}) == ""
        assert _mod.description_of({"description": 5}) == ""


def test_demand_entry_arithmetic_and_cap() -> None:
    capped = _mod.demand_entry("name", _mod.REPO_SKILL, "x" * 2000)
    assert capped.desc_len == 1536
    assert capped.chars == len("name") + 4 + 1536
    bare = _mod.demand_entry("name", _mod.REPO_SKILL, "")
    assert bare.desc_len == 0
    assert bare.chars == len("name") + 2
    assert _mod.demand_total_chars([capped, bare]) == capped.chars + bare.chars + 1
    assert _mod.demand_total_chars([]) == 0


def test_enumerate_demand_counts_each_source(tree: SimpleNamespace) -> None:
    (tree.repo / ".claude" / "skills" / "empty-dir").mkdir()
    (tree.repo / ".claude" / "commands" / "notes.txt").write_text("x")
    demand = _by_name(_mod.enumerate_demand(tree.repo, tree.home))
    assert set(demand) == {"rskill-a", "rskill-b", "rcmd", "gskill", "gcmd"}
    assert demand["rskill-a"].source == _mod.REPO_SKILL
    assert demand["rskill-a"].chars == len("rskill-a") + 4 + len("alpha skill")
    assert demand["rskill-b"].desc_len == 0
    assert demand["rskill-b"].chars == len("rskill-b") + 2
    assert demand["rcmd"].source == _mod.REPO_COMMAND
    assert demand["rcmd"].chars == len("rcmd") + 2
    assert demand["gskill"].source == _mod.GLOBAL_CLAUDE
    assert demand["gskill"].desc_len == 1536
    assert demand["gcmd"].desc_len == len("global command")


def test_enumerate_demand_dedup_rule(tmp_path: Path) -> None:
    repo_claude = tmp_path / "repo" / ".claude"
    home_claude = tmp_path / "home" / ".claude"
    write_skill(repo_claude, "dup-skill", "repo")
    write_skill(home_claude, "dup-skill", "global")
    write_command(repo_claude, "dup-cmd", "repo")
    write_command(home_claude, "dup-cmd", "global")
    write_skill(repo_claude, "both", "skill wins")
    write_command(repo_claude, "both", "command loses")
    write_skill(repo_claude, "linked", "linked skill")
    (home_claude / "skills").mkdir(exist_ok=True)
    (home_claude / "skills" / "linked").symlink_to(repo_claude / "skills" / "linked")

    entries = _mod.enumerate_demand(tmp_path / "repo", tmp_path / "home")
    demand = _by_name(entries)

    assert len(entries) == len(demand) == 4
    assert demand["dup-skill"].source == _mod.REPO_SKILL
    assert demand["dup-cmd"].source == _mod.REPO_COMMAND
    assert demand["both"].source == _mod.REPO_SKILL
    assert demand["linked"].source == _mod.REPO_SKILL


def test_known_names_partition(tmp_path: Path) -> None:
    repo_claude = tmp_path / "repo" / ".claude"
    home_claude = tmp_path / "home" / ".claude"
    write_skill(repo_claude, "s", "d")
    write_command(repo_claude, "c", "d")
    write_skill(home_claude, "g", "d")
    write_skill(home_claude, "s", "d")
    write_command(home_claude, "c", "d")

    demand = _mod.enumerate_demand(tmp_path / "repo", tmp_path / "home")
    known = _mod.known_names(demand)

    assert known.repo_skills == {"s"}
    assert known.repo_commands == {"c"}
    assert known.global_names == {"g"}
    assert not known.repo_skills & known.repo_commands
    assert not known.repo_skills & known.global_names
    assert not known.repo_commands & known.global_names
    assert known.repo_skills | known.repo_commands | known.global_names == {
        e.name for e in demand
    }


class TestParseListing:
    def test_full_vs_name_only_and_names(self) -> None:
        content = "\n".join(["- a: desc", "- b", "- c:ns", "- slack:x: d", "- slack:y"])
        entries = _by_name(_mod.parse_listing(content))
        assert entries["a"].full is True
        assert entries["a"].chars == len("- a: desc")
        assert entries["b"].full is False
        assert entries["c:ns"].full is False
        assert entries["slack:x"].full is True
        assert entries["slack:y"].full is False

    def test_name_only_entry_with_alias_suffix(self) -> None:
        content = "- a: x\n- hookify:writing-rules (hookify:writing-hookify-rules)"
        entries = _by_name(_mod.parse_listing(content))
        assert entries["hookify:writing-rules"].full is False
        assert entries["hookify:writing-rules"].chars == len(
            "- hookify:writing-rules (hookify:writing-hookify-rules)"
        )

    def test_continuation_lines_attach_to_previous_entry(self) -> None:
        first = "- claude-api: first line\n  continuation line"
        content = "\n".join(["- a: x", first, "- z: last"])
        entries = _mod.parse_listing(content)
        assert [e.name for e in entries] == ["a", "claude-api", "z"]
        assert entries[1].chars == len(first)

    def test_preamble_and_bare_dash_are_dropped(self) -> None:
        content = "preamble line\n- a: x\n- \n- z: y"
        assert [e.name for e in _mod.parse_listing(content)] == ["a", "z"]

    def test_chars_plus_separators_equal_content_length(self) -> None:
        content = "- a: x\n- b\n- claude-api: l1\n  l2\n- z: last"
        entries = _mod.parse_listing(content)
        assert sum(e.chars for e in entries) + (len(entries) - 1) == len(content)


def test_classify_buckets() -> None:
    known = _mod.KnownNames(
        repo_skills=frozenset({"x"}),
        repo_commands=frozenset({"x", "y"}),
        global_names=frozenset({"x", "y", "g"}),
    )
    assert _mod.classify("x", known) == _mod.REPO_SKILL
    assert _mod.classify("y", known) == _mod.REPO_COMMAND
    assert _mod.classify("g", known) == _mod.GLOBAL_CLAUDE
    assert _mod.classify("slack:x", known) == "ns:slack"
    assert _mod.classify("a:b:c", known) == "ns:a"
    assert _mod.classify("unknown", known) == _mod.BUNDLED_OTHER


def test_summarize_delivered_and_demand_by_source(tree: SimpleNamespace) -> None:
    report = _report(tree)

    by_source = report.by_source
    assert set(by_source) == {
        _mod.REPO_SKILL,
        "global-claude",
        "ns:slack",
        _mod.BUNDLED_OTHER,
    }
    assert all(isinstance(s, _mod.SourceStats) for s in by_source.values())
    repo_skill = by_source[_mod.REPO_SKILL]
    assert repo_skill.entries == 2
    assert repo_skill.name_only == 1
    assert repo_skill.chars == len("- rskill-a: alpha skill") + len("- rskill-b")
    total_chars = sum(s.chars for s in by_source.values())
    entries = sum(s.entries for s in by_source.values())
    assert total_chars + (entries - 1) == report.delivered_chars == len(_LISTING)
    assert repo_skill.share_pct == round(100 * repo_skill.chars / total_chars, 1)
    assert report.delivered_entries == 5
    assert report.delivered_skill_count == 6
    assert report.demand_by_source[_mod.REPO_SKILL].entries == 2
    assert report.demand_by_source[_mod.REPO_SKILL].name_only == 1


def test_summarize_without_delivery_keeps_demand(tree: SimpleNamespace) -> None:
    report = _report(tree, content=None)
    assert report.delivered_chars is None
    assert report.delivered_skill_count is None
    assert report.delivered_entries is None
    assert report.by_source is None
    assert report.demand_by_source
    assert report.demand_total_chars > 0
    assert report.gap_components.listing_chars is None


def test_load_listing_picks_first_initial_attachment(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"type": "assistant", "message": {"content": []}}),
        "{not json",
        json.dumps(skill_listing_record("- stale", 1, is_initial=False)),
        json.dumps(skill_listing_record("- a: x\n- b", 2)),
        json.dumps(skill_listing_record("- later", 1)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert _mod.load_listing(path) == _mod.RawListing("- a: x\n- b", 2)


def test_load_listing_missing_attachment_raises_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    make_transcript(path, [{"type": "assistant"}])

    with pytest.raises(SystemExit) as exc:
        _mod.load_listing(path)

    message = str(exc.value)
    assert path.name in message
    assert "skill_listing" in message
    assert str(tmp_path) not in message


def test_latest_transcript_encoding(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = Path("/a/b.c")
    old = _write_idle_transcript(home, cwd, "old.jsonl")
    new = _write_idle_transcript(home, cwd, "new.jsonl")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    assert (home / ".claude" / "projects" / "-a-b-c").is_dir()
    assert _mod.latest_transcript(home, cwd) == new

    with pytest.raises(SystemExit) as missing:
        _mod.latest_transcript(home, Path("/no/such"))
    assert "--cwd" in str(missing.value)
    assert str(tmp_path) not in str(missing.value)

    (home / ".claude" / "projects" / "-empty").mkdir()
    with pytest.raises(SystemExit) as empty:
        _mod.latest_transcript(home, Path("/empty"))
    assert str(tmp_path) not in str(empty.value)


def test_parse_debug_budget_line() -> None:
    assert _mod.parse_debug_budget(_WARN) == _mod.BudgetObserved(
        skills=122, demand_chars=32039, budget_chars=8000
    )
    assert _mod.parse_debug_budget("nothing to see") is None
    assert _mod.budget_from_formula() == 8000
    assert _mod.budget_from_formula(1_000_000) == 40000


def test_gap_components(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_context_files(
        home,
        claude_md="a" * 40,
        local_md="b" * 8,
        rules={"x": "c" * 20, "sub/y": "d" * 12},
    )

    gap = _mod.gap_components(home, 100)

    assert gap.global_claude_md_chars == 40
    assert gap.claude_local_md_chars == 8
    assert gap.rules_chars == 32
    assert gap.listing_chars == 100
    assert gap.est_tokens_context_files == (40 + 8 + 32) // 4
    assert gap.est_tokens_listing == 25
    assert gap.gap_tokens == 10217

    empty = _mod.gap_components(tmp_path / "nohome", None)
    assert empty.global_claude_md_chars == 0
    assert empty.claude_local_md_chars == 0
    assert empty.rules_chars == 0
    assert empty.listing_chars is None
    assert empty.est_tokens_listing is None


def test_render_json_shape(tree: SimpleNamespace) -> None:
    data = json.loads(_mod.render_json(_report(tree)))

    assert set(data) == _JSON_KEYS
    assert set(data["gap_components"]) == _GAP_KEYS
    assert data["budget_observed"] is None
    for stats in (*data["by_source"].values(), *data["demand_by_source"].values()):
        assert set(stats) == _SOURCE_KEYS

    demand = _mod.enumerate_demand(tree.repo, tree.home)
    observed = _mod.BudgetObserved(1, 2, 3)
    report = _mod.summarize(
        demand,
        None,
        _mod.known_names(demand),
        observed,
        _mod.gap_components(tree.home, None),
    )
    shaped = json.loads(_mod.render_json(report))
    assert shaped["budget_observed"] == {
        "skills": 1,
        "demand_chars": 2,
        "budget_chars": 3,
    }
    assert shaped["by_source"] is None


def test_render_text_caveats_and_no_paths(
    tree: SimpleNamespace, tmp_path: Path
) -> None:
    report = _report(tree)
    text = _mod.render_text(report)
    as_json = _mod.render_json(report)

    lines = text.splitlines()
    assert "chars/4 is an uncalibrated estimate" in lines
    assert "internal, version-pinned (Claude Code 2.1.322)" in lines
    for output in (text, as_json):
        assert str(tmp_path) not in output
        assert "/home" not in output


class TestMain:
    def _argv(self, tree: SimpleNamespace, *extra: str | Path) -> list[str]:
        return [
            "--repo",
            str(tree.repo),
            "--home",
            str(tree.home),
            *(str(e) for e in extra),
        ]

    def test_transcript_text(
        self,
        tree: SimpleNamespace,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        transcript = tmp_path / "t.jsonl"
        make_transcript(transcript, [skill_listing_record(_LISTING, 6)])

        assert _mod.main(self._argv(tree, "--transcript", transcript)) == 0

        lines = capsys.readouterr().out.splitlines()
        assert f"delivered chars: {len(_LISTING)}" in lines
        # Exact table rows, not a substring: "repo-command" is in the demand
        # table only, because rcmd is never delivered (#3417).
        delivered = _table_rows(lines, "delivered by source")
        assert set(delivered) == {
            "repo-skill",
            "global-claude",
            "ns:slack",
            "bundled/other",
        }
        assert "repo-command" not in delivered
        assert "repo-command" in _table_rows(lines, "demand by source")

    def test_debug_log_budget_line(
        self,
        tree: SimpleNamespace,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        transcript = tmp_path / "t.jsonl"
        make_transcript(transcript, [skill_listing_record(_LISTING, 6)])
        log = tmp_path / "debug.log"
        log.write_text(_WARN + "\n", encoding="utf-8")

        code = _mod.main(
            self._argv(tree, "--transcript", transcript, "--debug-log", log)
        )

        assert code == 0
        lines = capsys.readouterr().out.splitlines()
        assert (
            "budget observed (WARN): 8000 chars (122 skills, 32039 chars demand)"
            in lines
        )

    def test_no_transcript_is_not_measured(
        self, tree: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _mod.main(self._argv(tree)) == 0

        text = capsys.readouterr().out
        assert "delivered: not measured" in text.splitlines()
        assert "repo-skill" in text

    def test_transcript_and_cwd_are_mutually_exclusive(
        self, tree: SimpleNamespace, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _mod.main(
                self._argv(tree, "--transcript", tmp_path / "t", "--cwd", tmp_path)
            )
        assert exc.value.code == 2

    def test_cwd_selects_latest_transcript(
        self,
        tree: SimpleNamespace,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cwd = tmp_path / "work.dir"
        path = _write_idle_transcript(tree.home, cwd, "s.jsonl")
        make_transcript(path, [skill_listing_record(_LISTING, 6)])

        assert _mod.main(self._argv(tree, "--cwd", cwd)) == 0

        lines = capsys.readouterr().out.splitlines()
        assert f"delivered chars: {len(_LISTING)}" in lines

    def test_transcript_without_attachment_fails_clearly(
        self, tree: SimpleNamespace, tmp_path: Path
    ) -> None:
        transcript = tmp_path / "t.jsonl"
        make_transcript(transcript, [{"type": "assistant"}])

        with pytest.raises(SystemExit) as exc:
            _mod.main(self._argv(tree, "--transcript", transcript))

        assert transcript.name in str(exc.value)
        assert str(tmp_path) not in str(exc.value)

    def test_json_output(
        self,
        tree: SimpleNamespace,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        transcript = tmp_path / "t.jsonl"
        make_transcript(transcript, [skill_listing_record(_LISTING, 6)])

        assert _mod.main(self._argv(tree, "--transcript", transcript, "--json")) == 0

        data = json.loads(capsys.readouterr().out)
        assert set(data) == _JSON_KEYS
        assert data["delivered_chars"] == len(_LISTING)


# --- #3414: dangling symlinks, non-files and unreadable demand entries ---------


def test_enumerate_demand_skips_dangling_symlinks_and_non_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    claude = repo / ".claude"
    write_skill(claude, "good", "ok")
    broken_skill = claude / "skills" / "broken"
    broken_skill.mkdir(parents=True)
    (broken_skill / "SKILL.md").symlink_to(tmp_path / "missing-target")
    (claude / "commands").mkdir(parents=True)
    (claude / "commands" / "broken.md").symlink_to(tmp_path / "missing-cmd")
    (claude / "commands" / "odd.md").mkdir()

    demand = _mod.enumerate_demand(repo, tmp_path / "home")

    assert {entry.name for entry in demand} == {"good"}


def test_enumerate_demand_unreadable_file_exits_with_name_only(
    tree: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = OSError("denied")

    def _denied(path: Path) -> str:
        raise denied

    monkeypatch.setattr(_mod, "_read", _denied)

    with pytest.raises(SystemExit) as exc:
        _mod.enumerate_demand(tree.repo, tree.home)

    message = str(exc.value)
    assert "cannot read" in message
    assert "rskill-a" in message
    assert str(tmp_path) not in message


# --- #3415: edge inputs to parsing, summary and share arithmetic --------------


def test_parse_listing_empty_content_has_no_entries() -> None:
    assert _mod.parse_listing("") == ()


@pytest.mark.parametrize("content", ["preamble only\nstill nothing", "- \n- "])
def test_parse_listing_no_parseable_entries(content: str) -> None:
    assert _mod.parse_listing(content) == ()


def test_parse_listing_trailing_newline_invariant() -> None:
    content = "- a: x\n- b\n"
    entries = _mod.parse_listing(content)
    assert [e.name for e in entries] == ["a", "b"]
    # Pins the invariant only, not how the trailing newline is attributed.
    assert sum(e.chars for e in entries) + (len(entries) - 1) == len(content)


def test_summarize_empty_listing(tree: SimpleNamespace) -> None:
    demand = _mod.enumerate_demand(tree.repo, tree.home)
    report = _mod.summarize(
        demand,
        _mod.RawListing("", 0),
        _mod.known_names(demand),
        None,
        _mod.gap_components(tree.home, 0),
    )
    assert report.delivered_chars == 0
    assert report.delivered_entries == 0
    assert report.by_source == {}
    assert "delivered by source" in _mod.render_text(report)
    json.loads(_mod.render_json(report))


def test_with_shares_zero_total_is_zero_pct() -> None:
    # Unreachable from real input (every entry has chars >= 2), so the private
    # helper is called directly to reach the ``if total else 0.0`` branch.
    assert _mod._stats_by_bucket([("x", 0, True)])["x"].share_pct == 0.0


# --- #3416: error and defensive branches ---------------------------------------


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_load_listing_unreadable_transcript_exits(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "transcript.jsonl"
    if kind == "directory":
        path.mkdir()

    with pytest.raises(SystemExit) as exc:
        _mod.load_listing(path)

    message = str(exc.value)
    assert "cannot read transcript transcript.jsonl" in message
    assert str(tmp_path) not in message


def test_main_missing_debug_log_exits(tree: SimpleNamespace, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    make_transcript(transcript, [skill_listing_record(_LISTING, 6)])
    missing = tmp_path / "nope.log"
    argv = [
        "--repo",
        str(tree.repo),
        "--home",
        str(tree.home),
        "--transcript",
        str(transcript),
        "--debug-log",
        str(missing),
    ]

    with pytest.raises(SystemExit) as exc:
        _mod.main(argv)

    message = str(exc.value)
    assert "cannot read debug log nope.log" in message
    assert str(tmp_path) not in message


def test_load_listing_skips_non_object_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    non_str_content = skill_listing_record("x", 1)
    non_str_content["attachment"]["content"] = 123
    lines = [
        "{not json",
        "[1, 2]",
        "42",
        '"s"',
        "null",
        json.dumps({"attachment": ["not", "a", "dict"]}),
        json.dumps(non_str_content),
        json.dumps(skill_listing_record("- a: x", 1)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert _mod.load_listing(path) == _mod.RawListing("- a: x", 1)


@pytest.mark.parametrize("count", ["7", None, 1.5, "MISSING"])
def test_load_listing_skill_count_fallback(tmp_path: Path, count: object) -> None:
    record = skill_listing_record("- a", 1)
    if count == "MISSING":
        del record["attachment"]["skillCount"]
    else:
        record["attachment"]["skillCount"] = count
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert _mod.load_listing(path) == _mod.RawListing("- a", 0)
