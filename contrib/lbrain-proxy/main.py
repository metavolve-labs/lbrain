"""LBrain complimentary-API proxy — a Gemini-compatible forwarding gateway.

Holds the REAL Gemini key server-side and forwards LBrain's requests to Google,
swapping in the real key. Users authenticate with a revocable, rate-limited token
issued by you (passed by LBrain as the usual `?key=` param). The real key never
leaves this server.

Deploy to Cloud Run (see README.md). Point LBrain at it:
    lbrain init --api-base https://YOUR-PROXY/v1beta --gemini-key <USER_TOKEN>

PRIVACY NOTE: text to be embedded transits this proxy. That is the trade-off vs
bring-your-own-key. Disclose it to users; sovereignty-sensitive users should BYOK.
"""

import hmac
import os
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

REAL_KEY = os.environ["GEMINI_API_KEY"]  # the real key — server-side only, never sent to clients
# Comma-separated allowlist of user tokens you issue.
ALLOWED = {t for t in os.environ.get("LBRAIN_PROXY_TOKENS", "").split(",") if t}
RATE_PER_MIN = int(os.environ.get("LBRAIN_PROXY_RATE_PER_MIN", "120"))
UPSTREAM = os.environ.get(
    "LBRAIN_PROXY_UPSTREAM", "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")
# Only these Generative Language API method suffixes may be proxied — so a caller
# can't reach arbitrary methods the real key is authorized for.
ALLOWED_SUFFIXES = (":batchEmbedContents", ":embedContent", ":generateContent")

app = FastAPI(title="lbrain-proxy")
_hits: dict[str, deque] = defaultdict(deque)


def _client_token(request: Request) -> str:
    """User token, from the x-goog-api-key header (preferred) or legacy ?key=.
    Never logged."""
    return request.headers.get("x-goog-api-key") or request.query_params.get("key", "")


def _check(token: str) -> None:
    # Fail CLOSED: a proxy with no tokens configured serves NO ONE. (Open mode would
    # turn a first-boot / misconfigured deploy into a public relay spending the real
    # key.) Invalid tokens are rejected before any per-token state is allocated, so
    # the rate-limit dict stays bounded by len(ALLOWED).
    if not ALLOWED:
        raise HTTPException(status_code=503, detail="proxy not configured: set LBRAIN_PROXY_TOKENS")
    # Constant-time per-token compare so a valid token can't be recovered by timing.
    if not token or not any(hmac.compare_digest(token, t) for t in ALLOWED):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    now = time.time()
    dq = _hits[token]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_PER_MIN:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    dq.append(now)


@app.get("/health")
def health():
    return {"ok": True, "tokens_configured": len(ALLOWED), "rate_per_min": RATE_PER_MIN}


@app.post("/v1beta/{path:path}")
async def proxy(path: str, request: Request):
    _check(_client_token(request))
    if ".." in path or not path.endswith(ALLOWED_SUFFIXES):
        raise HTTPException(status_code=403, detail="endpoint not allowed")
    body = await request.body()
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            f"{UPSTREAM}/{path}",
            # Real key in the header, never the URL (keeps it out of upstream logs).
            headers={"Content-Type": "application/json", "x-goog-api-key": REAL_KEY},
            content=body,
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")
