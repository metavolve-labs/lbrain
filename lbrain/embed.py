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


class LocalEmbedClient:
    """On-device embeddings via fastembed (ONNX, no torch, no network, no API key).

    This is the zero-friction path: a new user gets a working brain from
    `pip install lbrain[local]` with no credential step at all, and their text never
    leaves the machine — the privacy claim becomes structural rather than promissory.

    Default model is BAAI/bge-small-en-v1.5 (384-dim, ~67 MB) — small enough that the
    first-run download is not an install blocker. Vectors are L2-normalized to match
    the hosted clients so cosine math is provider-agnostic.

    NOTE: 384 != the hosted default of 1536. Switching an EXISTING brain to this
    provider is a re-embed migration, not a config flip; `Store.embedding_config_status`
    reports `dim_changed` and the vec tables must be rebuilt. Fresh installs are
    unaffected.
    """

    DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
    DEFAULT_DIM = 384

    def __init__(self, model: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM):
        try:
            from fastembed import TextEmbedding
        except ModuleNotFoundError as e:  # pragma: no cover - install-path guidance
            raise RuntimeError(
                "local embeddings need the extra: pip install 'lbrain[local]'"
            ) from e
        if model.startswith(("text-embedding", "gemini-")):  # stale hosted model in config
            model = self.DEFAULT_MODEL
        self.model = model
        self.dim = dim
        # 2026-11-14 (outsider path, Sostra item 3): fastembed's default cache is /tmp/fastembed_cache, which a reboot
        # empties, so the "fetched once, never again" promise broke. The model lives under the brain home.
        home = os.environ.get("LBRAIN_HOME") or os.path.join(os.path.expanduser("~"), ".lbrain")
        cache_dir = os.environ.get("LBRAIN_MODEL_CACHE") or os.path.join(home, "models")
        # 2026-11-15 (M: the LCB Claude smoke died at `lbrain embed` in a FRESH home with no network fetch): a home whose
        # models/ holds nothing falls back to a cache that already has the model (fastembed's legacy /tmp/fastembed_cache,
        # or the default home's models/) instead of failing. An explicit LBRAIN_MODEL_CACHE is never overridden.
        home_cache_empty = not (os.path.isdir(cache_dir) and os.listdir(cache_dir))
        if not os.environ.get("LBRAIN_MODEL_CACHE") and home_cache_empty:
            # Seed the home from a cache that already holds the model (copy, never point at it): the home stays
            # self-sufficient, `init`'s "cached under <home>/models" stays true, and deleting the shared /tmp copy
            # later (as the outsider guide tells 0.1.10 users to) breaks nothing. (Sostra, 2026-11-15T09:27Z.)
            import shutil
            for legacy in (os.path.join(os.path.expanduser("~"), ".lbrain", "models"), "/tmp/fastembed_cache"):
                if os.path.isdir(legacy) and os.listdir(legacy) and os.path.realpath(legacy) != os.path.realpath(cache_dir):
                    try:
                        # symlinks=True: the cache's snapshot files are links to its blobs; following them stored
                        # every model file twice (FX-123, measured 2026-11-15).
                        shutil.copytree(legacy, cache_dir, dirs_exist_ok=True, symlinks=True)
                    except OSError:
                        cache_dir = legacy  # copy refused (space, perms): use the legacy copy rather than fail
                    break
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_dir = cache_dir
        self._enc = TextEmbedding(model_name=model, cache_dir=cache_dir)

    def embed(self, texts: list[str], batch_size: int = 64) -> list[bytes]:
        out: list[bytes] = []
        for vec in self._enc.embed(texts, batch_size=batch_size):
            v = [float(x) for x in vec]
            if len(v) != self.dim:
                raise RuntimeError(
                    f"{self.model} returned a {len(v)}-dim vector, expected {self.dim} "
                    f"— set embedding_dim to {len(v)} in ~/.lbrain/config.toml"
                )
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append(struct.pack(f"<{len(v)}f", *[x / norm for x in v]))
        if len(out) != len(texts):
            raise RuntimeError(f"local embedder returned {len(out)} vectors for {len(texts)} inputs")
        return out

    def embed_one(self, text: str) -> bytes:
        return self.embed([text])[0]

    def close(self) -> None:
        return None


KNOWN_PROVIDERS = ("local", "gemini", "openai")


class UnknownProviderError(ValueError):
    """`embedding_provider` is not one we recognise.

    Raised instead of guessing. See make_embedder for why this is fatal.
    """


def make_embedder(cfg):
    """Factory: return the embedder for the configured provider.

    FAIL-CLOSED. An unrecognised provider raises; it must never resolve to a
    hosted one. Until 2026-08-03 this function ended with an unconditional
    `return EmbedClient(...)` — the OpenAI client — so any value that was not
    `local` or `gemini` fell through to it. A single transposed character in
    `config.toml` ("gemni", "Local", "gemini ") silently shipped the user's
    entire corpus to a third party, with no error and nothing in `doctor` to
    show for it. That is the exact opposite of the promise on README.md:30,
    and it is the failure mode doctrine calls out: a default that wires an
    external side effect is itself a bug — defaults must be empty and
    fail-loud, never plausible.

    Found by the CSO session's fresh-machine audit (S8), 2026-08-03.
    """
    provider = getattr(cfg, "embedding_provider", "gemini")
    if provider == "local":
        model = cfg.embedding_model or LocalEmbedClient.DEFAULT_MODEL
        return LocalEmbedClient(model, cfg.embedding_dim)
    if provider == "gemini":
        model = cfg.embedding_model or "gemini-embedding-001"
        if model.startswith("text-embedding"):  # stale OpenAI default in config
            model = "gemini-embedding-001"
        return GeminiEmbedClient(
            cfg.gemini_api_key, model, cfg.embedding_dim,
            base_url=getattr(cfg, "gemini_base_url", GEMINI_BASE),
        )
    if provider == "openai":
        return EmbedClient(cfg.openai_api_key, cfg.embedding_model, cfg.embedding_dim)
    raise UnknownProviderError(
        f"embedding_provider = {provider!r} is not recognised "
        f"(expected one of: {', '.join(KNOWN_PROVIDERS)}).\n"
        f"Refusing to guess: an unrecognised provider previously fell through "
        f"to OpenAI, which would send your documents off this machine.\n"
        f"Fix the value in your config.toml, or run: lbrain init --provider local"
    )
