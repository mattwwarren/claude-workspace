"""Tests for structured handoff functions in cw.handoff."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.handoff import (
    build_task_prompt,
    parse_structured_handoff,
    write_structured_handoff,
)
from cw.models import SessionPurpose, TaskSpec

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    description: str = "Refactor auth module",
    purpose: SessionPurpose = SessionPurpose.IMPL,
    prompt: str = "Move auth logic into its own service layer.",
    context_files: list[str] | None = None,
    success_criteria: str | None = None,
    source_session: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        description=description,
        purpose=purpose,
        prompt=prompt,
        context_files=context_files or [],
        success_criteria=success_criteria,
        source_session=source_session,
    )


# ---------------------------------------------------------------------------
# TestWriteStructuredHandoff
# ---------------------------------------------------------------------------


class TestWriteStructuredHandoff:
    def test_creates_json_file_in_handoffs_dir(self, tmp_path: Path) -> None:
        task = _make_task()
        path = write_structured_handoff(tmp_path, "sess-abc", [task])

        assert path.exists()
        assert path.suffix == ".json"
        assert path.parent == tmp_path / ".handoffs"
        assert "session-structured-" in path.name

    def test_file_contains_correct_top_level_keys(self, tmp_path: Path) -> None:
        task = _make_task()
        path = write_structured_handoff(tmp_path, "sess-abc", [task])

        data = json.loads(path.read_text())
        assert data["version"] == 1
        assert data["source_session"] == "sess-abc"
        assert "timestamp" in data
        assert "tasks" in data
        assert "context" in data

    def test_tasks_serialized_correctly(self, tmp_path: Path) -> None:
        task = _make_task(
            description="Fix linting",
            purpose=SessionPurpose.DEBT,
            prompt="Run ruff and fix all violations.",
        )
        path = write_structured_handoff(tmp_path, "sess-x", [task])

        data = json.loads(path.read_text())
        assert len(data["tasks"]) == 1
        t = data["tasks"][0]
        assert t["description"] == "Fix linting"
        assert t["purpose"] == "debt"
        assert t["prompt"] == "Run ruff and fix all violations."

    def test_multiple_tasks_written(self, tmp_path: Path) -> None:
        tasks = [
            _make_task(description="Task A", purpose=SessionPurpose.IMPL),
            _make_task(description="Task B", purpose=SessionPurpose.IDEA),
            _make_task(description="Task C", purpose=SessionPurpose.DEBT),
        ]
        path = write_structured_handoff(tmp_path, "sess-multi", tasks)

        data = json.loads(path.read_text())
        assert len(data["tasks"]) == 3
        descriptions = [t["description"] for t in data["tasks"]]
        assert descriptions == ["Task A", "Task B", "Task C"]

    def test_context_branch_stored(self, tmp_path: Path) -> None:
        task = _make_task()
        path = write_structured_handoff(
            tmp_path, "sess-abc", [task], branch="feat/new-auth"
        )

        data = json.loads(path.read_text())
        assert data["context"]["branch"] == "feat/new-auth"

    def test_context_branch_none_when_not_provided(self, tmp_path: Path) -> None:
        task = _make_task()
        path = write_structured_handoff(tmp_path, "sess-abc", [task])

        data = json.loads(path.read_text())
        assert data["context"]["branch"] is None

    def test_context_recent_changes_stored(self, tmp_path: Path) -> None:
        task = _make_task()
        changes = ["Added auth service", "Updated models.py"]
        path = write_structured_handoff(
            tmp_path, "sess-abc", [task], recent_changes=changes
        )

        data = json.loads(path.read_text())
        assert data["context"]["recent_changes"] == changes

    def test_context_recent_changes_empty_when_not_provided(
        self, tmp_path: Path
    ) -> None:
        task = _make_task()
        path = write_structured_handoff(tmp_path, "sess-abc", [task])

        data = json.loads(path.read_text())
        assert data["context"]["recent_changes"] == []

    def test_context_blockers_stored(self, tmp_path: Path) -> None:
        task = _make_task()
        blockers = ["mypy errors in session.py", "failing test_config tests"]
        path = write_structured_handoff(tmp_path, "sess-abc", [task], blockers=blockers)

        data = json.loads(path.read_text())
        assert data["context"]["blockers"] == blockers

    def test_context_blockers_empty_when_not_provided(self, tmp_path: Path) -> None:
        task = _make_task()
        path = write_structured_handoff(tmp_path, "sess-abc", [task])

        data = json.loads(path.read_text())
        assert data["context"]["blockers"] == []

    def test_creates_handoffs_dir_if_missing(self, tmp_path: Path) -> None:
        workspace = tmp_path / "new-workspace"
        workspace.mkdir()
        handoffs_dir = workspace / ".handoffs"

        assert not handoffs_dir.exists()

        write_structured_handoff(workspace, "sess-abc", [_make_task()])

        assert handoffs_dir.is_dir()

    def test_empty_task_list(self, tmp_path: Path) -> None:
        path = write_structured_handoff(tmp_path, "sess-empty", [])

        data = json.loads(path.read_text())
        assert data["tasks"] == []


# ---------------------------------------------------------------------------
# TestParseStructuredHandoff
# ---------------------------------------------------------------------------


class TestParseStructuredHandoff:
    def test_returns_empty_list_if_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-file.json"
        result = parse_structured_handoff(missing)
        assert result == []

    def test_returns_empty_list_on_malformed_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not valid json {{{")
        result = parse_structured_handoff(bad)
        assert result == []

    def test_returns_empty_list_on_missing_tasks_key(self, tmp_path: Path) -> None:
        no_tasks = tmp_path / "no-tasks.json"
        no_tasks.write_text(json.dumps({"version": 1, "source_session": "x"}))
        result = parse_structured_handoff(no_tasks)
        assert result == []

    def test_returns_empty_list_on_empty_tasks(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty-tasks.json"
        empty.write_text(json.dumps({"tasks": []}))
        result = parse_structured_handoff(empty)
        assert result == []

    def test_skips_malformed_task_entries(self, tmp_path: Path) -> None:
        mixed = tmp_path / "mixed.json"
        mixed.write_text(
            json.dumps(
                {
                    "tasks": [
                        # Valid task
                        {
                            "description": "Good task",
                            "purpose": "impl",
                            "prompt": "Do the thing.",
                        },
                        # Missing required fields - should be skipped
                        {"description": "Broken task"},
                    ]
                }
            )
        )
        result = parse_structured_handoff(mixed)
        assert len(result) == 1
        assert result[0].description == "Good task"

    def test_returns_task_spec_instances(self, tmp_path: Path) -> None:
        payload = tmp_path / "valid.json"
        payload.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "description": "Explore codebase",
                            "purpose": "explore",
                            "prompt": "Map out the module structure.",
                        }
                    ]
                }
            )
        )
        result = parse_structured_handoff(payload)
        assert len(result) == 1
        assert isinstance(result[0], TaskSpec)
        assert result[0].purpose == SessionPurpose.EXPLORE


# ---------------------------------------------------------------------------
# TestWriteParseRoundtrip
# ---------------------------------------------------------------------------


class TestWriteParseRoundtrip:
    def test_simple_task_survives_roundtrip(self, tmp_path: Path) -> None:
        task = _make_task(
            description="Implement search",
            purpose=SessionPurpose.IMPL,
            prompt="Add full-text search to the API.",
        )
        path = write_structured_handoff(tmp_path, "sess-rt", [task])
        result = parse_structured_handoff(path)

        assert len(result) == 1
        assert result[0].description == task.description
        assert result[0].purpose == task.purpose
        assert result[0].prompt == task.prompt

    def test_optional_fields_survive_roundtrip(self, tmp_path: Path) -> None:
        task = _make_task(
            context_files=["src/cw/session.py", "src/cw/models.py"],
            success_criteria="All tests pass with zero ruff violations.",
            source_session="sess-origin",
        )
        path = write_structured_handoff(tmp_path, "sess-rt", [task])
        result = parse_structured_handoff(path)

        assert len(result) == 1
        r = result[0]
        assert r.context_files == ["src/cw/session.py", "src/cw/models.py"]
        assert r.success_criteria == "All tests pass with zero ruff violations."
        assert r.source_session == "sess-origin"

    def test_multiple_tasks_survive_roundtrip(self, tmp_path: Path) -> None:
        tasks = [
            _make_task(
                description="Task A",
                purpose=SessionPurpose.IMPL,
                prompt="Implement A.",
            ),
            _make_task(
                description="Task B",
                purpose=SessionPurpose.IDEA,
                prompt="Review B.",
            ),
            _make_task(
                description="Task C",
                purpose=SessionPurpose.DEBT,
                prompt="Pay off C.",
            ),
        ]
        path = write_structured_handoff(tmp_path, "sess-rt", tasks)
        result = parse_structured_handoff(path)

        assert len(result) == 3
        for original, parsed in zip(tasks, result, strict=True):
            assert parsed.description == original.description
            assert parsed.purpose == original.purpose
            assert parsed.prompt == original.prompt

    def test_all_purpose_values_survive_roundtrip(self, tmp_path: Path) -> None:
        tasks = [
            _make_task(purpose=purpose, description=purpose.value)
            for purpose in SessionPurpose
        ]
        path = write_structured_handoff(tmp_path, "sess-rt", tasks)
        result = parse_structured_handoff(path)

        assert len(result) == len(list(SessionPurpose))
        for parsed, purpose in zip(result, SessionPurpose, strict=True):
            assert parsed.purpose == purpose

    def test_none_optional_fields_survive_roundtrip(self, tmp_path: Path) -> None:
        task = _make_task(
            context_files=[],
            success_criteria=None,
            source_session=None,
        )
        path = write_structured_handoff(tmp_path, "sess-rt", [task])
        result = parse_structured_handoff(path)

        assert len(result) == 1
        assert result[0].context_files == []
        assert result[0].success_criteria is None
        assert result[0].source_session is None


# ---------------------------------------------------------------------------
# TestBuildTaskPrompt
# ---------------------------------------------------------------------------


class TestBuildTaskPrompt:
    def test_includes_description(self) -> None:
        task = _make_task(description="Write integration tests")
        result = build_task_prompt(task)
        assert "Write integration tests" in result

    def test_includes_instructions_header(self) -> None:
        task = _make_task(prompt="Use pytest parametrize for edge cases.")
        result = build_task_prompt(task)
        assert "Instructions:" in result

    def test_includes_prompt_text(self) -> None:
        task = _make_task(prompt="Use pytest parametrize for edge cases.")
        result = build_task_prompt(task)
        assert "Use pytest parametrize for edge cases." in result

    def test_includes_context_files(self) -> None:
        task = _make_task(context_files=["src/cw/handoff.py", "tests/test_handoff.py"])
        result = build_task_prompt(task)
        assert "Context files:" in result
        assert "src/cw/handoff.py" in result
        assert "tests/test_handoff.py" in result

    def test_includes_success_criteria(self) -> None:
        task = _make_task(success_criteria="100% pass rate, zero ruff violations.")
        result = build_task_prompt(task)
        assert "Success criteria:" in result
        assert "100% pass rate, zero ruff violations." in result

    def test_no_context_files_section_when_empty(self) -> None:
        task = _make_task(context_files=[])
        result = build_task_prompt(task)
        assert "Context files:" not in result

    def test_no_success_criteria_section_when_none(self) -> None:
        task = _make_task(success_criteria=None)
        result = build_task_prompt(task)
        assert "Success criteria:" not in result

    def test_minimal_task_has_required_sections(self) -> None:
        task = _make_task(
            description="Minimal task",
            prompt="Just do the thing.",
            context_files=[],
            success_criteria=None,
        )
        result = build_task_prompt(task)
        assert "Minimal task" in result
        assert "Instructions:" in result
        assert "Just do the thing." in result

    def test_description_appears_before_instructions(self) -> None:
        task = _make_task(
            description="Important task",
            prompt="The actual instructions.",
        )
        result = build_task_prompt(task)
        desc_pos = result.index("Important task")
        instr_pos = result.index("Instructions:")
        assert desc_pos < instr_pos

    def test_context_files_appear_before_instructions(self) -> None:
        task = _make_task(
            context_files=["some/file.py"],
            prompt="Do something.",
        )
        result = build_task_prompt(task)
        files_pos = result.index("Context files:")
        instr_pos = result.index("Instructions:")
        assert files_pos < instr_pos

    def test_success_criteria_appears_before_instructions(self) -> None:
        task = _make_task(
            success_criteria="All green.",
            prompt="Do something.",
        )
        result = build_task_prompt(task)
        criteria_pos = result.index("Success criteria:")
        instr_pos = result.index("Instructions:")
        assert criteria_pos < instr_pos

    def test_multiple_context_files_each_listed(self) -> None:
        files = ["a.py", "b.py", "c.py"]
        task = _make_task(context_files=files)
        result = build_task_prompt(task)
        for f in files:
            assert f in result
