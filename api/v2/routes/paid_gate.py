"""Access gate for the standalone payment form (pay.*).

The paid SPA is static files behind Caddy; Caddy calls ``GET /check`` here via
``forward_auth`` before serving ANY byte of the site. Admission chain:

  1. ``Referer`` domain is in the hand-edited landing allowlist -> allow and
     remember the client's IP in Redis (24h TTL);
  2. the IP is already remembered -> allow (TTL refreshed);
  3. a valid signed cookie is present -> allow and remember the (possibly new)
     IP — this is how a client survives an IP change;
  4. Redis is unreachable -> allow (fail-open: the gate is soft protection and
     must never kill the funnel);
  5. otherwise -> 403, Caddy serves nothing.

``POST /enter`` is called once by the SPA after it loads and issues the signed
cookie — only to clients that already passed the gate, so the cookie cannot be
farmed directly. The cookie is stateless (HMAC over a timestamp): Redis keeps
only the IP list. The allowlist file is reloaded on mtime change; a missing
OR empty allowlist disables the gate entirely (deploy-order safety / kill
switch).

Both endpoints are deliberately session- and DB-free: one Redis round-trip at
most, cheap enough to run on every asset request.
"""

import hashlib
import time

from fastapi import APIRouter, HTTPException, Request, Response

import config

from api.depends import _is_secure_request
from core.redis_cache import _get_redis
from logger import logger
from utils.paid_gate import load_domains, pick_client_ip, referer_matches, sign_gate_token, verify_gate_token


router = APIRouter()

_IP_TTL_SEC = 24 * 60 * 60
_COOKIE_MAX_AGE_SEC = 30 * 24 * 60 * 60
_COOKIE_NAME = str(getattr(config, "PAID_GATE_COOKIE_NAME", "dino_gate"))
# Hand-edited allowlist; lives next to the bot so it moves with the server.
_DOMAINS_FILE = str(
    getattr(config, "PAID_GATE_DOMAINS_FILE", "") or __file__.rsplit("/api/", 1)[0] + "/paid_gate_domains.txt"
)
# Extra trusted proxies for X-Forwarded-For (needed once the pay-server's
# Caddy no longer talks to us over loopback).
_TRUSTED_PROXIES = list(getattr(config, "PAID_GATE_TRUSTED_PROXIES", []) or [])


def _gate_secret() -> bytes:
    """HMAC key for gate cookies: explicit config secret or bot-token derived."""
    explicit = str(getattr(config, "PAID_GATE_SECRET", "") or "")
    material = explicit if explicit else f"{getattr(config, 'API_TOKEN', '')}:paid-gate"
    return hashlib.sha256(material.encode()).digest()


def _client_ip_for_gate(request: Request) -> str:
    peer = (request.client.host if request.client else "") or ""
    xff = request.headers.get("x-forwarded-for")
    return pick_client_ip(peer, xff, _TRUSTED_PROXIES)


async def _ip_known(ip: str) -> bool | None:
    """True/False = Redis answered; None = Redis unavailable (fail-open cue)."""
    client = await _get_redis()
    if client is None:
        return None
    try:
        return bool(await client.exists(f"paid_gate:ip:{ip}"))
    except Exception:
        return None


async def _remember_ip(ip: str) -> None:
    """Stores/refreshes the IP with the 24h TTL; silent on Redis failure."""
    if not ip:
        return
    client = await _get_redis()
    if client is None:
        return
    try:
        await client.setex(f"paid_gate:ip:{ip}", _IP_TTL_SEC, 1)
    except Exception:
        pass


def _has_valid_cookie(request: Request) -> bool:
    return verify_gate_token(_gate_secret(), request.cookies.get(_COOKIE_NAME), _COOKIE_MAX_AGE_SEC)


@router.get("/check")
async def paid_gate_check(request: Request):
    """Admission decision for Caddy's ``forward_auth`` (200 = serve, 403 = deny)."""
    domains = load_domains(_DOMAINS_FILE)
    if not domains:
        # No allowlist file, or an empty one — gate is off, serve everyone.
        return {"ok": True, "gate": "off"}

    ip = _client_ip_for_gate(request)

    if referer_matches(request.headers.get("referer"), domains):
        # Await the write: the asset requests that follow the HTML rely on
        # the IP already being remembered.
        await _remember_ip(ip)
        return {"ok": True}

    known = await _ip_known(ip)
    if known:
        await _remember_ip(ip)  # sliding TTL
        return {"ok": True}

    if _has_valid_cookie(request):
        await _remember_ip(ip)  # client changed IP — remember the new one
        return {"ok": True}

    if known is None:
        logger.warning("[PaidGate] Redis недоступен — пропускаем без проверки IP (fail-open)")
        return {"ok": True, "gate": "degraded"}

    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/enter")
async def paid_gate_enter(request: Request, response: Response):
    """Issues the signed gate cookie to a client that already passed the gate.

    Requiring a remembered IP (or an existing valid cookie) means the cookie
    cannot be obtained by hitting this endpoint directly without first coming
    through an allow-listed landing.
    """
    domains = load_domains(_DOMAINS_FILE)
    if not domains:
        return {"ok": True, "gate": "off"}

    known = await _ip_known(_client_ip_for_gate(request))
    if not (known or known is None or _has_valid_cookie(request)):
        raise HTTPException(status_code=403, detail="Forbidden")

    response.set_cookie(
        key=_COOKIE_NAME,
        value=sign_gate_token(_gate_secret(), int(time.time())),
        max_age=_COOKIE_MAX_AGE_SEC,
        path="/",
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
    )
    return {"ok": True}
