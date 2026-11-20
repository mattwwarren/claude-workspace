# The merge target is not the trust root

**Status:** Accepted
**Driven by:** #3779 (RFC 0020 D-3), epic #3777

## Decision

`client.default_branch` is the trust root. `TicketTask.base_branch` is the
merge target. They are different things, and a row's base never changes what
the trust root reads.

## Invariant

1. Anything that exists so a branch cannot alter its own judge stays on
   `client.default_branch`, regardless of the row's base. That covers the repo
   review engine source, its guard set and manifest, review attestation rules,
   and review configuration. These are read from git on the default branch.
2. Every read that asks "what does this branch merge into or diverge from"
   goes through `resolve_base_branch(client, task)`, and passes the row.
3. `base_branch` is immutable once the row has `ever_spawned`.
   `cw.models.tasks.base_branch.set_base_branch` is the only writer, and it
   refuses any change, including null to set.
4. Siblings minted by `add_ticket` at another stage are fresh rows with a null
   base. They do NOT inherit the base. Any later B1 writer must stamp each row
   explicitly.

## What this means for callers

- A reader of review judgment or trust-root material calls
  `client.default_branch` directly. It must not call `resolve_base_branch`.
- A reader of merge or divergence state calls `resolve_base_branch(client, task)`
  with the row in scope. Passing no row silently ignores a recorded base.
- Per-read classification (trust root versus merge target) is the job of the
  stages that change those readers (S2/S3). It is established by grep per
  ARCHITECTURE §7.11. The inventory command is
  `grep -rn "\.default_branch\b" src --include=*.py`. At `80f5296e0` that is 94
  attribute reads in 45 files outside `src/cw/models/client.py`. No reader is
  classified here, because S1 changes no reader.

## Consequences

- A null `base_branch` keeps every read byte-identical to today. The seam
  exists, but no production reader routes through it yet.
- Schema v66 adds the field with no migration filler. A pre-v66 row loads null
  and resolves to the client default.
- **Downgrade hazard.** An older cw that re-saves the queue drops the unknown
  field. For an epic row that silently retargets it to the default branch. This
  is the dangerous direction, unlike v65's fail-open hold on
  `merge_prerequisites`.
- Legacy rows default `ever_spawned=True`, so they are frozen. Fail-closed is
  intended.
- A bad on-disk `base_branch` would brick `load_dev_queue()` for every client
  (the #1129 mode). Every writer validates the charset first, through the
  shared `_check_branch_charset` helper.

## Alternatives considered

- **Epic row type.** Rejected: a new row type would duplicate the queue machinery
  for one attribute.
- **A per-client base.** Rejected: one client has several epics with different
  bases.
- **An immutable-at-creation validator.** Rejected: `TicketTask` has no
  `validate_assignment`, so a validator cannot see the old value. A guarded
  writer is the only available freeze.

## Referenced by

- #3777, #3779
