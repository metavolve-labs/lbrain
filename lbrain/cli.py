"""LBrain CLI — fast, opinionated, no-ceremony."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from . import __version__
from .config import CONFIG_DIR, CONFIG_PATH, Config
from .embed import EmbedClient, make_embedder
from .index import chunk as chunk_doc
from .index import discover, parse
from .lair_protocol import (
    cognitive_nutrition_preamble,
    detect_anti_pattern,
    should_commit_to_lair,
)
from .onboard import run_onboarding
from .search import keyword_only, search
from .store import Store


@click.group()
@click.version_option(__version__, prog_name="lbrain")
def main():
    """LBrain by Metavolve Labs — AI-native engineering memory with the Lair Protocol."""


@main.command()
@click.option("--provider", type=click.Choice(["gemini", "openai"]), default="gemini",
              help="Embedding provider (default: gemini, GCP-native, no third-party lock-in)")
@click.option("--gemini-key", envvar="GEMINI_API_KEY", default=None, help="Gemini API key (provider=gemini)")
@click.option("--api-key", envvar="OPENAI_API_KEY", default=None, help="OpenAI API key (provider=openai)")
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Path(),
    help="Directory to index (repeatable)",
)
def init(provider: str, gemini_key: str, api_key: str, sources: tuple[str, ...]):
    """Initialize LBrain config + DB (Gemini-native by default).

    Out-of-the-box: `lbrain init --gemini-key <KEY> --source ./docs --source ./notes`
    The key is written to ~/.lbrain/env (chmod 600), never to plaintext config.
    """
    cfg = Config.load()
    cfg.embedding_provider = provider
    if provider == "gemini":
        cfg.embedding_model = "gemini-embedding-001"
        if gemini_key:
            cfg.gemini_api_key = gemini_key
    else:
        cfg.embedding_model = "text-embedding-3-small"
        if api_key:
            cfg.openai_api_key = api_key
    if sources:
        cfg.sources = [Path(s).expanduser().resolve() for s in sources]
    cfg.write()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    stats = store.stats()
    store.close()
    active_key = cfg.gemini_api_key if provider == "gemini" else cfg.openai_api_key
    click.secho(f"✓ LBrain initialized at {CONFIG_DIR}", fg="green")
    click.echo(f"  provider: {provider} ({cfg.embedding_model}, {cfg.embedding_dim}d)")
    click.echo(f"  config:   {CONFIG_PATH}")
    click.echo(f"  db:       {cfg.db_path}")
    click.echo(f"  sources:  {len(cfg.sources)} configured")
    click.echo(f"  docs:     {stats['docs']}")
    if not active_key:
        click.secho(f"  ⚠️  No {provider} API key set — add it: lbrain init --{'gemini-key' if provider=='gemini' else 'api-key'} <KEY>", fg="yellow")
    if not cfg.sources:
        click.secho("  ⚠️  No sources yet — add one: lbrain add-source <dir>, then `lbrain import && lbrain embed --stale`", fg="yellow")
    else:
        click.echo("  next: lbrain import && lbrain embed --stale")


@main.command(name="add-source")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
def add_source(path: str):
    """Add a directory to the indexed-sources list."""
    cfg = Config.load()
    p = Path(path).expanduser().resolve()
    if p not in cfg.sources:
        cfg.sources.append(p)
        cfg.write()
        click.secho(f"✓ Added source: {p}", fg="green")
    else:
        click.echo(f"  Already a source: {p}")


@main.command(name="import")
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option("--prune/--no-prune", default=True, help="Drop docs no longer on disk")
def import_cmd(paths: tuple[str, ...], prune: bool):
    """Walk source directories and ingest markdown into the brain."""
    cfg = Config.load()
    sources = [Path(p).expanduser().resolve() for p in paths] if paths else cfg.sources
    if not sources:
        click.secho("✗ No sources configured. Run `lbrain init --source <dir>`.", fg="red")
        sys.exit(1)

    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    t0 = time.time()
    new_docs = 0
    updated_docs = 0
    unchanged_docs = 0
    total_chunks = 0

    for src in sources:
        files = discover([src])
        click.echo(f"  scanning {src} → {len(files)} markdown files")
        with store.transaction():
            for path in files:
                doc = parse(path, repo_root=src)
                existing_hash = store.get_doc_hash(doc.rel_path)
                if existing_hash == doc.doc_hash:
                    unchanged_docs += 1
                    continue
                if existing_hash is None:
                    new_docs += 1
                else:
                    updated_docs += 1
                    store.delete_doc_chunks(doc.rel_path)
                store.upsert_doc(doc)
                store.replace_wikilinks(doc)
                chunks = chunk_doc(
                    doc,
                    max_tokens=cfg.chunk_tokens,
                    overlap=cfg.chunk_overlap,
                    contextualize=cfg.contextual_prefix,
                )
                store.insert_chunks(chunks)
                total_chunks += len(chunks)

    pruned: list[str] = []
    if prune:
        with store.transaction():
            pruned = store.prune_missing()

    stats = store.stats()
    store.close()
    dt = time.time() - t0
    click.secho(
        f"✓ Imported in {dt:.1f}s — new: {new_docs}, updated: {updated_docs}, "
        f"unchanged: {unchanged_docs}, chunks: {total_chunks}, pruned: {len(pruned)}",
        fg="green",
    )
    if pruned:
        for rel in pruned[:10]:
            click.echo(f"    pruned (file gone): {rel}")
        if len(pruned) > 10:
            click.echo(f"    … +{len(pruned) - 10} more")
    click.echo(
        f"  brain stats — docs: {stats['docs']}, chunks: {stats['chunks']}, "
        f"embedded: {stats['embedded']}, wikilinks: {stats['wikilinks']}"
    )


@main.command()
@click.option("--stale/--all", default=True, help="Only embed un-embedded chunks (default)")
@click.option("--batch", default=96, type=int, help="Embedding batch size")
def embed(stale: bool, batch: int):
    """Generate embeddings for chunks (re-embeds stale or all)."""
    cfg = Config.load()
    _active_key = cfg.gemini_api_key if cfg.embedding_provider == "gemini" else cfg.openai_api_key
    if not _active_key:
        click.secho(
            f"✗ No API key for embedding_provider='{cfg.embedding_provider}'. "
            "Add it to ~/.lbrain/env or run `lbrain init --api-key=...`.",
            fg="red",
        )
        sys.exit(1)

    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)

    if stale:
        pending = store.stale_chunks()
    else:
        rows = store.db.execute(
            f"SELECT chunk_id, {store.EMBED_TEXT_SQL} AS etext FROM chunks ORDER BY chunk_id"
        ).fetchall()
        pending = [(r["chunk_id"], r["etext"]) for r in rows]

    if not pending:
        click.echo("  Nothing to embed.")
        store.close()
        return

    click.echo(f"  embedding {len(pending)} chunks (batch {batch}, model {cfg.embedding_model})…")
    t0 = time.time()
    with store.transaction():
        for i in range(0, len(pending), batch):
            chunk_batch = pending[i : i + batch]
            ids = [c[0] for c in chunk_batch]
            texts = [c[1] for c in chunk_batch]
            blobs = embedder.embed(texts, batch_size=batch)
            store.write_embeddings(ids, blobs)
            click.echo(f"    {min(i + batch, len(pending))}/{len(pending)} done")
    embedder.close()
    store.close()
    dt = time.time() - t0
    click.secho(f"✓ Embedded {len(pending)} chunks in {dt:.1f}s", fg="green")


@main.command()
@click.argument("query")
@click.option("-k", default=10, type=int, help="Number of results")
@click.option(
    "--type",
    "doc_type",
    default=None,
    help="Filter by frontmatter type (user/feedback/project/reference)",
)
@click.option("--priority", is_flag=True, help="Only priority lairs")
@click.option("--no-prime", is_flag=True, help="Suppress Cognitive Nutrition preamble")
def query(query: str, k: int, doc_type: str | None, priority: bool, no_prime: bool):
    """Semantic + keyword hybrid search across the brain."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)

    t0 = time.time()
    hits = search(cfg, store, embedder, query, k=k, doc_type=doc_type, priority_only=priority)
    dt_ms = (time.time() - t0) * 1000

    if not no_prime:
        preamble = cognitive_nutrition_preamble(query, hits)
        if preamble:
            click.echo(preamble)

    click.secho(f"--- {len(hits)} hits ({dt_ms:.0f} ms) ---\n", fg="cyan")
    for i, h in enumerate(hits, 1):
        prefix = "★" if h.is_priority else " "
        click.secho(
            f"{prefix} [{i}] {h.title}  ({h.score:.3f})", fg="yellow"
        )
        click.echo(f"   {h.rel_path} :: chunk {h.chunk_idx}")
        if h.doc_type:
            click.echo(f"   type={h.doc_type}  v={h.vector_score:.2f}  kw={h.keyword_score:.2f}  boosts={h.boosts}")
        text_preview = h.text.strip().replace("\n", " ")[:240]
        click.echo(f"   {text_preview}\n")

    embedder.close()
    store.close()


@main.command()
@click.argument("query")
@click.option("-k", default=10, type=int)
def search_cmd(query: str, k: int):
    """Exact-keyword search (FTS5 only, no embeddings, no API call)."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    t0 = time.time()
    hits = keyword_only(store, query, k=k)
    dt_ms = (time.time() - t0) * 1000
    click.secho(f"--- {len(hits)} keyword hits ({dt_ms:.0f} ms) ---\n", fg="cyan")
    for i, h in enumerate(hits, 1):
        click.secho(f"  [{i}] {h.title}", fg="yellow")
        click.echo(f"   {h.rel_path} :: chunk {h.chunk_idx}")
        click.echo(f"   {h.text.strip().replace(chr(10), ' ')[:240]}\n")
    store.close()


main.add_command(search_cmd, name="search")


@main.command()
def stats():
    """Show brain statistics."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    s = store.stats()
    store.close()
    click.echo(f"docs:           {s['docs']}")
    click.echo(f"chunks:         {s['chunks']}")
    click.echo(f"embedded:       {s['embedded']}")
    click.echo(f"  → coverage:   {s['embedded'] / max(s['chunks'], 1) * 100:.1f}%")
    click.echo(f"priority docs:  {s['priority_docs']}")
    click.echo(f"wikilinks:      {s['wikilinks']}")


@main.command()
@click.option("--threshold", default=None, type=float, help="Cosine distance cap (default: per provider)")
@click.option("--min-size", default=4, type=int, help="Min chunks to form a cluster")
@click.option("--max", "max_clusters", default=20, type=int, help="Cap clusters consolidated")
@click.option("--model", default=None, help="Synthesis chat model (default: per provider)")
def consolidate(threshold: float, min_size: int, max_clusters: int, model: str):
    """Consolidate related chunks into dense summary memories (the neocortical layer).

    Clusters chunks over their existing vectors and synthesizes one dense,
    provenance-linked summary per cluster. Regenerable; never touches source.
    """
    cfg = Config.load()
    _active_key = cfg.gemini_api_key if cfg.embedding_provider == "gemini" else cfg.openai_api_key
    if not _active_key:
        click.secho(
            f"✗ No API key for embedding_provider='{cfg.embedding_provider}'.", fg="red"
        )
        sys.exit(1)
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)
    from .consolidate import consolidate as run_consolidation

    t0 = time.time()
    n = run_consolidation(
        cfg, store, embedder, synth_model=model,
        distance_threshold=threshold, min_size=min_size, max_clusters=max_clusters,
        log=click.echo,
    )
    embedder.close()
    store.close()
    click.secho(f"✓ Consolidated {n} dense summary memories in {time.time() - t0:.1f}s", fg="green")


@main.command()
def summaries():
    """List the dense summary memories in the consolidation layer."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    rows = store.list_summaries()
    store.close()
    if not rows:
        click.echo("  No summaries yet. Run `lbrain consolidate`.")
        return
    click.secho(f"--- {len(rows)} dense summary memories ---", fg="cyan")
    for r in rows:
        import json

        paths = json.loads(r["source_paths"])
        click.secho(f"  [{r['summary_id']}] {r['title']}", fg="yellow")
        click.echo(f"     {r['n_sources']} source docs · {r['len']} chars")
        click.echo(f"     from: {', '.join(p.rsplit('/', 1)[-1] for p in paths[:6])}"
                   + (" …" if len(paths) > 6 else ""))


@main.command(name="commit-check")
@click.argument("text", required=False)
@click.option("--file", "from_file", type=click.Path(exists=True), help="Read text from file")
def commit_check(text: str | None, from_file: str | None):
    """Check whether a piece of text should be committed to a lair/memory entry."""
    if from_file:
        text = Path(from_file).read_text()
    if not text:
        click.echo("Paste text and press Ctrl-D:")
        text = sys.stdin.read()

    sug = should_commit_to_lair(text)
    click.echo(f"should_commit:   {sug.should_commit}")
    click.echo(f"confidence:      {sug.confidence:.2f}")
    click.echo(f"suggested_type:  {sug.suggested_type}")
    click.echo(f"suggested_slug:  {sug.suggested_slug}")
    click.echo(f"reasoning:       {sug.reasoning}")


@main.command(name="check-action")
@click.argument("action_text")
@click.option("-k", default=8, type=int, help="Feedback hits to cross-check against")
def check_action(action_text: str, k: int):
    """Cross-check a proposed action against saved feedback rules (anti-pattern detector)."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)
    hits = search(cfg, store, embedder, action_text, k=k, doc_type="feedback")
    warnings = detect_anti_pattern(action_text, hits)
    if not warnings:
        click.secho("✓ No conflicts with saved feedback rules.", fg="green")
    else:
        click.secho(f"⚠️  {len(warnings)} potential conflict(s) detected:", fg="yellow")
        for w in warnings:
            click.echo(f"  {w}")
    embedder.close()
    store.close()


@main.command()
@click.argument("target_dir", type=click.Path())
def onboard(target_dir: str):
    """Interactive onboarding — scaffolds CLAUDE.md + starter lairs."""
    run_onboarding(Path(target_dir).expanduser().resolve())


@main.command()
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "sse", "streamable-http"]),
    help="MCP transport. stdio for Claude Code, streamable-http for remote/container agents.",
)
@click.option("--host", default="127.0.0.1", help="Bind host for HTTP transports.")
@click.option("--port", default=7370, type=int, help="Bind port for HTTP transports.")
def mcp(transport: str, host: str, port: int):
    """Start the MCP server.

    \b
    stdio              — for Claude Code subprocess (default, spawned via mcp-launcher).
    streamable-http    — for remote autonomous agents (containerized deployments).
    sse                — legacy SSE transport.
    """
    from .mcp_server import serve

    serve(transport=transport, host=host, port=port)


@main.command(name="lair-from-repo")
@click.argument("repo_path", type=click.Path(exists=True))
@click.option("--dest", default=None, help="Lairs dir (default: first configured source)")
@click.option("--name", default=None, help="Override the lair title")
@click.option("--priority", type=click.Choice(["CRITICAL", "HIGH", "MEDIUM", "LOW"], case_sensitive=False), default=None)
@click.option("--model", default="gemini-2.5-flash", help="Gemini model for the fill")
@click.option("--dry-run", is_flag=True, help="Print the lair to stdout; write nothing")
@click.option("--no-embed", is_flag=True, help="Skip the index re-sync after writing")
def lair_from_repo_cmd(repo_path, dest, name, priority, model, dry_run, no_embed):
    """Convert a code repo + its README/CLAUDE.md into a filled LAIR.md.

    Deterministic harvest + Python-resolved Status/Priority + Gemini fill + linter.
    """
    from .lair_from_repo import run_from_repo
    run_from_repo(repo_path, dest, name, priority, model, dry_run, no_embed, echo=click.echo)


@main.command()
@click.argument("text", required=False)
@click.option("--from-file", type=click.Path(exists=True), default=None, help="Read the work text from a file")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (for the calling agent)")
def suggest(text, from_file, as_json):
    """Subtle prompt: should recent work be recorded? Suggests CREATE vs AMEND.

    Built for an in-terminal agent to call at a natural breakpoint (end of a task /
    session), then *offer* the result to the user. It NEVER writes — it only suggests,
    so the human stays in the loop. Discipline-builder: the more you say yes, the
    sharper your memory gets.
    """
    import json as _json
    import re as _re
    if from_file:
        text = Path(from_file).read_text(encoding="utf-8", errors="replace")
    elif not text:
        text = "" if sys.stdin.isatty() else sys.stdin.read()
    text = (text or "").strip()
    if not text:
        click.echo("  (no input text — pass TEXT, --from-file, or pipe via stdin)")
        return
    s = should_commit_to_lair(text)
    out = {"should_commit": s.should_commit, "confidence": round(s.confidence, 2),
           "type": s.suggested_type, "slug": s.suggested_slug, "reasoning": s.reasoning,
           "action": None, "target": None}
    if s.should_commit:
        cfg = Config.load()
        store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
        hits = []
        try:
            hits = search(cfg, store, make_embedder(cfg), text, k=3)
        except Exception:
            pass
        finally:
            store.close()
        slug_words = set(_re.findall(r"[a-z0-9]{4,}", s.suggested_slug.lower()))
        amend = None
        for h in hits:
            hay = set(_re.findall(r"[a-z0-9]{4,}", (h.rel_path + " " + (h.title or "")).lower()))
            if slug_words and len(slug_words & hay) >= 2:
                amend = h.rel_path
                break
        if amend:
            out["action"], out["target"] = "amend", amend
        else:
            out["action"], out["target"] = "create", f"{s.suggested_type}-{s.suggested_slug}.md"
    if as_json:
        click.echo(_json.dumps(out))
        return
    if not s.should_commit:
        click.echo(f"  ⬜ Probably not worth recording (confidence {s.confidence:.2f}). {s.reasoning}")
        return
    click.secho(f"  💡 Worth remembering — {s.suggested_type}, confidence {s.confidence:.2f}.", fg="cyan")
    click.echo(f"     {s.reasoning}")
    if out["action"] == "amend":
        click.echo(f"     Looks like it belongs in an existing note: {out['target']}")
        click.echo(f"     → Ask the user; on yes, append the note to that file, then `lbrain import && lbrain embed --stale`.")
    else:
        click.echo(f"     Suggest a new {s.suggested_type} memory: {out['target']}")
        click.echo(f"     → Ask the user; on yes: lbrain remember \"<the fact>\" --write")


@main.command()
@click.argument("text")
@click.option("--write", is_flag=True, help="Write a memory stub if worth committing")
def remember(text: str, write: bool):
    """Quick-capture: should this be remembered? (runs the commit-check primitive).

    With --write, drafts a memory/<slug>.md stub in the memory source dir.
    """
    s = should_commit_to_lair(text)
    mark = "✅ commit" if s.should_commit else "⬜ skip"
    click.echo(f"  {mark}  confidence={s.confidence:.2f}  type={s.suggested_type}  slug={s.suggested_slug}")
    click.echo(f"  reasoning: {s.reasoning}")
    if not write:
        return
    if not s.should_commit:
        click.secho("  not committing (should_commit=false); use the lair editor manually if you disagree.", fg="yellow")
        return
    cfg = Config.load()
    mem_dir = next((p for p in cfg.sources if "memory" in str(p).lower()), cfg.sources[0] if cfg.sources else Path.cwd())
    import datetime as _dt
    out = Path(mem_dir) / f"{s.suggested_type}-{s.suggested_slug}-{_dt.date.today().isoformat()}.md"
    body = (
        f"---\nname: {s.suggested_slug}\n"
        f"description: {text[:120].strip()}\n"
        f"metadata:\n  type: {s.suggested_type}\n  sfmp:\n    generated: true\n"
        f"    confidence: {s.confidence:.2f}\n    trigger: explicit\n---\n\n{text.strip()}\n"
    )
    out.write_text(body, encoding="utf-8")
    click.echo(f"  ✍️  wrote {out}  (run `lbrain import {mem_dir} && lbrain embed --stale` to index)")


if __name__ == "__main__":
    main()
