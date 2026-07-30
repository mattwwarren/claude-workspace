"""Operator self-identity resolution (RFC 0011 S1 D-S2b).

Resolves "who is the operator, on GitHub" — the login used to derive the
counterparty axis (:data:`cw.pr_hydrate.Counterparty`) for a PR: one authored
by the operator's own gh identity is ``"self"``.

This is distinct from ``cw.prompts``'s client-name-based ``[cw identity]``
tag, which names a *client workspace*, not a *GitHub account* — the two
identity concepts are unrelated and this module does not touch that one.

Mirrors ``cw.review_strategy``'s shape (a small dedicated module, a typed
public resolution function, lenient any-failure -> safe-default philosophy)
but NOT its config source: ``review_strategy`` reads
``.claude/project-config.yaml``; this module's runtime source is
``cw.gh.current_gh_login`` (a ``gh api user`` subprocess call), optionally
overridden by ``ClientConfig.operator_github_login``.

The runtime login is expensive to fetch (a subprocess call) and stable for
the life of the process, so a successful resolution is cached for the
process lifetime (mirrors ``cw.notify._peon_sh_path``'s no-arg
``@lru_cache(maxsize=1)`` shape). Unlike that cache, a FAILED resolution
must NOT be memoized — a transient `gh` hiccup should not permanently wedge
every subsequent counterparty derivation to "unknown." This is done by
raising a local sentinel exception on failure so ``functools.lru_cache``
never caches that call (stdlib ``lru_cache`` never caches a raised
exception).
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from cw.gh import current_gh_login

if TYPE_CHECKING:
    from cw.models import ClientConfig, OrchestratorConfig

_GH_LOGIN_TIMEOUT_SECONDS = 10


class _GhLoginUnresolvedError(Exception):
    """Internal sentinel: current_gh_login() returned None this call.

    Never exported. Raising (rather than returning None) is what keeps
    ``functools.lru_cache`` from memoizing a failed resolution.
    """


@lru_cache(maxsize=1)
def _fetch_and_cache_gh_login() -> str:
    """Fetch and cache the operator's gh login; raise on failure.

    Raising ``_GhLoginUnresolvedError`` (instead of returning ``None``) is
    deliberate: ``lru_cache`` never memoizes a call that raises, so a failed
    fetch is retried on the next call rather than permanently cached.
    """
    login = current_gh_login(timeout=_GH_LOGIN_TIMEOUT_SECONDS)
    if login is None:
        raise _GhLoginUnresolvedError
    return login


def cached_gh_login() -> str | None:
    """Return the operator's gh login, caching a successful result.

    Returns None on any failure to resolve it (gh binary absent, non-zero
    exit, a timeout) without caching that failure — the next call retries.
    """
    try:
        return _fetch_and_cache_gh_login()
    except _GhLoginUnresolvedError:
        return None


def cache_clear() -> None:
    """Reset the process-lifetime gh-login cache.

    Test-only reset hook — production code never needs to invalidate this
    cache mid-process.
    """
    _fetch_and_cache_gh_login.cache_clear()


def resolve_operator_login(client: ClientConfig) -> str | None:
    """Return the GitHub login to treat as "the operator" for *client*.

    ``client.operator_github_login`` wins when set (RFC 0011 S1 D-S2b
    override for the rare multi-account case); otherwise falls back to the
    runtime-resolved, process-cached gh login. Returns None when there is no
    override and the runtime login cannot be resolved.
    """
    return client.operator_github_login or cached_gh_login()


def resolve_operator_login_for_repo(
    repo: str, config: OrchestratorConfig, *, fallback: str | None
) -> str | None:
    """Return the GitHub login to treat as "the operator" for *repo*.

    RFC 0011 follow-up (#1171): honors
    ``OrchestratorConfig.operator_github_login_by_repo`` at the client-less
    entry points (``cw review register``, the ``review_requested`` webhook,
    ``hydrate_pr_states``) that have no ``ClientConfig`` to consult
    :func:`resolve_operator_login` for. *repo* is matched exactly
    (case-sensitive "owner/repo") against the map; a miss returns *fallback*
    unchanged.

    Deliberately never calls :func:`cached_gh_login` itself, unlike
    :func:`resolve_operator_login`'s inline fallback call -- this function is
    called once PER PR repo (potentially many times per ``hydrate_pr_states``
    tick), and a failed :func:`cached_gh_login` resolution is NOT memoized
    (see ``_GhLoginUnresolvedError`` above). An inline call here would
    reintroduce the per-candidate ``gh api user`` subprocess retry storm
    #1195 fixed. Callers resolve the fallback once (at most one
    :func:`cached_gh_login` call per tick) and thread it through, keeping
    this function a pure, zero-I/O dict lookup. Do not "fix" this by calling
    :func:`cached_gh_login` inline -- that is the bug this design avoids.
    """
    return config.operator_github_login_by_repo.get(repo, fallback)
