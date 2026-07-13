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
    from cw.models import ClientConfig

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
