"""Long-lived per-identity «self-link» tokens.

Each identity gets one opaque ~20-char base62 token (``self_link_code``).
The owner shares the URL ``<site>/?l=<code>`` from their Profile;
whoever opens it can be auto-linked to the originating identity via
``POST /auth/consume-self-link`` (web ↔ TG merge without OTP).

Properties:

* The code is generated from ``secrets.choice`` — not derived from the
  identity id, the tg_id, or the email. Recovering the identity from the
  code requires reading Redis.
* Stored both forward (``self_link_code:{identity_id}`` → code) and
  reverse (``self_link_identity:{code}`` → identity_id), so lookup is
  O(1) in both directions.
* TTL is long (1 year) but not infinite — Redis is the only backing
  store (the Identity table is sealed). A lost Redis entry just regrows
  on the next ``ensure_self_link_code`` call; the old shared URL goes
  stale.
"""

import secrets
import string

from core.redis_cache import cache_delete, cache_get, cache_set


_ALPHABET = string.ascii_letters + string.digits
_TOKEN_LEN = 20
_TTL_SEC = 365 * 24 * 60 * 60  # 1 year


def _fwd_key(identity_id: str) -> str:
    """Forward key: ``identity_id`` → ``code``."""
    return f"self_link_code:{identity_id}"


def _rev_key(code: str) -> str:
    """Reverse key: ``code`` → ``identity_id``."""
    return f"self_link_identity:{code}"


def _generate_code() -> str:
    """Returns a random 20-char base62 code."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LEN))


async def ensure_self_link_code(identity_id: str) -> str:
    """Returns the identity's existing self-link code, or mints a new one.

    The code is stable per identity — repeated calls return the same
    value until Redis loses the entry (then a fresh one is minted on the
    next call, invalidating any previously-shared URLs).

    Args:
        identity_id: Stringified identity UUID.

    Returns:
        The opaque code (URL-safe, no ``?l=`` prefix).
    """
    existing = await cache_get(_fwd_key(identity_id))
    if existing and isinstance(existing, str) and len(existing) == _TOKEN_LEN:
        return existing
    code = _generate_code()
    await cache_set(_fwd_key(identity_id), code, _TTL_SEC)
    await cache_set(_rev_key(code), identity_id, _TTL_SEC)
    return code


async def resolve_self_link_code(code: str) -> str | None:
    """Looks up the identity behind a self-link code.

    Args:
        code: The opaque string from ``?l=<code>``.

    Returns:
        Identity id when the code is live, or ``None`` if missing /
        expired / malformed.
    """
    if not code or len(code) != _TOKEN_LEN:
        return None
    value = await cache_get(_rev_key(code))
    return str(value) if value else None


async def drop_self_link_code(identity_id: str) -> None:
    """Invalidates the identity's self-link code (drops both Redis keys).

    Called after a successful merge that absorbed the identity into
    another one — the code now points at a defunct identity and is
    useless.

    Args:
        identity_id: Stringified identity UUID.
    """
    existing = await cache_get(_fwd_key(identity_id))
    if isinstance(existing, str):
        await cache_delete(_rev_key(existing))
    await cache_delete(_fwd_key(identity_id))
