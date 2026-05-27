"""Mini App user-name endpoint.

Companion to :mod:`api.v2.routes.auth.photo` — same idea, different
payload. The Identity row only stores ``email`` and ``tg_id``; the
display name is resolved from the Bot API via ``bot.get_chat(tg_id)``
on first request, then cached.

* ``GET /api/auth/me/tg-name``  → ``{ first_name, username }`` — both
  fields optional. Returns ``null``-ish payload when the caller has no
  attached ``tg_id`` (web-only account) or the Bot API refuses the
  lookup (privacy settings, rate limit, never wrote to the bot).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from logger import logger
from utils.tg_name_cache import (
    get_cached_tg_name,
    store_tg_name,
)


router = APIRouter()


class IdentityTgNameResponse(BaseModel):
    """Display name pulled from Telegram for the caller's ``tg_id``.

    Both fields are intentionally optional — a user may have no
    ``first_name`` set (rare), no ``username`` set (common), or the
    backend may have failed to resolve either of them.
    """

    first_name: str | None = None
    username: str | None = None


async def _resolve_tg_name(tg_id: int) -> dict | None:
    """Asks the bot for the user's first_name / username.

    Args:
        tg_id: Telegram user id to query the Bot API for.

    Returns:
        A ``{"first_name": str | None, "username": str | None}`` dict
        when at least one field came back, or ``None`` on any failure
        (no access, privacy settings, rate limit, network error).
    """
    try:
        from bot import bot as bot_instance

        chat = await bot_instance.get_chat(chat_id=tg_id)
        first_name = getattr(chat, "first_name", None)
        username = getattr(chat, "username", None)
        if not first_name and not username:
            return None
        return {
            "first_name": str(first_name) if first_name else None,
            "username": str(username) if username else None,
        }
    except Exception as exc:
        # ``get_chat`` raises a wide range of aiogram errors (forbidden,
        # not-found, rate limit) — none of them carry the bot token in
        # their default string repr, but log the class only to match the
        # cautious posture of the photo endpoint.
        logger.info(
            "[me/tg-name] resolve failed for tg_id=%s: %s", tg_id, type(exc).__name__
        )
        return None


async def _resolve_or_cached(identity_id: str, tg_id: int) -> dict | None:
    """Returns either a cached name blob or a freshly-resolved one.

    The absence of a name is itself cached (negative cache) to avoid
    hammering the Bot API on every page open.

    Args:
        identity_id: Stringified identity UUID — used as the cache key.
        tg_id: Telegram user id, queried only on cache miss.

    Returns:
        Name blob or ``None`` when the user has no resolvable TG name.
    """
    cached, payload = await get_cached_tg_name(identity_id)
    if cached:
        return payload
    fresh = await _resolve_tg_name(tg_id)
    await store_tg_name(identity_id, fresh)
    return fresh


@router.get("/me/tg-name", response_model=IdentityTgNameResponse)
async def me_tg_name(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns the caller's Telegram display name, when one exists.

    Returns:
        ``IdentityTgNameResponse`` — both fields ``None`` for web-only
        accounts (no ``tg_id``) or when the Bot API lookup failed.
    """
    del session
    tg_id = getattr(identity, "tg_id", None)
    if tg_id is None:
        return IdentityTgNameResponse()
    payload = await _resolve_or_cached(str(identity.id), int(tg_id))
    if not payload:
        return IdentityTgNameResponse()
    return IdentityTgNameResponse(
        first_name=payload.get("first_name"),
        username=payload.get("username"),
    )
