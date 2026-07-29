"""LBrain CLI — fast, opinionated, no-ceremony."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from . import __version__
from . import amp
from .config import CONFIG_DIR, CONFIG_PATH, Config
from .embed import make_embedder
from .index import chunk as chunk_doc
from .index import discover, parse
from .lair_protocol import detect_anti_pattern, should_commit_to_lair
from .onboard import run_onboarding
from .search import keyword_only, search
from .serve import fence_block, render_response, resolve_mode, sanitize_field
from .store import Store


@click.group()
@click.version_option(__version__, prog_name="lbrain")
def main():
    """LBrain by Metavolve Labs — AI-native engineering memory with the Lair Protocol."""


# Settings whose value silently changes system behavior and which are therefore
# worth showing with provenance. Ordered for reading, not alphabetically.
_DOCTOR_FIELDS = [
    "embedding_provider", "embedding_model", "embedding_dim",
    "serve_mode", "serve_admissibility", "serve_chunk_chars",
    "gate_min_near", "gate_density",
    "rerank", "temporal_decay", "supersede_aware", "use_summaries",
    "arweave_enabled", "arweave_transport", "db_path",
]


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def doctor(as_json: bool):
    """Print the EFFECTIVE runtime state, with per-setting provenance.

    Answers the only question that matters before asserting what LBrain does:
    *is this value configured, or is it a code default?* Reading a default out of
    the source and reporting it as live behavior is how four working features got
    disabled on 2026-06-08 and how the embedding provider was misreported on
    2026-07-25. This command makes the check one second of work instead of an act
    of will — the admissibility gate, applied to our own configuration.

    Exits non-zero if the stored vectors disagree with the live embedding config,
    so it can gate a script.
    """
    import dataclasses
    import json as _json

    raw: dict = {}
    cfg_exists = CONFIG_PATH.exists()
    if cfg_exists:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - 3.10 backport
            import tomli as tomllib
        try:
            raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            click.secho(f"✗ config.toml exists but failed to parse: {e}", fg="red")
            raw = {}

    cfg = Config.load()
    defaults = {f.name: f.default for f in dataclasses.fields(Config)}

    rows = []
    for name in _DOCTOR_FIELDS:
        if not hasattr(cfg, name):
            continue
        value = getattr(cfg, name)
        in_file = name in raw
        default = defaults.get(name, dataclasses.MISSING)
        differs = default is not dataclasses.MISSING and value != default
        source = "config" if in_file else "DEFAULT"
        rows.append({"setting": name, "value": value, "source": source,
                     "differs_from_default": bool(differs)})

    # Keys present in config.toml that Config does not load are SILENT NO-OPS: the
    # operator believes the setting is on, and nothing reads it. That false belief
    # is the same class of error as reading a code default and calling it live.
    _field_names = {f.name for f in dataclasses.fields(Config)}
    inert = sorted(k for k in raw if k not in _field_names)

    # --- stored-vector fingerprint vs live config (the silent-corruption guard) ---
    drift = None
    stats = {}
    try:
        store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
        stored = {k: store.get_meta(k) for k in
                  ("embedding_provider", "embedding_model", "embedding_dim")}
        model = cfg.embedding_model
        if cfg.embedding_provider == "gemini" and not model.startswith("models/"):
            model = f"models/{model}"  # factory normalizes; compare like-for-like
        drift = store.embedding_config_status(
            cfg.embedding_dim, model, cfg.embedding_provider)
        stats = store.stats()
        store.close()
    except Exception as e:
        drift = f"unreadable: {e}"
        stored = {}

    if as_json:
        click.echo(_json.dumps({
            "config_path": str(CONFIG_PATH), "config_exists": cfg_exists,
            "settings": rows, "inert_config_keys": inert,
            "stored_fingerprint": stored,
            "embedding_drift": drift, "stats": stats,
        }, indent=2, default=str))
    else:
        click.secho(f"config:  {CONFIG_PATH}"
                    f"{'' if cfg_exists else '   ⚠ MISSING — every value below is a CODE DEFAULT'}",
                    fg=("green" if cfg_exists else "red"))
        click.echo()
        for r in rows:
            tag = ("[config]" if r["source"] == "config" else "[DEFAULT]")
            colour = "green" if r["source"] == "config" else "yellow"
            click.echo(f"  {r['setting']:<22} {str(r['value'])[:44]:<46} ", nl=False)
            click.secho(tag, fg=colour)
        if inert:
            click.echo()
            click.secho(f"  ⚠ {len(inert)} key(s) in config.toml that NOTHING READS "
                        f"(set, but inert):", fg="yellow")
            for k in inert:
                click.echo(f"      {k} = {raw[k]!r}")
        click.echo()
        if stored and any(v is not None for v in stored.values()):
            click.echo(f"  stored vectors:  {stored.get('embedding_model')} "
                       f"({stored.get('embedding_dim')}d, {stored.get('embedding_provider')})")
        if drift == "match":
            click.secho("  ✓ stored vectors match the live embedding config", fg="green")
        elif drift == "unset":
            click.secho("  · no vectors stored yet (fresh brain)", fg="yellow")
        else:
            click.secho(f"  ✗ EMBEDDING DRIFT: {drift} — re-embed required "
                        f"before these vectors can be trusted", fg="red")
        if stats:
            click.echo(f"  docs: {stats.get('docs')}  chunks: {stats.get('chunks')}"
                       f"  embedded: {stats.get('embedded')}")

    if isinstance(drift, str) and drift not in ("match", "unset"):
        raise SystemExit(1)


@main.command()
@click.option("--provider", type=click.Choice(["local", "gemini", "openai"]), default=None,
              help="Embedding provider. Default 'local' (on-device). A hosted provider is "
                   "used ONLY if you pass --gemini-key/--api-key on the command line; "
                   "a key in the environment is never treated as consent.")
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
    """Initialize LBrain config + DB — on-device by default.

    Out-of-the-box: `lbrain init --source ./docs --source ./notes` — no key, no
    account, embeddings computed locally. To use a hosted provider instead, pass
    the key explicitly: `lbrain init --gemini-key <KEY> --source ./docs`. The key
    is written to ~/.lbrain/env (chmod 600), never to plaintext config.
    """
    existing_config = CONFIG_PATH.exists()
    cfg = Config.load()
    if api_base:
        # Validate before assignment — direct attribute set bypasses __post_init__,
        # and `write()` would otherwise persist an unvalidated (possibly plaintext)
        # base URL that bricks every later command on reload.
        from .config import _validate_base_url

        cfg.gemini_base_url = _validate_base_url(api_base.rstrip("/"))
    # Auto-select: never make a first-time user find an API key before their first
    # query. If no credential is present, fall back to on-device embeddings.
    if provider is None:
        # An API key sitting in the environment must NEVER be read as consent to
        # ship the user's corpus to a third party. `--gemini-key` carries
        # envvar=GEMINI_API_KEY, and Config.load() reads the same variables, so
        # the previous rule ("a key exists anywhere -> use the remote provider")
        # silently sent every document to Google for any developer who had that
        # variable exported — while the README promised nothing leaves your
        # machine. Remote is now opt-IN, and only via an explicit flag on this
        # invocation. An existing install is never switched out from under it.
        ctx = click.get_current_context()

        def _on_command_line(param: str) -> bool:
            src = ctx.get_parameter_source(param)
            return src is not None and getattr(src, "name", "") == "COMMANDLINE"

        if existing_config and cfg.embedding_provider:
            provider = cfg.embedding_provider
        elif _on_command_line("gemini_key"):
            provider = "gemini"
        elif _on_command_line("api_key"):
            provider = "openai"
        else:
            provider = "local"
    cfg.embedding_provider = provider
    if provider == "local":
        from .embed import LocalEmbedClient
        cfg.embedding_model = LocalEmbedClient.DEFAULT_MODEL
        cfg.embedding_dim = LocalEmbedClient.DEFAULT_DIM
    elif provider == "gemini":
        cfg.embedding_model = "gemini-embedding-001"
        if gemini_key:
            cfg.gemini_api_key = gemini_key
    else:
        cfg.embedding_model = "text-embedding-3-small"
        if api_key:
            cfg.openai_api_key = api_key
    if sources:
        cfg.sources = [Path(s).expanduser().resolve() for s in sources]
    # New installs get structured serving. The CODE default stays "prose" on
    # purpose (fail-open to the legacy pipeline on an unrecognized value, and a
    # one-line rollback) — but a fresh install must produce the output the README
    # documents, `binds` annotations included. Without this, a stranger runs the
    # documented query and gets flat results with no admissibility flag: the
    # product's whole claim, invisible. Only written when there is no config yet,
    # so an existing install is never silently switched out from under its owner.
    if not existing_config:
        cfg.serve_mode = "structured"
    cfg.write()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    stats = store.stats()
    store.close()
    active_key = ("(none needed — on-device)" if provider == "local"
                  else cfg.gemini_api_key if provider == "gemini" else cfg.openai_api_key)
    if provider == "local" and (gemini_key or api_key or cfg.gemini_api_key or cfg.openai_api_key):
        click.secho(
            "  note: an API key was found in your environment. It was NOT used — indexing stays\n"
            "        on-device. To embed with a hosted provider instead, pass it explicitly:\n"
            "        lbrain init --gemini-key \"$GEMINI_API_KEY\"",
            fg="yellow",
        )
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
            click.echo(f"    pruned (gone or no longer indexable): {rel}")
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
    # provider="local" runs on-device and needs no credential — the whole point of
    # the zero-friction install path. Only the hosted providers gate on a key.
    _active_key = ("local" if cfg.embedding_provider == "local"
                   else cfg.gemini_api_key if cfg.embedding_provider == "gemini"
                   else cfg.openai_api_key)
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
        # reset_vectors drops + recreates both vec tables (chunks, archives) and
        # zeroes every embedded flag, so no stale old-model vectors survive in any
        # layer. `--all` then re-embeds chunks below; archives are restored on the
        # next archive capture.
        click.secho(
            f"  {reason} changed — rebuilding all vector tables and re-embedding all chunks.\n"
            "    (archives invalidated; re-capture to restore them.)",
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
@click.option("--rerank", is_flag=True, help="Cross-encoder precision pass (for PRECISE lookups; not broad queries; needs lbrain[rerank])")
@click.option("--recency", is_flag=True, help="Bounded mtime-freshness lift (for 'latest on X' queries)")
@click.option("--mode", "serve_mode", default=None, type=click.Choice(["structured", "prose"]),
              help="Serving mode: structured (attribution-bound records) or prose (legacy). Default: config serve_mode.")
def query(query: str, k: int, doc_type: str | None, priority: bool, rerank: bool, recency: bool,
          serve_mode: str | None):
    """Semantic + keyword hybrid search across the brain."""
    cfg = Config.load()
    if getattr(cfg, "amp_gating", True):
        ok, reason = amp.gate(query, getattr(cfg, "amp_min_chars", 12))
        if not ok:
            click.secho(f"[AMP gate] no memory injected — {reason}.", fg="yellow")
            return
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)
    try:
        t0 = time.time()
        hits = search(cfg, store, embedder, query, k=k, doc_type=doc_type, priority_only=priority,
                      rerank=rerank, recency=recency)
        dt_ms = (time.time() - t0) * 1000
        mode, warn = resolve_mode(cfg, serve_mode)
        if warn:
            click.secho(warn, fg="yellow", nl=False)
        if mode == "structured":
            click.secho(f"--- structured serve ({dt_ms:.0f} ms retrieval) ---", fg="cyan")
            click.echo(render_response(cfg, hits, query))
            return
        kept, used = amp.budget(hits, getattr(cfg, "amp_budget_chars", 0), getattr(cfg, "amp_per_chunk_chars", 360))

        # The CLI prose path emitted raw corpus text with no notice, no fence and
        # no control-char stripping — weaker than the MCP prose path, which at
        # least fenced. Our own CLAUDE.md tells agents to shell out to
        # `lbrain query`, so this output lands straight in an agent's context;
        # \x1b also reached a human's terminal intact. Red-team 2026-07-28, #5.
        if kept:
            click.secho(amp.UNTRUSTED_NOTICE, fg="red")

        core = amp.core_block(getattr(cfg, "core_memory_path", ""), getattr(cfg, "core_memory_chars", 900))
        if core:
            click.secho(fence_block(core.strip()), fg="green")

        label = f"{len(kept)} of {len(hits)} hits, AMP-budgeted" if len(kept) < len(hits) else f"{len(hits)} hits"
        click.secho(f"--- {label} ({dt_ms:.0f} ms) ---\n", fg="cyan")
        for i, h in enumerate(kept, 1):
            prefix = "★" if h.is_priority else " "
            click.secho(
                f"{prefix} [{i}] {sanitize_field(h.title, 120)}  ({h.score:.3f})", fg="yellow"
            )
            click.echo(f"   {sanitize_field(h.rel_path, 160)} :: chunk {h.chunk_idx}")
            if h.doc_type:
                click.echo(f"   type={sanitize_field(h.doc_type, 32)}  v={h.vector_score:.2f}  kw={h.keyword_score:.2f}  boosts={h.boosts}")
            text_preview = h.text.strip()[:getattr(cfg, "amp_per_chunk_chars", 360)]
            click.echo(fence_block(text_preview) + "\n")

        if getattr(cfg, "amp_provenance", True):
            click.secho(amp.provenance(kept, len(hits), used, getattr(cfg, "amp_budget_chars", 0)), fg="cyan")
    finally:
        embedder.close()
        store.close()


@main.command()
@click.argument("query")
@click.option("-k", default=10, type=int)
def search_cmd(query: str, k: int):
    """Exact-keyword search (FTS5 only, no embeddings, no API call)."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        t0 = time.time()
        hits = keyword_only(store, query, k=k)
        dt_ms = (time.time() - t0) * 1000
        mode, warn = resolve_mode(cfg, None)
        if warn:
            click.secho(warn, fg="yellow", nl=False)
        if mode == "structured":
            click.secho(f"--- structured serve ({dt_ms:.0f} ms) ---", fg="cyan")
            click.echo(render_response(cfg, hits, query, admissibility_on=False,
                                       include_core=False, include_provenance=False,
                                       hits_label="keyword hits"))
            return
        if hits:
            click.secho(amp.UNTRUSTED_NOTICE, fg="red")
        click.secho(f"--- {len(hits)} keyword hits ({dt_ms:.0f} ms) ---\n", fg="cyan")
        for i, h in enumerate(hits, 1):
            click.secho(f"  [{i}] {sanitize_field(h.title, 120)}", fg="yellow")
            click.echo(f"   {sanitize_field(h.rel_path, 160)} :: chunk {h.chunk_idx}")
            click.echo(fence_block(h.text.strip()[:240]) + "\n")
    finally:
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


@main.command(name="commit-check")
@click.argument("text", required=False)
@click.option("--file", "from_file", type=click.Path(exists=True), help="Read text from file")
def commit_check(text: str | None, from_file: str | None):
    """Check whether a piece of text should be committed to a lair/memory entry."""
    if from_file:
        text = Path(from_file).read_text(encoding="utf-8")
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


@main.command()
@click.option("--threshold", default=0.92, help="Cosine similarity threshold for clustering (0.0 to 1.0)")
@click.option("--model", default=None, help="Gemini model for synthesis (default: models/gemini-3.1-pro)")
@click.option("--limit", default=0, help="Synthesize at most N new clusters this run (0 = no limit)")
@click.option("--dry-run", is_flag=True, help="Cluster and report only — no API calls, no files written")
def consolidate(threshold: float, model: str, limit: int, dry_run: bool):
    """Cluster related chunks and synthesize dense abstraction memories (GATED).

    Output goes to ~/.lbrain/abstractions/ — NOT a source tree, so nothing
    enters retrieval automatically. To serve abstractions (after measuring),
    add that directory to `sources` in config.toml, then import + embed.
    Idempotent: re-runs skip clusters that already have an abstraction file.
    """
    from .consolidate import ABSTRACTIONS_DIR, DEFAULT_MODEL, run_consolidation
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        generated, skipped, total = run_consolidation(
            cfg, store,
            threshold=threshold,
            model=model or DEFAULT_MODEL,
            limit=limit,
            dry_run=dry_run,
        )
    finally:
        store.close()
    if dry_run:
        click.echo(f"  ✓ Dry run: {total} clusters; {skipped} already synthesized, {total - skipped} would be new.")
    else:
        click.echo(f"  ✓ Consolidation complete: {generated} new, {skipped} skipped (existing), {total} clusters total.")
        click.echo(f"  Output: {ABSTRACTIONS_DIR}  (gated — measure via A/B before adding to sources)")


# ---------------------------------------------------------------------------
# Optional Tier-2 archive commands — registered only if the archive extra is
# installed (importing lbrain.archive.cli requires `cryptography`). Without it,
# the archive/capture/recall/retrieve/shred/archive-status/archives commands
# simply do not appear and the core CLI runs unchanged.
# ---------------------------------------------------------------------------
try:
    from .archive.cli import register as _register_archive_commands

    _register_archive_commands(main)
except ImportError:
    pass


@main.command()
@click.option("--since", default=0, show_default=True,
              help="Only show claims unverified for at least N days. Default 0 — see below.")
@click.option("--all", "show_all", is_flag=True, help="Do not truncate the ranked list.")
@click.option("--path", "path_prefix", default="", help="Restrict to paths starting with this.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def stale(since: int, show_all: bool, path_prefix: str, as_json: bool):
    """Find records asserting an OPEN state that nobody has re-verified.

    LBrain knows when a record was written. It cannot know whether the claim
    inside it is still true — that ground truth lives at a registry, a
    counterparty, or in someone's head. This command does the half a local
    engine honestly can: it finds claims that have a shelf life, prints how long
    since anyone stood behind them, and leaves the judgement to you.

    On the default of --since 0: the case that motivated this command went false
    in EIGHTEEN DAYS. Every age threshold tested suppressed it. Age is
    information to report, not a gate to pass.

    Exits non-zero only on the DECIDABLE section, so it can gate a
    pre-publication script without heuristics ever breaking a build.
    """
    import datetime
    import json as _json
    from .staleness import (claim_date, days_since, expired, is_excluded,
                            open_claims, volatility)

    cfg = Config.load()
    today = datetime.date.today()
    decidable, ranked = [], []
    scanned = excluded = undated = 0

    for src in cfg.sources:
        root = Path(src)
        if not root.exists():
            continue
        for f in sorted(root.rglob("*.md")):
            rel = str(f.relative_to(root))
            if is_excluded(rel) or (path_prefix and not rel.startswith(path_prefix)):
                excluded += 1
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            mtime = datetime.date.fromtimestamp(f.stat().st_mtime).isoformat()
            label, date = claim_date(text, rel, mtime)
            if label in ("file-dated", ""):
                undated += 1
            exp = expired(text, today)
            if exp:
                decidable.append({"path": rel, "reason": f"verify_by {exp} passed",
                                  "days": days_since(exp, today)})
            if volatility(text) != "open":
                continue
            age = days_since(date, today)
            if age is None or age < since:
                continue
            ranked.append({"path": rel, "days": age, "label": label, "date": date,
                           "claims": open_claims(text)})

    ranked.sort(key=lambda r: -r["days"])
    pct = round(undated * 100 / scanned) if scanned else 0

    if as_json:
        click.echo(_json.dumps({"today": today.isoformat(), "scanned": scanned,
                                "excluded": excluded, "decidable": decidable,
                                "unverified_open": ranked,
                                "no_verification_date": undated,
                                "blind_spot_pct": pct}, indent=2))
    else:
        click.secho(f"Verification audit — {scanned} docs scanned · "
                    f"{excluded} archived/skipped · today {today}", fg="cyan")
        click.echo()
        if decidable:
            click.secho(f"PROVABLY STALE ({len(decidable)})", fg="red", bold=True)
            for d in decidable:
                click.echo(f"  EXPIRED  {d['path']}")
                click.echo(f"           {d['reason']} — {d['days']}d ago")
            click.echo()
        click.secho(f"UNVERIFIED OPEN CLAIMS ({len(ranked)} docs, oldest first)",
                    fg="yellow", bold=True)
        for r in (ranked if show_all else ranked[:20]):
            click.echo(f"  {r['days']:>5}d  {r['label']:<10} {r['path']}")
            for c in r["claims"][:2]:
                click.echo(f"          ↳ {c}")
        if not show_all and len(ranked) > 20:
            click.echo(f"  … {len(ranked) - 20} more (--all)")
        click.echo()
        # Mandatory. The single most dangerous reading of this tool is
        # "not listed = verified". It must state its own coverage every run.
        click.secho("BLIND SPOT", fg="yellow", bold=True)
        click.echo(f"  {undated} of {scanned} docs ({pct}%) carry no verification date, so they are")
        click.echo("  ranked on filename or mtime — when the file was WRITTEN, not when the claim")
        click.echo("  was CHECKED. Absence from this list is NOT evidence a record is current.")

    if decidable:
        raise SystemExit(1)

if __name__ == "__main__":
    main()


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def whoami(as_json: bool):
    """Report who this brain is and what it is trusted for.

    Answers, in one place, the question an agent should be able to ask before it
    relies on retrieved records: what am I reading, how does it serve, and does
    it carry any credential beyond its own say-so.

    An unregistered brain is a normal, fully-functional state — not an error.
    """
    import json as _json

    from .identity import describe

    cfg = Config.load()
    stats = {}
    try:
        store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
        stats = store.stats()
        store.close()
    except Exception:
        pass

    info = describe(cfg, stats)
    if as_json:
        click.echo(_json.dumps(info, indent=2, default=str))
        return

    ident = info["identity"]
    if ident["registered"]:
        click.secho(f"  {ident['gcx']}", fg="green", bold=True)
        click.echo(f"  address:      {ident['address']}")
        creds = ", ".join(ident["credentials"]) or "none yet"
        click.echo(f"  credentials:  {creds}")
        if ident["trust_score"] is not None:
            click.echo(f"  trust score:  {ident['trust_score']}")
    else:
        click.secho("  no ecosystem identity", fg="yellow")
        click.echo(f"  {ident['note']}")

    b, s = info["brain"], info["serving_contract"]
    click.echo()
    click.secho("  brain", fg="cyan")
    click.echo(f"    db:       {b['db']}")
    click.echo(f"    indexed:  {b['docs']} docs · {b['chunks']} chunks · {b['embedded']} embedded")
    click.echo(f"    sources:  {len(b['sources'])} configured")
    click.echo()
    click.secho("  serving contract", fg="cyan")
    click.echo(f"    mode:       {s['mode']}  (provider: {s['provider']})")
    click.echo(f"    attributed: {s['attribution']}")
    click.echo(f"    staleness:  {'marked inline' if s['staleness_marked'] else 'NOT marked'}")
    click.echo(f"    untrusted:  retrieved text is fenced as data, never instructions")


@main.command()
@click.argument("name")
@click.option("--gateway", default=None, help="Arweave gateway (default arweave.net; point at your own).")
@click.option("--graphql", default=None, help="GraphQL endpoint for name lookup.")
@click.option("--out", type=click.Path(), default=None, help="Write the record to a file.")
@click.option("--quiet", is_flag=True, help="Print only the content (pipe-friendly).")
def resolve(name: str, gateway: str, graphql: str, out: str, quiet: bool):
    """Resolve a gcx:// name to its permanent record and verify it.

    \b
      lbrain resolve gcx://rfc/793

    `gcx` and `aet` are IANA-registered URI schemes. The verification hash comes
    from an on-chain tag written at mint time — not from this package and not
    from our servers — so the check does not require trusting us.

    Exits non-zero if the record cannot be verified, so it can gate a script.
    """
    from .gcx import GATEWAY, GRAPHQL, ResolveError
    from .gcx import resolve as _resolve

    try:
        r = _resolve(name, gateway=gateway or GATEWAY, graphql=graphql or GRAPHQL)
    except ResolveError as e:
        click.secho(f"✗ {e}", fg="red")
        raise SystemExit(1)

    if out:
        Path(out).write_bytes(r.content)

    if quiet:
        click.echo(r.content.decode("utf-8", errors="replace"), nl=False)
        raise SystemExit(0 if r.verified else 1)

    ok = r.verified
    click.secho(f"  {r.name}", fg="cyan", bold=True)
    click.echo(f"    txid:     {r.txid}")
    if r.tags.get("Title"):
        click.echo(f"    title:    {r.tags['Title']}")
    click.echo(f"    bytes:    {len(r.content):,}")
    click.echo(f"    expected: {r.expected_sha256 or '(none recorded on-chain)'}")
    click.echo(f"    actual:   {r.actual_sha256}")
    click.secho(f"    {r.status}", fg=("green" if ok else "red"), bold=True)
    click.echo(f"    gateway:  {r.gateway}")
    if not out:
        head = r.content.decode("utf-8", errors="replace").strip().splitlines()[:4]
        click.echo()
        for ln in head:
            click.echo(f"    │ {ln[:96]}")
        click.echo(f"    │ … ({len(r.content):,} bytes — use --out FILE or --quiet)")
    raise SystemExit(0 if ok else 1)
