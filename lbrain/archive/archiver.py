"""Tier-2 — permanent, verifiable, encrypted episodic archive (Arweave substrate).

LBrain's Tier-1 is the hot retrieval/curation layer (sqlite-vec + FTS5 + the Lair
Protocol): "selectivity at the surface." This module is Tier-2: "permanence at the
substrate." The policy is fixed:

    STORE FULL, READ SNAPSHOT.

- The FULL session is encrypted (``crypto.encrypt``) and written to a transport
  (Arweave for real use; a content-addressed local store for the offline MVP / tests).
  Storage is cheap and the ground truth must survive intact — a summary you cannot audit
  against the original is just another hallucination, which would defeat *verifiable* memory.
- A structured-bullet SNAPSHOT (~20-30%) is mirrored into LBrain's index with its own
  embedding (the card-catalog entry). Retrieval reads the cheap snapshot; deep-recall /
  audit fetches the full session by txid and decrypts it.

Transports are pluggable so the entire round-trip (encrypt → archive → index → recall →
fetch-by-txid → decrypt → byte-identical → crypto-shred) is verifiable with zero cost or
network, while the real Arweave write is the same code path with the wallet configured.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import crypto
from .crypto import Keystore

# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def _content_txid(data: bytes) -> str:
    """Arweave-shaped id (43-char base64url of a 32-byte hash). Content-addressed,
    so the local transport is deterministic and naturally de-duplicates."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _ascii_tag(s: str) -> str:
    """arweave-python's ``encode_tag`` hardcodes ``.encode('ascii')``, so a single
    non-ASCII char in a tag name/value (e.g. an em-dash in a session title) raises
    UnicodeEncodeError and aborts the entire upload. Transliterate to a safe ASCII
    form here: the tag is only the GraphQL-queryable index handle, and the full
    Unicode title also lives inside the encrypted payload, so nothing is lost."""
    import unicodedata

    subs = {
        "—": "-", "–": "-",        # em / en dash
        "‘": "'", "’": "'",        # smart single quotes
        "“": '"', "”": '"',        # smart double quotes
        "…": "...", " ": " ",      # ellipsis, nbsp
    }
    s = "".join(subs.get(ch, ch) for ch in s)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


class LocalTransport:
    """Offline, content-addressed blob store at ``~/.lbrain/archive/<txid>.bin``.

    Stands in for the permaweb so the full verifiable loop runs with no wallet, no
    network, no cost — the MVP the handoff's Definition of Done is written against.
    The id is the sha256 of the ciphertext, so a write is verifiable and idempotent.
    """

    name = "local"

    def __init__(self, archive_dir: Path):
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, tags: dict[str, str]) -> str:
        txid = _content_txid(data)
        (self.archive_dir / f"{txid}.bin").write_bytes(data)
        # Tags travel beside the blob so the local store mirrors Arweave's GraphQL-tag model.
        (self.archive_dir / f"{txid}.tags.json").write_text(json.dumps(tags, indent=2))
        return txid

    def get(self, txid: str) -> bytes:
        p = self.archive_dir / f"{txid}.bin"
        if not p.exists():
            raise FileNotFoundError(f"no local archive blob for txid {txid}")
        return p.read_bytes()


def _load_arweave_wallet(wallet_ref: str):
    """Resolve an Arweave wallet from either a local JWK file or a runtime secret ref.

    Supported ``wallet_ref`` forms:
      - ``gcp-secret:<project>/<secret>`` (or ``gcp:…``) — fetch the JWK from GCP Secret
        Manager at runtime and build the wallet IN MEMORY (``Wallet.from_data``). The
        private key is never written to disk (e.g. ``gcp-secret:my-project/my-arweave-jwk``).
      - a filesystem path to a JWK json.
    """
    import json

    from arweave import Wallet

    ref = (wallet_ref or "").strip()
    if ref.startswith(("gcp-secret:", "gcp:")):
        body = ref.split(":", 1)[1]
        if "/" not in body:
            raise RuntimeError(f"bad secret ref {ref!r} — expected gcp-secret:<project>/<secret>")
        project, secret = body.split("/", 1)
        jwk_str = _fetch_gcp_secret(project, secret)
        return Wallet.from_data(json.loads(jwk_str))
    p = Path(ref).expanduser()
    if not ref or not p.exists():
        raise RuntimeError(
            f"Arweave wallet not found at {ref!r}. Set arweave_wallet_path in config "
            "(a JWK path or 'gcp-secret:<project>/<secret>') or LBRAIN_ARWEAVE_WALLET in ~/.lbrain/env."
        )
    return Wallet(str(p))


def _fetch_gcp_secret(project: str, secret: str) -> str:
    """Read a secret's latest version. Prefers the Python client; falls back to gcloud."""
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # mute google.api_core py-version FutureWarning
            from google.cloud import secretmanager  # type: ignore

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{secret}/versions/latest"
        return client.access_secret_version(name=name).payload.data.decode("utf-8")
    except Exception:
        import subprocess

        out = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={secret}", f"--project={project}"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise RuntimeError(
                f"could not fetch secret {secret} from {project}: {out.stderr.strip()}"
            )
        return out.stdout


class ArweaveL1Transport:
    """Real permaweb writes via direct Arweave L1 (arweave-python-client).

    Direct-L1 permaweb archival. Signs with the wallet JWK, tags
    the tx (GraphQL-queryable), and posts it. NOTE: L1 settlement costs AR — for the
    text-sized sessions this archive targets that is pennies, but a funded wallet is
    required, so this path is exercised in real use, not in the offline test suite.
    (Turbo / up.arweave.net free <100 KiB ANS-104 bundling is a v2 optimization — it
    needs data-item signing this client doesn't expose; the layers above are identical.)
    """

    name = "arweave-l1"

    def __init__(self, wallet_path: str, gateway: str = "https://arweave.net",
                 archive_dir: Path | None = None):
        # Load the wallet LAZILY: only uploads (put) need it. Retrieval (get) is a plain
        # gateway fetch, so reads must not depend on wallet/secret availability or auth.
        self._wallet_path = wallet_path
        self._wallet = None
        self.gateway = gateway.rstrip("/")
        # Optional on-disk mirror. When set, put() drops a copy and get() serves from it
        # first — so a txid this node captured is retrievable offline / instantly, even
        # before gateway propagation, and local-only "ghost" txids resolve instead of 404.
        self.archive_dir = Path(archive_dir) if archive_dir else None

    @property
    def wallet(self):
        if self._wallet is None:
            self._wallet = _load_arweave_wallet(self._wallet_path)
        return self._wallet

    def put(self, data: bytes, tags: dict[str, str]) -> str:
        # Lazily imported — optional dep at runtime.
        import io
        from arweave import Transaction
        from arweave.transaction_uploader import get_uploader

        tx = Transaction(self.wallet, data=data)
        for k, v in tags.items():
            # ASCII-sanitize: the lib's encode_tag can't handle non-ASCII (see _ascii_tag).
            tx.add_tag(_ascii_tag(str(k)), _ascii_tag(str(v)))
        # The uploader streams merkle chunk proofs computed from the tx's file_handler
        # over the RAW bytes (self.data is base64-encoded, so it can't be used here).
        # An in-memory data= tx has no handler, so point one at the bytes and prepare
        # the chunks BEFORE signing — sign() commits to the data_root that
        # prepare_chunks() sets, so the order matters.
        tx.file_handler = io.BytesIO(data)
        tx.prepare_chunks()
        tx.sign()

        # CHUNKED upload, not a single tx.send(): tx.send() posts the entire tx —
        # header AND data — in one request, which 413s once a session crosses the
        # gateway's body limit (~a few hundred KB). The uploader posts the small
        # header, then streams the data in 256 KiB chunks (MAX_CHUNKS_IN_BODY=1), so
        # arbitrarily large sessions upload cleanly.
        #
        # It also closes the "ghost txid" hole: upload_chunk() retries transient
        # errors and RAISES on persistent failure, and we re-check is_complete before
        # returning — so a txid is only ever returned for data that actually landed
        # on the network, never for a failed/rejected upload.
        uploader = get_uploader(tx, io.BytesIO(data))
        guard = uploader.total_chunks + 16  # bound the loop; ~12 chunks for a 3 MB session
        while not uploader.is_complete and guard > 0:
            uploader.upload_chunk()
            guard -= 1
        if not uploader.is_complete:
            raise RuntimeError(
                "Arweave upload did not complete "
                f"({uploader.uploaded_chunks}/{uploader.total_chunks} chunks, "
                f"last HTTP {uploader.last_response_status} {uploader.last_response_error})"
            )
        # Mirror locally so the bytes are retrievable immediately, before the gateway
        # has propagated the new tx (and as a free offline cache thereafter).
        if self.archive_dir is not None:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            (self.archive_dir / f"{tx.id}.bin").write_bytes(data)
        return tx.id

    def get(self, txid: str) -> bytes:
        # Local-first: if this node holds the blob (we captured it, or it's a
        # local/ghost txid), serve it without a gateway round-trip that may 404.
        if self.archive_dir is not None:
            p = self.archive_dir / f"{txid}.bin"
            if p.exists():
                return p.read_bytes()

        import httpx

        r = httpx.get(f"{self.gateway}/{txid}", timeout=60.0, follow_redirects=True)
        r.raise_for_status()
        return r.content


def verify_on_chain(txid: str, gateway: str = "https://arweave.net", timeout: float = 20.0) -> dict:
    """Is this txid ACTUALLY settled on Arweave? Hits the gateway's
    ``/tx/<id>/status`` endpoint directly — deliberately bypassing any local
    mirror, so a local-only "ghost" txid (one that was content-addressed but never
    broadcast, or whose upload failed) reports ``settled=False`` instead of looking
    retrievable. This is the proof step the local-first ``get()`` can't give.

    Returns: ``{settled, confirmations, block_height, http, error}``. HTTP 200 = on
    chain; 202 = accepted-but-pending (not yet mined); 404 = ghost / not found.
    """
    import httpx

    url = f"{gateway.rstrip('/')}/tx/{txid}/status"
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
    except Exception as e:
        return {"settled": False, "confirmations": None, "block_height": None, "http": 0, "error": str(e)}
    if r.status_code == 200:
        try:
            j = r.json()
        except Exception:
            j = {}
        return {
            "settled": True,
            "confirmations": j.get("number_of_confirmations"),
            "block_height": j.get("block_height"),
            "http": 200,
            "error": None,
        }
    return {
        "settled": False,
        "confirmations": None,
        "block_height": None,
        "http": r.status_code,
        "error": "pending" if r.status_code == 202 else "not found",
    }


def make_transport(cfg):
    """Pick a transport from config. Defaults to the offline local store; the real
    Arweave path is opt-in (``arweave_enabled = true`` + ``arweave_transport``)."""
    from ..config import CONFIG_DIR

    transport = getattr(cfg, "arweave_transport", "local")
    if getattr(cfg, "arweave_enabled", False) and transport in ("arweave", "arweave-l1", "l1"):
        return ArweaveL1Transport(
            getattr(cfg, "arweave_wallet_path", ""),
            getattr(cfg, "arweave_gateway", "https://arweave.net"),
            archive_dir=CONFIG_DIR / "archive",
        )
    return LocalTransport(CONFIG_DIR / "archive")


# ---------------------------------------------------------------------------
# Snapshot generation (the read-time card-catalog entry)
# ---------------------------------------------------------------------------

SNAPSHOT_SYSTEM = (
    "You distill a full session/document into a DENSE structured-bullet SNAPSHOT (~20-30% "
    "of the source) for a memory index. Rules: (1) preserve every decision, date, name, "
    "identifier, address, number, and open question; (2) organize as terse labeled bullets; "
    "(3) no preamble, no filler, no hedging; (4) invent NOTHING — this snapshot is the "
    "searchable surface that points back to the full immutable record, so it must be a "
    "faithful index of it, not a paraphrase. Output concise markdown bullets."
)


def make_snapshot(text: str, cfg, *, model: str | None = None, force_extractive: bool = False) -> str:
    """Structured-bullet snapshot of a session.

    Uses the configured LLM (Gemini/OpenAI) when a key is available; falls back to a
    deterministic extractive snapshot (headings + lead sentences) so archiving — and the
    test suite — works fully offline. ``force_extractive`` skips the LLM entirely (used by
    hook-driven auto-capture so session-end is fast, free, and offline; the full record is
    stored intact, so a richer snapshot can be re-derived later)."""
    if force_extractive:
        return _extractive_snapshot(text)
    provider = getattr(cfg, "embedding_provider", "gemini")
    key = cfg.gemini_api_key if provider == "gemini" else cfg.openai_api_key
    if key:
        try:
            return _llm_snapshot(text, key, provider, model)
        except Exception:
            pass  # fall through to extractive
    return _extractive_snapshot(text)


def _llm_snapshot(text: str, key: str, provider: str, model: str | None) -> str:
    import httpx

    user = f"Distil this into a structured-bullet snapshot:\n\n{text[:120000]}"
    with httpx.Client(timeout=120.0) as client:
        if provider == "gemini":
            model = model or "gemini-2.5-flash"
            r = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},  # header, not ?key= — this runs in the capture hook whose stderr is logged
                json={
                    "systemInstruction": {"parts": [{"text": SNAPSHOT_SYSTEM}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.2},
                },
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        model = model or "gpt-4o-mini"
        r = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SNAPSHOT_SYSTEM},
                    {"role": "user", "content": user},
                ],
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def _extractive_snapshot(text: str, target_ratio: float = 0.25) -> str:
    """Deterministic, no-API fallback: keep markdown headings + the lead line of each
    block, up to ~target_ratio of the source. Faithful (verbatim extraction, invents
    nothing) — the offline analogue of the LLM snapshot."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    budget = max(int(len(text) * target_ratio), 200)
    out, used, lead_expected = [], 0, True
    for ln in lines:
        s = ln.strip()
        is_heading = s.startswith("#")
        is_struct = is_heading or s.startswith(("- ", "* ", ">")) or (s[:2].isdigit() and "." in s[:4])
        # Keep structure (headings/bullets/numbered) and the lead line of each block —
        # where a "block" starts after a blank line OR immediately after a heading.
        keep = bool(s) and (is_struct or lead_expected)
        if keep:
            out.append(ln)
            used += len(ln)
            if used >= budget:
                out.append("… [snapshot truncated — full session in the archived record]")
                break
        # Next line is a lead if this line was blank (new paragraph) or a heading (its body).
        lead_expected = (not s) or is_heading
    snap = "\n".join(out).strip()
    return snap or text[:budget]


# ---------------------------------------------------------------------------
# Archiver — orchestrates encrypt → put → snapshot → index, and the reverse
# ---------------------------------------------------------------------------


@dataclass
class ArchiveResult:
    txid: str
    namespace: str
    title: str
    n_bytes: int
    snapshot: str
    snapshot_chars: int
    transport: str
    source_hash: str = ""
    skipped: bool = False  # True when idempotent capture found this content already archived


def _source_hash(payload: bytes) -> str:
    """Stable content id of the PLAINTEXT — the dedup key for idempotent capture."""
    return hashlib.sha256(payload).hexdigest()


class Archiver:
    """Ties together crypto, the transport, the keystore, and LBrain's index."""

    def __init__(self, cfg, store, embedder=None, transport=None):
        from ..config import CONFIG_DIR
        from .storage import ArchiveStore

        self.cfg = cfg
        self.store = store
        # The archive's own storage layer (its tables + queries), sharing the core
        # Store's sqlite connection so there's a single writer. ensure_schema runs here.
        self.astore = ArchiveStore(store.db, store.embedding_dim)
        self.embedder = embedder
        # transport override lets hook-driven capture force the offline local store
        # regardless of the global config (which may be arweave-enabled).
        self.transport = transport if transport is not None else make_transport(cfg)
        self.keystore = Keystore(CONFIG_DIR / "keys")

    # ---- write path -------------------------------------------------------

    def archive(self, payload: bytes, *, title: str, passphrase: str,
                namespace: str | None = None, extra_tags: dict | None = None,
                snapshot_model: str | None = None, skip_if_exists: bool = False,
                force_extractive: bool = False) -> ArchiveResult:
        """Encrypt the full session → transport.put → wrap+store the key → snapshot →
        index the snapshot. The full record is permanent and verifiable; the snapshot is
        the cheap searchable surface that points back to it by txid.

        ``skip_if_exists`` makes capture idempotent: if this exact plaintext is already
        archived (and not shredded), return that record untouched instead of re-uploading
        (no duplicate, no second AR spend). ``force_extractive`` skips the LLM snapshot."""
        namespace = namespace or getattr(self.cfg, "archive_namespace", "private")
        src_hash = _source_hash(payload)

        if skip_if_exists:
            existing = self.astore.get_archive_by_source(src_hash)
            if existing is not None:
                return ArchiveResult(
                    txid=existing["txid"], namespace=existing["namespace"],
                    title=existing["title"], n_bytes=existing["n_bytes"],
                    snapshot=existing["snapshot"], snapshot_chars=len(existing["snapshot"]),
                    transport=existing["transport"], source_hash=src_hash, skipped=True,
                )

        payload_env, key_env = crypto.encrypt(payload, passphrase)

        tags = {
            "App-Name": "LBrain",
            "App-Version": "tier2-archive-v1",
            "Content-Type": "application/octet-stream",
            "LBrain-Namespace": namespace,
            "LBrain-Title": title[:200],
            "LBrain-Encryption": "AES-256-GCM+Argon2id",
        }
        if extra_tags:
            tags.update({str(k): str(v) for k, v in extra_tags.items()})

        txid = self.transport.put(payload_env, tags)
        # The wrapped DEK is stored LOCALLY ONLY — this is the deletable half of crypto-shred.
        self.keystore.put(txid, key_env)

        text = _decode_text(payload)
        snapshot = make_snapshot(text, self.cfg, model=snapshot_model, force_extractive=force_extractive)

        self.astore.insert_archive(
            txid=txid,
            namespace=namespace,
            title=title,
            snapshot=snapshot,
            tags=tags,
            n_bytes=len(payload),
            created=time.time(),
            transport=self.transport.name,
            source_hash=src_hash,
        )
        if self.embedder is not None:
            try:
                self.astore.write_archive_embedding(txid, self.embedder.embed_one(snapshot))
            except Exception:
                pass  # embedding is best-effort; snapshot is still FTS-searchable

        return ArchiveResult(
            txid=txid, namespace=namespace, title=title, n_bytes=len(payload),
            snapshot=snapshot, snapshot_chars=len(snapshot), transport=self.transport.name,
            source_hash=src_hash, skipped=False,
        )

    # ---- read path --------------------------------------------------------

    def retrieve(self, txid: str, passphrase: str) -> bytes:
        """Deep-recall: fetch the full encrypted record by txid and decrypt it."""
        key_env = self.keystore.get(txid)
        if key_env is None:
            raise crypto.CryptoError(
                f"no local key for {txid} — it was crypto-shredded or never archived here. "
                "The permanent ciphertext (if any) is unrecoverable without its key."
            )
        payload_env = self.transport.get(txid)
        return crypto.decrypt(payload_env, key_env, passphrase)

    def shred(self, txid: str, purge_snapshot: bool = True) -> bool:
        """Crypto-shred: destroy the local key → the permanent ciphertext is now
        unrecoverable. With ``purge_snapshot`` (default) ALSO erase the local cleartext
        snapshot + its FTS/vector rows, leaving only an audit stub — so nothing readable
        about the record survives locally. ``purge_snapshot=False`` keeps the snapshot.

        Note: this only achieves true erasure if ``~/.lbrain/keys/`` is not backed up
        elsewhere (a backed-up wrapped key + the passphrase can still recover the payload)."""
        had_key = self.keystore.shred(txid)
        self.astore.mark_archive_shredded(txid, purge_snapshot=purge_snapshot)
        return had_key


def _decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace")
