"""Tests for ``cw.markdown_fence``: the fence widener and the line classifier."""

from __future__ import annotations

import pytest

from cw.markdown_fence import LineKind, classify_lines, fence_for


@pytest.mark.parametrize(
    ("text", "fence"),
    [
        ("", "```"),
        ("plain", "```"),
        ("one ` tick", "```"),
        ("two `` ticks", "```"),
        ("three ``` ticks", "````"),
        ("five ````` ticks", "``````"),
        ("five ````` ticks and ``` three", "``````"),
        ("`` then ```` then `", "`````"),
    ],
)
def test_fence_is_one_longer_than_the_longest_backtick_run(
    text: str, fence: str
) -> None:
    assert fence_for(text) == fence


def _kinds(text: str) -> list[LineKind]:
    return classify_lines(text.splitlines())


def test_plain_text_is_text() -> None:
    assert _kinds("one\n\ntwo") == ["text", "text", "text"]


def test_empty_input_is_empty() -> None:
    assert classify_lines([]) == []


def test_backtick_fence_marks_opener_body_and_closer() -> None:
    assert _kinds("a\n```\nbody\n```\nb") == [
        "text",
        "fence",
        "fence",
        "fence",
        "text",
    ]


def test_tilde_fence() -> None:
    assert _kinds("~~~\nbody\n~~~\nafter") == ["fence", "fence", "fence", "text"]


def test_info_string_opener() -> None:
    assert _kinds("```bash\necho hi\n```\nx") == ["fence", "fence", "fence", "text"]


def test_shorter_closer_does_not_close() -> None:
    assert _kinds("````\n```\nstill\n````\nout") == [
        "fence",
        "fence",
        "fence",
        "fence",
        "text",
    ]


def test_closer_of_other_character_does_not_close() -> None:
    assert _kinds("```\n~~~\nstill\n```\nout") == [
        "fence",
        "fence",
        "fence",
        "fence",
        "text",
    ]


def test_closer_with_info_string_does_not_close() -> None:
    assert _kinds("```\n```py\nstill\n```\nout") == [
        "fence",
        "fence",
        "fence",
        "fence",
        "text",
    ]


def test_nested_longer_fence_holds_inner_fence() -> None:
    text = "````markdown\n```bash\nx\n```\n````\nout"
    assert _kinds(text) == ["fence"] * 5 + ["text"]


def test_indented_fence() -> None:
    assert _kinds("- item\n  ```\n  body\n  ```\nout") == [
        "text",
        "fence",
        "fence",
        "fence",
        "text",
    ]


def test_unterminated_fence_swallows_the_rest() -> None:
    assert _kinds("a\n```\nb\nc") == ["text", "fence", "fence", "fence"]


def test_table_row_outside_fence_is_table() -> None:
    assert _kinds("| a | b |\n|---|---|\n  | c |\nx") == [
        "table",
        "table",
        "table",
        "text",
    ]


def test_table_row_inside_fence_is_fence() -> None:
    assert _kinds("```\n| a |\n```") == ["fence", "fence", "fence"]


def test_two_backticks_are_not_a_fence() -> None:
    assert _kinds("``x``\nnext") == ["text", "text"]
