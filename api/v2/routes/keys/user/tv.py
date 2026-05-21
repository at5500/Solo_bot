"""Per-key TV web-import endpoint (POST /api/keys/{client_id}/tv).

Pushes a key's subscription link to a TV running Happ, using Happ's "Web
import" pairing code. The Mini App collects the short code shown on the TV
and posts it here; the backend resolves the subscription link for the key
(ownership-checked), base64-encodes it and relays it to Happ's public
endpoint ``https://check.happ.su/sendtv/{code}``.

Relaying server-side avoids a cross-origin request from the Mini App and
keeps the subscription link off the client for this action.
"""

import json
import re

from base64 import b64encode

import aiohttp

from .._common import *  # noqa: F401,F403 — pull every shared symbol
from .._common import (
    _resolve_billing_user_id,
    user_router,
)
from api.v2.schemas.web_public import TvConnectRequest, TvConnectResponse

# Happ pairing codes shown on the TV's web-import screen: short alphanumeric.
_TV_CODE_RE = re.compile(r"^[A-Za-z0-9]{5}$")

# Public Happ endpoint that delivers a config/subscription to a paired TV.
_HAPP_SENDTV_URL = "https://check.happ.su/sendtv/{code}"


async def _load_user_key(session: AsyncSession, billing_user_id: int, client_id: str) -> Key | None:
    """Loads a key owned by ``billing_user_id`` with the given ``client_id``.

    Args:
        session: Database session.
        billing_user_id: Billing user the key must belong to.
        client_id: Key UUID to look up.

    Returns:
        The matching ``Key`` row, or ``None`` when not found / not owned.
    """
    return (
        await session.execute(
            select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1)
        )
    ).scalar_one_or_none()


@user_router.post("/{client_id}/tv", response_model=TvConnectResponse)
async def user_key_connect_tv(
    client_id: str,
    body: TvConnectRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Relays the key's subscription link to a TV via Happ's web-import code.

    Args:
        client_id: UUID of the key whose subscription should be sent.
        body: Request body carrying the TV pairing ``code``.
        request: Incoming request (used for rate limiting and actor lookup).
        session: Database session.
        identity: Authenticated identity from the bearer token.

    Returns:
        TvConnectResponse: ``ok=True`` when Happ accepted the push.

    Raises:
        HTTPException: 400 for a malformed code or a key without a link,
            404 when the key is not owned by the caller, 502 when Happ
            rejects the relay or is unreachable.
    """
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="tv_connect", max_per_window=10, window_sec=60)

    code = str(body.code or "").strip()
    if not _TV_CODE_RE.match(code):
        raise HTTPException(status_code=400, detail="Неверный код подключения")

    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = await _load_user_key(session, billing_user_id, client_id)
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    link = str(getattr(db_key, "key", "") or getattr(db_key, "remnawave_link", "") or "").strip()
    if not link:
        raise HTTPException(status_code=400, detail="У подписки нет ссылки для подключения")

    payload = {"data": b64encode(link.encode("utf-8")).decode("ascii")}
    url = _HAPP_SENDTV_URL.format(code=code)

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            async with http.post(url, json=payload) as resp:
                raw = (await resp.text() or "").strip()
    except aiohttp.ClientError as exc:
        logger.warning("[connect_tv] Happ request failed for code=%s: %s", code, exc)
        raise HTTPException(status_code=502, detail="Сервис Happ недоступен, попробуйте позже")

    # Happ answers HTTP 200 regardless of outcome; the real result is in the
    # JSON body — {"status":"success"} or {"status":"error","message":"..."}.
    # Note: Happ only validates the UID *format*, not whether a TV session for
    # that code actually exists, so a well-formed but stale code still succeeds.
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}

    if str(data.get("status")).lower() == "success":
        return TvConnectResponse(ok=True, message="Подписка отправлена на телевизор", client_id=client_id)

    logger.warning("[connect_tv] Happ rejected code=%s body=%s", code, raw[:200])
    raise HTTPException(status_code=400, detail="Не удалось отправить подписку. Проверьте код и повторите")
