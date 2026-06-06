"""Decision helper for identity merges where both sides own ``Key`` rows.

The bare ``attach_email`` / ``attach_telegram`` guard used to refuse the
merge whenever both billing users had any keys at all. That bucketed a
freshly-activated trial together with a paid yearly subscription and
blocked legitimate web↔Telegram joins the moment either side had even
clicked «активировать пробник».

Andrey's matrix (NONE / TRIAL / EXPIRED / ACTIVE) is the right
discrimination layer:

* a stronger status (``ACTIVE`` > ``EXPIRED`` > ``TRIAL`` > ``NONE``)
  always wins outright — the loser's keys are dropped;
* a tie on ``TRIAL`` / ``EXPIRED`` keeps the **newer** key (most recent
  ``created_at``);
* a tie on ``ACTIVE`` is the only outright refusal — we can't pick a
  paid subscription to discard;
* a tie on ``NONE`` is a no-op (no keys anywhere).

The helper *mutates* the database: it deletes the loser's keys via
``database.keys.delete_key`` (which also invalidates the Redis
``keys_list`` / ``key_count`` cache). Caller is expected to either let
the surrounding transaction commit or roll back.

We don't talk to the VPN backend (Remnawave) here — neither does the
existing ``delete_key``. The unreachable zombie client on the VPN side
will fall off naturally at ``expiry_time``.
"""

import time
from enum import Enum
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.keys import delete_key, get_keys
from database.models import Tariff
from logger import logger


class SubStatus(Enum):
    """Best subscription a user currently holds."""

    NONE = 0
    """No keys at all."""

    TRIAL = 1
    """Has a non-expired key whose tariff is priced at 0 (a trial)."""

    EXPIRED = 2
    """Has keys but every one of them is past ``expiry_time``."""

    ACTIVE = 3
    """Has a non-expired key on a paid (price_rub > 0) tariff."""


_PRIORITY = {
    SubStatus.NONE: 0,
    SubStatus.TRIAL: 1,
    SubStatus.EXPIRED: 2,
    SubStatus.ACTIVE: 3,
}

MergeOutcome = Literal["keep_dst", "use_src", "fail"]


async def _trial_tariff_ids(session: AsyncSession, tariff_ids: set[int]) -> set[int]:
    """Returns the subset of ``tariff_ids`` whose ``Tariff.price_rub == 0``.

    Args:
        session: Active SQLAlchemy session.
        tariff_ids: Candidate tariff ids to filter (typically gathered from
            the user's keys).

    Returns:
        Set of ids classified as trial — empty when none qualify.
    """
    if not tariff_ids:
        return set()
    rows = (
        await session.execute(
            select(Tariff.id).where(Tariff.id.in_(tariff_ids), Tariff.price_rub == 0)
        )
    ).scalars().all()
    return {int(r) for r in rows}


async def classify_subscription(session: AsyncSession, user_id: int) -> tuple[SubStatus, int]:
    """Determines the best subscription a billing user currently holds.

    Args:
        session: Active SQLAlchemy session.
        user_id: Legacy ``users.id``.

    Returns:
        ``(status, newest_relevant_created_at)`` — the second value is
        the ``created_at`` of the key that drove the status decision
        (used to break ties on ``TRIAL`` / ``EXPIRED``). Returns ``0``
        for ``SubStatus.NONE``.
    """
    keys = await get_keys(session, int(user_id))
    if not keys:
        return SubStatus.NONE, 0

    now_ms = int(time.time() * 1000)
    tariff_ids = {
        int(getattr(k, "tariff_id"))
        for k in keys
        if getattr(k, "tariff_id", None) is not None
    }
    trial_ids = await _trial_tariff_ids(session, tariff_ids)

    best_active_at = 0
    best_trial_at = 0
    best_expired_at = 0

    for k in keys:
        expiry = int(getattr(k, "expiry_time", 0) or 0)
        created = int(getattr(k, "created_at", 0) or 0)
        tariff = getattr(k, "tariff_id", None)
        is_trial = tariff is not None and int(tariff) in trial_ids
        is_active = expiry > now_ms

        if is_active and not is_trial:
            best_active_at = max(best_active_at, created)
        elif is_active and is_trial:
            best_trial_at = max(best_trial_at, created)
        else:
            best_expired_at = max(best_expired_at, created)

    if best_active_at:
        return SubStatus.ACTIVE, best_active_at
    if best_trial_at:
        return SubStatus.TRIAL, best_trial_at
    if best_expired_at:
        return SubStatus.EXPIRED, best_expired_at
    return SubStatus.NONE, 0


async def prepare_keys_for_merge(
    session: AsyncSession,
    src_user_id: int,
    dst_user_id: int,
) -> MergeOutcome:
    """Decides whose keys survive a planned identity merge and clears
    the loser's.

    Decision matrix (rows = src status, columns = dst status, values =
    outcome):

    .. code-block:: text

                  N        T        E        A
        N         dst      dst      dst      dst
        T         src      newer    dst      dst
        E         src      src      newer    dst
        A         src      src      src      FAIL

    For ``dst`` outcomes we delete every ``src`` key; for ``src``
    outcomes we delete every ``dst`` key; for ``FAIL`` nothing changes.

    Args:
        session: Active SQLAlchemy session.
        src_user_id: Legacy ``users.id`` of the *source* billing user
            (the one whose identity the caller plans to absorb).
        dst_user_id: Legacy ``users.id`` of the *destination* billing
            user (the surviving identity).

    Returns:
        ``"keep_dst"`` — dst keys preserved, src keys deleted.
        ``"use_src"`` — src keys preserved, dst keys deleted.
        ``"fail"`` — both sides have active paid subscriptions; caller
        should bail out of the merge.
    """
    src_status, src_at = await classify_subscription(session, int(src_user_id))
    dst_status, dst_at = await classify_subscription(session, int(dst_user_id))

    p_src = _PRIORITY[src_status]
    p_dst = _PRIORITY[dst_status]

    if p_src > p_dst:
        winner: MergeOutcome = "use_src"
    elif p_dst > p_src:
        winner = "keep_dst"
    else:
        # Equal priority — break the tie by status.
        if src_status == SubStatus.NONE:
            winner = "keep_dst"  # nothing to do either way
        elif src_status == SubStatus.ACTIVE:
            return "fail"
        else:
            # TRIAL == TRIAL or EXPIRED == EXPIRED — newest wins.
            winner = "use_src" if src_at > dst_at else "keep_dst"

    if winner == "use_src":
        if dst_status != SubStatus.NONE:
            logger.info(
                "[Merge] dropping dst keys src={} dst={} src_status={} dst_status={}",
                src_user_id, dst_user_id, src_status.name, dst_status.name,
            )
            await delete_key(session, int(dst_user_id))
    else:  # keep_dst
        if src_status != SubStatus.NONE:
            logger.info(
                "[Merge] dropping src keys src={} dst={} src_status={} dst_status={}",
                src_user_id, dst_user_id, src_status.name, dst_status.name,
            )
            await delete_key(session, int(src_user_id))

    return winner
