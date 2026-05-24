"""Endpoints that issue (and consume) link-tokens for Mini App ↔ Telegram
identity linking.

Three flows:

* ``POST /auth/link-tokens/telegram`` — caller is a *web*-authenticated user
  who wants to attach a Telegram account. Returns a Mini App deeplink
  (``https://t.me/<bot>/<miniapp>?startapp=link_<token>``) when
  ``MINIAPP_NAME`` env is set, falling back to the bot ``?start=link_<token>``
  form otherwise. The Mini App side posts the token to
  ``POST /auth/link-miniapp`` to perform the attach.

* ``POST /auth/link-tokens/web`` — caller is a *Telegram WebApp*-authenticated
  user who wants to attach a web account. Returns a web URL with
  ``?link_token=<token>``; the OTP login flow on the web side picks it up
  and feeds it to ``/auth/login-by-code`` for the actual merge.

* ``POST /auth/link-miniapp`` — Mini App-side consumer for the TG-kind
  token. The caller must already be authenticated via initData (so the
  identity has a ``tg_id``), and the token tells us which web identity
  the caller wants to merge into.

The Mini App route exists because Telegram clients silently swallow the
``?start=`` payload when the bot is already in the user's chat list — the
``startapp`` form sidesteps that by opening the Mini App directly.

Tokens themselves are stored in Redis and live in ``utils/identity_link.py``.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from config import USERNAME_BOT
from core.redis_cache import cache_incr_checked
from core.settings.web_config import get_site_url
from database import identities as idb
from utils.identity_link import (
    LINK_KIND_TG,
    LINK_KIND_WEB,
    consume_link_token,
    store_link_token,
)
from utils.photo_cache import invalidate_photo_cache


router = APIRouter()

# Cap how often a single identity can mint fresh link tokens — protects
# Redis memory from a misbehaving client (or a malicious script) that
# would otherwise spam 30-minute-lived entries.
_RATE_LIMIT_MAX = 20
_RATE_LIMIT_WINDOW_SEC = 600  # 10 minutes

# Short-name of the Mini App registered with @BotFather (``/newapp`` →
# ``Choose a short name``). When set, the TG-link URL switches to the
# ``?startapp=`` form, which is reliable even when the bot is already
# open in the user's Telegram client.
_MINIAPP_NAME = (os.getenv("MINIAPP_NAME") or "").strip()


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


class LinkMiniappRequest(BaseModel):
    """Mini App-side consumer payload — just the opaque token string."""

    link_token: str


class LinkMiniappResult(BaseModel):
    """Outcome of a Mini App-side link-token consume attempt.

    ``ok`` mirrors the request status (no exception was raised). ``linked``
    is the actual business result — ``True`` only when the attach
    succeeded. ``message`` is a Russian, user-facing string the Mini App
    can surface directly.
    """

    ok: bool
    linked: bool
    message: str


@router.post("/link-tokens/telegram", response_model=LinkTokenResponse)
async def create_telegram_link_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Issues a deeplink for attaching Telegram to the caller's identity.

    Returns the Mini App ``?startapp=`` URL when ``MINIAPP_NAME`` is set
    (preferred — Telegram delivers ``start_param`` reliably even when the
    bot is already open in the user's chat list). Falls back to the bot
    ``?start=link_<token>`` deeplink otherwise.

    Raises:
        HTTPException: 429 when the per-identity rate limit is exceeded;
            503 if neither the bot username nor the Mini App name is
            configured server-side.
    """
    del session, request
    await _enforce_link_rate_limit(str(identity.id))
    bot = (USERNAME_BOT or "").replace("@", "").strip()
    if not bot:
        raise HTTPException(status_code=503, detail="Бот не настроен на сервере")
    token = await store_link_token(LINK_KIND_TG, str(identity.id))
    if _MINIAPP_NAME:
        url = f"https://t.me/{bot}/{_MINIAPP_NAME}?startapp=link_{token}"
    else:
        url = f"https://t.me/{bot}?start=link_{token}"
    return LinkTokenResponse(url=url)


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


@router.post("/link-miniapp", response_model=LinkMiniappResult)
async def consume_miniapp_link(
    body: LinkMiniappRequest,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Mini App-side consumer for a TG-kind link-token.

    Caller is expected to be authenticated via initData (so the current
    identity has a ``tg_id``). The token references the *web* identity
    that originally requested the link — we call ``attach_telegram`` to
    fold the caller's Telegram into that identity.

    Returns:
        :class:`LinkMiniappResult` with a user-facing Russian message in
        ``message`` that the Mini App can surface directly to the user.
    """
    token = (body.link_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Токен обязателен")

    tg_id = getattr(identity, "tg_id", None)
    if tg_id is None:
        raise HTTPException(
            status_code=400,
            detail="Mini App не привязан к Telegram",
        )

    remote_id = await consume_link_token(LINK_KIND_TG, token)
    if not remote_id:
        return LinkMiniappResult(
            ok=True,
            linked=False,
            message="Ссылка для привязки устарела или уже использована.",
        )

    if remote_id == str(identity.id):
        return LinkMiniappResult(
            ok=True,
            linked=False,
            message="Эта ссылка ведёт на ваш же аккаунт — связывать нечего.",
        )

    merged = await idb.attach_telegram(session, remote_id, int(tg_id))
    if merged is None:
        return LinkMiniappResult(
            ok=True,
            linked=False,
            message="Не удалось связать — на обоих аккаунтах оформлены подписки.",
        )

    await session.commit()
    # Drop the negative photo-cache entry from the pre-link web identity
    # so the next ``/me/photo`` resolves the freshly-linked Telegram avatar.
    await invalidate_photo_cache(str(merged.id))

    return LinkMiniappResult(
        ok=True,
        linked=True,
        message="Аккаунты связаны. Теперь все ваши подписки видны и тут, и в личном кабинете.",
    )
