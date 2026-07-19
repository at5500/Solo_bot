"""Nightly cleanup of abandoned guest-checkout identities.

The guest email-purchase flow (``purchase-by-email``) creates an Identity +
billing User *before* payment. When the buyer never pays, an empty, unverified,
unclaimed account is left behind — table clutter that anyone can mass-produce
by submitting throwaway emails. This job deletes ONLY such leftovers, on a
deliberately narrow filter so a real (even not-yet-paying) user is never hit:

    identity.email_verified is false      (never confirmed the email)
    AND identity.tg_id is null            (no Telegram attached)
    AND google_sub / yandex_sub / password_hash all null  (no other login)
    AND its billing user(s): no keys, zero balance, no SUCCESSFUL payment
    AND identity older than N hours       (not mid-payment right now)

The successful-payment guard matters: if someone paid but the key has not
materialised yet, the ``success`` Payment row keeps the account safe from
deletion.

Dry-run by default (``GUEST_CLEANUP_DRY_RUN`` != "false"): it logs how many
rows it WOULD delete without touching anything, so the filter can be verified
against real data before deletion is switched on.
"""

import os
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Identity, Key, Payment, User
from database.models.users import TemporaryData
from logger import logger

# Old enough that an in-progress checkout (identity created, user still on the
# payment page) is never caught.
_OLDER_THAN_HOURS = 24
# Cap per run so a one-off surge can never delete an unbounded slice in a
# single transaction.
_BATCH_LIMIT = 500


def _dry_run() -> bool:
    """Whether the job only reports (default) instead of deleting.

    @return: True unless ``GUEST_CLEANUP_DRY_RUN`` is explicitly "false".
    """
    return os.getenv("GUEST_CLEANUP_DRY_RUN", "true").strip().lower() != "false"


async def cleanup_abandoned_guest_identities(session: AsyncSession) -> int:
    """Deletes abandoned, never-paid guest-checkout identities.

    See the module docstring for the exact (intentionally narrow) filter.

    @param session: Active async DB session (committed by the caller).
    @return: Number of identities deleted (0 in dry-run, even if candidates
        were found — the count is logged instead).
    """
    cutoff = datetime.utcnow() - timedelta(hours=_OLDER_THAN_HOURS)
    candidate_ids = [
        row[0]
        for row in (
            await session.execute(
                select(Identity.id)
                .where(
                    Identity.email_verified.is_(False),
                    Identity.tg_id.is_(None),
                    Identity.google_sub.is_(None),
                    Identity.yandex_sub.is_(None),
                    Identity.password_hash.is_(None),
                    Identity.created_at < cutoff,
                )
                .limit(_BATCH_LIMIT)
            )
        ).all()
    ]
    if not candidate_ids:
        return 0

    # Keep only identities whose billing users are all empty. A single
    # non-empty user (key / balance / successful payment) spares the identity.
    doomed: list[tuple[str, list[int], list[int]]] = []
    for identity_id in candidate_ids:
        users = (
            (await session.execute(select(User).where(User.identity_id == identity_id))).scalars().all()
        )
        keep = False
        for u in users:
            has_key = await session.scalar(select(exists().where(Key.user_id == u.id)))
            if not has_key and u.tg_id is not None:
                has_key = await session.scalar(select(exists().where(Key.tg_id == u.tg_id)))
            has_paid = await session.scalar(
                select(exists().where((Payment.user_id == u.id) & (func.lower(Payment.status) == "success")))
            )
            if has_key or has_paid or float(u.balance or 0) > 0:
                keep = True
                break
        if not keep:
            doomed.append(
                (identity_id, [u.id for u in users], [u.tg_id for u in users if u.tg_id is not None])
            )

    if not doomed:
        return 0

    if _dry_run():
        logger.info(
            "[GuestCleanup] dry-run: удалил бы {} брошенных гостевых identity "
            "(email не подтверждён, без TG, без ключей/денег/оплат, старше {}ч). "
            "Установите GUEST_CLEANUP_DRY_RUN=false для реального удаления.",
            len(doomed),
            _OLDER_THAN_HOURS,
        )
        return 0

    deleted = 0
    for identity_id, user_ids, tg_ids in doomed:
        # Each identity in its own savepoint: a stray FK to users.tg_id without
        # ON DELETE CASCADE (e.g. gifts) would otherwise abort the whole batch.
        # Rolling back one leftover keeps the rest of the run deleting.
        try:
            async with session.begin_nested():
                # Payments cascade on user delete (FK ondelete=CASCADE);
                # temporary_data keyed by the (synthetic) tg_id is removed here.
                if tg_ids:
                    await session.execute(delete(TemporaryData).where(TemporaryData.tg_id.in_(tg_ids)))
                if user_ids:
                    await session.execute(delete(User).where(User.id.in_(user_ids)))
                await session.execute(delete(Identity).where(Identity.id == identity_id))
            deleted += 1
        except Exception as error:
            logger.warning("[GuestCleanup] Пропущена identity {} при удалении: {}", identity_id, error)

    logger.info("[GuestCleanup] удалено брошенных гостевых identity: {}", deleted)
    return deleted
