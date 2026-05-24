"""Mini App user-avatar endpoints.

The Identity database table is sealed inside the obfuscated backend bundle,
so we cannot persist the photo. Instead the JSON endpoint reports *whether*
an avatar exists for the caller (cheap, cached Redis lookup), and a
streaming endpoint proxies the actual bytes from Telegram on demand —
that way the bot token never leaves the server.

* ``GET /api/auth/me/photo``       → ``{ photo_url: "/auth/me/photo/file" | null }``
* ``GET /api/auth/me/photo/file``  → image bytes (auth-required, streamed)

The Redis layer (key format, TTL, negative sentinel, invalidation) is
factored into :mod:`utils.photo_cache` so the bot's identity-link handler
can drop a stale entry without dragging the API stack into bot startup.
"""

import aiohttp

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from api.depends import get_session, verify_identity_token
from config import API_TOKEN
from logger import logger
from utils.photo_cache import get_cached_file_path, store_file_path


router = APIRouter()


# Browser-side per-user cache for the image bytes. Short enough that an
# avatar change in Telegram propagates within ~5 minutes.
_BROWSER_CACHE_HEADER = "private, max-age=300"


class IdentityPhotoResponse(BaseModel):
    """Path to the proxy endpoint, or null when no avatar is available.

    The path is intentionally relative to the API base — clients prefix it
    with their own ``VITE_API_BASE`` to build the final URL.
    """

    photo_url: str | None = None


async def _resolve_file_path(tg_id: int) -> str | None:
    """Asks the bot for the user's most-recent profile photo and returns the
    largest tile's ``file_path``. Returns ``None`` on any failure (no photo,
    privacy settings, rate limit, network error).

    Args:
        tg_id: Telegram user id to query the Bot API for.

    Returns:
        Telegram-relative ``file_path`` string, or ``None`` if no avatar is
        available or the Bot API call failed.
    """
    try:
        # Late import to avoid pulling bot init into the API request path on
        # cold reloads.
        from bot import bot as bot_instance

        photos = await bot_instance.get_user_profile_photos(user_id=tg_id, limit=1)
        if not photos or not photos.photos or not photos.photos[0]:
            return None
        sizes = photos.photos[0]
        biggest = max(sizes, key=lambda s: int(getattr(s, "file_size", 0) or 0))
        file = await bot_instance.get_file(biggest.file_id)
        file_path = getattr(file, "file_path", None)
        return str(file_path) if file_path else None
    except Exception as exc:
        # Same precaution as :func:`me_photo_file`: aiogram normally wraps
        # aiohttp errors, but a stray ``ClientResponseError`` would carry
        # the bot ``API_TOKEN`` in its ``__str__``. Log the class only.
        logger.info("[me/photo] resolve failed for tg_id=%s: %s", tg_id, type(exc).__name__)
        return None


async def _resolve_or_cached_file_path(identity_id: str, tg_id: int) -> str | None:
    """Returns either a cached ``file_path`` or a freshly-resolved one.

    The absence of an avatar is itself cached (negative cache) to avoid
    hammering the Bot API on every Profile open.

    Args:
        identity_id: Stringified identity UUID — used as the cache key.
        tg_id: Telegram user id, queried only on cache miss.

    Returns:
        ``file_path`` or ``None`` when the user has no Telegram avatar.
    """
    cached, file_path = await get_cached_file_path(identity_id)
    if cached:
        return file_path
    fresh = await _resolve_file_path(tg_id)
    await store_file_path(identity_id, fresh)
    return fresh


@router.get("/me/photo", response_model=IdentityPhotoResponse)
async def me_photo(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Reports whether the caller has a Telegram avatar.

    The actual bytes live behind :func:`me_photo_file` to keep the bot
    token server-side.

    Returns:
        ``IdentityPhotoResponse`` whose ``photo_url`` is either the proxy
        path or ``None``.
    """
    del session
    tg_id = getattr(identity, "tg_id", None)
    if tg_id is None:
        return IdentityPhotoResponse(photo_url=None)
    file_path = await _resolve_or_cached_file_path(str(identity.id), int(tg_id))
    if not file_path:
        return IdentityPhotoResponse(photo_url=None)
    return IdentityPhotoResponse(photo_url="/auth/me/photo/file")


@router.get("/me/photo/file")
async def me_photo_file(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Streams the user's Telegram avatar through this server so the bot
    token in the TG file URL never reaches the browser. 404 when there's
    no avatar to serve.

    Cleanup is *paranoid* on purpose: ``aiohttp.ClientSession`` must close
    on every exit path — including ``asyncio.CancelledError`` (Starlette
    cancels the task when the client disconnects mid-handshake). The outer
    ``try/except BaseException`` covers the window before the
    ``StreamingResponse`` takes ownership via ``BackgroundTask(http.close)``.
    """
    del session
    tg_id = getattr(identity, "tg_id", None)
    if tg_id is None:
        raise HTTPException(status_code=404, detail="Аватар не найден")
    file_path = await _resolve_or_cached_file_path(str(identity.id), int(tg_id))
    if not file_path:
        raise HTTPException(status_code=404, detail="Аватар не найден")

    # The token-bearing URL is built and consumed entirely on the server.
    tg_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_path}"
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        try:
            resp = await http.get(tg_url)
        except aiohttp.ClientError as exc:
            # Log only the exception class — several aiohttp errors embed the
            # full request URL in their ``__str__``, and that URL carries the
            # bot ``API_TOKEN``. Never let it land in log sinks (Sentry/ELK).
            logger.info("[me/photo/file] upstream fetch failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Не удалось загрузить аватар") from None

        if resp.status != 200:
            await resp.release()
            raise HTTPException(status_code=502, detail="Telegram вернул ошибку")

        content_type = resp.headers.get("Content-Type", "image/jpeg")

        async def streamer():
            try:
                async for chunk in resp.content.iter_chunked(8 * 1024):
                    yield chunk
            finally:
                await resp.release()

        # Ownership of ``http`` transfers to ``StreamingResponse`` here — the
        # background task closes it after Starlette finishes flushing the
        # body (or aborts the response). ``ClientSession.close()`` is
        # idempotent, so the cleanup is safe under any exit path.
        return StreamingResponse(
            streamer(),
            media_type=content_type,
            headers={"Cache-Control": _BROWSER_CACHE_HEADER},
            background=BackgroundTask(http.close),
        )
    except BaseException:
        # Covers everything between session creation and the successful
        # ``return``: HTTPException raises above, ``asyncio.CancelledError``
        # from a disconnected client, KeyboardInterrupt, etc. Without this,
        # the session would leak whenever an exception fired before
        # ``StreamingResponse`` could take ownership of cleanup.
        await http.close()
        raise
