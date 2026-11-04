---
name: adr
description: Use when a task involves Architecture Decision Records — read the existing index when present, propose an ADR after consequential decisions, and offer to bootstrap docs/decisions/ when needed. Reads `docs/decisions/INDEX.md` for context, drafts new ADRs from `~/.agents/skills/adr/templates/adr-template.md`.
---

# ADR skill

You manage Architecture Decision Records (ADRs) in MADR 3.0 style. When a project uses ADRs, they live in `docs/decisions/`.

## When to act

**Always:**
- At the start of a session in a project that has `docs/decisions/INDEX.md` —
  read it once. Cheap, one file, gives you decision context.
- Before proposing a consequential choice (see categories below), check the
  INDEX for an existing ADR that decides or supersedes the topic.
- After the user commits to a consequential choice, propose drafting an ADR.

**On first consequential decision in a project without `docs/decisions/`:**
Offer once to bootstrap: create the directory + `INDEX.md` + `_template.md` +
the first ADR `YYYY-MM-DD-use-adr.md`, plus the ADR for the current decision.
If declined, don't ask again in the session.

**On new-project bootstrap (`/gsd:new-project`, empty repo init, etc.):**
Add `docs/decisions/` only when the user requested ADRs or accepted a concrete proposal to adopt them.

## What counts as "consequential"

**Yes — write an ADR:**
- Technology / framework / runtime / language choice
- License (your own, or accepting a dependency with a non-trivial license)
- Packaging / distribution model
- Architectural pattern with long-term consequences
- Major dependency adoption or drop
- Deprecation / supersedure / breaking change
- Security or privacy tradeoff

**No — don't write an ADR:**
- Bug fix
- Refactor without architectural impact
- Tactical implementation detail
- Temporary stub / TODO
- Config tweak / cosmetic change

## How to write one

1. Copy the skeleton from `~/.agents/skills/adr/templates/adr-template.md`.
2. Filename: `docs/decisions/YYYY-MM-DD-kebab-slug.md` using today's date and
   a short verb-noun slug (e.g. `mit-license-only-mit-deps`,
   `postgres-as-primary-store`).
3. Fill the **mandatory sections**: Title, Status, Date, Context, Decision,
   Consequences (Positive / Neutral / Negative bullets).
4. Add **Alternatives considered** when the decision had real competitors —
   one paragraph per loser with why it lost.
5. Add **References** to related ADRs, RFCs, vendor docs, commits.
6. **Update `docs/decisions/INDEX.md`** — add a row at the top of the table.
7. If the ADR supersedes another, change the old ADR's Status to
   `Superseded by [link]` and link the new one in References.

## Bootstrap a project from zero

When a project lacks `docs/decisions/`:

```
docs/decisions/
├── INDEX.md              # from ~/.agents/skills/adr/templates/adr-INDEX-template.md
├── _template.md          # from ~/.agents/skills/adr/templates/adr-template.md
└── YYYY-MM-DD-use-adr.md # from ~/.agents/skills/adr/templates/adr-first-decision-template.md
```

Replace `YYYY-MM-DD` placeholders with today's date in every file. Update
the INDEX row to point at the `use-adr` ADR.

## Behavior rules

- **Create an ADR when the user requested it or accepted a concrete proposal.** After a consequential decision, offer to record it; do not insert an approval pause when that authorization is already present. The user owns the decision; you record it.
- **Don't duplicate** — if an existing ADR covers the topic, reference it
  instead of writing a new one. If the new decision overrides, supersede.
- **Stay generic** when the ADR lands in a public/OSS project — no
  project-specific hostnames, IPs, secrets, real users.
- **Match house style** — if the project has existing ADRs, mirror their
  level of detail and section structure.
- **One commit per ADR** is the default; batch only when atomically adopting
  a convention (e.g. "adopt ADR + N retro-ADRs" at project start).

## Cross-refs

- Templates: `~/.agents/skills/adr/templates/adr-{template,INDEX-template,first-decision-template}.md`
- MADR spec: https://adr.github.io/madr/
- adr-tools: https://github.com/npryce/adr-tools
