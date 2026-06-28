"""Identity-link tokens.

Short-lived, one-shot tokens that let the Mini App link its current session
to the *other* side: a web-logged-in user can attach their Telegram account
via a bot deeplink, and a Telegram-logged-in user can attach a web session
via an OTP redirect.

The token itself is just an opaque short string — the actual identity is
stored in Redis under a kind-discriminated key so a token issued for the
``web`` flow cannot be consumed by the ``tg`` flow and vice versa.
"""

import secrets
import string

from core.redis_cache import cache_delete, cache_get, cache_set


_TTL_SEC = 30 * 60  # 30 minutes — enough to switch apps and finish OTP / /start.
_ALPHABET = string.ascii_letters + string.digits
_TOKEN_LEN = 8

# Kind discriminators. The matching client flow is described in the linked
# Mini App endpoints (`/auth/link-tokens/{web|telegram}`).
LINK_KIND_WEB = "web"   # issued in tgapp; consumed by the web /login-by-code flow.
LINK_KIND_TG = "tg"     # issued in web app; consumed by the bot /start link_… handler.


def _key(kind: str, token: str) -> str:
    return f"link_token:{kind}:{token}"


def generate_token() -> str:
    """Returns a fresh random 8-char base62 token."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LEN))


async def store_link_token(kind: str, identity_id: str) -> str:
    """Creates a new token and stores ``identity_id`` against it for ``_TTL_SEC``
    seconds. Returns the token string for the caller to embed into a URL.

    Args:
        kind: Either :data:`LINK_KIND_WEB` or :data:`LINK_KIND_TG`.
        identity_id: The identity the token is issued for — the *origin* side
            of the link operation.
    """
    token = generate_token()
    await cache_set(_key(kind, token), identity_id, _TTL_SEC)
    return token


async def consume_link_token(kind: str, token: str) -> str | None:
    """Looks up the identity behind a token and deletes it (one-shot).

    Args:
        kind: Discriminator the token was stored under. A token issued under a
            different ``kind`` won't be found and the call returns ``None``.
        token: Opaque token string from the URL.

    Returns:
        The originating ``identity_id`` on success, or ``None`` if the token
        is missing, expired, or stored under another ``kind``.
    """
    if not token:
        return None
    key = _key(kind, token)
    value = await cache_get(key)
    if not value:
        return None
    await cache_delete(key)
    return str(value)


async def peek_link_token(kind: str, token: str) -> str | None:
    """Looks up the identity behind a token *without* deleting it.

    Used by consent flows where the actual attach happens only after the
    recipient explicitly approves the link — peek is read-only, the
    attach call later uses ``consume_link_token`` to lock the operation
    in atomically.

    Args:
        kind: Discriminator the token was stored under.
        token: Opaque token string from the URL.

    Returns:
        The originating ``identity_id`` on success, or ``None`` if the
        token is missing, expired, or stored under another ``kind``.
    """
    if not token:
        return None
    value = await cache_get(_key(kind, token))
    return str(value) if value else None


async def drop_link_token(kind: str, token: str) -> None:
    """Best-effort delete of a link token. Called when the recipient
    explicitly rejects the link or the consent flow times out — kills
    the token so a leaked URL can't be reused later.

    Args:
        kind: Discriminator the token was stored under.
        token: Opaque token string from the URL.
    """
    if not token:
        return
    await cache_delete(_key(kind, token))
