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
    bm25_weight: float = 0.4
    vector_weight: float = 0.6
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
            db_path=Path(raw["db_path"]).expanduser() if "db_path" in raw else DB_PATH,
        )

    def write(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            f'openai_api_key = "{self.openai_api_key}"',
            f'embedding_model = "{self.embedding_model}"',
            f"embedding_dim = {self.embedding_dim}",
            f"chunk_tokens = {self.chunk_tokens}",
            f"chunk_overlap = {self.chunk_overlap}",
            f"priority_boost = {self.priority_boost}",
            f"wikilink_boost = {self.wikilink_boost}",
            f"bm25_weight = {self.bm25_weight}",
            f"vector_weight = {self.vector_weight}",
            f'db_path = "{self.db_path}"',
            "sources = [",
        ]
        for s in self.sources:
            lines.append(f'  "{s}",')
        lines.append("]")
        CONFIG_PATH.write_text("\n".join(lines) + "\n")
