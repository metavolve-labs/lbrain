"""LBrain CLI — fast, opinionated, no-ceremony."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from . import __version__
from . import amp
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
@click.option("--api-base", default=None, help="Override the Gemini base URL (point at a proxy / self-hosted gateway)")
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Path(),
    help="Directory to index (repeatable)",
)
def init(provider: str, gemini_key: str, api_key: str, api_base: str, sources: tuple[str, ...]):
    """Initialize LBrain config + DB (Gemini-native by default).

    Out-of-the-box: `lbrain init --gemini-key <KEY> --source ./docs --source ./notes`
    The key is written to ~/.lbrain/env (chmod 600), never to plaintext config.
    """
    cfg = Config.load()
    cfg.embedding_provider = provider
    if api_base:
        # Validate before assignment — direct attribute set bypasses __post_init__,
        # and `write()` would otherwise persist an unvalidated (possibly plaintext)
        # base URL that bricks every later command on reload.
        from .config import _validate_base_url

        cfg.gemini_base_url = _validate_base_url(api_base.rstrip("/"))
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
@click.option("--force-prune", is_flag=True, help="Override the prune safety guards (mount-gone / >50%)")
def import_cmd(paths: tuple[str, ...], prune: bool, force_prune: bool):
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
                # Supersession edges are resolved at search time, so keep them current
                # for every doc — even ones whose chunks are unchanged (a Supersedes
                # marker can be added/removed without re-chunking). With foreign_keys=ON
                # the supersessions FK (src_path → docs.rel_path) requires the docs row
                # to already exist, so this MUST run after the row is present: in the
                # unchanged branch the row exists already; otherwise after upsert_doc.
                if existing_hash == doc.doc_hash:
                    unchanged_docs += 1
                    store.replace_supersessions(doc)
                    continue
                if existing_hash is None:
                    new_docs += 1
                else:
                    updated_docs += 1
                    store.delete_doc_chunks(doc.rel_path)
                store.upsert_doc(doc)
                store.replace_supersessions(doc)
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
        try:
            with store.transaction():
                pruned = store.prune_missing(source_roots=sources, force=force_prune)
        except RuntimeError as e:
            store.close()
            click.secho(f"✗ {e}", fg="red")
            sys.exit(1)

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

    # Guard against silently corrupting the vector space when the embedding config
    # changes. Use the embedder's RESOLVED model (make_embedder may rewrite a stale
    # default) so the fingerprint we check and stamp are identical.
    provider = cfg.embedding_provider
    model = getattr(embedder, "model", cfg.embedding_model)
    status = store.embedding_config_status(cfg.embedding_dim, model, provider)
    if status in ("dim_changed", "model_changed"):
        reason = "embedding_dim" if status == "dim_changed" else "embedding model/provider"
        if stale:
            click.secho(
                f"✗ {reason} changed since these vectors were built; the old vectors live "
                "in a different space and mixing them gives meaningless distances. "
                "Re-embed the whole corpus with `lbrain embed --all`.",
                fg="red",
            )
            store.close()
            sys.exit(1)
        # reset_vectors drops + recreates ALL three vec tables (chunks, summaries,
        # archives) and zeroes every embedded flag, so no stale old-model vectors
        # survive in any layer. `--all` then re-embeds chunks below; summaries and
        # archives are restored on the next `lbrain consolidate` / archive capture.
        click.secho(
            f"  {reason} changed — rebuilding all vector tables and re-embedding all chunks.\n"
            "    (summaries/archives invalidated; re-run `lbrain consolidate` / re-capture to restore them.)",
            fg="yellow",
        )
        store.reset_vectors(cfg.embedding_dim)

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
    # Stamp the fingerprint of the vectors now in the store (enables the
    # model/dim-change guard on the next run).
    store.stamp_embedding_config(cfg.embedding_dim, model, provider)
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
    if getattr(cfg, "amp_gating", True):
        ok, reason = amp.gate(query, getattr(cfg, "amp_min_chars", 12))
        if not ok:
            click.secho(f"[AMP gate] no memory injected — {reason}.", fg="yellow")
            return
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)

    t0 = time.time()
    hits = search(cfg, store, embedder, query, k=k, doc_type=doc_type, priority_only=priority)
    dt_ms = (time.time() - t0) * 1000
    kept, used = amp.budget(hits, getattr(cfg, "amp_budget_chars", 0), getattr(cfg, "amp_per_chunk_chars", 360))

    if not no_prime:
        preamble = cognitive_nutrition_preamble(query, kept)
        if preamble:
            click.echo(preamble)

    core = amp.core_block(getattr(cfg, "core_memory_path", ""), getattr(cfg, "core_memory_chars", 900))
    if core:
        click.secho(core, fg="green")

    label = f"{len(kept)} of {len(hits)} hits, AMP-budgeted" if len(kept) < len(hits) else f"{len(hits)} hits"
    click.secho(f"--- {label} ({dt_ms:.0f} ms) ---\n", fg="cyan")
    for i, h in enumerate(kept, 1):
        prefix = "★" if h.is_priority else " "
        click.secho(
            f"{prefix} [{i}] {h.title}  ({h.score:.3f})", fg="yellow"
        )
        click.echo(f"   {h.rel_path} :: chunk {h.chunk_idx}")
        if h.doc_type:
            click.echo(f"   type={h.doc_type}  v={h.vector_score:.2f}  kw={h.keyword_score:.2f}  boosts={h.boosts}")
        text_preview = h.text.strip().replace("\n", " ")[:getattr(cfg, "amp_per_chunk_chars", 360)]
        click.echo(f"   {text_preview}\n")

    if getattr(cfg, "amp_provenance", True):
        click.secho(amp.provenance(kept, len(hits), used, getattr(cfg, "amp_budget_chars", 0)), fg="cyan")
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
    click.echo(f"tier-2 archives:{s.get('archives', 0):>3}")


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


# ---------------------------------------------------------------------------
# Tier-2 — permanent, verifiable, encrypted archive (Arweave substrate)
# ---------------------------------------------------------------------------


def _resolve_passphrase(confirm: bool = False) -> str:
    """Archive passphrase from ~/.lbrain/env, else prompt (and offer to persist it)."""
    from .config import archive_passphrase, set_archive_passphrase

    pp = archive_passphrase()
    if pp:
        return pp
    pp = click.prompt("Archive passphrase", hide_input=True,
                      confirmation_prompt=confirm, default="", show_default=False)
    if not pp:
        click.secho("✗ An archive passphrase is required (it locks the encryption key).", fg="red")
        sys.exit(1)
    if confirm and click.confirm("Save passphrase to ~/.lbrain/env (chmod 600) for future commands?",
                                 default=True):
        set_archive_passphrase(pp)
    return pp


@main.command()
@click.argument("source", required=False)
@click.option("--from-file", type=click.Path(exists=True), default=None, help="Archive a file's contents")
@click.option("--title", default=None, help="Human label for the record (defaults to file/first line)")
@click.option("--namespace", default=None, help="Silo (default: config archive_namespace = 'private')")
@click.option("--snapshot-model", default=None, help="Override the snapshot LLM model")
def archive(source, from_file, title, namespace, snapshot_model):
    """Tier-2: encrypt a full session → permanent substrate → index its snapshot.

    Stores the WHOLE session (verifiable ground truth) on the archive transport and mirrors
    a structured-bullet snapshot into the index for cheap semantic recall. The encryption
    key is wrapped by your passphrase and stored locally only — destroy it with `lbrain
    shred` to crypto-shred the permanent record.
    """
    if from_file:
        payload = Path(from_file).read_bytes()
        default_title = Path(from_file).name
    elif source:
        payload = source.encode("utf-8")
        default_title = source.strip().splitlines()[0][:80] if source.strip() else "session"
    else:
        click.echo("Paste the session text and press Ctrl-D:")
        payload = sys.stdin.buffer.read()
        default_title = "session"
    if not payload:
        click.secho("✗ Nothing to archive.", fg="red")
        sys.exit(1)

    title = title or default_title
    passphrase = _resolve_passphrase(confirm=True)

    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = None
    active_key = cfg.gemini_api_key if cfg.embedding_provider == "gemini" else cfg.openai_api_key
    if active_key:
        try:
            embedder = make_embedder(cfg)
        except Exception:
            embedder = None
    from .archive import Archiver

    t0 = time.time()
    try:
        res = Archiver(cfg, store, embedder).archive(
            payload, title=title, passphrase=passphrase,
            namespace=namespace, snapshot_model=snapshot_model,
        )
    finally:
        if embedder:
            embedder.close()
        store.close()

    click.secho(f"✓ Archived '{res.title}' in {time.time() - t0:.1f}s", fg="green")
    click.echo(f"  txid:      {res.txid}")
    click.echo(f"  transport: {res.transport}   namespace: {res.namespace}")
    click.echo(f"  stored:    {res.n_bytes} bytes (full, encrypted)  →  snapshot {res.snapshot_chars} chars (indexed)")
    click.echo(f"  recall:    lbrain recall \"<query>\"   |   full: lbrain retrieve --txid {res.txid}")
    if not active_key:
        click.secho("  note: no embedding key — snapshot is keyword-searchable but not semantically indexed.", fg="yellow")


@main.command()
@click.option("--from-file", type=click.Path(exists=True), required=True, help="Session transcript to capture")
@click.option("--session-id", default=None, help="Stable session id (for title/tags)")
@click.option("--title", default=None, help="Human label (defaults to session id / filename)")
@click.option("--namespace", default=None, help="Silo (default: config archive_namespace)")
@click.option("--remote", is_flag=True, help="Push to the configured (Arweave) transport instead of the local store")
@click.option("--llm-snapshot", is_flag=True, help="Use the LLM for the snapshot (default: fast offline extractive)")
@click.option("--quiet", is_flag=True, help="Print one terse status line (for hook use)")
def capture(from_file, session_id, title, namespace, remote, llm_snapshot, quiet):
    """Auto-capture a session into Tier-2 — idempotent, local-by-default, non-interactive.

    Built for a SessionEnd/PreCompact hook: archives to the offline LOCAL store (free,
    no AR) and skips if this exact content is already captured, so it's safe to fire on
    every session end. Use --remote to push to Arweave deliberately. The passphrase is
    resolved non-interactively (env / GCP ref); if unavailable it exits 3 without prompting.
    """
    from .config import archive_passphrase
    from .archive import Archiver, LocalTransport

    payload = Path(from_file).read_bytes()
    if not payload:
        if not quiet:
            click.secho("✗ empty transcript — nothing to capture", fg="yellow")
        sys.exit(0)

    passphrase = archive_passphrase()
    if not passphrase:
        if not quiet:
            click.secho("✗ no archive passphrase available (set LBRAIN_ARCHIVE_PASSPHRASE) — skipping capture", fg="yellow")
        sys.exit(3)  # distinct code so the hook can tell "not configured" from a real error

    label = title or session_id or Path(from_file).stem
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = None
    active_key = cfg.gemini_api_key if cfg.embedding_provider == "gemini" else cfg.openai_api_key
    if active_key:
        try:
            embedder = make_embedder(cfg)
        except Exception:
            embedder = None
    # Force the local store unless --remote, so hook-driven capture never spends AR.
    transport = None if remote else LocalTransport(CONFIG_DIR / "archive")
    try:
        res = Archiver(cfg, store, embedder, transport=transport).archive(
            payload, title=label, passphrase=passphrase, namespace=namespace,
            extra_tags={"LBrain-SessionId": session_id} if session_id else None,
            skip_if_exists=True, force_extractive=not llm_snapshot,
        )
    finally:
        if embedder:
            embedder.close()
        store.close()

    if res.skipped:
        click.echo(f"· already captured: {label} ({res.txid[:16]}…)")
        return
    if quiet:
        click.echo(f"✓ captured {label} → {res.transport}:{res.txid[:16]}… ({res.n_bytes}B)")
    else:
        click.secho(f"✓ Captured '{res.title}' → {res.transport}", fg="green")
        click.echo(f"  txid {res.txid}  ·  {res.n_bytes} bytes  ·  snapshot {res.snapshot_chars} chars indexed")
        click.echo(f"  recall: lbrain recall \"<query>\"   ·   full: lbrain retrieve --txid {res.txid}")


@main.command()
@click.argument("query")
@click.option("-k", default=5, type=int, help="Number of archived records to surface")
@click.option("--namespace", default=None, help="Restrict to a silo")
def recall(query, k, namespace):
    """Deep-recall: semantic search over archived-session snapshots (the read surface)."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)
    try:
        q_vec = embedder.embed_one(query)
        rows = store.search_archives(q_vec, k=k, namespace=namespace)
    finally:
        embedder.close()
        store.close()
    if not rows:
        click.echo("  No archived records matched. (Archive sessions with `lbrain archive`.)")
        return
    click.secho(f"--- {len(rows)} archived record(s) ---\n", fg="cyan")
    for i, r in enumerate(rows, 1):
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(r["created"]).strftime("%Y-%m-%d") if r["created"] else "?"
        click.secho(f"  [{i}] {r['title']}  (dist {r['dist']:.3f})", fg="yellow")
        click.echo(f"      txid {r['txid']}  ·  {when}  ·  {r['namespace']}  ·  {r['n_bytes']} bytes")
        preview = r["snapshot"].strip().replace("\n", " ")[:300]
        click.echo(f"      {preview}\n")
    click.echo(f"  → full record: lbrain retrieve --txid <txid>")


@main.command()
@click.option("--txid", required=True, help="Archive transaction id")
@click.option("--out", type=click.Path(), default=None, help="Write decrypted bytes here (else stdout)")
def retrieve(txid, out):
    """Deep-recall by txid: fetch the full encrypted record and decrypt it (byte-identical)."""
    passphrase = _resolve_passphrase(confirm=False)
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    from .archive import Archiver
    from .crypto import CryptoError

    try:
        data = Archiver(cfg, store).retrieve(txid, passphrase)
    except (CryptoError, FileNotFoundError) as e:
        click.secho(f"✗ {e}", fg="red")
        store.close()
        sys.exit(1)
    finally:
        store.close()
    if out:
        Path(out).write_bytes(data)
        click.secho(f"✓ Wrote {len(data)} bytes → {out}", fg="green")
    else:
        sys.stdout.buffer.write(data)


@main.command()
@click.option("--txid", required=True, help="Archive transaction id to crypto-shred")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--soft", is_flag=True, help="Keep the local cleartext snapshot for browsing "
              "(default HARD shred also purges the snapshot + its FTS/vector rows)")
def shred(txid, yes, soft):
    """Crypto-shred: destroy the local key → the permanent ciphertext becomes unrecoverable.

    This is how "permanent but erasable" works: the record stays on the substrate forever,
    but without its key it is undecryptable. By default the shred is HARD — it also erases
    the local ~20-30% cleartext snapshot and its FTS/vector rows, leaving only an audit stub
    (txid, title, dates, flag), so nothing readable about the record survives locally.
    `--soft` keeps the snapshot for fast browsing (the on-chain payload is still unrecoverable).

    Caveat: crypto-shred only holds if ~/.lbrain/keys/ is NOT backed up elsewhere — a
    backed-up wrapped key plus the passphrase can still recover the payload.
    """
    mode = "soft (snapshot kept)" if soft else "HARD (snapshot purged)"
    if not yes and not click.confirm(
        f"Permanently destroy the key for {txid} [{mode}]? The record will be UNRECOVERABLE.",
        default=False,
    ):
        click.echo("  Aborted.")
        return
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    from .archive import Archiver

    had_key = Archiver(cfg, store).shred(txid, purge_snapshot=not soft)
    store.close()
    detail = "key destroyed" + ("" if soft else " + local snapshot purged")
    if had_key:
        click.secho(f"✓ Crypto-shredded {txid} — {detail}; ciphertext now undecryptable.", fg="green")
    else:
        click.secho(f"  No local key for {txid} — {detail} (already shredded or archived elsewhere).", fg="yellow")


@main.command(name="archive-status")
def archive_status():
    """Show the Tier-2 transport config and (for Arweave) the wallet address + balance."""
    cfg = Config.load()
    enabled = getattr(cfg, "arweave_enabled", False)
    transport = getattr(cfg, "arweave_transport", "local")
    real = enabled and transport in ("arweave", "arweave-l1", "l1")
    click.secho("Tier-2 archive status", fg="cyan")
    click.echo(f"  arweave_enabled:  {enabled}")
    click.echo(f"  transport:        {'arweave-l1 (permaweb)' if real else 'local (offline, content-addressed)'}")
    click.echo(f"  namespace:        {getattr(cfg, 'archive_namespace', 'private')}")
    click.echo(f"  gateway:          {getattr(cfg, 'arweave_gateway', '')}")
    if not real:
        click.echo("  → using the offline local store. Enable real writes: set arweave_enabled=true, "
                   "arweave_transport=\"arweave\", arweave_wallet_path in config.")
        return
    click.echo(f"  wallet_ref:       {getattr(cfg, 'arweave_wallet_path', '') or '(unset)'}")
    try:
        from .archive import _load_arweave_wallet

        w = _load_arweave_wallet(getattr(cfg, "arweave_wallet_path", ""))
        click.echo(f"  wallet address:   {w.address}")
        try:
            bal = w.balance  # AR (float)
            click.secho(f"  balance:          {bal} AR", fg=("green" if bal and bal > 0 else "yellow"))
            if not bal or bal <= 0:
                click.secho("  ⚠️  Wallet has no AR — L1 archiving will fail until funded. "
                            f"Fund: {w.address}", fg="yellow")
        except Exception as e:
            click.secho(f"  balance:          (could not fetch — {e})", fg="yellow")
    except Exception as e:
        click.secho(f"  ✗ wallet did not load: {e}", fg="red")


@main.command(name="archives")
@click.option("--namespace", default=None, help="Restrict to a silo")
@click.option("--verify", is_flag=True,
              help="Check each Arweave record is actually settled on-chain (bypasses the local mirror)")
def archives_cmd(namespace, verify):
    """List Tier-2 archived records."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    rows = store.list_archives(namespace=namespace)
    store.close()
    if not rows:
        click.echo("  No archives yet. Create one with `lbrain archive`.")
        return
    gateway = getattr(cfg, "arweave_gateway", "https://arweave.net")
    arweave_total = settled = 0
    click.secho(f"--- {len(rows)} archived record(s) ---", fg="cyan")
    for r in rows:
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(r["created"]).strftime("%Y-%m-%d %H:%M") if r["created"] else "?"
        flag = "  ⨯ SHREDDED" if r["shredded"] else ""
        click.secho(f"  {r['txid']}{flag}", fg=("red" if r["shredded"] else "yellow"))
        click.echo(f"     {r['title']}  ·  {when}  ·  {r['namespace']}  ·  {r['n_bytes']}B  ·  {r['transport']}")
        if verify and not r["shredded"]:
            if r["transport"] in ("arweave", "arweave-l1", "l1"):
                arweave_total += 1
                from .archive import verify_on_chain

                v = verify_on_chain(r["txid"], gateway)
                if v["settled"]:
                    settled += 1
                    click.secho(
                        f"     ✓ on-chain — {v['confirmations']} confirmations (block {v['block_height']})",
                        fg="green",
                    )
                else:
                    click.secho(
                        f"     ✗ NOT settled on-chain — {v['error']} (HTTP {v['http']}). "
                        "Local-only ghost: re-archive to push it to the permaweb.",
                        fg="red",
                    )
            else:
                click.secho("     • local-only transport — not on the permaweb substrate", fg="yellow")
    if verify and arweave_total:
        click.secho(
            f"\n  {settled}/{arweave_total} Arweave record(s) confirmed settled on-chain.",
            fg=("green" if settled == arweave_total else "yellow"),
        )


if __name__ == "__main__":
    main()
