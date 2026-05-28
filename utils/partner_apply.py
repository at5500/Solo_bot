"""Idempotent helper that attaches a partner referral to an identity.

Shared between the email-OTP signup path (``send-login-code`` with a
``partner_code`` body field) and the Mini App entry path
(``apply-partner-code`` endpoint, called when ``start_param`` is a
``partner_<code>`` deep link). Both call sites want the same outcome:

* normalise the incoming string (strip URL noise, ``partner_`` prefix),
* resolve it to a legacy ``users.id`` via :func:`decode_partner_code`,
* skip silently if the identity already has a referral, points at the
  caller themselves, or the inviter doesn't exist.

Returning a boolean ``applied`` flag lets the caller log/respond
appropriately without re-implementing all the silent-skip rules.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from database import identities as idb
from database.access.resolution import resolve_user_optional
from database.referrals import add_referral, get_referral_by_referred_id
from logger import logger
from utils.referral_codes import decode_partner_code


def _normalize_code(raw: str | None) -> str:
    """Strips URL noise and ``partner_`` prefix to leave just the slug.

    Tolerates either a bare code (``p1_xxx`` or a custom slug), a
    ``partner_<code>`` deep-link payload, or a full URL like
    ``https://t.me/<bot>?start=partner_<code>``.

    Args:
        raw: Whatever the caller had at hand.

    Returns:
        Bare code, or empty string when nothing usable is left.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if "/" in s:
        s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/").split("/")[-1]
    if s.startswith("partner_"):
        s = s[len("partner_"):]
    return s


async def apply_partner_code(
    session: AsyncSession,
    identity,
    raw_code: str | None,
) -> bool:
    """Attaches the inviter behind ``raw_code`` to ``identity``.

    Silent no-op when the identity already has a referral, when the
    code doesn't resolve to any inviter, when the inviter would be the
    identity itself, or when the code is malformed.

    Does *not* commit — leaves that to the caller so this helper
    composes cleanly inside larger transactions.

    Args:
        session: Active SQLAlchemy session.
        identity: Authenticated identity object (must have ``id`` and
            be resolvable to a billing user via
            :func:`ensure_billing_user_for_identity`).
        raw_code: Partner code in any of the accepted forms (see
            :func:`_normalize_code`).

    Returns:
        ``True`` when a fresh referral row was added, ``False`` for
        every other outcome.
    """
    code = _normalize_code(raw_code)
    if not code:
        return False
    try:
        inviter_legacy = decode_partner_code(code)
    except Exception:
        inviter_legacy = None
    inviter = None
    if inviter_legacy is not None:
        inviter = await resolve_user_optional(session, inviter_legacy)
    if inviter is None:
        # ``decode_partner_code`` only handles ``p1_…``, ``r1_…`` and bare
        # numeric ids. Custom slugs the user picked themselves through
        # ``/api/v1/partners/.../partner_code`` live in ``users.partner_code``
        # as plain text — fall back to a direct lookup for those.
        try:
            row = await session.execute(
                text("SELECT id FROM users WHERE partner_code = :code LIMIT 1"),
                {"code": code},
            )
            fallback_id = row.scalar_one_or_none()
        except Exception:
            fallback_id = None
        if fallback_id is None:
            return False
        inviter = await resolve_user_optional(session, int(fallback_id))
        if inviter is None:
            return False
    billing_user_id = await idb.ensure_billing_user_for_identity(session, identity)
    if int(billing_user_id) == int(inviter.id):
        return False
    if await get_referral_by_referred_id(session, billing_user_id):
        return False
    await add_referral(session, billing_user_id, inviter.id)
    logger.info(
        "[Partner] Referral attached: identity={} inviter={}", identity.id, inviter.id
    )
    return True
