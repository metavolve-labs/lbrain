# Fast-Start Lair Build Protocol

`lbrain lair from-repo <path>` — convert a code repo + its `README.md`/`CLAUDE.md` into a filled, governance-conformant `LAIR.md`.

Implements as a new Click subcommand in `lbrain/cli.py` (the CLI is Click-grouped: `@main.command()`, entry `lbrain.cli:main`). Companion module: `lbrain/lair_from_repo.py`.

---

## (a) Step-by-step pipeline

```
lbrain lair from-repo <repo-path> [--dest <lairs-dir>] [--name NAME] [--priority CRITICAL|HIGH|MEDIUM|LOW]
                                  [--dry-run] [--no-embed] [--model gpt-4o|claude-...]
```

**Stage 0 — Resolve target dir.** Default `--dest` = the configured lairs root; honor it even if invoked from elsewhere.

**Stage 1 — Harvest deterministic signals (no LLM).** Pure-Python collector emits a JSON `RepoFacts` blob:
- `git -C <repo> log -1 --format=%cs` → `last_commit_date`; `git log -1 --format=%H` → commit hash; `git remote get-url origin` → repo URL.
- Read `README.md`, `CLAUDE.md` (root + any nested), `START_HERE.md` if present.
- Detect deploy artifacts: `Dockerfile`, `firebase.json`, `deploy.sh`, `pyproject.toml`, `package.json`, `*.tf`, `cloudbuild.yaml`.
- Extract H1, the `## Mission` blockquote, `**What this is**:` line, README tagline (bold line under H1).
- Extract every annotated directory-tree line (` # comment`) and any `path:line` refs → Key Files candidates.
- Extract all fenced bash blocks under `## Key Commands` / "Quick Start" / "Self-Hosting".
- Extract every markdown table verbatim (status tables, pricing, Working Solutions Index, Live Contracts, Cross-Workspace References).
- Probe live URLs found in docs (HEAD request, 3s timeout) → resolves? flag (used for Status inference).
- Scan for blocker tokens: `BLOCKED`, `needs deploy`, `broken`, `regression`, `TODO`, `WIP`, `(COLD, min=0)`.

**Stage 2 — Pre-infer the two hard fields (deterministic, before LLM).**
- **Status** by signal hierarchy (see mapping §Status). Resolved in Python so the LLM can't hallucinate "OPERATIONAL".
- **Priority**: `--priority` flag wins; else parent `CLAUDE.md` framing (`000-PRIORITY-*` / "LAUNCH READY" → CRITICAL/HIGH; live payment rails → HIGH; research/internal → MEDIUM; `_archive/` → LOW).
- Compute folder name: `000-PRIORITY-{NAME}/` if Priority ∈ {CRITICAL,HIGH} else `{kebab-case}/`. NAME from H1 minus `CLAUDE.md — ` prefix, else repo basename.

**Stage 3 — LLM fill.** Pass `RepoFacts` JSON + the pre-inferred Status/Priority/Last-Updated/folder-name + the verbatim `LAIR_TEMPLATE.md` skeleton to the converter prompt (b). The model returns ONE markdown document.

**Stage 4 — Validate + repair (deterministic lint, no LLM).** Reject/auto-fix:
- First 6 lines must be `# Title`, `**Status**`, `**Priority**`, `**Last Updated**`, `**Mission**`.
- Status ∈ {PLANNING,ACTIVE,OPERATIONAL,BLOCKED,DORMANT}; Priority ∈ {CRITICAL,HIGH,MEDIUM,LOW}; date matches `\d{4}-\d{2}-\d{2}`.
- `## Current State` table present with `Blocked by:` + `Next action:` lines.
- `## Related Lairs` section present and non-empty (governance: every lair MUST end with it). If empty, inject parent-repo + sibling rows from Cross-Workspace table.
- **Line cap**: ≤500 lines. If over, truncate longest inventory tables to top-15 + `(see repo CLAUDE.md for full list)` pointer; push any inline session log to `sessions/`.
- Mission ≤1 sentence.

**Stage 5 — Write.** `mkdir -p <dest>/<folder>/`; write `LAIR.md`. Print a checklist of the manual follow-ups governance still requires: add back-references in the named Related Lairs, add row to `START_HERE.md` Active Lairs + the index (these touch files outside the repo, so the command surfaces them rather than silently editing).

**Stage 6 — Re-sync (mandatory).** Unless `--no-embed`:
```bash
lbrain import <dest> && lbrain embed --stale
```
Run programmatically (reuse the existing `import`/`embed` code paths). `--dry-run` stops after Stage 4 and prints the doc to stdout.

---

## (b) The converter LLM prompt (copy-pasteable)

System + user template. Substitute `{{...}}`.

```
SYSTEM:
You are a Lair Compiler. You convert a software repository's documentation into ONE
governance-conformant LAIR.md file. Output ONLY the markdown document — no code fences
around the whole thing, no preamble, no commentary. Obey every rule below; a deterministic
linter will reject violations.

HARD RULES (LAIR_RULES.md, authoritative):
- Follow the supplied TEMPLATE skeleton exactly: section names, order, and table headers.
  Order: Title → Status/Priority/Last Updated/Mission header → Current State → Architecture
  (+ Key Files) → Decisions Log → Implementation Checklist → Related Lairs → optional motto.
- Front-load: the first 6 lines are the H1 title then the four bold header fields. Status,
  mission, and current state must be graspable in the first 30 lines.
- Tables over prose. Key-value pairs over paragraphs. NEVER paste source code; reference
  files as `path/to/file.py:line`. Code blocks only for load-bearing commands/patterns.
- One concern only. This lair describes EXACTLY ONE system (this repo). Do not pull in
  unrelated sibling projects except as Related Lairs cross-reference rows.
- ≤500 lines total. If an inventory is long, keep the top ~15 rows and add
  "(see repo CLAUDE.md for full list)".
- Decisions Log entries are ONE line each, format: `- **YYYY-MM-DD**: DECISION: X over Y because Z`.
- The document MUST end with a non-empty `## Related Lairs` table (`| Lair | Relationship |`).
- Do NOT invent facts. Use only the supplied RepoFacts. If a required field has no signal,
  write the most defensible inference and nothing speculative. Leave optional sections out
  rather than padding.

FIXED FIELDS (already resolved deterministically — copy verbatim, do not re-derive):
- Title: {{TITLE}}
- Status: {{STATUS}}
- Priority: {{PRIORITY}}
- Last Updated: {{LAST_COMMIT_DATE}}

FIELD-DERIVATION GUIDE:
- Mission: one sentence. Prefer the `## Mission` blockquote; else `**What this is**` line;
  else the README tagline. Collapse to a single sentence.
- Current State table (| Aspect | Status | Notes |): one row per agent/tool/subsystem found
  in the source status tables or annotated directory tree. Normalize cell Status to
  Working / Broken / Blocked: Deployed|OPERATIONAL|TESTED|COMPLETE→Working;
  needs-deploy|COLD|min=0|WIP→Blocked; broken|regression|403|timeout→Broken.
  Then `**Blocked by**:` (from blocker tokens, or "Nothing") and
  `**Next action**:` (top unchecked checklist item / "needs deploy" note / most obvious deploy step).
- Architecture: copy any ## Architecture ASCII diagram verbatim; else synthesize a brief
  flow from the directory structure + pipeline/value-chain description. Add a
  `### Key Commands` subsection holding the build/test/deploy bash commands (keep their
  inline # comments; drop one-off exploratory commands).
- ### Key Files table (| File | Purpose |): from annotated tree lines and `path:line` refs and
  the Working Solutions Index (Location→File, Problem+Solution→Purpose). Cap ~10-15 load-bearing
  entry points.
- Decisions Log: from "Critical Patterns / DO NOT BREAK / Sacred Files" and any "X over Y
  because Z" prose. Date with the commit date if none given.
- Implementation Checklist: from unchecked items, "needs deploy/polish" notes, numbered
  Deploy Checklist steps, and gaps implied when Status < OPERATIONAL.
- Related Lairs: from "Cross-Workspace References", "Sibling product", "Parent company" lines,
  and README Links. Resolve to an existing lair folder path where one exists, else the repo path.
- If the source has Live URLs / contract addresses / pricing, append a trailing
  `## Live Contracts & URLs` table (the template tolerates optional trailing sections);
  tag each row Type = domain|contract-address(chain)|cloud-run-url|mcp-endpoint|dataset|doi.
- Optional closing line: copy the italic footer/motto verbatim if present.

CONFLICT RULE: if two sources give different values for the same datum (e.g. pricing in
CLAUDE.md vs README), prefer the public-facing README value and note the discrepancy in the
relevant Notes cell.

TEMPLATE (fill this skeleton exactly):
{{LAIR_TEMPLATE_MD_VERBATIM}}

USER:
RepoFacts JSON for the repository at {{REPO_PATH}}:
{{REPOFACTS_JSON}}

Produce the filled LAIR.md now.
```

---

## (c) Source-signal → lair-field mapping

| Lair field | Primary source signal | Fallback | Transform |
|---|---|---|---|
| **Title** (`# `) | README H1 / CLAUDE.md H1 minus `CLAUDE.md — ` prefix | repo dir basename, kebab-cased | drives folder name |
| **Folder name** | Priority + Title | — | `000-PRIORITY-{NAME}/` if CRITICAL/HIGH else `{kebab-case}/` |
| **Status** | live-URL-resolves + deploy artifact + "production" badges → OPERATIONAL; mixed status table → ACTIVE; blocker token → BLOCKED; stub/no-commits → PLANNING/DORMANT | DORMANT | deterministic, pre-LLM |
| **Priority** | parent CLAUDE.md `000-PRIORITY-*`/"LAUNCH READY" framing → CRITICAL/HIGH; live payment rails → HIGH; research → MEDIUM; `_archive/` → LOW | `--priority` flag overrides | deterministic, pre-LLM |
| **Last Updated** | `git log -1 --format=%cs` | converter run date | ISO `YYYY-MM-DD`; preserve any `Last audited:` in Notes |
| **Mission** | `## Mission` blockquote | `**What this is**` line / README tagline | collapse to 1 sentence |
| **Current State** rows | per-component status table / annotated tree state tags (`— DEPLOYED`) | synthesize from tool list | normalize → Working/Broken/Blocked |
| **Blocked by** | blocker tokens (`needs deploy`, `BLOCKED`, `COLD min=0`, Sacred-Files caveat) | "Nothing" | — |
| **Next action** | top unchecked checklist / "next ships" / deploy note | most obvious deploy/test step | — |
| **Architecture** | `## Architecture` ASCII diagram | directory structure + value-chain prose | copy verbatim if present |
| **Key Commands** (Arch subsection) | `## Key Commands` / Quick Start bash blocks | — | keep inline `# comment`, drop exploratory |
| **Key Files** | annotated tree `# comment` lines, `path:line` refs, Working Solutions Index Location col | — | path→File, comment/Problem+Solution→Purpose; cap ~15 |
| **Decisions Log** | Critical Patterns / DO NOT BREAK / Sacred Files / "X over Y because Z" prose | — | one line, dated with commit date |
| **Implementation Checklist** | unchecked items, Deploy Checklist steps, Status<OPERATIONAL gaps | — | `- [ ]` |
| **Related Lairs** | Cross-Workspace References table, "Sibling product"/"Parent company", README Links | parent repo + org rows | resolve to existing lair folder path; **bidirectional follow-up flagged** |
| **Live Contracts & URLs** (trailing) | Live Contracts table, x402/Stripe addresses, MCP endpoints, Zenodo DOIs | — | tag Type per row |
| **Closing motto** | italic footer of CLAUDE.md/README | omit | verbatim |

---

## (d) Worked example — a sample repo → lair

Inputs: README H1 `# ExampleHub`, blockquote `## Mission`, live `/mcp` + `/.well-known/*` endpoints (resolve), `firebase.json` + `server.py` present, MCP tool + pricing tables, footer motto. Parent CLAUDE.md lists it as sibling product, revenue-bearing (x402). `git log -1 --format=%cs` → `2026-05-29`.

Pre-inferred: Status=OPERATIONAL (live endpoints + deploy artifacts); Priority=HIGH (revenue-bearing sibling product); folder=`000-PRIORITY-EXAMPLEHUB/`.

```markdown
# ExampleHub

**Status**: OPERATIONAL
**Priority**: HIGH
**Last Updated**: 2026-05-29
**Mission**: An MCP service that exposes a paid tool surface to AI agents.

---

## Current State

| Aspect | Status | Notes |
|--------|--------|-------|
| MCP server (`/mcp`) | Working | Live, production; `server.py` entry point |
| Discovery endpoints | Working | `/.well-known/mcp.json`, `/.well-known/agent.json`, `/llms.txt` resolve |
| Payment layer (x402) | Working | Per-call pricing; Base L2 settlement |

**Blocked by**: Nothing
**Next action**: Confirm pricing discrepancy (README vs CLAUDE.md) and align docs.

...
```

Then: `lbrain import <lairs> && lbrain embed --stale`, and the command prints follow-ups: add back-ref rows in the sibling/parent lairs, and add the new lair to `START_HERE.md` + the index.

---

## Implementation notes (for the engineer)

- Add as `@main.command(name="lair")` group with a `from-repo` subcommand, or a flat `@main.command(name="lair-from-repo")`, in `lbrain/cli.py` (Click group). Logic in new `lbrain/lair_from_repo.py`.
- Reuse `EmbedClient`/`make_embedder` and the existing `import`/`embed` command bodies for Stage 6 rather than shelling out.
- Stages 1, 2, 4 are pure Python (deterministic); only Stage 3 calls the model — this keeps Status/Priority/line-cap conformance out of the LLM's hands, which is where hallucination risk lives.
- Read the template at runtime from the configured lairs dir (do not hardcode) so the protocol tracks governance edits; the linter reads enums/cap from `LAIR_RULES.md`.

*Designed by an 8-agent LBrain workflow sweep over a real lair corpus, 2026-05-31.*
