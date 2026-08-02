# Pin by digest in production for reproducible builds + supply-chain safety:
#   FROM python:3.11-slim-bookworm@sha256:<digest>
FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="LBrain"
LABEL org.opencontainers.image.description="AI-native engineering memory with the Lair Protocol — Metavolve Labs"
LABEL org.opencontainers.image.source="https://github.com/metavolve-labs/lbrain"
LABEL org.opencontainers.image.licenses="BSD-3-Clause"

WORKDIR /app

# Install build deps first (cached layer)
COPY pyproject.toml README.md ./
COPY lbrain/__init__.py ./lbrain/__init__.py
RUN pip install --no-cache-dir -e . \
    && python -c "import sqlite_vec, frontmatter, tiktoken, click, httpx, mcp; print('deps OK')"

# Copy the rest of the source
COPY lbrain/ ./lbrain/
COPY scripts/ ./scripts/

# Brain data lives outside the image — mount /data as a volume.
ENV LBRAIN_HOME=/data
# Run as a non-root user; own the data + app dirs so the volume is writable.
RUN useradd --system --create-home --uid 10001 lbrain \
    && mkdir -p /data \
    && chown -R lbrain:lbrain /data /app
USER lbrain

EXPOSE 7370

# Default command: HTTP MCP server bound to 0.0.0.0 — required so other containers
# on the Docker network (the agent sidecar) can reach it. The MCP server has NO
# built-in auth, so this is SAFE ONLY inside a trusted container network. When
# publishing the port, bind localhost (`-p 127.0.0.1:7370:7370`) or put an
# authenticated, TLS-terminating reverse proxy in front. NEVER `-p 7370:7370` on a
# public host.
CMD ["lbrain", "mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "7370"]
