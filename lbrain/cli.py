"""LBrain CLI — fast, opinionated, no-ceremony."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from . import __version__
from . import amp
from .config import CONFIG_DIR, CONFIG_PATH, Config
from .embed import make_embedder, UnknownProviderError
from .index import chunk as chunk_doc
from .index import CHUNKER_VERSION, chunker_fingerprint, discover, parse
from .lair_protocol import core_rules, detect_anti_pattern, should_commit_to_lair
from .onboard import run_onboarding
from .search import keyword_only, search
from .serve import blinding_notice, fence_block, render_response, resolve_mode, sanitize_field
from .store import SqliteExtensionError, Store


class _LBrainGroup(click.Group):
    """Turn misconfiguration into an error message, not a traceback.

    A Python without loadable-extension support fails on the very first command a
    new user runs. A stack trace ending in AttributeError tells them nothing; the
    exception carries instructions, so print those and exit 1.

    `UnknownProviderError` gets the same treatment for the same reason: it is a
    typo in config.toml, i.e. a user-fixable mistake, and its message already
    names the fix. Both are *config* faults, not bugs — neither should ever
    reach the user as a stack trace.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (SqliteExtensionError, UnknownProviderError) as exc:
            raise click.ClickException(str(exc)) from None


@click.group(cls=_LBrainGroup)
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

# A-427 — the two evidence-threshold knobs run BACKWARDS relative to their natural
# reading. serve.py gates on `near >= gate_min_near and near/len(kept) >= gate_density`,
# so LOWER values make the ambiguity warning fire MORE often, which is the STRICTER
# standard of proof. Anyone tuning for "strict" reaches for a high number and gets
# the loosest configuration in the fleet, while the config file, the persona name and
# the docs all still say strict. It is undetectable by reading — only by diffing
# behaviour. Surfaced here because `doctor` is where an operator goes to learn what
# their configuration MEANS, and a bare `3` teaches nothing.
_DIRECTION = {
    "gate_min_near": "↓ LOWER = STRICTER (fires the ambiguity notice sooner)",
    "gate_density": "↓ LOWER = STRICTER (fires the ambiguity notice sooner)",
    "serve_admissibility": "master ENABLE, not a dial — false turns the gate OFF entirely",
}


def _chunker_drift(live: str | None, stored: str | None) -> str | None:
    """'match' | 'unset' | a human description of the mismatch | None if unreadable.

    Exact inequality, never startswith — stored '20' starts with '2' and would
    read as current against version 2 (the trap already noted in `import`).
    """
    if live is None:
        return None
    if stored is None:
        return "unset"
    return "match" if stored == live else f"{stored} != {live}"


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
        chunker_live = chunker_fingerprint(
            cfg.chunk_tokens, cfg.chunk_overlap,
            getattr(cfg, "contextual_prefix", False))
        chunker_stored = store.get_meta("chunker_version")
        if chunker_stored is None and stats.get("docs", 0):
            # Same rule `import` uses: no recorded version on a populated brain
            # means it predates the guard, i.e. v1 by definition. Reading absence
            # as "current" is what made this blind spot invisible.
            chunker_stored = "1 (unversioned)"
        store.close()
    except Exception as e:
        drift = f"unreadable: {e}"
        stored = {}
        chunker_live = chunker_stored = None

    if as_json:
        click.echo(_json.dumps({
            "config_path": str(CONFIG_PATH), "config_exists": cfg_exists,
            "settings": rows, "inert_config_keys": inert,
            "stored_fingerprint": stored,
            "embedding_drift": drift, "stats": stats,
            "chunker_live": chunker_live, "chunker_stored": chunker_stored,
            "chunker_drift": _chunker_drift(chunker_live, chunker_stored),
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
            click.secho(tag, fg=colour, nl=not _DIRECTION.get(r["setting"]))
            # A-427: these two read backwards. `doctor` is where an operator goes to
            # learn what their config MEANS, and a bare `3` teaches nothing — someone
            # tuning for "strict" writes a high number and gets the loosest setting,
            # with the config file, the persona name and the docs all still saying
            # strict. Annotate at the point of reading, not only in the source.
            if _DIRECTION.get(r["setting"]):
                click.secho(f"   {_DIRECTION[r['setting']]}", fg="cyan")
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
        cdrift = _chunker_drift(chunker_live, chunker_stored)
        if cdrift == "match":
            click.secho(f"  ✓ index was built by this chunker ({chunker_live})", fg="green")
        elif cdrift == "unset":
            click.secho("  · no chunks indexed yet (fresh brain)", fg="yellow")
        elif cdrift is not None:
            click.secho(
                f"  ⚠ CHUNKER DRIFT: index built with {chunker_stored}, this run is "
                f"{chunker_live}", fg="yellow")
            click.secho(
                "    Retrieval still works — it is served from chunks the current "
                "code would not produce.\n"
                "    Run `lbrain import` to re-chunk (it re-embeds what changed).",
                fg="yellow")
        if stats:
            click.echo(f"  docs: {stats.get('docs')}  chunks: {stats.get('chunks')}"
                       f"  embedded: {stats.get('embedded')}")

    # Deliberately NOT part of the non-zero contract. `doctor` exits 1 when the
    # stored vectors cannot be trusted; chunker drift is a weaker claim — results
    # are stale, not wrong — and `import` already repairs it. Widening the exit
    # code would silently start failing every script that gates on `doctor`, to
    # report something the next import fixes on its own.
    if isinstance(drift, str) and drift not in ("match", "unset"):
        raise SystemExit(1)



def _confirm_local_model(assume_yes: bool) -> bool:
    """Explain the one-time model download and ask. Returns True to proceed.

    Never blocks a non-interactive run: with no TTY (CI, a script, a Dockerfile)
    it prints the same notice and proceeds, because a prompt that hangs a
    pipeline is a worse bug than the surprise it was added to prevent.
    """
    from .embed import LocalEmbedClient

    click.secho("\n  On-device embeddings (the default — no API key, no account)", bold=True)
    click.echo(
        "    LBrain needs to turn your text into vectors so it can search by meaning.\n"
        f"    On first use it downloads {LocalEmbedClient.DEFAULT_MODEL} (~67 MB, once)\n"
        "    and then runs it on your CPU.\n"
    )
    click.secho("    That download is the model coming DOWN, not your notes going UP.", fg="green")
    click.echo(
        "    It is the only network call this path makes. After it, embedding is\n"
        "    fully offline and your documents never leave this machine.\n"
        "    Cached at ~/.cache/huggingface — fetched once, never again.\n"
    )
    click.echo("    Prefer not to? Use a hosted provider instead (your key, your billing):")
    click.echo("      lbrain init --gemini-key <KEY> --source ./docs\n")

    if assume_yes:
        click.secho("    --yes given; proceeding.", fg="cyan")
        return True
    if not sys.stdin.isatty():
        click.secho("    non-interactive session; proceeding with on-device embeddings.", fg="cyan")
        return True
    return click.confirm("    Download the model and keep everything on-device?", default=True)

@main.command()
@click.option("--provider", type=click.Choice(["local", "gemini", "openai"]), default=None,
              help="Embedding provider. Default 'local' (on-device). A hosted provider is "
                   "used ONLY if you pass --gemini-key/--api-key on the command line; "
                   "a key in the environment is never treated as consent.")
@click.option("--gemini-key", envvar="GEMINI_API_KEY", default=None, help="Gemini API key (provider=gemini)")
@click.option("--api-key", envvar="OPENAI_API_KEY", default=None, help="OpenAI API key (provider=openai)")
@click.option("--api-base", default=None, help="Override the Gemini base URL (point at a proxy / self-hosted gateway)")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip the on-device model download prompt.")
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Path(),
    help="Directory to index (repeatable)",
)
def init(provider: str, gemini_key: str, api_key: str, api_base: str, assume_yes: bool,
         sources: tuple[str, ...]):
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
    ctx = click.get_current_context()

    def _on_command_line(param: str) -> bool:
        src = ctx.get_parameter_source(param)
        return src is not None and getattr(src, "name", "") == "COMMANDLINE"

    # Whether a key was typed on THIS invocation, as opposed to merely sitting in
    # the environment. The two cases need different advice, and conflating them
    # produced a note that told the user to run the command they had just run.
    key_on_cli = _on_command_line("gemini_key") or _on_command_line("api_key")
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
        # Consent, not surprise. The on-device path fetches a ~67 MB model on
        # first embed. That is the ONE network call the local install makes, and
        # a silent download on a tool marketed as local-first reads as the exact
        # opposite of what it is. The founder himself did not expect it
        # (2026-07-30) — if the author is surprised, a stranger certainly is.
        if not _confirm_local_model(assume_yes):
            click.secho(
                "  aborted. To use a hosted provider instead, pass a key explicitly:\n"
                "    lbrain init --gemini-key <KEY> --source ./docs",
                fg="yellow",
            )
            raise SystemExit(1)
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
        if key_on_cli:
            # The key WAS passed explicitly and still did not take effect, because
            # an existing brain keeps its provider on purpose. Telling this user to
            # "pass it explicitly" is advice to repeat the command that just failed.
            wanted = "openai" if _on_command_line("api_key") else "gemini"
            flag = "--api-key" if wanted == "openai" else "--gemini-key"
            click.secho(
                "  note: this brain already uses the on-device provider, so the key you passed was\n"
                "        NOT applied — an existing install is never switched out from under it.\n"
                "        To switch deliberately, name the provider:\n"
                f"        lbrain init --provider {wanted} {flag} <KEY>\n"
                "        Then re-embed: lbrain embed --stale  (old vectors are a different space).",
                fg="yellow",
            )
        else:
            click.secho(
                "  note: an API key is present in your environment. It was NOT used — a key in the\n"
                "        environment is not consent to send your corpus away, so indexing stays\n"
                "        on-device. To embed with a hosted provider instead, ask for it explicitly:\n"
                "        lbrain init --provider gemini --gemini-key \"$GEMINI_API_KEY\"",
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


def warn_if_unprovisioned() -> str:
    """Warn — loudly — when a READ runs against a home that was never provisioned.

    Closes the router lane's A-425. `LBRAIN_HOME=<typo> lbrain stats` used to
    mint a fresh empty brain and answer `docs: 0`, exit 0, in silence. Under the
    persona architecture a brain is selected BY THAT ENV VAR, so one typo turns a
    specialist into a **confident amnesiac**: an agent that fluently reports "I
    have no records" instead of erroring. That is doctrine rule 9's shape
    (*defaults must be empty and fail-loud, never plausible*) displaced from an
    identifier onto a home path — the var is an opaque string that nothing ever
    dereferences against a real brain.

    Warns rather than refuses: a genuinely fresh install is the same state, and
    breaking first-run onboarding to catch a typo is the worse trade. Returns the
    message (empty when provisioned) so callers can test it without capturing
    stderr.
    """
    import os

    if CONFIG_PATH.exists():
        return ""
    chosen = os.environ.get("LBRAIN_HOME")
    msg = (
        f"⚠️  {CONFIG_DIR} has no config.toml — this brain is UNPROVISIONED and will "
        f"answer as if it simply knows nothing.\n"
        + (f"   LBRAIN_HOME is set to {chosen!r}. If that is a typo you are talking to an "
           "empty brain, not the one you meant.\n" if chosen else "")
        + "   Provision it: lbrain init --source <dir>"
    )
    click.secho(msg, fg="yellow", err=True)
    return msg


def _resolve_envelope(cfg, disclosure: str | None, sealed: str | None):
    """The effective disclosure envelope for one CLI invocation.

    Returns None when NOTHING about disclosure was asked for — no env ceiling, no
    flag, and no standing permissions in config. That keeps an ordinary install
    on exactly its pre-existing code path rather than routing it through a filter
    that happens to admit everything; "inert by construction" is easier to prove
    than "inert by arithmetic".
    """
    import os

    from . import disclosure as _d

    asked = (
        disclosure is not None
        or sealed is not None
        or "LBRAIN_DISCLOSURE" in os.environ
        or "LBRAIN_SEALED" in os.environ
        or getattr(cfg, "allowed_doc_types", [])
        or getattr(cfg, "allowed_path_prefixes", [])
        or getattr(cfg, "force_priority_only", False)
    )
    if not asked:
        return None
    return _d.resolve(cfg, requested_mode=disclosure, requested_seal=sealed)


def _project_belief(store, doc) -> int:
    """Keep the beliefs table in step with a doc's frontmatter. Returns 1 if the
    doc is a belief, else 0 (and clears any stale projection).

    Called on BOTH import branches. A belief's whole lifecycle lives in
    frontmatter, so the "body unchanged" path is the common case for a
    promotion — missing it there is how the feature would look broken.
    """
    from . import beliefs as _b

    b = _b.from_doc(doc)
    if b is None:
        # Fail CLOSED on unreadable metadata. A malformed YAML block makes
        # `parse` return doc_type="" — indistinguishable, to this function, from
        # an author deliberately removing `type: belief`. Deleting the projection
        # on that basis silently strips a RETRACTED belief of its burial and its
        # marking, turning a known-wrong record back into an ordinary document
        # that ranks on equal terms. Observed 2026-07-31 (anomaly A-431) when a
        # one-character YAML indent error did exactly that.
        if not getattr(doc, "metadata_ok", True) and store.belief_row_for_path(doc.rel_path):
            print(
                f"[lbrain] WARNING: {doc.rel_path} is a known belief whose frontmatter no "
                "longer parses — KEEPING its last known state. Fix the YAML.",
                file=sys.stderr,
            )
            return 1
        store.delete_belief_for_path(doc.rel_path)
        return 0
    store.replace_belief(
        {
            "belief_id": b.belief_id, "rel_path": b.rel_path, "persona": b.persona,
            "state": b.state, "subject": b.subject, "claim": b.claim,
            "confidence": b.confidence, "impact": b.impact, "created": b.created,
            "promoted_at": b.promoted_at, "verify_by": b.verify_by,
            "countersigned_by": b.countersigned_by,
        },
        [(e.ref, e.kind, e.verified) for e in b.evidence],
    )
    return 1


@main.command(name="import")
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option("--prune/--no-prune", default=True, help="Drop docs no longer on disk")
@click.option("--force-prune", is_flag=True, help="Override the prune safety guards (mount-gone / >50%)")
@click.option("--rechunk", is_flag=True,
              help="Re-chunk every document even if its body is unchanged.")
def import_cmd(paths: tuple[str, ...], prune: bool, force_prune: bool, rechunk: bool):
    """Walk source directories and ingest markdown into the brain."""
    cfg = Config.load()
    sources = [Path(p).expanduser().resolve() for p in paths] if paths else cfg.sources
    if not sources:
        click.secho("✗ No sources configured. Run `lbrain init --source <dir>`.", fg="red")
        sys.exit(1)

    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)

    # A chunker upgrade must reach the DATA, not just the code. Import short-circuits
    # on the body hash, so shipping new chunk boundaries left every existing corpus on
    # the old ones — silently, and with no way for a user to notice their index was
    # built by code they no longer run. Same failure shape as reading a code default
    # and calling it live (doctrine rule 2), one layer down: `installed` != `applied`.
    # The fingerprint must cover EVERY input to chunk boundaries, not just the
    # algorithm. Shipped this morning as a bare int, which left chunk_tokens,
    # chunk_overlap and contextual_prefix outside it — so turning on the
    # Contextual-Retrieval prefix, or halving chunk_tokens, was a silent no-op on
    # any existing corpus: `unchanged: 1`, feature reported [config] by every
    # reader, applied to zero chunks. Fixing A-412 and leaving these out is the
    # "fixed the instance, missed the class" shape from the discipline doc, in a
    # fix written to prevent exactly that.
    current_cv = chunker_fingerprint(
        getattr(cfg, "chunk_tokens", ""),
        getattr(cfg, "chunk_overlap", ""),
        getattr(cfg, "contextual_prefix", False),
    )
    stored_cv = store.get_meta("chunker_version")
    # A brain that predates this guard carries NO version — and that is precisely
    # the population that needs re-chunking, since it was built by v1 by definition.
    # Treating "unknown" as "current" would have made the guard a no-op for every
    # existing install while looking correct on a fresh one: the same incomplete-fix
    # shape logged as A-005 and A-404.
    if stored_cv is None:
        stored_cv = "1 (unversioned)" if store.stats().get("docs", 0) else None
    # Exact inequality, never startswith: stored "20" starts with "2" and would
    # have been read as current against version 2.
    cv_stale = stored_cv is not None and stored_cv != current_cv
    if cv_stale and not rechunk:
        # Say what it COSTS, not merely what it does. On-device re-embedding is
        # time; a hosted provider is money the user did not ask to spend, and an
        # upgrade that quietly bills is the "plausible default" failure in its
        # most expensive form.
        hosted = getattr(cfg, "embedding_provider", "local") != "local"
        cost = (
            f"    Your provider is {cfg.embedding_provider!r} — re-embedding is a "
            f"BILLED API call. Run `lbrain embed --stale` when you are ready to "
            f"spend it; retrieval keeps working on the old vectors until you do.\n"
            if hosted else
            "    Your provider is 'local', so re-embedding costs time, not money.\n"
        )
        click.secho(
            f"  ⚠ chunking changed (index built with {stored_cv}, this run is "
            f"{current_cv}) — re-chunking every document.\n"
            f"{cost}"
            f"    To skip: pin the previous lbrain version.", fg="yellow")
    force_rechunk = rechunk or cv_stale

    t0 = time.monotonic()
    new_docs = 0
    updated_docs = 0
    unchanged_docs = 0
    meta_refreshed = 0   # frontmatter changed, body did not (A-401)
    beliefs_seen = 0
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
                if existing_hash == doc.doc_hash and not force_rechunk:
                    # Body unchanged. Frontmatter may still have changed, and it
                    # is invisible to doc_hash (A-401) — refresh the row only,
                    # never the chunks: a metadata edit changes no chunk, so
                    # re-embedding would be pure cost.
                    if store.doc_metadata_differs(doc):
                        store.upsert_doc(doc)
                        meta_refreshed += 1
                    else:
                        unchanged_docs += 1
                    store.replace_supersessions(doc)
                    # Belief state lives ENTIRELY in frontmatter, so a promotion
                    # changes no chunk and would land in exactly this branch. If
                    # the projection were only refreshed on a body edit, a
                    # promoted belief would stay invisible until someone happened
                    # to edit its prose — i.e. the gate would appear to do nothing.
                    beliefs_seen += _project_belief(store, doc)
                    continue
                if existing_hash is None:
                    new_docs += 1
                else:
                    updated_docs += 1
                    store.delete_doc_chunks(doc.rel_path)
                store.upsert_doc(doc)
                store.replace_supersessions(doc)
                store.replace_wikilinks(doc)
                beliefs_seen += _project_belief(store, doc)
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

    # Stamp AFTER a successful pass only, and ONLY when that pass covered every
    # configured source. `lbrain import <one-dir>` re-chunks one source and would
    # otherwise mark the whole brain current, permanently stranding the others on
    # old boundaries with nothing left to detect it.
    covered = set(sources) >= {Path(p).expanduser().resolve() for p in cfg.sources}
    if covered:
        store.set_meta("chunker_version", current_cv)
    elif cv_stale:
        click.secho(
            f"  ⚠ partial import — {len(cfg.sources) - len(sources)} configured "
            f"source(s) NOT re-chunked, so the version stamp is left stale on "
            f"purpose. Run `lbrain import` with no arguments to finish.", fg="yellow")
    stats = store.stats()
    store.close()
    dt = time.monotonic() - t0
    click.secho(
        f"✓ Imported in {dt:.1f}s — new: {new_docs}, updated: {updated_docs}, "
        f"unchanged: {unchanged_docs}, chunks: {total_chunks}, pruned: {len(pruned)}"
        + (f", meta-refreshed: {meta_refreshed}" if meta_refreshed else "")
        + (f", beliefs: {beliefs_seen}" if beliefs_seen else ""),
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
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def selftest(as_json):
    """Verify THIS installed build actually serves correctly, here.

    Indexes a tiny shipped golden corpus into a throwaway brain via the real
    pipeline and asserts the serving invariants — retrieval, honest dating,
    supersession, staleness, and fencing. Pure FTS: no network, no API key, and
    it never touches your real brain. Exits non-zero if any invariant fails, so
    it works as an install smoke test in CI or after an upgrade."""
    from .selftest import run_selftest

    ok = run_selftest(as_json=as_json)
    sys.exit(0 if ok else 1)


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
    t0 = time.monotonic()
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
    dt = time.monotonic() - t0
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
@click.option("--persona", default=None,
              help="Ask as this agent: its own DRAFT beliefs become visible. Omit and no drafts are.")
@click.option("--disclosure", default=None,
              type=click.Choice(["adversarial", "independent", "collaborative", "full"]),
              help="Blinding mode for THIS request. May only NARROW the LBRAIN_DISCLOSURE ceiling.")
@click.option("--sealed", default=None,
              help="Adversarial mode: comma/space-separated slugs to disclose. Narrows only.")
def query(query: str, k: int, doc_type: str | None, priority: bool, rerank: bool, recency: bool,
          serve_mode: str | None, persona: str | None, disclosure: str | None, sealed: str | None):
    """Semantic + keyword hybrid search across the brain."""
    warn_if_unprovisioned()
    cfg = Config.load()
    if getattr(cfg, "amp_gating", True):
        ok, reason = amp.gate(query, getattr(cfg, "amp_min_chars", 12))
        if not ok:
            click.secho(f"[AMP gate] no memory injected — {reason}.", fg="yellow")
            return
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)
    try:
        t0 = time.monotonic()
        envelope = _resolve_envelope(cfg, disclosure, sealed)
        hits = search(cfg, store, embedder, query, k=k, doc_type=doc_type, priority_only=priority,
                      rerank=rerank, recency=recency, persona=persona, envelope=envelope)
        dt_ms = (time.monotonic() - t0) * 1000
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
        # Core first: splitting it is what discovers the core-context withholding
        # the notice must report (same ordering rule as render_response).
        core = amp.core_block(
            getattr(cfg, "core_memory_path", ""), getattr(cfg, "core_memory_chars", 900),
            envelope=getattr(hits, "envelope", None), withheld=getattr(hits, "withheld", None),
        )
        blind = blinding_notice(hits)   # prose must disclose the blinding too
        if blind:
            click.secho(blind + "\n", fg="yellow")
        if kept:
            click.secho(amp.UNTRUSTED_NOTICE, fg="red")

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
@click.option("--persona", default=None,
              help="Ask as this agent: its own DRAFT beliefs become visible. Omit and no drafts are.")
@click.option("--disclosure", default=None,
              type=click.Choice(["adversarial", "independent", "collaborative", "full"]),
              help="Blinding mode for THIS request. May only NARROW the LBRAIN_DISCLOSURE ceiling.")
@click.option("--sealed", default=None, help="Adversarial mode: slugs to disclose. Narrows only.")
def search_cmd(query: str, k: int, persona: str | None, disclosure: str | None, sealed: str | None):
    """Exact-keyword search (FTS5 only, no embeddings, no API call)."""
    warn_if_unprovisioned()
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        t0 = time.monotonic()
        hits = keyword_only(store, query, k=k, persona=persona,
                            envelope=_resolve_envelope(cfg, disclosure, sealed))
        dt_ms = (time.monotonic() - t0) * 1000
        mode, warn = resolve_mode(cfg, None)
        if warn:
            click.secho(warn, fg="yellow", nl=False)
        if mode == "structured":
            click.secho(f"--- structured serve ({dt_ms:.0f} ms) ---", fg="cyan")
            click.echo(render_response(cfg, hits, query, admissibility_on=False,
                                       include_core=False, include_provenance=False,
                                       hits_label="keyword hits"))
            return
        blind = blinding_notice(hits)
        if blind:
            click.secho(blind + "\n", fg="yellow")
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
    warn_if_unprovisioned()  # `lbrain stats` is where A-425 was observed
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
    warn_if_unprovisioned()  # "no conflicts" from an EMPTY brain is a green light
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = make_embedder(cfg)
    # Same omission as the MCP twin: this was the one retrieval path with no
    # envelope, so a standing permission scope did not apply to the tool called
    # immediately before an irreversible action.
    hits = search(cfg, store, embedder, action_text, k=k, doc_type="feedback",
                  envelope=_resolve_envelope(cfg, None, None))
    _n = blinding_notice(hits)
    if _n:
        click.echo(_n)
    rules = list(hits) + core_rules(getattr(cfg, "core_memory_path", ""))
    warnings = detect_anti_pattern(action_text, rules)
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
@click.argument("name", required=False)
@click.option("--export", "export_dir", type=click.Path(file_okay=False),
              help="Write every framework doc into DIR.")
def framework(name: str | None, export_dir: str | None):
    """Read the lair authoring framework — how to write a corpus worth ranking.

    \b
    lbrain framework                       list the docs
    lbrain framework AUTHORING_DISCIPLINE  print one
    lbrain framework --export ./lairs      write them all out

    Ranking quality is bounded by corpus quality. These ship with the tool because
    an engine without its authoring contract is half a product (A-408).
    """
    from .framework import DOCS, path, read

    if export_dir:
        dest = Path(export_dir).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        for doc in DOCS:
            (dest / f"{doc}.md").write_text(read(doc), encoding="utf-8")
        click.secho(f"✓ wrote {len(DOCS)} framework docs to {dest}", fg="green")
        return
    if name:
        key = name.removesuffix(".md").upper()
        try:
            click.echo(read(key))
        except FileNotFoundError as e:
            click.secho(f"✗ {e}", fg="red")
            sys.exit(1)
        return
    click.secho("Lair authoring framework", bold=True)
    click.echo()
    for doc, blurb in DOCS.items():
        click.secho(f"  {doc:<24}", fg="cyan", nl=False)
        click.echo(blurb)
    click.echo()
    click.echo("  lbrain framework <NAME>          print one")
    click.echo("  lbrain framework --export <DIR>  write them all out")


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
    warn_if_unprovisioned()  # A-425: an MCP server on an empty home is a confident amnesiac
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


# ==========================================================================
# beliefs — per-agent memory that accumulates without contaminating shared truth
# ==========================================================================


def _beliefs_dir(cfg) -> Path:
    d = getattr(cfg, "beliefs_dir", "") or ""
    return Path(d).expanduser() if d else (CONFIG_DIR / "beliefs")


def _belief_path(cfg, store, slug: str) -> Path | None:
    """Absolute path of a belief's source FILE.

    Resolved through the docs table (abs_path) rather than reconstructed from
    the beliefs dir: a belief may legitimately live anywhere in the corpus, and
    guessing its location would edit the wrong file — or none — while reporting
    success.
    """
    row = store.belief_row(slug)
    if row is None:
        return None
    d = store.db.execute(
        "SELECT abs_path FROM docs WHERE rel_path = ?", (row["rel_path"],)
    ).fetchone()
    return Path(d["abs_path"]) if d else None


def _rewrite_frontmatter(path: Path, updates: dict) -> None:
    """Apply frontmatter changes in place, preserving the body byte-for-byte.

    The FILE is the source of truth (corpus hierarchy: sources authoritative,
    index derivative), so every state transition must land here. Writing only
    the DB projection would produce a promotion that vanishes on the next
    `lbrain import` — a change that looks applied and is not.
    """
    import frontmatter

    post = frontmatter.load(str(path))
    for k, v in updates.items():
        if v is None:
            post.metadata.pop(k, None)
        else:
            post.metadata[k] = v
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def _open_store(cfg):
    return Store(cfg.db_path, embedding_dim=cfg.embedding_dim)


@main.group()
def belief():
    """Per-agent beliefs: draft in private, promote through a gate, retract without deleting."""
    warn_if_unprovisioned()


@belief.command(name="new")
@click.argument("slug")
@click.option("--persona", required=True, help="Which agent is claiming this.")
@click.option("--subject", required=True, help="Topic key — drives contradiction detection (G5).")
@click.option("--claim", required=True, help="The claim, in one sentence.")
@click.option("--evidence", multiple=True,
              help="A citation. Repeatable. [[slug]] for corpus, https://… for external.")
@click.option("--confidence", type=click.Choice(["low", "medium", "high"]), default="medium")
@click.option("--impact", type=click.Choice(["observation", "analysis", "action"]), default="analysis")
@click.option("--verify-by", default="", help="ISO date this claim should be re-checked.")
def belief_new(slug, persona, subject, claim, evidence, confidence, impact, verify_by):
    """Write a new DRAFT belief. Private to its persona until it passes the gate."""
    cfg = Config.load()
    d = _beliefs_dir(cfg) / persona
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug}.md"
    if path.exists():
        click.secho(f"✗ {path} already exists — edit it, or supersede it with a new slug.", fg="red")
        sys.exit(1)

    import datetime

    lines = [
        "---", f"name: {slug}", "type: belief", f"persona: {persona}",
        "state: draft", f"subject: {subject}", f"claim: {claim!r}",
        f"confidence: {confidence}", f"impact: {impact}",
        f"created: {datetime.date.today().isoformat()}",
    ]
    if verify_by:
        lines.append(f"verify_by: {verify_by}")
    lines.append("evidence:")
    for e in evidence:
        lines.append(f"  - {e!r}")
    lines += ["---", "", f"# {slug}", "", claim, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    click.secho(f"✓ draft belief written: {path}", fg="green")
    if not evidence:
        click.secho("  ⚠️  no evidence cited — this cannot pass G1/G6 until it has some.", fg="yellow")

    # Do NOT auto-append to cfg.sources. Rewriting a live brain's source list
    # without being asked is the 2026-07-27 incident; warn instead.
    covered = any(str(path.resolve()).startswith(str(Path(s).resolve())) for s in cfg.sources)
    if not covered:
        click.secho(
            f"  ⚠️  {d} is not an indexed source — this belief is invisible to retrieval.\n"
            f"     Run: lbrain add-source {_beliefs_dir(cfg)} && lbrain import && lbrain embed --stale",
            fg="yellow",
        )
    else:
        click.echo("  next: lbrain import && lbrain embed --stale")


@belief.command(name="list")
@click.option("--persona", default=None, help="Only this agent's beliefs.")
@click.option("--state", default=None,
              type=click.Choice(["draft", "promoted", "retracted", "needs_review"]))
def belief_list(persona, state):
    """List beliefs and their lifecycle state."""
    cfg = Config.load()
    store = _open_store(cfg)
    try:
        rows = [
            r for r in store.belief_rows()
            if (persona is None or r["persona"] == persona)
            and (state is None or r["state"] == state)
        ]
        if not rows:
            click.echo("  (no beliefs)")
            return
        for r in rows:
            colour = {"draft": "yellow", "promoted": "green",
                      "retracted": "red", "needs_review": "magenta"}.get(r["state"], "white")
            click.secho(f"  {r['state']:<13}", fg=colour, nl=False)
            click.echo(
                f"{r['belief_id']}  [{r['persona'] or '?'}]  subject={r['subject'] or '-'}"
            )
    finally:
        store.close()


@belief.command(name="gate")
@click.argument("slug")
def belief_gate(slug):
    """Run the promotion gate WITHOUT promoting. Prints every check and its reason."""
    from . import beliefs as _b

    cfg = Config.load()
    store = _open_store(cfg)
    try:
        view = _b.StoreCorpusView(store)
        b = view.beliefs.get(slug)
        if b is None:
            click.secho(f"✗ no belief '{slug}' in the brain (did you `lbrain import`?)", fg="red")
            sys.exit(1)
        res = _b.gate(b, view)
        for c in res.checks:
            click.secho(f"  {'PASS' if c.passed else 'FAIL'}  {c.code:<4}", 
                        fg=("green" if c.passed else "red"), nl=False)
            click.echo(c.detail)
        click.echo(f"  roots={len(res.roots)}  depth={'-' if res.depth is None else res.depth}")
        click.secho(
            "  → PROMOTABLE" if res.passed else "  → BLOCKED",
            fg=("green" if res.passed else "red"), bold=True,
        )
        sys.exit(0 if res.passed else 1)
    finally:
        store.close()


@belief.command(name="promote")
@click.argument("slug")
def belief_promote(slug):
    """Promote a draft into shared truth — only if all gate checks pass."""
    import datetime

    from . import beliefs as _b

    cfg = Config.load()
    store = _open_store(cfg)
    try:
        view = _b.StoreCorpusView(store)
        b = view.beliefs.get(slug)
        if b is None:
            click.secho(f"✗ no belief '{slug}' in the brain.", fg="red")
            sys.exit(1)
        if b.state == _b.STATE_PROMOTED:
            click.echo(f"  already promoted ({b.promoted_at or 'date not recorded'})")
            return
        res = _b.gate(b, view)
        if not res.passed:
            click.secho(f"✗ {slug} is not promotable:", fg="red")
            for c in res.failures:
                click.echo(f"    {c.code}  {c.detail}")
            sys.exit(1)
        path = _belief_path(cfg, store, slug)
        if path is None or not path.exists():
            click.secho(f"✗ source file for '{slug}' is missing — cannot record the promotion.", fg="red")
            sys.exit(1)
        today = datetime.date.today().isoformat()
        _rewrite_frontmatter(path, {"state": _b.STATE_PROMOTED, "promoted_at": today})
        with store.transaction():
            store.set_belief_state(slug, _b.STATE_PROMOTED, today)
        click.secho(f"✓ promoted {slug} — {len(res.roots)} distinct root(s), depth {res.depth}", fg="green")
        click.echo(f"  file updated: {path}")
    finally:
        store.close()


@belief.command(name="retract")
@click.argument("slug")
@click.option("--reason", required=True, help="Why it was wrong. This is the negative example — say it plainly.")
def belief_retract(slug, reason):
    """Withdraw a belief. It stays retrievable and buried; dependants are flagged for review."""
    import datetime

    from . import beliefs as _b

    cfg = Config.load()
    store = _open_store(cfg)
    try:
        view = _b.StoreCorpusView(store)
        b = view.beliefs.get(slug)
        if b is None:
            click.secho(f"✗ no belief '{slug}' in the brain.", fg="red")
            sys.exit(1)
        today = datetime.date.today().isoformat()
        path = _belief_path(cfg, store, slug)
        if path is None or not path.exists():
            click.secho(f"✗ source file for '{slug}' is missing — cannot record the retraction.", fg="red")
            sys.exit(1)
        _rewrite_frontmatter(
            path, {"state": _b.STATE_RETRACTED, "retracted_at": today, "retracted_because": reason}
        )
        targets = _b.cascade_targets(slug, view.beliefs.values())
        with store.transaction():
            store.set_belief_state(slug, _b.STATE_RETRACTED, b.promoted_at)
            for t in targets:
                tp = _belief_path(cfg, store, t)
                if tp and tp.exists():
                    _rewrite_frontmatter(
                        tp, {"state": _b.STATE_NEEDS_REVIEW, "review_because": f"rests on retracted {slug}"}
                    )
                store.set_belief_state(t, _b.STATE_NEEDS_REVIEW, "")
        click.secho(f"✓ retracted {slug} — kept and buried, not deleted.", fg="yellow")
        # Cascade REPAIR, not cascade delete: flagging is reversible, auto-retracting
        # a graph is not (doctrine rule 7). The list is the finding; a human decides.
        if targets:
            click.secho(f"  {len(targets)} dependant belief(s) flagged needs_review:", fg="magenta")
            for t in targets:
                click.echo(f"    {t}")
        else:
            click.echo("  nothing else rested on it.")
    finally:
        store.close()


# The `python -m lbrain.cli` entry point. MUST stay the last statement in this
# file: it used to sit mid-module (line 943 at d58b45f), so main() was dispatched
# before the rest of the file had been executed and EVERY command defined below it
# was silently absent — `whoami`, `resolve`, the archive group and (as written)
# `belief`. `lbrain <cmd>` worked, `python -m lbrain.cli <cmd>` answered "No such
# command", which reads as a missing feature rather than a loading order. Anomaly
# A-430; guarded by tests/test_beliefs.py::test_every_command_is_reachable_via_python_m.
if __name__ == "__main__":
    main()
