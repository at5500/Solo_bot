"""Endpoints that issue link-tokens for Mini App ↔ Telegram identity linking.

Two flows, mirroring each other:

* ``POST /auth/link-tokens/telegram`` — caller is a *web*-authenticated user
  who wants to attach a Telegram account. Returns a bot deeplink
  (``https://t.me/<bot>?start=link_<token>``) that the matching bot handler
  (``modules/account_link/router.py``) consumes.

* ``POST /auth/link-tokens/web`` — caller is a *Telegram WebApp*-authenticated
  user who wants to attach a web account. Returns a web URL with
  ``?link_token=<token>``; the OTP login flow on the web side picks it up
  and feeds it to ``/auth/login-by-code`` for the actual merge.

Tokens themselves are stored in Redis and live in ``utils/identity_link.py``.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from config import USERNAME_BOT
from core.redis_cache import cache_incr_checked
from core.settings.web_config import get_site_url
from utils.identity_link import LINK_KIND_TG, LINK_KIND_WEB, store_link_token


router = APIRouter()

# Cap how often a single identity can mint fresh link tokens — protects
# Redis memory from a misbehaving client (or a malicious script) that
# would otherwise spam 30-minute-lived entries.
_RATE_LIMIT_MAX = 20
_RATE_LIMIT_WINDOW_SEC = 600  # 10 minutes


async def _enforce_link_rate_limit(identity_id: str) -> None:
    """Caps minting at ``_RATE_LIMIT_MAX`` per identity inside a
    ``_RATE_LIMIT_WINDOW_SEC`` window.

    Falls back to the process-local in-memory limiter when Redis is
    unavailable — mirrors the pattern used by the email/password routes
    (``password.py``) so a Redis outage does not silently disable
    rate-limiting on this endpoint.

    Args:
        identity_id: Caller's stringified identity UUID.

    Raises:
        HTTPException: 429 when the per-identity quota is exhausted.
    """
    key = f"link_token_rate:{identity_id}"
    count, redis_ok = await cache_incr_checked(key, _RATE_LIMIT_WINDOW_SEC)
    if not redis_ok:
        from api.v2.routes.auth._fallback_limiter import check_and_increment

        count = check_and_increment(key, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW_SEC)
    if count > _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток. Попробуйте позже.",
        )


class LinkTokenResponse(BaseModel):
    """Ready-to-open URL for the caller. The token itself is opaque to the
    client — only the URL is meaningful."""

    url: str


@router.post("/link-tokens/telegram", response_model=LinkTokenResponse)
async def create_telegram_link_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Issues a bot deeplink for attaching Telegram to the caller's identity.

    Returns:
        ``{ "url": "https://t.me/<bot>?start=link_<token>" }``

    Raises:
        HTTPException: 429 when the per-identity rate limit is exceeded;
            503 if the bot username is not configured server-side.
    """
    del session, request
    await _enforce_link_rate_limit(str(identity.id))
    bot = (USERNAME_BOT or "").replace("@", "").strip()
    if not bot:
        raise HTTPException(status_code=503, detail="Бот не настроен на сервере")
    token = await store_link_token(LINK_KIND_TG, str(identity.id))
    return LinkTokenResponse(url=f"https://t.me/{bot}?start=link_{token}")


@router.post("/link-tokens/web", response_model=LinkTokenResponse)
async def create_web_link_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Issues a web deeplink for attaching a web session to the caller's
    Telegram identity. The caller opens the URL in a browser; the web app
    grabs the ``?link_token=`` param and posts it alongside the OTP code.

    Returns:
        ``{ "url": "<site>/?link_token=<token>" }``

    Raises:
        HTTPException: 429 when the per-identity rate limit is exceeded;
            503 if the web app URL is not configured.
    """
    del session, request
    await _enforce_link_rate_limit(str(identity.id))
    site = get_site_url()
    if not site:
        raise HTTPException(status_code=503, detail="URL веб-приложения не настроен")
    token = await store_link_token(LINK_KIND_WEB, str(identity.id))
    return LinkTokenResponse(url=f"{site}/?link_token={token}")
