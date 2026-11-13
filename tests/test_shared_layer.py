"""Layer pins for ``tests/conftest.py`` and the ``tests/_shared/`` package (#2668).

``tests/conftest.py`` is the bootstrap-and-registration entry point only; the
shared fixtures and helpers live in concern modules under ``tests/_shared/``,
registered through conftest's ``pytest_plugins``. These pins keep the layer
from re-growing into one oversized file and keep the pieces that make the
split behavior-neutral (single autouse holder, assertion rewriting, the repo
root constant) from drifting.
"""

from __future__ import annotations

import ast
import graphlib
import importlib.util
from pathlib import Path

import pytest

import cw

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"
_CONFTEST = _TESTS_DIR / "conftest.py"
_SHARED_DIR = _TESTS_DIR / "_shared"
_SHARED_PACKAGE = "tests._shared"

#: CLAUDE.md "Module Size" ceiling for the shared test-infrastructure layer.
_MODULE_LINE_LIMIT = 1000

#: The one holder of function-scope autouse fixtures (see the autouse pin).
_AUTOUSE_MODULE = f"{_SHARED_PACKAGE}.autouse_isolation"
#: The one holder of session-scope autouse fixtures.
_SESSION_AUTOUSE_MODULE = f"{_SHARED_PACKAGE}.session_guards"


def _shared_modules() -> list[Path]:
    return sorted(_SHARED_DIR.glob("*.py"))


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _resolve_from(rel_path: Path, node: ast.ImportFrom) -> str:
    """Resolve ``node`` to a dotted module name, honouring relative levels.

    ``rel_path`` is the importing file relative to the repo root. Its package
    is the parent directory's dotted path (for ``tests/x.py`` that is
    ``tests``); a level-``L`` import strips ``L - 1`` trailing package parts.
    """
    if node.level == 0:
        return node.module or ""
    package = list(rel_path.parts[:-1])
    if node.level > 1:
        package = package[: -(node.level - 1)]
    if node.module:
        package.append(node.module)
    return ".".join(package)


def _resolved_conftest_imports(rel_path: Path, tree: ast.AST) -> list[int]:
    """Return the line of every import of ``tests.conftest``, in any form.

    Flags ``from tests.conftest import x`` and ``from .conftest import x``,
    ``from tests import conftest`` and ``from . import conftest``, and
    ``import tests.conftest``. Mentions inside string literals are ignored.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve_from(rel_path, node)
            if module == "tests.conftest" or (
                module == "tests" and any(a.name == "conftest" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
            a.name == "tests.conftest" for a in node.names
        ):
            lines.append(node.lineno)
    return lines


def _pytest_plugins(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "pytest_plugins"
            and isinstance(node.value, ast.List)
        ):
            return [
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    return []


def _fixture_names(tree: ast.Module) -> set[str]:
    """Spellings that name ``pytest.fixture`` in this module.

    Bounded to ``import pytest [as x]`` and ``from pytest import fixture [as x]``.
    No yield_fixture, star imports or dataflow: every current shared fixture is
    a literal spelling, so anything else is simply not matched.
    """
    names = {"pytest.fixture"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    names.add(f"{alias.asname or 'pytest'}.fixture")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                if alias.name == "fixture":
                    names.add(alias.asname or "fixture")
    return names


def _fixture_decorators(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    """Return ``(function name, decorator)`` for every ``pytest.fixture`` use."""
    names = _fixture_names(tree)
    found: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if ast.unparse(target) in names:
                found.append((node.name, decorator))
    return found


def _keyword(decorator: ast.expr, name: str) -> object:
    if not isinstance(decorator, ast.Call):
        return None
    for keyword in decorator.keywords:
        if keyword.arg != name:
            continue
        # A non-literal value must not read as None: that would pass an autouse
        # pin vacuously. Fail closed instead.
        assert isinstance(keyword.value, ast.Constant), (
            f"fixture keyword {name!r} must be a literal: {ast.unparse(keyword.value)}"
        )
        return keyword.value.value
    return None


def _runtime_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """Every import statement not nested under ``if TYPE_CHECKING:``."""
    found: list[ast.Import | ast.ImportFrom] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and ast.unparse(child.test) == "TYPE_CHECKING":
                for orelse in child.orelse:
                    visit(orelse)
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                found.append(child)
            visit(child)

    visit(tree)
    return found


@pytest.mark.parametrize(
    "path",
    [_CONFTEST, *_shared_modules()],
    ids=lambda p: p.relative_to(_REPO_ROOT).as_posix(),
)
def test_layer_modules_stay_under_the_module_size_ceiling(path: Path) -> None:
    """Every layer module stays under CLAUDE.md's ~1000-line module ceiling."""
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    assert line_count < _MODULE_LINE_LIMIT, path.name


def test_conftest_is_bootstrap_and_registration_only() -> None:
    """``tests/conftest.py`` defines nothing: new helpers go to ``tests/_shared``."""
    tree = _parse(_CONFTEST)
    rewrite_calls = 0
    for index, node in enumerate(tree.body):
        is_docstring = isinstance(node, ast.Expr) and isinstance(
            node.value, ast.Constant
        )
        if index == 0 and is_docstring:
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "pytest.register_assert_rewrite"
        ):
            rewrite_calls += 1
            continue
        is_plugin_list = (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "pytest_plugins"
            and isinstance(node.value, ast.List)
            and all(
                isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                for elt in node.value.elts
            )
        )
        assert is_plugin_list, f"conftest.py:{node.lineno} is not registration"
    assert rewrite_calls == 1


def test_every_fixture_bearing_module_is_a_registered_plugin() -> None:
    """A ``tests/_shared`` module with fixtures that is not registered never loads."""
    plugins = _pytest_plugins(_parse(_CONFTEST))
    for path in _shared_modules():
        if _fixture_decorators(_parse(path)):
            assert _dotted(path) in plugins, path.name
    for entry in plugins:
        assert importlib.util.find_spec(entry) is not None, entry


def test_autouse_fixtures_live_only_in_the_order_pinned_modules() -> None:
    """Autouse fixtures stay in the one holder that pins their setup order.

    pytest collects a holder's autouse fixtures by ``dir()`` (alphabetical by
    name) and then orders holders by registration, so spreading function-scope
    autouse fixtures across several plugin modules would reorder their setup.
    Function-scope autouse fixtures therefore live only in
    ``autouse_isolation``; session-scope ones (scope sorts first, so they are
    order-neutral) live only in ``session_guards``.
    """
    for path in _shared_modules():
        module = _dotted(path)
        for name, decorator in _fixture_decorators(_parse(path)):
            if _keyword(decorator, "autouse") is not True:
                continue
            expected = (
                _SESSION_AUTOUSE_MODULE
                if _keyword(decorator, "scope") == "session"
                else _AUTOUSE_MODULE
            )
            assert module == expected, f"{module}.{name}"


def test_no_module_imports_tests_conftest() -> None:
    """Shared code is imported from its ``tests/_shared`` home, never conftest."""
    offenders = []
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        rel_path = path.relative_to(_REPO_ROOT)
        offenders.extend(
            f"{rel_path.as_posix()}:{line}"
            for line in _resolved_conftest_imports(rel_path, _parse(path))
        )
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from tests.conftest import x", True),
        ("from .conftest import x", True),
        ("from . import conftest", True),
        ("from tests import conftest", True),
        ("import tests.conftest", True),
        ("from tests._shared.git_repos import x", False),
        ("from .helpers import x", False),
    ],
)
def test_conftest_import_detector_flags_every_form(source: str, flagged: bool) -> None:
    """Positive control: the detector above sees every import form."""
    hits = _resolved_conftest_imports(Path("tests/test_x.py"), ast.parse(source))
    assert bool(hits) is flagged


def test_shared_modules_form_an_import_dag() -> None:
    """Runtime sibling imports under ``tests/_shared`` have no cycle."""
    modules = {_dotted(path): path for path in _shared_modules()}
    graph: dict[str, set[str]] = {}
    for module, path in modules.items():
        rel_path = path.relative_to(_REPO_ROOT)
        tree = _parse(path)
        assert _resolved_conftest_imports(rel_path, tree) == [], module
        deps: set[str] = set()
        for node in _runtime_imports(tree):
            if isinstance(node, ast.ImportFrom):
                targets = [_resolve_from(rel_path, node)]
            else:
                targets = [alias.name for alias in node.names]
            deps.update(t for t in targets if t in modules and t != module)
        graph[module] = deps
    tuple(graphlib.TopologicalSorter(graph).static_order())


def test_package_init_imports_nothing() -> None:
    """Importing ``tests._shared`` never pulls ``cw`` ahead of the bootstrap."""
    tree = _parse(_SHARED_DIR / "__init__.py")
    assert len(tree.body) == 1
    node = tree.body[0]
    assert isinstance(node, ast.Expr)
    assert isinstance(node.value, ast.Constant)
    assert isinstance(node.value.value, str)


def test_repo_root_is_the_checkout() -> None:
    """``_REPO_ROOT`` moved one directory deeper; it must still name the checkout.

    ``_real_cw_guard`` arms the write guard on ``_REPO_ROOT / ".cw"``, so a
    wrong value would make that guard silently vacuous.
    """
    from tests._shared import workflow_text

    shared_root = workflow_text._REPO_ROOT
    assert shared_root == Path(cw.__file__).resolve().parents[2]
    assert (shared_root / "pyproject.toml").is_file()


def test_shared_modules_get_assertion_rewriting() -> None:
    """Helper asserts keep pytest's introspection after leaving conftest."""
    module = importlib.import_module(f"{_SHARED_PACKAGE}.guard_fences")
    assert "@pytest_ar" in vars(module)


# --- #3467: the fixture matcher sees every spelling, and fails closed ---------

_FIXTURE_SPELLINGS = [
    "import pytest\n@pytest.fixture\ndef f(): ...\n",
    "import pytest\n@pytest.fixture(autouse=True)\ndef f(): ...\n",
    "import pytest as pt\n@pt.fixture\ndef f(): ...\n",
    "from pytest import fixture\n@fixture\ndef f(): ...\n",
    "from pytest import fixture as fx\n@fx(autouse=True)\ndef f(): ...\n",
]


@pytest.mark.parametrize("src", _FIXTURE_SPELLINGS)
def test_fixture_matcher_sees_every_spelling(src: str) -> None:
    """Positive control: each supported spelling yields exactly one fixture."""
    found = _fixture_decorators(ast.parse(src))
    assert [name for name, _ in found] == ["f"]


@pytest.mark.parametrize(
    "src",
    [
        "import other\n@other.fixture\ndef f(): ...\n",
        "import pytest\n@pytest.mark.slow\ndef f(): ...\n",
        "def f(): ...\n",
    ],
)
def test_fixture_matcher_ignores_non_fixtures(src: str) -> None:
    assert _fixture_decorators(ast.parse(src)) == []


def test_autouse_detected_through_alias() -> None:
    tree = ast.parse(
        "from pytest import fixture as fx\n@fx(autouse=True)\ndef f(): ...\n"
    )
    ((_, decorator),) = _fixture_decorators(tree)
    assert _keyword(decorator, "autouse") is True


def test_non_literal_fixture_keyword_fails_closed() -> None:
    # A non-literal autouse= must not read as None, which would pass the pin
    # vacuously.
    src = (
        "import pytest\nSOME_FLAG = True\n"
        "@pytest.fixture(autouse=SOME_FLAG)\ndef f(): ...\n"
    )
    tree = ast.parse(src)
    ((_, decorator),) = _fixture_decorators(tree)
    with pytest.raises(AssertionError, match="must be a literal"):
        _keyword(decorator, "autouse")


def test_matcher_sees_real_layer_autouse_fixtures() -> None:
    """Real-tree control: the scan is not empty, so the pins above are not vacuous."""
    found = _fixture_decorators(_parse(_SHARED_DIR / "autouse_isolation.py"))
    assert found
    assert any(_keyword(decorator, "autouse") is True for _, decorator in found)
