"""Changelog pins survive a release cut (#3157).

Two parts. First, the contract of the shared whole-changelog readers in
``tests/_shared/workflow_text.py``. Second, a release-cut simulation: every
changelog pin in ``tests/test_*.py`` is run against three states of the
changelog: the real file, a *pooled* file (every released heading removed, so
all entries sit under ``[Unreleased]``, the pre-cut state), and a *cut* file
(the pooled file with a new version heading inserted under ``[Unreleased]``,
which is what a release does).

The simulation runs in process through the ``CHANGELOG_PATH`` seam: the pins'
own assertions execute against the cut text, with no subprocess and no mocks.
Pins are discovered by AST (any test or helper that calls ``changelog_text`` or
``changelog_entry``), so a new pin is covered without a hand-kept list.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._shared import workflow_text
from tests._shared.workflow_text import _REPO_ROOT, changelog_entry, changelog_text

_TESTS_ROOT = _REPO_ROOT / "tests"
_READERS = frozenset({"changelog_text", "changelog_entry"})
_MIN_PINS = 12
_CANARIES = (
    ("test_shipped_red_docs", "test_changelog_entry_exists"),
    ("test_stage_preflight", "test_the_changelog_names_every_renamed_reason"),
)
_SCRATCH_VERSION = "9.9.9"
_SCRATCH_DATE = "2099-01-01"
_RELEASED_HEADING = re.compile(r"(?m)^## \[\d+\.\d+\.\d+\].*\n")
_UNRELEASED = "## [Unreleased]\n"
_EXPECTED_EXCEPTIONS = (AssertionError, ValueError, StopIteration)


def _pool(text: str) -> str:
    """Move every released entry under ``[Unreleased]``: the pre-cut state."""
    return _RELEASED_HEADING.sub("", text)


def _cut(text: str, version: str = _SCRATCH_VERSION, date: str = _SCRATCH_DATE) -> str:
    """Insert a version heading under ``[Unreleased]``, as a release cut does."""
    if text.count(_UNRELEASED) != 1:
        msg = f"expected exactly one {_UNRELEASED!r} in the changelog"
        raise AssertionError(msg)
    heading = f"## [{version}] - {date}\n"
    return text.replace(_UNRELEASED, f"{_UNRELEASED}\n{heading}")


_TWO_SECTIONS = (
    "# Changelog\n\n## [Unreleased]\n\n- **Alpha entry (#1).**\n\n"
    "## [1.0.0] - 2026-01-01\n\n- **Beta entry (#2).**\n"
)


def _scratch_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    scratch = tmp_path / "CHANGELOG.md"
    scratch.write_text(text, encoding="utf-8")
    monkeypatch.setattr(workflow_text, "CHANGELOG_PATH", scratch)


def test_the_repo_seam_defaults_to_the_checkout_changelog() -> None:
    assert workflow_text.CHANGELOG_PATH == _REPO_ROOT / "CHANGELOG.md"


def test_changelog_text_returns_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_changelog(tmp_path, monkeypatch, _TWO_SECTIONS)
    assert changelog_text() == _TWO_SECTIONS


def test_changelog_text_is_read_fresh_on_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_changelog(tmp_path, monkeypatch, _TWO_SECTIONS)
    assert changelog_text() == _TWO_SECTIONS
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    assert changelog_text() == "# Changelog\n"


def test_changelog_entry_returns_the_single_matching_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_changelog(tmp_path, monkeypatch, _TWO_SECTIONS)
    assert changelog_entry("(#2)") == "- **Beta entry (#2).**"


def test_changelog_entry_raises_on_zero_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_changelog(tmp_path, monkeypatch, _TWO_SECTIONS)
    with pytest.raises(AssertionError, match="matched 0 lines"):
        changelog_entry("(#999)")


def test_changelog_entry_raises_on_two_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scratch_changelog(tmp_path, monkeypatch, _TWO_SECTIONS + "- **Gamma (#2).**\n")
    with pytest.raises(AssertionError, match="matched 2 lines"):
        changelog_entry("(#2)")


def _discover() -> list[tuple[str, str]]:
    """``(module, qualname)`` of every test or helper that calls a changelog reader."""
    found: list[tuple[str, str]] = []
    for path in sorted(_TESTS_ROOT.glob("test_*.py")):
        if path.name == "test_changelog_text.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend((path.stem, qualname) for qualname in _reader_pins(tree))
    return found


def _reader_pins(tree: ast.Module) -> list[str]:
    """Qualified names of the module-level and class-method pins in *tree*."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and _calls_reader(node):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            names.extend(
                f"{node.name}.{item.name}"
                for item in node.body
                if isinstance(item, ast.FunctionDef) and _calls_reader(item)
            )
    return names


def _calls_reader(func: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id in _READERS for node in ast.walk(func)
    )


def _resolve(module: str, qualname: str) -> Callable[[], object]:
    """Import a discovered pin and return it as a zero-argument callable."""
    mod = importlib.import_module(f"tests.{module}")
    owner_name, _, attr = qualname.rpartition(".")
    if owner_name:
        owner: object = getattr(mod, owner_name)()
        target = getattr(owner, attr)
    else:
        target = getattr(mod, attr)
    if inspect.signature(target).parameters:
        msg = (
            f"changelog pin {module}::{qualname} takes parameters; "
            "a changelog pin must be zero-argument"
        )
        raise AssertionError(msg)
    fn: Callable[[], object] = target
    return fn


def _pin_id(pin: tuple[str, str]) -> str:
    return f"{pin[0]}::{pin[1]}"


_PINS = _discover()
_STATES = ("real", "pooled", "cut")


def _fails(fn: Callable[[], object]) -> bool:
    try:
        fn()
    except _EXPECTED_EXCEPTIONS:
        return True
    return False


def _use_state(state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if state == "real":
        return
    real = workflow_text.CHANGELOG_PATH.read_text(encoding="utf-8")
    text = _pool(real) if state == "pooled" else _cut(_pool(real))
    scratch = tmp_path / "CHANGELOG.md"
    scratch.write_text(text, encoding="utf-8")
    monkeypatch.setattr(workflow_text, "CHANGELOG_PATH", scratch)


def test_discovery_finds_the_known_pins() -> None:
    assert len(_PINS) >= _MIN_PINS, _PINS
    for module, name in _CANARIES:
        assert (module, name) in _PINS, (module, name)


def test_cut_builder_shapes_the_scratch_changelog() -> None:
    real = workflow_text.CHANGELOG_PATH.read_text(encoding="utf-8")
    cut = _cut(_pool(real))
    assert cut.count(_UNRELEASED) == 1
    assert f"{_UNRELEASED}\n## [{_SCRATCH_VERSION}] - {_SCRATCH_DATE}\n" in cut
    assert _RELEASED_HEADING.findall(cut) == [
        f"## [{_SCRATCH_VERSION}] - {_SCRATCH_DATE}\n"
    ]


@pytest.mark.parametrize("state", _STATES)
@pytest.mark.parametrize("pin", _PINS, ids=_pin_id)
def test_changelog_pins_hold_in_every_release_state(
    state: str,
    pin: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_state(state, tmp_path, monkeypatch)
    _resolve(*pin)()


def test_a_slicing_pin_would_break_on_the_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control 1: a bounded Unreleased window dies at the cut.

    The old pins sliced from ``## [Unreleased]`` up to the next version heading.
    Right after a cut that section is empty, so the entry is no longer inside
    it; the helper still finds it.
    """
    pre = (
        "# Changelog\n\n## [Unreleased]\n\n- **Entry X (#1).**\n\n"
        "## [1.0.0] - 2026-01-01\n\n- old\n"
    )
    entry = "- **Entry X (#1).**"

    def bounded_window(text: str) -> str:
        start = text.index(_UNRELEASED)
        return text[start : text.index("\n## [", start + 1)]

    cut = _cut(pre)
    assert entry in bounded_window(pre)
    assert entry not in bounded_window(cut)

    scratch = tmp_path / "CHANGELOG.md"
    scratch.write_text(cut, encoding="utf-8")
    monkeypatch.setattr(workflow_text, "CHANGELOG_PATH", scratch)
    assert entry in changelog_text()
    assert changelog_entry(entry) == entry


def test_every_pin_fails_against_an_empty_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control 2: with no entry, every pin must fail, not pass vacuously."""
    scratch = tmp_path / "CHANGELOG.md"
    scratch.write_text("# Changelog\n", encoding="utf-8")
    monkeypatch.setattr(workflow_text, "CHANGELOG_PATH", scratch)
    survivors = [_pin_id(pin) for pin in _PINS if not _fails(_resolve(*pin))]
    assert survivors == []
