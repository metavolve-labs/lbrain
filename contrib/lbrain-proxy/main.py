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

import os
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

REAL_KEY = os.environ["GEMINI_API_KEY"]  # the real key — server-side only, never sent to clients
# Comma-separated allowlist of user tokens you issue. Empty = open (NOT recommended in prod).
ALLOWED = {t for t in os.environ.get("LBRAIN_PROXY_TOKENS", "").split(",") if t}
RATE_PER_MIN = int(os.environ.get("LBRAIN_PROXY_RATE_PER_MIN", "120"))
UPSTREAM = os.environ.get("LBRAIN_PROXY_UPSTREAM", "https://generativelanguage.googleapis.com/v1beta")

app = FastAPI(title="lbrain-proxy")
_hits: dict[str, deque] = defaultdict(deque)


def _check(token: str) -> None:
    if ALLOWED and token not in ALLOWED:
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
    token = request.query_params.get("key", "")
    _check(token)
    body = await request.body()
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            f"{UPSTREAM}/{path}",
            params={"key": REAL_KEY},
            content=body,
            headers={"Content-Type": "application/json"},
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")
