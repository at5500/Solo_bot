"""Redis cache layer for the Mini App user-avatar endpoint.

Lives in ``utils`` rather than next to the API route so the bot handler in
``modules/account_link/router.py`` can drop a cached entry after a
successful identity-link without dragging the whole FastAPI app (and its
``aiohttp`` / ``starlette`` imports) into the bot import path. The route
module — ``api/v2/routes/auth/photo.py`` — imports the same helpers and
treats this file as the source of truth for cache layout.

Negative-cache discriminator (``_NEGATIVE_VALUE``) lets us cache *the
absence* of a Telegram avatar — typical TG ``file_path`` strings look like
``photos/file_42.jpg`` and never collide with ``_none_``.
"""

from core.redis_cache import cache_delete, cache_get, cache_set


_CACHE_KEY_FMT = "identity_photo_path:{identity_id}"
_NEGATIVE_VALUE = "_none_"

# TG file URLs nominally live ~1 h — keep the cached ``file_path`` slightly
# shorter so the next refresh has a buffer before Telegram expires it.
CACHE_TTL_SEC = 50 * 60


def _key(identity_id: str) -> str:
    """Builds the per-identity Redis key for the cached ``file_path``.

    Args:
        identity_id: Stringified identity UUID — caller is expected to
            normalise to ``str`` itself, this function does not coerce.

    Returns:
        Fully-qualified Redis key.
    """
    return _CACHE_KEY_FMT.format(identity_id=identity_id)


async def get_cached_file_path(identity_id: str) -> tuple[bool, str | None]:
    """Reads the cached Telegram ``file_path`` for an identity.

    Args:
        identity_id: Stringified identity UUID.

    Returns:
        ``(False, None)`` on cache miss — caller should resolve and store.
        ``(True, None)`` if the absence was cached (negative cache hit).
        ``(True, "<file_path>")`` on a positive hit.
    """
    raw = await cache_get(_key(identity_id))
    if raw is None:
        return False, None
    return True, None if str(raw) == _NEGATIVE_VALUE else str(raw)


async def store_file_path(identity_id: str, file_path: str | None) -> None:
    """Persists a resolved ``file_path`` (or the absence of one) in Redis.

    Passing ``None`` caches the negative result — so the next request
    short-circuits instead of poking the Bot API again.

    Args:
        identity_id: Stringified identity UUID.
        file_path: Telegram ``file_path`` or ``None`` when the user has no
            avatar.
    """
    await cache_set(_key(identity_id), file_path or _NEGATIVE_VALUE, CACHE_TTL_SEC)


async def invalidate_photo_cache(identity_id: str) -> None:
    """Drops the cached entry (positive or negative) for an identity.

    Call this right after attaching a ``tg_id`` to a previously web-only
    identity — otherwise the negative cache from before the link keeps
    serving ``null`` until ``CACHE_TTL_SEC`` elapses and the Profile UI
    shows the default avatar despite a freshly-linked Telegram account.

    Args:
        identity_id: Stringified identity UUID.
    """
    await cache_delete(_key(identity_id))
