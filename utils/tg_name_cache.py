"""Redis cache layer for the Mini App user-name endpoint.

Mirrors :mod:`utils.photo_cache` — same access pattern, same negative
sentinel idea, but stores a JSON blob with ``first_name`` / ``username``
instead of a single string. The cache lets the web build show a real
display name in the header without hitting the Bot API on every Profile
or Home render.

A 24-hour TTL is fine for a Telegram display name — they rarely change,
and a stale read recovers within a day even without explicit
invalidation.
"""

import json

from core.redis_cache import cache_delete, cache_get, cache_set


_CACHE_KEY_FMT = "identity_tg_name:{identity_id}"
_NEGATIVE_VALUE = "_none_"

CACHE_TTL_SEC = 24 * 60 * 60


def _key(identity_id: str) -> str:
    """Builds the per-identity Redis key for the cached TG name blob.

    Args:
        identity_id: Stringified identity UUID — caller is expected to
            normalise to ``str`` itself.

    Returns:
        Fully-qualified Redis key.
    """
    return _CACHE_KEY_FMT.format(identity_id=identity_id)


async def get_cached_tg_name(identity_id: str) -> tuple[bool, dict | None]:
    """Reads the cached Telegram name blob for an identity.

    Args:
        identity_id: Stringified identity UUID.

    Returns:
        ``(False, None)`` on cache miss — caller should resolve and store.
        ``(True, None)`` if the absence was cached (negative cache hit).
        ``(True, {"first_name": ..., "username": ...})`` on a positive hit.
    """
    raw = await cache_get(_key(identity_id))
    if raw is None:
        return False, None
    if str(raw) == _NEGATIVE_VALUE:
        return True, None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return True, parsed
    except (TypeError, ValueError):
        pass
    return False, None


async def store_tg_name(identity_id: str, payload: dict | None) -> None:
    """Persists a resolved name blob (or the absence of one) in Redis.

    Passing ``None`` (or an empty dict) caches the negative result so the
    next request short-circuits instead of poking the Bot API again.

    Args:
        identity_id: Stringified identity UUID.
        payload: ``{"first_name": str | None, "username": str | None}``
            or ``None`` when the lookup failed entirely.
    """
    if not payload or not any(payload.values()):
        await cache_set(_key(identity_id), _NEGATIVE_VALUE, CACHE_TTL_SEC)
        return
    await cache_set(_key(identity_id), json.dumps(payload), CACHE_TTL_SEC)


async def invalidate_tg_name_cache(identity_id: str) -> None:
    """Drops the cached entry (positive or negative) for an identity.

    Call right after attaching a ``tg_id`` to a previously web-only
    identity — otherwise the negative cache from before the link keeps
    serving ``null`` until the TTL elapses.

    Args:
        identity_id: Stringified identity UUID.
    """
    await cache_delete(_key(identity_id))
