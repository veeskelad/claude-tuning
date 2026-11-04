# ADR: Use Architecture Decision Records

**Status:** Accepted
**Date:** YYYY-MM-DD
**Tags:** meta

## Context

This project needs a low-friction way to capture *why* architectural choices
were made — license, framework, packaging, third-party dependencies, security
tradeoffs, deprecations. Without an explicit record those decisions live in
chat history and commits, decay quickly, and force every future contributor
(human or AI) to re-derive context from grep'ing the codebase.

## Decision

Adopt **Architecture Decision Records (ADRs)** in
[MADR 3.0](https://adr.github.io/madr/) style, stored in `docs/decisions/`.

- One file per decision, named `YYYY-MM-DD-kebab-slug.md`.
- Mandatory sections: Title, Status, Date, Context, Decision, Consequences.
- Optional: Alternatives considered, References.
- `docs/decisions/INDEX.md` lists every ADR with status, date, and tags.
- `docs/decisions/_template.md` is the canonical skeleton for new ADRs.

This first ADR exists to declare the convention itself, following the
classic `adr-tools` pattern.

## Consequences

**Positive**
- Every significant decision has a permanent, searchable home.
- AI assistants (Claude Code, Cursor, others) can read `INDEX.md` at session
  start and pick up project context without re-deriving it from code.
- New contributors can scan one directory to understand the why behind
  current architecture.
- Reverting or revisiting a decision becomes a Status change with a clear
  audit trail, not a silent rewrite.

**Neutral**
- Adds a documentation step to consequential PRs.
- Reviewers must check that a new dependency or framework choice is matched
  by an ADR (or argue why it isn't consequential).

**Negative**
- Light upfront tax on writing a new ADR (~10–20 minutes per consequential
  decision).
- Risk of ADR-pollution if the bar for "consequential" is too low; mitigated
  by the explicit non-ADR list in the `adr` skill.

## References

- [MADR 3.0 specification](https://adr.github.io/madr/)
- [adr-tools (Michael Nygard pattern)](https://github.com/npryce/adr-tools)
- Template: [`_template.md`](./_template.md)
