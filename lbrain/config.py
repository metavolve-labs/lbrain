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


@dataclass
class Config:
    sources: list[Path] = field(default_factory=list)
    openai_api_key: str = ""
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
    db_path: Path = field(default_factory=lambda: DB_PATH)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            return cls(
                openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            )
        raw = tomllib.loads(CONFIG_PATH.read_text())
        sources = [Path(s).expanduser() for s in raw.get("sources", [])]
        api_key = raw.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        return cls(
            sources=sources,
            openai_api_key=api_key,
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
            db_path=Path(raw["db_path"]).expanduser() if "db_path" in raw else DB_PATH,
        )

    def write(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            'openai_api_key = ""  # secret lives in ~/.lbrain/env (chmod 600), never here',
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
