# LBrain — setup & usage for an in-terminal coding agent

You (the AI coding assistant in this terminal) are the intended operator of LBrain. This file tells you how to stand it up cleanly for your human and how to use it so their **memory system compounds over time**. The whole point: *it takes a little discipline, and the more it's used, the sharper it gets.*

LBrain is a local, private, Gemini-native semantic memory: it indexes a folder of markdown **lairs** (structured per-project context docs) + a rolling **memory** journal, and serves hybrid semantic+keyword recall over them. Data stays on the user's machine; only embedding calls leave, to the user's own Gemini key.

---

## 1. Install & initialize (out of the box)

```bash
pip install -e .                      # or: pip install lbrain
lbrain init --gemini-key <GEMINI_API_KEY> \
            --source ./docs --source ./notes      # any markdown dirs you want remembered
lbrain import && lbrain embed --stale             # build the index
lbrain stats                                      # confirm: docs / 100% coverage
```

- **Gemini-native by default.** Get the user a key from Google AI Studio; it's written to `~/.lbrain/env` (chmod 600), never to plaintext config. (`--provider openai --api-key …` is supported but not the default.)
- If the user has no lairs yet, **seed from their repos** (see §3) — don't make them write docs by hand.

**Wire the MCP server** (so you can query memory mid-conversation) — register `lbrain mcp` as an MCP stdio server in the host (Claude Code, etc.). Then prefer the MCP tools `lair_query` / `lair_search` before grep/Read when asking "have we decided/discussed/built this before?"

---

## 2. The daily loop (your discipline, on the user's behalf)

1. **Query first.** Before re-deriving context, run `lbrain query "<the task>"` (or the MCP tool). Stops the 30-minute context rebuild.
2. **Re-sync after edits.** The source `.md` is authoritative; the index is a derivative cache that does NOT auto-detect edits. After any lair/memory change: `lbrain import <dir> && lbrain embed --stale`.
3. **Offer to remember — the subtle prompt (this is the habit that makes it work).** At natural breakpoints — a decision locked, a bug fixed, a task finished, end of session — run:
   ```bash
   lbrain suggest "<a 1-3 sentence summary of what just happened>"   # add --json to parse
   ```
   It returns whether the work is worth recording, and whether to **create** a new note or **amend** an existing one. **If it says yes, gently ask the user** — e.g.:
   > 💡 "Want me to record that Gemini-vs-OpenAI decision in your memory? (I'd add it to `project-embeddings.md`.)"
   On **yes**:
   - create → `lbrain remember "<the fact>" --write`
   - amend → append the note to the suggested file, then re-sync.
   **Never write to memory without asking.** The suggestion is a nudge, not an action. Keep it light — one line, easy to decline.

---

## 3. Seed memory from existing repos (don't start empty)

Turn any repo + its README/CLAUDE.md into a filled lair automatically:

```bash
lbrain lair-from-repo /path/to/repo --dry-run     # preview the generated lair
lbrain lair-from-repo /path/to/repo               # write it to the lairs dir + re-index
```

Status/Priority are inferred deterministically (not by the model); you get a conformant `LAIR.md` with a current-state table, architecture, key files, and decisions. Run it across the user's active repos on day one to bootstrap a real memory base in minutes. **Lair creation is human-gated by design** — show the `--dry-run` first and let the user approve what enters their canon.

---

## 4. What good looks like (the lair discipline)

See `docs/lair-framework/` — the template + authoring rules. The essentials you should enforce when writing/editing lairs:
- **Front-load status** (Status / Priority / Last Updated / Mission in the first ~5 lines).
- **Tables over prose**; reference files as `path:line`, never paste code.
- **One concern per lair**; ≤500 lines (extract session logs to `sessions/`).
- **Append, never overwrite.** New work prepends/appends; supersede explicitly, don't delete. History grows forward.
- Cross-link related lairs (bidirectionally).

---

## 5. Command reference (the ones you'll use)

| Command | Use |
|---------|-----|
| `lbrain init --gemini-key K --source D` | one-time setup (Gemini-native) |
| `lbrain query "<question>"` | hybrid semantic recall (the default lookup) |
| `lbrain search "<keyword>"` | fast exact keyword (no API call) |
| `lbrain import <dir> && lbrain embed --stale` | re-sync after any edit |
| `lbrain suggest "<summary>" [--json]` | **the subtle prompt** — should this be remembered? |
| `lbrain remember "<fact>" --write` | capture a memory the user approved |
| `lbrain lair-from-repo <path> [--dry-run]` | seed a lair from a repo |
| `lbrain stats` | doc count + embedding coverage |
| `lbrain mcp` | start the MCP server for in-conversation queries |

---

*LBrain — your memory, your machine, your keys. Metavolve Labs, BSD-3-Clause.*
