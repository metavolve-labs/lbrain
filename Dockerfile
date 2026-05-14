FROM python:3.11-slim

LABEL org.opencontainers.image.title="LBrain"
LABEL org.opencontainers.image.description="AI-native engineering memory with the Lair Protocol — Metavolve Labs"
LABEL org.opencontainers.image.source="https://github.com/codex-curator/lbrain"
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
RUN mkdir -p /data

EXPOSE 7370

# Default command: HTTP MCP server, bound on all interfaces (container-internal network only —
# expose externally via docker-compose port mapping or k8s Service).
CMD ["lbrain", "mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "7370"]
