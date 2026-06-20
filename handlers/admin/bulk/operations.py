from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_tariff_by_id, update_key_expiry
from database.keys import delete_key, mark_key_as_frozen, mark_key_as_unfrozen
from database.models import Key
from logger import logger
from services.operations import (
    delete_key_from_cluster,
    renew_key_in_cluster,
    toggle_client_on_cluster,
)

DAY_MS = 86400 * 1000


async def _key_limits(session: AsyncSession, key: Key) -> tuple[int, int]:
    traffic = 0
    device = 0
    if key.tariff_id:
        tariff = await get_tariff_by_id(session, key.tariff_id)
        if tariff:
            traffic = int(tariff.get("traffic_limit") or 0)
            device = int(tariff.get("device_limit") or 0)
    if key.current_traffic_limit is not None:
        traffic = int(key.current_traffic_limit)
    if key.current_device_limit is not None:
        device = int(key.current_device_limit)
    return traffic, device


async def bulk_add_days(session: AsyncSession, keys: list[Key], days: int) -> tuple[int, int, int]:
    add_ms = days * DAY_MS
    ok = fail = skipped = 0
    for key in keys:
        if not key.client_id:
            skipped += 1
            continue
        try:
            traffic, device = await _key_limits(session, key)
            new_expiry = key.expiry_time + add_ms
            await renew_key_in_cluster(
                key.server_id,
                email=key.email,
                client_id=key.client_id,
                new_expiry_time=new_expiry,
                total_gb=traffic,
                session=session,
                hwid_device_limit=device,
                reset_traffic=False,
                plan=key.tariff_id,
            )
            await update_key_expiry(session, key.client_id, new_expiry)
            ok += 1
        except Exception as e:
            fail += 1
            logger.error(f"[Bulk] add_days {key.email}: {type(e).__name__}: {e!r}")
    return ok, fail, skipped


async def bulk_add_gb(session: AsyncSession, keys: list[Key], gb: int) -> tuple[int, int, int]:
    ok = fail = skipped = 0
    for key in keys:
        if not key.client_id:
            skipped += 1
            continue
        try:
            traffic, device = await _key_limits(session, key)
            if traffic <= 0:
                skipped += 1
                continue
            new_total = traffic + int(gb)
            await renew_key_in_cluster(
                key.server_id,
                email=key.email,
                client_id=key.client_id,
                new_expiry_time=key.expiry_time,
                total_gb=new_total,
                session=session,
                hwid_device_limit=device,
                reset_traffic=False,
                plan=key.tariff_id,
            )
            await session.execute(
                update(Key).where(Key.client_id == key.client_id).values(current_traffic_limit=new_total)
            )
            ok += 1
        except Exception as e:
            fail += 1
            logger.error(f"[Bulk] add_gb {key.email}: {type(e).__name__}: {e!r}")
    return ok, fail, skipped


async def bulk_delete(session: AsyncSession, keys: list[Key]) -> tuple[int, int, int]:
    ok = fail = skipped = 0
    for key in keys:
        if not key.client_id:
            skipped += 1
            continue
        try:
            await delete_key_from_cluster(key.server_id, key.email, key.client_id, session=session)
            await delete_key(session, key.client_id)
            ok += 1
        except Exception as e:
            fail += 1
            logger.error(f"[Bulk] delete {key.email}: {type(e).__name__}: {e!r}")
    return ok, fail, skipped


async def bulk_freeze(session: AsyncSession, keys: list[Key]) -> tuple[int, int, int]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ok = fail = skipped = 0
    for key in keys:
        if not key.client_id or key.is_frozen:
            skipped += 1
            continue
        try:
            result = await toggle_client_on_cluster(
                key.server_id, key.email, key.client_id, enable=False, session=session
            )
            if result.get("status") != "success":
                fail += 1
                continue
            time_left = max(0, key.expiry_time - now_ms)
            await mark_key_as_frozen(session, key.tg_id or key.user_id, key.client_id, time_left)
            ok += 1
        except Exception as e:
            fail += 1
            logger.error(f"[Bulk] freeze {key.email}: {type(e).__name__}: {e!r}")
    return ok, fail, skipped


async def bulk_unfreeze(session: AsyncSession, keys: list[Key]) -> tuple[int, int, int]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ok = fail = skipped = 0
    for key in keys:
        if not key.client_id or not key.is_frozen:
            skipped += 1
            continue
        try:
            result = await toggle_client_on_cluster(
                key.server_id, key.email, key.client_id, enable=True, session=session
            )
            if result.get("status") != "success":
                fail += 1
                continue
            traffic, device = await _key_limits(session, key)
            new_expiry = now_ms + max(0, key.expiry_time)
            await renew_key_in_cluster(
                key.server_id,
                email=key.email,
                client_id=key.client_id,
                new_expiry_time=new_expiry,
                total_gb=traffic,
                session=session,
                hwid_device_limit=device,
                reset_traffic=False,
                plan=key.tariff_id,
            )
            await mark_key_as_unfrozen(session, key.tg_id or key.user_id, key.client_id, new_expiry)
            ok += 1
        except Exception as e:
            fail += 1
            logger.error(f"[Bulk] unfreeze {key.email}: {type(e).__name__}: {e!r}")
    return ok, fail, skipped
