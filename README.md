# LBrain by Metavolve Labs

**AI-native engineering memory with the Lair Protocol.**

LBrain indexes structured markdown lairs and memory files, gives you fast hybrid (semantic + keyword) search, and surfaces *Cognitive Nutrition* context to AI agents so they prefer the right substrate, the right schema, the right pattern — out of the box.

## Why

Existing "RAG" tools index text and forget structure. The lair protocol *is* structure — priority hierarchies, wikilink graphs, frontmatter types, governance cadence. LBrain reads those signals and treats them as first-class retrieval inputs. The protocol is the product; the search engine just respects it.

## What it does

- **Hybrid retrieval** — BM25 (SQLite FTS5) + cosine (sqlite-vec) + wikilink graph boost + priority-folder boost + frontmatter-type filter.
- **Cognitive Nutrition preambles** — substrate-aligned context injected ahead of search results so agents adopt the right defaults without being reminded.
- **Lair Protocol check** — `should_commit_to_lair(text)` decides what's worth saving so you don't have to think about it.
- **Anti-pattern detection** — cross-checks proposed actions against your saved `feedback_*.md` rules.
- **Onboarding flow** — three-minute questionnaire scaffolds CLAUDE.md + starter priority lairs.
- **MCP server** — direct integration with Claude Code (`claude mcp add -s user lbrain -- /path/to/lbrain-mcp`).

## Stack

- Python 3.10+
- SQLite + sqlite-vec + FTS5 (native, no WASM, no daemon)
- OpenAI text-embedding-3-small (~$0.12 per 6M-token corpus; pennies on updates)
- `fastmcp` for MCP server
- ~700 LOC. No moving parts.

## Install

```bash
cd lbrain
pip install -e .

# Initialize config + DB
lbrain init --api-key=$OPENAI_API_KEY \
            --source=/path/to/your/lairs \
            --source=/path/to/your/memory

# Walk + ingest
lbrain import

# Embed
lbrain embed --stale
```

## Use

```bash
# Hybrid semantic search
lbrain query "how do we sign C2PA"

# Filter by frontmatter type
lbrain query "code style" --type feedback

# Priority lairs only
lbrain query "current quarter goals" --priority

# Pure keyword (no embedding call, sub-50ms)
lbrain search "snake_case lock"

# "Should I save this?"
lbrain commit-check "user said: don't auto-format imports in this repo"

# "Does this action conflict with anything I've been told?"
lbrain check-action "going to mock the database for these tests"

# Brain stats
lbrain stats
```

## Onboard a new project

```bash
lbrain onboard ~/repos/new-project
```

Three minutes of opinionated questions → working CLAUDE.md + three priority lairs + LAIR_RULES.md.

## Register MCP with Claude Code

```bash
chmod +x /path/to/repos/lbrain/scripts/lbrain-mcp
claude mcp add -s user lbrain -- /path/to/repos/lbrain/scripts/lbrain-mcp
```

Tools surfaced: `lair_query`, `lair_search`, `lair_protocol_check`, `lair_check_action`, `lair_stats`.

## Containerized deployment — for autonomous agents

For agents running in containers / Kubernetes / outside Claude Code, run LBrain as an HTTP MCP service:

```bash
# Local (no container):
lbrain mcp --transport streamable-http --host 0.0.0.0 --port 7370

# Docker:
docker build -t lbrain .
docker run --rm -p 7370:7370 -v $(pwd)/brain-data:/data \
  -e OPENAI_API_KEY=$OPENAI_API_KEY lbrain

# docker-compose (Kite Apprentice / Maestro pattern):
docker compose -f docker-compose.kite.yml up
```

The agent's MCP client connects to `http://lbrain:7370/mcp` and gets the same 5 tools. Use this for:

- **Cognitive Nutrition for autonomous loops** — agent calls `lair_query` at decision points, automatic substrate priming on schema/lair/deployment topics.
- **Anti-pattern guarding** — agent calls `lair_check_action` before destructive actions; the saved `feedback_*.md` rules become an automatic safety net.
- **Lair-Protocol output capture** — agent calls `lair_protocol_check` on session outputs to decide what's save-worthy, eliminating the "remind me to remember this" round trip with the user.

### Two-brain pattern (recommended for production agents)

1. **Shared brain** (read-only mount of your full corpus) — global Cognitive Nutrition + anti-pattern coverage.
2. **Task-specific brain** (per-agent, curated subset) — focused, cheaper, faster. E.g. a Buildathon agent gets only the relevant submission/spec/integration lairs.

Both can run in the same container with different brain.db files via `LBRAIN_HOME` switching, or as separate containers the agent queries in parallel.

See `docker-compose.kite.yml` for a full Apprentice + LBrain wiring example.

## Architecture

```
lbrain/
├── index.py          File walker + frontmatter + chunker + wikilink extractor
├── embed.py          OpenAI embeddings client (batched, stateless)
├── store.py          SQLite + sqlite-vec + FTS5 storage layer
├── search.py         Hybrid BM25 + cosine + graph boost reranker
├── lair_protocol.py  Cognitive Nutrition primers + commit-check + anti-pattern
├── onboard.py        Interactive scaffolding for new projects
├── mcp_server.py     fastmcp tool surface
├── cli.py            click CLI entry point
└── config.py         ~/.lbrain/config.toml
```

## Truth hierarchy

Source files (markdown lairs and memory entries) are authoritative. The SQLite index is a derivative cache. If they disagree, trust the file and run `lbrain import && lbrain embed --stale`.

---

*Metavolve Labs, Inc. — Build the infrastructure of memory for the AI age.*
