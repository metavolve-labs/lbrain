"""Tier-2 archive CLI commands (optional subpackage).

Registered onto the main ``lbrain`` group by ``register(main)`` only when this module
imports successfully — which requires the archive extra (``cryptography``) to be present.
Without it, ``import lbrain.archive.cli`` raises ImportError and the core CLI silently
omits these commands.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from ..config import CONFIG_DIR, Config
from ..embed import make_embedder
from ..store import Store
from .archiver import Archiver, LocalTransport, verify_on_chain
from .config import archive_passphrase, set_archive_passphrase
from .crypto import CryptoError
from .storage import ArchiveStore


def _resolve_passphrase(confirm: bool = False) -> str:
    """Archive passphrase from ~/.lbrain/env, else prompt (and offer to persist it)."""
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


@click.command(name="archive")
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
    # "local" needs no key and must never be gated behind an ambient hosted one
    # (red-team 2026-07-28 #11/#13: the same idiom routed provider=local into
    # the OpenAI branch elsewhere). Only a named hosted provider needs a key.
    if cfg.embedding_provider == "gemini":
        active_key = cfg.gemini_api_key
    elif cfg.embedding_provider == "openai":
        active_key = cfg.openai_api_key
    else:
        active_key = "local"
    if active_key:
        try:
            embedder = make_embedder(cfg)
        except Exception:
            embedder = None

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


@click.command(name="capture")
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
    # "local" needs no key and must never be gated behind an ambient hosted one
    # (red-team 2026-07-28 #11/#13: the same idiom routed provider=local into
    # the OpenAI branch elsewhere). Only a named hosted provider needs a key.
    if cfg.embedding_provider == "gemini":
        active_key = cfg.gemini_api_key
    elif cfg.embedding_provider == "openai":
        active_key = cfg.openai_api_key
    else:
        active_key = "local"
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


@click.command(name="recall")
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
        rows = ArchiveStore(store.db, store.embedding_dim).search_archives(q_vec, k=k, namespace=namespace)
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


@click.command(name="retrieve")
@click.option("--txid", required=True, help="Archive transaction id")
@click.option("--out", type=click.Path(), default=None, help="Write decrypted bytes here (else stdout)")
def retrieve(txid, out):
    """Deep-recall by txid: fetch the full encrypted record and decrypt it (byte-identical)."""
    passphrase = _resolve_passphrase(confirm=False)
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
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


@click.command(name="shred")
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

    Caveat: crypto-shred only holds if the wrapped key is GONE everywhere. It does
    NOT hold against (a) a backed-up ~/.lbrain/keys/ + the passphrase, or (b) a
    filesystem snapshot / backup that captured the key file before the shred (the
    on-disk overwrite is best-effort and a no-op on CoW/SSD filesystems). Destroy
    those copies too for the guarantee to be real.
    """
    mode = "soft (snapshot kept)" if soft else "HARD (snapshot purged)"
    if not yes and not click.confirm(
        f"Permanently destroy the key for {txid} [{mode}]? Without a key backup the "
        "record becomes undecryptable.",
        default=False,
    ):
        click.echo("  Aborted.")
        return
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    had_key = Archiver(cfg, store).shred(txid, purge_snapshot=not soft)
    store.close()
    detail = "key destroyed" + ("" if soft else " + local snapshot purged")
    if had_key:
        click.secho(f"✓ Crypto-shredded {txid} — {detail}; ciphertext now undecryptable.", fg="green")
    else:
        click.secho(f"  No local key for {txid} — {detail} (already shredded or archived elsewhere).", fg="yellow")


@click.command(name="archive-status")
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
        from .archiver import _load_arweave_wallet

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


@click.command(name="archives")
@click.option("--namespace", default=None, help="Restrict to a silo")
@click.option("--verify", is_flag=True,
              help="Check each Arweave record is actually settled on-chain (bypasses the local mirror)")
def archives_cmd(namespace, verify):
    """List Tier-2 archived records."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    rows = ArchiveStore(store.db, store.embedding_dim).list_archives(namespace=namespace)
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


_COMMANDS = [archive, capture, recall, retrieve, shred, archive_status, archives_cmd]


def register(main) -> None:
    """Attach the Tier-2 archive commands to the main ``lbrain`` click group."""
    for cmd in _COMMANDS:
        main.add_command(cmd)
