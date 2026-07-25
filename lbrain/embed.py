"""Embedding clients. Batched, stateless. Gemini (GCP-native, default) or OpenAI.

The active provider is chosen by `Config.embedding_provider` via `make_embedder`.
Both clients return little-endian f32 blobs (sqlite-vec wire format) at the same
dimension, so the vec tables and all downstream math are provider-agnostic.
"""

from __future__ import annotations

import math
import os
import struct

import httpx

OPENAI_URL = "https://api.openai.com/v1/embeddings"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class EmbedClient:
    """OpenAI text-embedding-3-small (or compatible)."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", dim: int = 1536):
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set — pass via env or `lbrain init --api-key=...`"
            )
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self._client = httpx.Client(timeout=60.0)

    def embed(self, texts: list[str], batch_size: int = 96) -> list[bytes]:
        """Returns one little-endian f32 blob per text (sqlite-vec wire format)."""
        out: list[bytes] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            r = self._client.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": batch, "dimensions": self.dim},
            )
            r.raise_for_status()
            data = r.json()["data"]
            if len(data) != len(batch):
                raise RuntimeError(
                    f"OpenAI returned {len(data)} embeddings for {len(batch)} inputs"
                )
            # Responses are not order-guaranteed; align by index before packing.
            data.sort(key=lambda d: d.get("index", 0))
            for d in data:
                vec = d["embedding"]
                if len(vec) != self.dim:
                    raise RuntimeError(
                        f"OpenAI returned a {len(vec)}-dim vector, expected {self.dim}"
                    )
                out.append(struct.pack(f"<{len(vec)}f", *vec))
        return out

    def embed_one(self, text: str) -> bytes:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()


class GeminiEmbedClient:
    """GCP-native Gemini embeddings (gemini-embedding-001) via the Generative
    Language API. API-key auth (no ADC), so the local hot path has no token-
    refresh dependency. Matryoshka (MRL) truncation gives the requested `dim`;
    truncated outputs are NOT unit-norm, so we L2-normalize to match OpenAI's
    normalized vectors and keep cosine/clustering math consistent."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        dim: int = 1536,
        task_type: str = "SEMANTIC_SIMILARITY",
        base_url: str = GEMINI_BASE,
    ):
        if not api_key:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_3_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set — add it to ~/.lbrain/env "
                "(GEMINI_API_KEY=...) or sourced from Secret Manager."
            )
        self.api_key = api_key
        self.model = model if model.startswith("models/") else f"models/{model}"
        self.dim = dim
        self.task_type = task_type
        self.base_url = (base_url or GEMINI_BASE).rstrip("/")
        self._client = httpx.Client(timeout=60.0)

    def embed(self, texts: list[str], batch_size: int = 100) -> list[bytes]:
        out: list[bytes] = []
        url = f"{self.base_url}/{self.model}:batchEmbedContents"
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            reqs = [
                {
                    "model": self.model,
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": self.dim,
                    "taskType": self.task_type,
                }
                for t in batch
            ]
            # Key travels in the x-goog-api-key HEADER, never the URL query string:
            # on an HTTP error httpx's exception message embeds the request URL, so a
            # ?key= param would leak the live key into any log that records the error
            # (e.g. the capture hook's stderr redirect).
            r = self._client.post(
                url, headers={"x-goog-api-key": self.api_key}, json={"requests": reqs}
            )
            r.raise_for_status()
            embs = r.json()["embeddings"]
            if len(embs) != len(batch):
                raise RuntimeError(
                    f"Gemini returned {len(embs)} embeddings for {len(batch)} inputs"
                )
            for e in embs:
                vec = e["values"]
                if len(vec) != self.dim:
                    raise RuntimeError(
                        f"Gemini returned a {len(vec)}-dim vector, expected {self.dim}"
                    )
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                vec = [x / norm for x in vec]
                out.append(struct.pack(f"<{len(vec)}f", *vec))
        return out

    def embed_one(self, text: str) -> bytes:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()


def make_embedder(cfg):
    """Factory: return the embedder for the configured provider."""
    provider = getattr(cfg, "embedding_provider", "gemini")
    if provider == "gemini":
        model = cfg.embedding_model or "gemini-embedding-001"
        if model.startswith("text-embedding"):  # stale OpenAI default in config
            model = "gemini-embedding-001"
        return GeminiEmbedClient(
            cfg.gemini_api_key, model, cfg.embedding_dim,
            base_url=getattr(cfg, "gemini_base_url", GEMINI_BASE),
        )
    return EmbedClient(cfg.openai_api_key, cfg.embedding_model, cfg.embedding_dim)
