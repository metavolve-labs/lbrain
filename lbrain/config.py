"""Config — single source of truth, lives at ~/.lbrain/config.toml."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 backport

CONFIG_DIR = Path(os.environ.get("LBRAIN_HOME", Path.home() / ".lbrain"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
ENV_PATH = CONFIG_DIR / "env"
DB_PATH = CONFIG_DIR / "brain.db"


def _validate_base_url(url: str) -> str:
    """Embeddings (and their text + key) must go over TLS to a real host. Reject a
    poisoned/plaintext ``gemini_base_url`` that would exfiltrate to http:// or a
    schemeless target — but allow http://localhost for local proxy development."""
    p = urlparse(url or "")
    if p.scheme == "https" and p.hostname:
        return url
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"):
        return url
    raise ValueError(
        f"gemini_base_url must be https:// (or http://localhost for dev) — got {url!r}"
    )


def _load_env_file() -> None:
    """Load ~/.lbrain/env into os.environ if present. Called at module import time
    so subprocess launches (MCP server) inherit credentials without shell sourcing."""
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


def _write_env_var(key: str, value: str) -> None:
    """Upsert KEY=value into ~/.lbrain/env with 0600 perms (the secret store).
    Keeps credentials out of the world-readable config.toml.

    The secret bytes are written through a file descriptor opened O_CREAT with mode
    0600 *up front* (and via a same-dir temp file + atomic os.replace), so the secret
    never exists on disk under the process umask (commonly 0644 = world-readable)
    even for the instant between create and a later chmod. Closes the TOCTOU window
    where another local user could read the file during that gap."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)  # the secret's parent dir, not just the file
    except OSError:
        pass
    lines, found = [], False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    body = ("\n".join(lines) + "\n").encode("utf-8")

    tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    try:
        # O_CREAT with 0600 means the file is private from the moment it exists.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)  # belt-and-suspenders if umask widened the create mode
        os.replace(tmp, ENV_PATH)  # atomic swap — readers see old or new, never partial
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        # Don't crash, but DON'T stay silent — the documented 600 guarantee did not
        # hold (e.g. a filesystem that ignores Unix perms), so the secret may be
        # world-readable. The operator needs to know.
        print(
            f"[lbrain] WARNING: could not securely write {ENV_PATH} ({e}); "
            "the secret file may be readable by other users on this filesystem.",
            file=sys.stderr,
        )


_load_env_file()


# NOTE: archive passphrase resolution (archive_passphrase / set_archive_passphrase)
# lives in the optional subpackage at lbrain.archive.config — the core config layer
# has no archive-specific behavior. The arweave_* fields below are passive data the
# archive layer reads (kept here so Config.write persists them round-trip).


@dataclass
class Config:
    sources: list[Path] = field(default_factory=list)
    embedding_provider: str = "openai"  # "openai" | "gemini" (GCP-native)
    openai_api_key: str = ""
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"  # override → proxy/self-host
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    priority_boost: float = 1.3
    rrf_k: int = 60  # Reciprocal Rank Fusion smoothing constant (higher = flatter)
    contextual_prefix: bool = False  # prepend doc macro-context to each chunk's embed/FTS text
    # --- AMP (Augmented Memory Protocol) injection layer — gating, budgeting, provenance ---
    amp_gating: bool = True  # skip injection for trivial/low-signal queries (Gate 1)
    amp_min_chars: int = 3  # gate only empty/near-empty queries (content-driven, not length)
    amp_budget_chars: int = 6000  # injection budget (~1.5k tokens); 0 = unbudgeted
    amp_per_chunk_chars: int = 360  # max preview chars per injected hit
    amp_provenance: bool = True  # append an auditable injection-metadata footer
    # --- core memory (Letta-style always-on context) ---
    core_memory_path: str = ""  # markdown file injected ahead of every query (empty = off)
    core_memory_chars: int = 900  # budget for the always-on core block
    # --- supersession-aware retrieval (Zep-inspired) — bury superseded, surface live truth ---
    supersede_aware: bool = True  # de-rank docs another doc explicitly supersedes
    supersede_penalty: float = 0.25  # multiplicative score penalty for a superseded doc
    # --- Tier 2: permanent verifiable archive (Arweave substrate) ---
    arweave_enabled: bool = False  # opt-in to real permaweb writes (else offline local store)
    arweave_transport: str = "local"  # "local" (offline, content-addressed) | "arweave"/"l1"
    arweave_wallet_path: str = ""  # JWK path; secret routed to ~/.lbrain/env, not plaintext
    arweave_gateway: str = "https://arweave.net"  # fetch/GraphQL gateway
    turbo_endpoint: str = "https://turbo.ardrive.io"  # ar.io Turbo (v2 bundled uploads)
    archive_namespace: str = "private"  # default silo: private working/research memory
    db_path: Path = field(default_factory=lambda: DB_PATH)

    def __post_init__(self):
        # Validate on every construction — catches a poisoned GEMINI_BASE_URL env
        # var or config value before it can redirect embeddings off-TLS.
        self.gemini_base_url = _validate_base_url(self.gemini_base_url)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            return cls(
                openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
                gemini_api_key=os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GEMINI_3_API_KEY", ""),
                gemini_base_url=os.environ.get("GEMINI_BASE_URL", cls.gemini_base_url),
            )
        raw = tomllib.loads(CONFIG_PATH.read_text())
        sources = [Path(s).expanduser() for s in raw.get("sources", [])]
        api_key = raw.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        gemini_key = (
            raw.get("gemini_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GEMINI_3_API_KEY", "")
        )
        return cls(
            sources=sources,
            embedding_provider=raw.get("embedding_provider", cls.embedding_provider),
            openai_api_key=api_key,
            gemini_api_key=gemini_key,
            gemini_base_url=raw.get("gemini_base_url")
            or os.environ.get("GEMINI_BASE_URL")
            or cls.gemini_base_url,
            embedding_model=raw.get("embedding_model", cls.embedding_model),
            embedding_dim=raw.get("embedding_dim", cls.embedding_dim),
            chunk_tokens=raw.get("chunk_tokens", cls.chunk_tokens),
            chunk_overlap=raw.get("chunk_overlap", cls.chunk_overlap),
            priority_boost=raw.get("priority_boost", cls.priority_boost),
            rrf_k=raw.get("rrf_k", cls.rrf_k),
            contextual_prefix=raw.get("contextual_prefix", cls.contextual_prefix),
            amp_gating=raw.get("amp_gating", cls.amp_gating),
            amp_min_chars=raw.get("amp_min_chars", cls.amp_min_chars),
            amp_budget_chars=raw.get("amp_budget_chars", cls.amp_budget_chars),
            amp_per_chunk_chars=raw.get("amp_per_chunk_chars", cls.amp_per_chunk_chars),
            amp_provenance=raw.get("amp_provenance", cls.amp_provenance),
            core_memory_path=raw.get("core_memory_path", cls.core_memory_path),
            core_memory_chars=raw.get("core_memory_chars", cls.core_memory_chars),
            supersede_aware=raw.get("supersede_aware", cls.supersede_aware),
            supersede_penalty=raw.get("supersede_penalty", cls.supersede_penalty),
            arweave_enabled=raw.get("arweave_enabled", cls.arweave_enabled),
            arweave_transport=raw.get("arweave_transport", cls.arweave_transport),
            arweave_wallet_path=raw.get("arweave_wallet_path")
            or os.environ.get("LBRAIN_ARWEAVE_WALLET", cls.arweave_wallet_path),
            arweave_gateway=raw.get("arweave_gateway", cls.arweave_gateway),
            turbo_endpoint=raw.get("turbo_endpoint", cls.turbo_endpoint),
            archive_namespace=raw.get("archive_namespace", cls.archive_namespace),
            db_path=Path(raw["db_path"]).expanduser() if "db_path" in raw else DB_PATH,
        )

    def write(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            f'embedding_provider = "{self.embedding_provider}"',
            'openai_api_key = ""  # secret lives in ~/.lbrain/env (chmod 600), never here',
            'gemini_api_key = ""  # secret lives in ~/.lbrain/env (chmod 600), never here',
            f'gemini_base_url = "{self.gemini_base_url}"',
            f'embedding_model = "{self.embedding_model}"',
            f"embedding_dim = {self.embedding_dim}",
            f"chunk_tokens = {self.chunk_tokens}",
            f"chunk_overlap = {self.chunk_overlap}",
            f"priority_boost = {self.priority_boost}",
            f"rrf_k = {self.rrf_k}",
            f"contextual_prefix = {str(self.contextual_prefix).lower()}",
            f"amp_gating = {str(self.amp_gating).lower()}",
            f"amp_min_chars = {self.amp_min_chars}",
            f"amp_budget_chars = {self.amp_budget_chars}",
            f"amp_per_chunk_chars = {self.amp_per_chunk_chars}",
            f"amp_provenance = {str(self.amp_provenance).lower()}",
            f'core_memory_path = "{self.core_memory_path}"',
            f"core_memory_chars = {self.core_memory_chars}",
            f"supersede_aware = {str(self.supersede_aware).lower()}",
            f"supersede_penalty = {self.supersede_penalty}",
            f"arweave_enabled = {str(self.arweave_enabled).lower()}",
            f'arweave_transport = "{self.arweave_transport}"',
            f'arweave_wallet_path = "{self.arweave_wallet_path}"',
            f'arweave_gateway = "{self.arweave_gateway}"',
            f'turbo_endpoint = "{self.turbo_endpoint}"',
            f'archive_namespace = "{self.archive_namespace}"',
            f'db_path = "{self.db_path}"',
            "sources = [",
        ]
        for s in self.sources:
            lines.append(f'  "{s}",')
        lines.append("]")
        CONFIG_PATH.write_text("\n".join(lines) + "\n")
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError as e:
            print(f"[lbrain] WARNING: could not chmod 600 {CONFIG_PATH} ({e}).", file=sys.stderr)
        # The secret never goes into the (potentially world-readable) config above.
        # Persist it to ~/.lbrain/env (chmod 600); _load_env_file() reads it at import.
        if self.openai_api_key:
            _write_env_var("OPENAI_API_KEY", self.openai_api_key)
        if self.gemini_api_key:
            _write_env_var("GEMINI_API_KEY", self.gemini_api_key)
