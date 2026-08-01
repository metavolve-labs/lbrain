# Lair Rules — Authoring Contract

**Last Updated**: {YYYY-MM-DD}
**Mission**: Keep every lair dense, current, single-concern, and navigable so any agent recovers full context cold.

---

## The Line Cap

**A LAIR.md must not exceed 500 lines.** When you approach it, in order:
1. Extract session logs to a `sessions/` subfolder.
2. Extract large code blocks to referenced files (link, don't paste).
3. Split multi-concern content into separate lairs.
4. Summarize verbose sections into tables.

If a domain genuinely needs >500 lines of *active* context, split it into multiple cross-referenced lairs.

---

## One Lair, One Concern

If you can't state a lair's purpose in one sentence, split it. Multiple *phases* of one concern are fine in one lair; multiple *concerns* are not.

---

## Format: Compact, AI-Optimized

**DO**
- Front-load Status, Priority, Mission, and current state in the **first 30 lines**.
- Tables over prose; key-value pairs over paragraphs.
- Code blocks only for load-bearing patterns (env vars, the one critical command) — never full scripts.
- Decisions as one line: `DECISION: X over Y because Z`.
- File references as `path/to/file.ext:line`, not pasted code.
- Use status emoji as inline cell values: ✅ working · 🔄 in progress · ⬜ not started · ❌ broken · ⏸ paused.

**DON'T**
- Long narrative paragraphs.
- Full script pastes (reference the file).
- Repeating info that lives in another lair.
- Session logs inside LAIR.md (use `sessions/`).
- More than one motto/quote (bottom only).

---

## Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Priority lair | `000-PRIORITY-{NAME}/` | `000-PRIORITY-LAUNCH/` |
| Standard lair | `{kebab-case-name}/` | `billing-pipeline/` |
| The lair file | always `LAIR.md` | — |
| Other doc files | `UPPER_SNAKE.md` | `RUNBOOK.md` |
| Session logs | `sessions/SESSION_{YYYY-MM-DD}.md` | `sessions/SESSION_2026-05-31.md` |

The `000-` prefix sorts priority lairs to the top. Date format is always `YYYY-MM-DD`. The `Last Updated` field is the staleness clock — update it on any meaningful change, not just reads. When downgrading priority, preserve the prior value: `MEDIUM (was HIGH)`.

---

## Required Sections (in order)

1. **Header** — name, Status, Priority, Last Updated, Mission. (required)
2. **Current State** — table + Blocked-by + Next action. (required)
3. **Architecture** — design + Key Files table. (if applicable)
4. **Decisions Log** — one line each. (required)
5. **Related Lairs** — cross-reference table. (required)

Optional: Implementation Checklist, Troubleshooting, Live URLs/Contracts, links to `sessions/`.

Enums — Status: `PLANNING | ACTIVE | OPERATIONAL | BLOCKED | DORMANT`. Priority: `CRITICAL | HIGH | MEDIUM | LOW`.

---

## Cross-References Are Mandatory and Bidirectional

Every lair MUST end with `## Related Lairs`. Creating a lair requires adding back-references in the lairs it names; archiving requires removing/updating them. This keeps the knowledge graph navigable and reveals orphans during audits.

---

## Staleness Policy

| Age (since Last Updated) | Action |
|--------------------------|--------|
| 0–30 days | Active — no action |
| 30–60 days | Flag in audit — update or mark DORMANT |
| 60+ days | Archive candidate — move to `_archive/` unless actively blocked |

---

## Lifecycle

**Create**: copy `LAIR_TEMPLATE.md` → `new-lair/LAIR.md`; fill required sections; pass the one-sentence test; add cross-refs (and back-refs); add to your index/START_HERE.

**Archive**: move to `_archive/`; route unique content to the canonical lair that absorbs it; remove from index; update back-references. Leave a one-line banner naming the replacement, so a reader is redirected, not silently misled.

---

## Index Re-sync (after every edit)

The source `.md` is authoritative; any vector/search index is a derivative cache and does **not** auto-detect edits. After changing any lair file — even a status line — re-import and re-embed your index so queries don't return stale answers:

```bash
lbrain import <lairs-dir> && lbrain embed --stale
```
