"""Map a working directory to the configured client that owns it (#1644, #3170).

A client "owns" a directory tree when the tree is its declared workspace, its
backing repo, or a base ``cw`` provisions its worktrees under (the sibling
``.worktrees`` base or the hashed ``~/.cw/wt/<hash>`` base). Matching is pure
path arithmetic: no subprocess, no git, no network, so ``cw statusline render``
can call it on every assistant message.

Two entry points share one matcher:

- :func:`resolve_client_for_cwd` answers "which client, if any" for the
  statusline. A tie goes to the first client in ``clients.yaml`` order.
- :func:`infer_client` answers "which client must this command act on" for
  ``cw orchestrator-start``. A tie or no match is refused with a message that
  says to pass ``-c``; a nested match is accepted with a warning naming both
  clients.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cw._util import NO_CLIENTS_MSG
from cw.config import load_clients
from cw.exceptions import CwError
from cw.worktree import effective_worktree_bases

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cw.models import ClientConfig

# A two-client tie reads "both claim"; three or more read "all claim".
_PAIR = 2


class ClientInferenceError(CwError):
    """No single configured client could be inferred from the working directory."""

    __slots__ = ()


@dataclass(frozen=True)
class ClientRootMatch:
    """The outcome of matching candidate paths against every client's roots.

    *winners* are the clients (in config order) that claim *root*, the deepest
    matching root; more than one winner is a tie. *shadowed* lists each other
    matching client with its deepest matching root, which is shallower than
    *root* (a nested layout). *root* is ``None`` and *winners* empty when no
    client matched.
    """

    winners: tuple[str, ...]
    root: Path | None
    shadowed: tuple[tuple[str, Path], ...]


@dataclass(frozen=True)
class InferredClient:
    """The client :func:`infer_client` settled on, the root that matched, and
    any warnings the caller should print (a nested match)."""

    name: str
    root: Path
    warnings: tuple[str, ...]


def client_roots(client: ClientConfig) -> frozenset[Path]:
    """Every directory tree that means "you are working in *client*".

    The declared workspace, the backing repo for a worktree-mode client, and
    each base ``cw`` may have provisioned worktrees under. Pure path arithmetic
    — ``effective_worktree_bases`` shells out to nothing.
    """
    roots = {client.workspace_path, *effective_worktree_bases(client)}
    if client.repo_path is not None:
        roots.add(client.repo_path)
    return frozenset(Path(root).resolve() for root in roots)


def _deepest_hits(
    paths: Sequence[Path], clients: Mapping[str, ClientConfig]
) -> dict[str, Path]:
    """Each matching client's deepest root containing any of *paths*.

    Hits are pooled across all candidate paths, so two paths landing in the
    same client count once. Insertion order follows *clients* (config order).
    """
    resolved = [Path(p).resolve() for p in paths]
    hits: dict[str, Path] = {}
    for name, client in clients.items():
        for root in client_roots(client):
            if not any(path == root or root in path.parents for path in resolved):
                continue
            best = hits.get(name)
            if best is None or len(root.parts) > len(best.parts):
                hits[name] = root
    return hits


def match_client_roots(
    paths: Sequence[Path], clients: Mapping[str, ClientConfig]
) -> ClientRootMatch:
    """Match *paths* against every client's roots; the deepest root wins."""
    hits = _deepest_hits(paths, clients)
    if not hits:
        return ClientRootMatch(winners=(), root=None, shadowed=())
    best_root: Path | None = None
    for root in hits.values():
        if best_root is None or len(root.parts) > len(best_root.parts):
            best_root = root
    winners = tuple(name for name, root in hits.items() if root == best_root)
    shadowed = tuple((name, root) for name, root in hits.items() if name not in winners)
    return ClientRootMatch(winners=winners, root=best_root, shadowed=shadowed)


def resolve_client_for_cwd(
    cwd: Path, clients: dict[str, ClientConfig] | None = None
) -> str | None:
    """Return the configured client whose tree contains *cwd*, else None.

    When several clients match (e.g. a client whose workspace nests inside
    another's), the longest — most specific — root wins, so the answer does not
    depend on ``clients.yaml`` ordering. Clients tied on the same root resolve
    to the first one in config order.

    *clients* lets a caller that already loaded ``clients.yaml`` (e.g.
    ``cw.statusline._resolve_segment``) pass it through instead of triggering a
    second ``yaml.safe_load``; defaults to a fresh :func:`load_clients` call.
    """
    match = match_client_roots(
        [cwd], clients if clients is not None else load_clients()
    )
    return match.winners[0] if match.winners else None


def _quoted_names(names: Sequence[str]) -> str:
    quoted = [f"'{name}'" for name in names]
    return ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


def infer_client(
    paths: Sequence[Path], clients: Mapping[str, ClientConfig]
) -> InferredClient:
    """Infer the one client that owns *paths*, or raise :class:`ClientInferenceError`.

    ``paths[0]`` is the directory named in messages. Refuses with a "pass -c"
    message when no client is configured, none matches, or several clients
    claim the deepest matching root. A match that shadows a shallower client
    (nested workspaces) is accepted with a warning naming both.
    """
    if not clients:
        raise ClientInferenceError(NO_CLIENTS_MSG)
    where = Path(paths[0]) if paths else Path.cwd()
    match = match_client_roots(paths, clients)
    if match.root is None:
        configured = ", ".join(clients)
        msg = (
            f"could not infer a client from {where}; pass -c <name>. "
            f"Configured clients: {configured}"
        )
        raise ClientInferenceError(msg)
    if len(match.winners) > 1:
        claim = "both claim" if len(match.winners) == _PAIR else "all claim"
        msg = (
            f"cannot infer a client from {where}: clients "
            f"{_quoted_names(match.winners)} {claim} {match.root}; "
            "pass -c <name>"
        )
        raise ClientInferenceError(msg)
    name = match.winners[0]
    warnings = tuple(
        f"warning: {where} is inside client '{name}' ({match.root}) and also "
        f"client '{other}' ({other_root}); using the more specific '{name}'. "
        "Pass -c <name> to choose."
        for other, other_root in match.shadowed
    )
    return InferredClient(name=name, root=match.root, warnings=warnings)
