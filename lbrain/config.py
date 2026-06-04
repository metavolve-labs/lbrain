"""Config — single source of truth, lives at ~/.lbrain/config.toml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 backport

CONFIG_DIR = Path(os.environ.get("LBRAIN_HOME", Path.home() / ".lbrain"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
ENV_PATH = CONFIG_DIR / "env"
DB_PATH = CONFIG_DIR / "brain.db"


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
    Keeps credentials out of the world-readable config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
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
    ENV_PATH.write_text("\n".join(lines) + "\n")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass


_load_env_file()


def archive_passphrase() -> str:
    """The Tier-2 archive passphrase. Sourced from ~/.lbrain/env (chmod 600), NEVER
    config.toml. The env value may be the literal passphrase OR a runtime reference
    ``gcp-secret:<project>/<secret>`` (resolved from GCP Secret Manager, like the wallet)
    so the actual secret lives only in IAM-controlled storage and the local file holds a
    pointer. Empty if unset; callers prompt interactively."""
    val = os.environ.get("LBRAIN_ARCHIVE_PASSPHRASE", "").strip()
    if val.startswith(("gcp-secret:", "gcp:")):
        from .archive import _fetch_gcp_secret  # lazy: avoids import cycle

        body = val.split(":", 1)[1]
        if "/" not in body:
            return ""
        project, secret = body.split("/", 1)
        return _fetch_gcp_secret(project, secret).strip()
    return val


def set_archive_passphrase(passphrase: str) -> None:
    """Persist the archive passphrase to the 600 env file (same secret pattern as keys)."""
    _write_env_var("LBRAIN_ARCHIVE_PASSPHRASE", passphrase)
    os.environ["LBRAIN_ARCHIVE_PASSPHRASE"] = passphrase


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
    wikilink_boost: float = 1.15
    bm25_weight: float = 0.4  # retained for back-compat; unused since RRF fusion
    vector_weight: float = 0.6  # retained for back-compat; unused since RRF fusion
    rrf_k: int = 60  # Reciprocal Rank Fusion smoothing constant (higher = flatter)
    contextual_prefix: bool = False  # prepend doc macro-context to each chunk's embed/FTS text
    # --- temporal dynamics (Tier 2a) — gentle, bounded; priority docs exempt ---
    temporal_decay: bool = False  # apply freshness + salience factor and record retrievals
    recency_weight: float = 0.15  # max ± lift from doc freshness (mtime half-life)
    salience_weight: float = 0.10  # max lift from retrieval frequency (reinforce-on-use)
    decay_half_life_days: float = 120.0  # freshness half-life
    # --- associative memory (Tier 2b) — Hebbian co-retrieval + spreading activation ---
    hebbian: bool = False  # learn co-retrieval edges and spread activation across them
    spread_weight: float = 0.5  # how strongly an associated doc inherits a seed's score
    assoc_min_strength: float = 2.0  # only spread along edges co-retrieved >= this many times
    max_injected: int = 3  # cap associatively-recalled docs that didn't directly match
    # --- cross-encoder reranking (Tier 2c) — optional, lightweight, flag-gated ---
    rerank: bool = False  # second-stage cross-encoder precision reorder of top candidates
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"  # fastembed name; ST maps automatically
    rerank_top_n: int = 30  # rerank this many fused candidates before final top-k
    # --- consolidation layer (Tier 3) — dense summary memories ---
    use_summaries: bool = False  # surface the most relevant dense abstraction ahead of fragments
    summary_max_dist: float = 0.55  # only surface a summary this cosine-close to the query
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
            wikilink_boost=raw.get("wikilink_boost", cls.wikilink_boost),
            bm25_weight=raw.get("bm25_weight", cls.bm25_weight),
            vector_weight=raw.get("vector_weight", cls.vector_weight),
            rrf_k=raw.get("rrf_k", cls.rrf_k),
            contextual_prefix=raw.get("contextual_prefix", cls.contextual_prefix),
            temporal_decay=raw.get("temporal_decay", cls.temporal_decay),
            recency_weight=raw.get("recency_weight", cls.recency_weight),
            salience_weight=raw.get("salience_weight", cls.salience_weight),
            decay_half_life_days=raw.get("decay_half_life_days", cls.decay_half_life_days),
            hebbian=raw.get("hebbian", cls.hebbian),
            spread_weight=raw.get("spread_weight", cls.spread_weight),
            assoc_min_strength=raw.get("assoc_min_strength", cls.assoc_min_strength),
            max_injected=raw.get("max_injected", cls.max_injected),
            rerank=raw.get("rerank", cls.rerank),
            rerank_model=raw.get("rerank_model", cls.rerank_model),
            rerank_top_n=raw.get("rerank_top_n", cls.rerank_top_n),
            use_summaries=raw.get("use_summaries", cls.use_summaries),
            summary_max_dist=raw.get("summary_max_dist", cls.summary_max_dist),
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
            f"wikilink_boost = {self.wikilink_boost}",
            f"bm25_weight = {self.bm25_weight}",
            f"vector_weight = {self.vector_weight}",
            f"rrf_k = {self.rrf_k}",
            f"contextual_prefix = {str(self.contextual_prefix).lower()}",
            f"temporal_decay = {str(self.temporal_decay).lower()}",
            f"recency_weight = {self.recency_weight}",
            f"salience_weight = {self.salience_weight}",
            f"decay_half_life_days = {self.decay_half_life_days}",
            f"hebbian = {str(self.hebbian).lower()}",
            f"spread_weight = {self.spread_weight}",
            f"assoc_min_strength = {self.assoc_min_strength}",
            f"max_injected = {self.max_injected}",
            f"rerank = {str(self.rerank).lower()}",
            f'rerank_model = "{self.rerank_model}"',
            f"rerank_top_n = {self.rerank_top_n}",
            f"use_summaries = {str(self.use_summaries).lower()}",
            f"summary_max_dist = {self.summary_max_dist}",
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
        except OSError:
            pass
        # The secret never goes into the (potentially world-readable) config above.
        # Persist it to ~/.lbrain/env (chmod 600); _load_env_file() reads it at import.
        if self.openai_api_key:
            _write_env_var("OPENAI_API_KEY", self.openai_api_key)
        if self.gemini_api_key:
            _write_env_var("GEMINI_API_KEY", self.gemini_api_key)
