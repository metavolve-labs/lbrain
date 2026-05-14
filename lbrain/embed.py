"""OpenAI text-embedding-3-small client. Batched. Stateless."""

from __future__ import annotations

import os
import struct

import httpx

OPENAI_URL = "https://api.openai.com/v1/embeddings"


class EmbedClient:
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
            for d in data:
                vec = d["embedding"]
                out.append(struct.pack(f"<{len(vec)}f", *vec))
        return out

    def embed_one(self, text: str) -> bytes:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()
