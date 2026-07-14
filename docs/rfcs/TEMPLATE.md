# RFC NNNN — <Title>

## Summary

<Two or three paragraphs. What changes and why.>

## Motivation

<The problem. Evidence, not assertion.>

## Design

<REQUIRED. Epics are parsed from the `### Epic` subsections below.>

### Epic I — <Epic name>

<Intent prose. This becomes the epic issue's Intent section.>

### Epic II — <Epic name>

<Intent prose.>

## Phasing

<Human-facing summary table. NOT parsed — `## Tickets` is the parse source.
Keep it readable for humans; it may pack multiple tickets per cell.>

| Wave | Track A | Track B |
|------|---------|---------|
| 0 (seams, blocking) | S1 — <name> | S2 — <name> |
| 1 | A1 · A2 | B1 · B2 |

## Resolved decisions

<Firm leans, decided to unblock sprint breakout. Each is reversible at
ticket-hardening if the code contradicts it. Each `D-*` id here is citable from a
ticket's `Scope:` field.>

- **D-S1 — <short name>.** <Decision text.>
- **D-A1 — <short name>.** <Decision text.>

## Tickets

<REQUIRED and PARSED. One `###` subsection per ticket. Every field is required
except `Depends on:`, which may be `none`. `Epic:` may be `none` for shared seams
that belong to no epic. Buildout transcribes these verbatim — it infers nothing.
Every field value (`Context:`, `Scope:`, etc.) must fit on a single line — do
not hard-wrap it across two physical lines. A wrapped continuation line is not
silently dropped: it is a contract violation and buildout refuses the RFC.>

### S1 — <ticket name>

- **Epic:** none
- **Wave:** 0
- **Sprint:** 0
- **Depends on:** none
- **Context:** <One or more sentences. Becomes the ticket's Context section.>
- **Scope:** D-S1, D-S2b
- **Acceptance:**
  - <Checkable statement.>
  - <Checkable statement.>

### A1 — <ticket name>

- **Epic:** I
- **Wave:** 1
- **Sprint:** 1
- **Depends on:** S1
- **Context:** <...>
- **Scope:** D-A1
- **Acceptance:**
  - <...>

## References

- `path/to/file.py:123` — <what lives here and why it matters>

## Issues

Issues: _(filled by `/sprint-buildout`)_
