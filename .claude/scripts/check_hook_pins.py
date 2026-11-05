#!/usr/bin/env python3
"""Gate: mirrors-mypy hook pins must equal their `uv.lock` versions (#2961).

Usage (local pre-commit hook `hook-pins`, run by CI's all-hooks step):
    uv run python .claude/scripts/check_hook_pins.py [--config PATH] [--lock PATH]

Context: the `mirrors-mypy` hook installs `additional_dependencies` into its own
isolated environment, outside `uv.lock`. Floor-only specs (`click>=8.3.1`) let
that environment resolve a newer release than the lock, so CI type-checks
against packages the project never tested with. This script is the backstop: it
reads the hook's dependency list and `uv.lock` and compares them. It reads no
git state, commit, or event context.

Invariants (every `additional_dependencies` entry of the `mypy` hook in the
`mirrors-mypy` repo):
    Rule 1 — the entry is an exact pin: `name==version`, optionally with
        `[extras]`. Any other specifier (`>=`, `~=`, `==8.*`, `===`, a marker,
        a bare name) is `not_exact_pin`.
    Rule 2 — the name is present in `uv.lock` (PEP 503 normalized). Otherwise
        `missing_from_lock`.
    Rule 3 — the pinned version equals the locked version. Otherwise `drift`.

Pin-bump workflow: after `uv lock --upgrade[-package]` moves one of these
packages, run the hook (or this script); the failure output prints the exact
replacement lines, ready to paste under the mypy hook's
`additional_dependencies` in `.pre-commit-config.yaml`. Transitive dependencies
of those packages are not pinned and still float.

Exit codes:
    0  — every invariant holds
    1  — at least one violation (report on stdout), or an input error: missing
         or malformed config/lock, no mypy hook, an ambiguous lock (message on
         stderr, nothing on stdout). Inputs are fail-closed.

PyYAML is the one deliberate non-stdlib import. Unlike its stdlib-only
siblings this is a CI/pre-commit gate listed in `scripts/excluded-scripts.txt`
(never installed or run by a dispatched worker) and always invoked through
`uv run python`, where PyYAML is a runtime dependency. Carries no
`cw-script-version` marker for the same reason.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

KIND_DRIFT = "drift"
KIND_NOT_EXACT = "not_exact_pin"
KIND_MISSING = "missing_from_lock"

_PREFIX = "check_hook_pins"
_MYPY_REPO_SUFFIX = "/mirrors-mypy"
_MYPY_HOOK_ID = "mypy"
_FIX_INDENT = " " * 10
_NAME_SEP_RE = re.compile(r"[-_.]+")
_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*(?P<extras>\[[^\]]*\])?"
    r"\s*(?P<rest>.*)$"
)
_EXACT_RE = re.compile(r"^==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)$")


class HookPinError(Exception):
    """An input problem that prevents evaluating the invariants."""


@dataclass(frozen=True)
class Violation:
    dep: str
    kind: str
    detail: str
    fix: str | None


@dataclass(frozen=True)
class Spec:
    name: str
    extras: str
    version: str | None  # None: not an exact `==` pin


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return _NAME_SEP_RE.sub("-", name).lower()


def parse_spec(raw: str) -> Spec | None:
    """Split *raw* into name, extras and exact version; None if no name parses."""
    match = _SPEC_RE.match(raw.strip())
    if match is None:
        return None
    exact = _EXACT_RE.match(match["rest"])
    version = exact["version"] if exact is not None else None
    return Spec(name=match["name"], extras=match["extras"] or "", version=version)


def load_lock_versions(path: Path) -> dict[str, list[str]]:
    """Map each normalized package name in *path* to its locked version(s)."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        message = f"could not read lock file {path}: {exc}"
        raise HookPinError(message) from exc
    packages = data.get("package")
    if not isinstance(packages, list) or not packages:
        message = f"{path} has no [[package]] entries"
        raise HookPinError(message)
    versions: dict[str, list[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        found = versions.setdefault(normalize(name), [])
        if version not in found:
            found.append(version)
    return versions


def _is_mypy_repo(repo: object) -> bool:
    if not isinstance(repo, dict):
        return False
    url = str(repo.get("repo", "")).rstrip("/").removesuffix(".git")
    return url.endswith(_MYPY_REPO_SUFFIX)


def load_hook_deps(path: Path) -> list[str]:
    """The mirrors-mypy `mypy` hook's `additional_dependencies`, in config order."""
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        message = f"could not read config {path}: {exc}"
        raise HookPinError(message) from exc
    if not isinstance(config, dict):
        message = f"{path} top level is not a mapping"
        raise HookPinError(message)
    repos = config.get("repos")
    if not isinstance(repos, list):
        message = f"{path} has no `repos` list"
        raise HookPinError(message)
    for repo in repos:
        if not _is_mypy_repo(repo):
            continue
        for hook in repo.get("hooks") or []:
            if isinstance(hook, dict) and hook.get("id") == _MYPY_HOOK_ID:
                return _hook_deps(hook, path)
    message = f"no `{_MYPY_HOOK_ID}` hook from a mirrors-mypy repo in {path}"
    raise HookPinError(message)


def _hook_deps(hook: dict[object, object], path: Path) -> list[str]:
    deps = hook.get("additional_dependencies")
    if (
        not isinstance(deps, list)
        or not deps
        or not all(isinstance(dep, str) for dep in deps)
    ):
        message = (
            f"mypy hook in {path} has no `additional_dependencies` list of strings"
        )
        raise HookPinError(message)
    return [str(dep) for dep in deps]


def _fix_line(spec: Spec, locked: str) -> str:
    return f"{spec.name}{spec.extras}=={locked}"


def _check_dep(raw: str, lock: dict[str, list[str]]) -> Violation | None:
    spec = parse_spec(raw)
    if spec is None:
        return Violation(raw, KIND_NOT_EXACT, f"cannot parse `{raw}`", None)
    locked_versions = lock.get(normalize(spec.name))
    if locked_versions is not None and len(locked_versions) > 1:
        message = (
            f"`{spec.name}` has several versions in uv.lock"
            f" ({', '.join(locked_versions)}); cannot pick one to pin"
        )
        raise HookPinError(message)
    locked = locked_versions[0] if locked_versions else None
    fix = _fix_line(spec, locked) if locked is not None else None
    if spec.version is None:
        suffix = f"; uv.lock has {locked}" if locked is not None else ""
        detail = f"`{raw}` is not an exact `==` pin{suffix}"
        return Violation(raw, KIND_NOT_EXACT, detail, fix)
    if locked is None:
        return Violation(raw, KIND_MISSING, f"`{spec.name}` is not in uv.lock", None)
    if spec.version != locked:
        detail = f"pinned {spec.version}, uv.lock has {locked}"
        return Violation(raw, KIND_DRIFT, detail, fix)
    return None


def evaluate(deps: list[str], lock: dict[str, list[str]]) -> list[Violation]:
    violations = (_check_dep(raw, lock) for raw in deps)
    return [v for v in violations if v is not None]


def _summarize(violations: list[Violation], total: int) -> str:
    if not violations:
        return f"{_PREFIX}: OK — {total} mypy hook pins match uv.lock"
    rows = [f"  [{v.kind}] {v.dep}: {v.detail}" for v in violations]
    lines = [f"{_PREFIX}: {len(violations)} violation(s):", *rows]
    fixes = [v.fix for v in violations if v.fix is not None]
    if fixes:
        lines.append(
            "Replace these entries under the mypy hook's additional_dependencies"
            " in .pre-commit-config.yaml:"
        )
        lines.extend(f"{_FIX_INDENT}- {fix}" for fix in fixes)
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    deps = load_hook_deps(Path(args.config))
    lock = load_lock_versions(Path(args.lock))
    violations = evaluate(deps, lock)
    print(_summarize(violations, len(deps)))
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Return 0 (pins match) or 1 (violation or input error)."""
    parser = argparse.ArgumentParser(
        description="Assert mirrors-mypy hook pins match uv.lock.",
    )
    parser.add_argument(
        "--config", default=".pre-commit-config.yaml", help="Path to the config"
    )
    parser.add_argument("--lock", default="uv.lock", help="Path to uv.lock")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except HookPinError as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
