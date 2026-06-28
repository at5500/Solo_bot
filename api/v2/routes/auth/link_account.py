"""Endpoints that issue (and consume) link-tokens for Mini App ↔ Telegram
identity linking.

Three flows:

* ``POST /auth/link-tokens/telegram`` — caller is a *web*-authenticated user
  who wants to attach a Telegram account. Returns a bot ``?start=link_<token>``
  deeplink plus the raw ``token``; the frontend rewrites it into the
  Mini App ``?startapp=`` form when its env is configured (the bot URL
  itself remains the safe fallback).

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
``startapp`` form (built client-side from ``VITE_BOT_USERNAME`` /
``VITE_MINIAPP_NAME``) sidesteps that by opening the Mini App directly.

Tokens themselves are stored in Redis and live in ``utils/identity_link.py``.
"""

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
    drop_link_token,
    peek_link_token,
    store_link_token,
)
from utils.photo_cache import invalidate_photo_cache


def _mask_email(email: str | None) -> str:
    """Renders ``i***e@example.com`` for a recognizable but redacted view
    of the originating identity's email in the Mini App consent dialog.

    Args:
        email: Raw email or ``None``.

    Returns:
        Masked string. Empty string when the input doesn't look like an
        email — the frontend then hides the field entirely.
    """
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    if len(local) <= 3:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


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
    """Ready-to-open URL for the caller. The frontend extracts the token
    from the URL when it needs to rebuild a Mini App ``?startapp=`` form."""

    url: str


class LinkMiniappRequest(BaseModel):
    """Mini App-side consumer payload.

    ``confirmed`` must be set to ``true`` — without it the endpoint
    refuses the attach. Frontend is expected to first fetch
    ``/auth/link-tokens/info`` to render a confirmation dialog and only
    set ``confirmed`` after the user explicitly approves ([audit
    F-NEW-tg-01]).
    """

    link_token: str
    confirmed: bool = False


class LinkTokenInfoRequest(BaseModel):
    """Read-only lookup payload — only the opaque token."""

    link_token: str


class LinkTokenInfoResult(BaseModel):
    """Public-facing info about the originating identity behind a link
    token. Surfaces just enough for the recipient to recognise their own
    account in the consent dialog — masked email plus the registration
    date — without leaking PII to whoever else might have the token."""

    valid: bool
    remote_email_masked: str = ""
    remote_created_at: str = ""


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


@router.post("/link-tokens/info", response_model=LinkTokenInfoResult)
async def link_token_info(
    body: LinkTokenInfoRequest,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Read-only lookup behind a TG-kind link token.

    Used by the Mini App to render the consent dialog before the actual
    attach — the token stays alive until either ``consume_miniapp_link``
    or ``link_token_drop`` is called.

    Returns:
        :class:`LinkTokenInfoResult` with ``valid=False`` when the token
        is missing, expired, or stored under another kind. On success,
        ``remote_email_masked`` is empty for identities without an email
        — the frontend hides the field in that case.
    """
    del identity  # caller must be authenticated, identity itself unused
    token = (body.link_token or "").strip()
    if not token:
        return LinkTokenInfoResult(valid=False)
    remote_id = await peek_link_token(LINK_KIND_TG, token)
    if not remote_id:
        return LinkTokenInfoResult(valid=False)
    remote = await idb.get_identity_by_id(session, remote_id)
    if remote is None:
        return LinkTokenInfoResult(valid=False)
    created_at = getattr(remote, "created_at", None)
    return LinkTokenInfoResult(
        valid=True,
        remote_email_masked=_mask_email(getattr(remote, "email", None)),
        remote_created_at=created_at.isoformat() if created_at else "",
    )


@router.post("/link-tokens/drop")
async def link_token_drop(
    body: LinkTokenInfoRequest,
    identity=Depends(verify_identity_token),
):
    """Drops a TG-kind link token outright — called from the Mini App
    when the user rejects the consent dialog so a leaked URL can't be
    reused later. Best-effort: missing/expired token returns 200 too."""
    del identity
    token = (body.link_token or "").strip()
    if token:
        await drop_link_token(LINK_KIND_TG, token)
    return {"ok": True}


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
    if not body.confirmed:
        # Consent gate ([audit F-NEW-tg-01]): the Mini App must show a
        # confirmation dialog using /auth/link-tokens/info before this
        # endpoint will perform the attach.
        raise HTTPException(status_code=400, detail="Привязка требует подтверждения")

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

    # Guard against silently replacing an already-attached Telegram on
    # the web side. ``attach_telegram`` would otherwise just overwrite
    # ``remote_identity.tg_id`` with ours — the old account loses its
    # Telegram channel without anyone noticing.
    remote_identity = await idb.get_identity_by_id(session, remote_id)
    if (
        remote_identity is not None
        and remote_identity.tg_id is not None
        and int(remote_identity.tg_id) != int(tg_id)
    ):
        return LinkMiniappResult(
            ok=True,
            linked=False,
            message=(
                "К этому аккаунту уже привязан другой Telegram. "
                "Отвяжите его в профиле и попробуйте ещё раз."
            ),
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
