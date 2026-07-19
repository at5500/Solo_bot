"""Opaque public references for guest payment status polling.

The guest email-purchase flow must expose *something* to the browser so it
can poll its own payment, but the provider ``payment_id`` is
``<timestamp>_<user_id>`` — it embeds the billing user id. Returning it leaks
the account id and the user-table counter, and lets anyone probe whether an
arbitrary email is a known customer (finding F-010). So instead of the raw
``payment_id`` we mint an opaque token, store the token -> payment_id mapping
in Redis, and hand the client only the token. The status endpoint resolves the
token back to the real ``payment_id`` server-side.

Redis (not a DB column) keeps this migration-free; the mapping only powers the
status poll, and if it is ever lost the payment itself is still settled by the
provider webhook — only the on-page confirmation would degrade.
"""

import secrets

from core.redis_cache import cache_get, cache_set

_REF_PREFIX = "paid_ref:"
# A payer may sit on the SBP QR for a while; a week covers any legitimate poll
# window without meaningfully growing Redis.
_REF_TTL_SEC = 7 * 24 * 3600


async def issue_public_ref(payment_id: str) -> str:
    """Mints an opaque token for ``payment_id`` and stores the mapping.

    @param payment_id: Provider order id (``<ts>_<user_id>``) — never exposed
        to the client.
    @return: URL-safe opaque token to hand to the client in place of the id.
    """
    ref = secrets.token_urlsafe(24)
    await cache_set(f"{_REF_PREFIX}{ref}", payment_id, _REF_TTL_SEC)
    return ref


async def resolve_public_ref(ref: str) -> str | None:
    """Resolves an opaque token back to the real ``payment_id``.

    @param ref: Token previously issued by :func:`issue_public_ref`.
    @return: The mapped ``payment_id``, or ``None`` when unknown/expired.
    """
    value = await cache_get(f"{_REF_PREFIX}{ref}")
    return str(value) if value else None
