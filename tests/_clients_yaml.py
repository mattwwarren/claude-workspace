"""One shared ``clients.yaml`` writer for the test suite (#2165).

Replaces the per-file clients.yaml writer copies, each of which wrote its own
subset of client fields as hand-built YAML text.
This module has no ``test_`` prefix, so pytest does not collect it (same
convention as ``tests/_reconcile_helpers.py``); test modules import it
explicitly.

It is a plain function rather than a fixture: the autouse ``tmp_config_dir``
fixture already isolates ``cw.config.CLIENTS_FILE``, and
:func:`write_clients_yaml` resolves its target through
``cw.config.clients_file()`` at call time, so it needs no fixture of its own and
works from class methods and module-level helpers alike.

Serialization contract:

- A :class:`ClientSpec` entry is its non-``None`` typed fields (in declaration
  order), then ``lanes``, then ``extra`` deep-merged over both, ``extra``
  winning. Dicts merge key by key; any other value in ``extra`` (lists included)
  replaces the base value wholesale.
- A ``ClientConfig`` is written through :meth:`ClientSpec.from_config`, which
  dumps every explicitly-set field via pydantic, so bool/int/``None`` values and
  enums round-trip without hand-built YAML text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

import cw.config
from cw.models import DEFAULT_LANE, ClientConfig, LaneConfig, ReapPolicy

# Typed ClientSpec fields, in the order they are written.
_TYPED_FIELDS = (
    "workspace_path",
    "repo_path",
    "branch",
    "default_branch",
    "worktree_base",
    "worker_model",
    "quality_gate_commands",
)
_STAGED_WORKSPACE = "/tmp/ws-staged"
_STAGED_STAGES = ("plan", "impl", "review", "finalize")


@dataclass(frozen=True)
class ClientSpec:
    """One ``clients.yaml`` entry; ``None`` typed fields are omitted.

    A test that needs a key absent versus present-and-empty, or any
    ``ClientConfig`` field without a typed slot here, passes it in ``extra``.
    """

    name: str
    workspace_path: Path | str | None = None
    repo_path: Path | str | None = None
    branch: str | None = None
    default_branch: str | None = None
    worktree_base: Path | str | None = None
    worker_model: str | None = None
    quality_gate_commands: str | None = None
    lanes: Sequence[LaneConfig | Mapping[str, object]] = ()
    extra: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls, client: ClientConfig, *, extra: Mapping[str, object] | None = None
    ) -> ClientSpec:
        """Build a spec holding every field explicitly set on *client*.

        Typed fields come from the dump; everything else rides in ``extra``,
        with the caller's *extra* deep-merged over it.
        """
        dumped: dict[str, object] = client.model_dump(
            mode="json", exclude_unset=True, exclude={"name"}
        )
        typed = {key: _pop_str(dumped, key) for key in _TYPED_FIELDS}
        lanes = dumped.pop("lanes", None)
        return cls(
            client.name,
            workspace_path=typed["workspace_path"],
            repo_path=typed["repo_path"],
            branch=typed["branch"],
            default_branch=typed["default_branch"],
            worktree_base=typed["worktree_base"],
            worker_model=typed["worker_model"],
            quality_gate_commands=typed["quality_gate_commands"],
            lanes=lanes if isinstance(lanes, list) else (),
            extra=_deep_merge(dumped, extra or {}),
        )


def write_clients_yaml(
    *clients: ClientConfig | ClientSpec, ensure_workspaces: bool = False
) -> Path:
    """Write *clients* to ``cw.config.clients_file()`` and return that path.

    With *ensure_workspaces*, each non-``None`` ``workspace_path`` is created
    first (for helpers whose old copies made the workspace directory).
    """
    specs = [
        client if isinstance(client, ClientSpec) else ClientSpec.from_config(client)
        for client in clients
    ]
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        msg = f"duplicate client names in clients.yaml: {duplicates}"
        raise ValueError(msg)

    path = cw.config.clients_file()
    cw.config.refuse_real_state_write(path)
    if ensure_workspaces:
        for spec in specs:
            if spec.workspace_path is not None:
                Path(spec.workspace_path).mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"clients": {spec.name: _spec_entry(spec) for spec in specs}}
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def staged_client(
    name: str,
    workspace_path: Path | str = _STAGED_WORKSPACE,
    *,
    sentinel_mismatch_veto: bool = False,
) -> ClientSpec:
    """A staged-pipeline client: requeue enabled, explicit four-stage pipeline.

    ``sentinel_mismatch_veto_enabled: true`` is written only when
    *sentinel_mismatch_veto* is set (the reconcile sites); otherwise the key is
    omitted (the CLI sites).
    """
    extra: dict[str, object] = {"blocked_result_requeue_enabled": True}
    if sentinel_mismatch_veto:
        extra["sentinel_mismatch_veto_enabled"] = True
    extra["pipeline"] = {"stages": list(_STAGED_STAGES)}
    return ClientSpec(name, workspace_path, default_branch="main", extra=extra)


def executor_backend_extra(stage: str, backend: str) -> dict[str, object]:
    """``extra`` that pins *stage*'s executor to *backend*."""
    return {"pipeline": {"executors": {stage: {"backend": backend}}}}


def review_backend_clients(
    workspace: Path | str,
    backend: str,
    *,
    names: tuple[str, ...] = ("client-a",),
    lane_reap_policies: Mapping[str, ReapPolicy] | None = None,
) -> list[ClientSpec]:
    """Clients in *names* whose review stage runs on *backend*.

    *lane_reap_policies* maps a client name to the ``reap_policy`` its
    ``DEFAULT_LANE`` lane declares; a client absent from it declares no lanes.
    """
    policies = lane_reap_policies or {}
    return [
        ClientSpec(
            name,
            workspace,
            default_branch="main",
            lanes=(
                [LaneConfig(name=DEFAULT_LANE, reap_policy=policies[name])]
                if name in policies
                else ()
            ),
            extra=executor_backend_extra("review", backend),
        )
        for name in names
    ]


def _pop_str(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.pop(key, None)
    return None if value is None else str(value)


def _spec_entry(spec: ClientSpec) -> dict[str, object]:
    entry: dict[str, object] = {}
    for key in _TYPED_FIELDS:
        value = getattr(spec, key)
        if value is not None:
            entry[key] = value
    if spec.lanes:
        entry["lanes"] = [_lane_entry(lane) for lane in spec.lanes]
    return _plain_mapping(_deep_merge(entry, spec.extra))


def _lane_entry(lane: LaneConfig | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(lane, LaneConfig):
        dumped: dict[str, object] = lane.model_dump(mode="json", exclude_unset=True)
        return dumped
    return lane


def _deep_merge(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _plain_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _plain(value) for key, value in mapping.items()}


def _plain(value: object) -> object:
    """Reduce *value* to what ``yaml.safe_dump`` represents (no Path/Enum)."""
    if isinstance(value, Mapping):
        return _plain_mapping(value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value
