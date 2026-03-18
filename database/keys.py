import asyncio
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache_config import (
    KEY_COUNT_CACHE_TTL_SEC,
    KEY_DETAILS_CACHE_TTL_SEC,
    KEYS_LIST_CACHE_TTL_SEC,
)
from core.redis_cache import cache_delete, cache_get, cache_key, cache_set
from database.models import Key, Tariff, User
from database.users import invalidate_profile_cache, invalidate_user_snapshot
from logger import logger


async def invalidate_key_details(email: str) -> None:
    await cache_delete(cache_key("key_details", email))


async def invalidate_key_email(client_id: str) -> None:
    await cache_delete(cache_key("key_email", client_id))


async def invalidate_keys_list(tg_id: int) -> None:
    await cache_delete(cache_key("keys_list", tg_id))
    await cache_delete(cache_key("key_count", tg_id))
    await invalidate_profile_cache(tg_id)


async def invalidate_key_details_by_client_id(session: AsyncSession, client_id: str) -> None:
    email = await cache_get(cache_key("key_email", client_id))
    await cache_delete(cache_key("key_email", client_id))
    if email:
        await invalidate_key_details(str(email))
    else:
        res = await session.execute(select(Key.email).where(Key.client_id == client_id).limit(1))
        row = res.scalar_one_or_none()
        if row is not None:
            await invalidate_key_details(str(row))


async def store_key(
    session: AsyncSession,
    tg_id: int,
    client_id: str,
    email: str,
    expiry_time: int,
    key: str,
    server_id: str,
    remnawave_link: str = None,
    tariff_id: int | None = None,
    alias: str | None = None,
    selected_device_limit: int | None = None,
    selected_traffic_limit: int | None = None,
    selected_price_rub: int | None = None,
    current_device_limit: int | None = None,
    current_traffic_limit: int | None = None,
):
    """Сохраняет или обновляет ключ подписки."""
    try:
        exists = await session.execute(select(Key).where(Key.tg_id == tg_id, Key.client_id == client_id))
        existing_key = exists.scalar_one_or_none()

        if existing_key:
            values: dict = {
                "email": email,
                "expiry_time": expiry_time,
                "key": key,
                "server_id": server_id,
                "remnawave_link": remnawave_link,
                "tariff_id": tariff_id,
                "alias": alias,
            }

            if selected_device_limit is not None:
                values["selected_device_limit"] = selected_device_limit
            if selected_traffic_limit is not None:
                values["selected_traffic_limit"] = selected_traffic_limit
            if selected_price_rub is not None:
                values["selected_price_rub"] = selected_price_rub
            if current_device_limit is not None:
                values["current_device_limit"] = current_device_limit
            if current_traffic_limit is not None:
                values["current_traffic_limit"] = current_traffic_limit

            await session.execute(update(Key).where(Key.tg_id == tg_id, Key.client_id == client_id).values(**values))
            logger.info(f"[Store Key] Ключ обновлён: tg_id={tg_id}, client_id={client_id}, server_id={server_id}")
        else:
            if current_device_limit is None:
                current_device_limit = selected_device_limit
            if current_traffic_limit is None:
                current_traffic_limit = selected_traffic_limit

            new_key = Key(
                tg_id=tg_id,
                client_id=client_id,
                email=email,
                created_at=int(datetime.utcnow().timestamp() * 1000),
                expiry_time=expiry_time,
                key=key,
                server_id=server_id,
                remnawave_link=remnawave_link,
                tariff_id=tariff_id,
                alias=alias,
                selected_device_limit=selected_device_limit,
                selected_traffic_limit=selected_traffic_limit,
                selected_price_rub=selected_price_rub,
                current_device_limit=current_device_limit,
                current_traffic_limit=current_traffic_limit,
            )
            add_result = session.add(new_key)
            if asyncio.iscoroutine(add_result):
                await add_result
            logger.info(f"[Store Key] Ключ создан: tg_id={tg_id}, client_id={client_id}, server_id={server_id}")

        await session.commit()
        invalidate_user_snapshot(tg_id)
        await invalidate_keys_list(tg_id)
        await invalidate_key_details(email)

    except SQLAlchemyError as e:
        logger.error(f"❌ Ошибка при сохранении ключа: {e}")
        await session.rollback()
        raise


def _key_to_cache_dict(k: Key) -> dict:
    return {
        "email": k.email,
        "alias": k.alias,
        "client_id": k.client_id,
        "expiry_time": int(k.expiry_time) if k.expiry_time is not None else 0,
        "created_at": int(k.created_at) if k.created_at is not None else 0,
        "tariff_id": k.tariff_id,
        "server_id": k.server_id,
        "key": k.key,
        "remnawave_link": k.remnawave_link,
        "is_frozen": bool(k.is_frozen) if k.is_frozen is not None else False,
    }


async def get_keys(session: AsyncSession, tg_id: int):
    ckey = cache_key("keys_list", tg_id)
    cached = await cache_get(ckey)
    if isinstance(cached, list):
        return [SimpleNamespace(**d) for d in cached]
    result = await session.execute(select(Key).where(Key.tg_id == tg_id))
    rows = result.scalars().all()
    serialized = [_key_to_cache_dict(k) for k in rows]
    await cache_set(ckey, serialized, KEYS_LIST_CACHE_TTL_SEC)
    return rows


async def get_all_keys(session: AsyncSession):
    result = await session.execute(select(Key))
    return result.scalars().all()


async def get_key_by_server(session: AsyncSession, tg_id: int, client_id: str):
    stmt = select(Key).where(Key.tg_id == tg_id, Key.client_id == client_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_key_by_email(session: AsyncSession, email: str, tg_id: int | None = None) -> Key | None:
    stmt = select(Key).where(Key.email == email)
    if tg_id is not None:
        stmt = stmt.where(Key.tg_id == tg_id)
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def get_key_by_client_id(session: AsyncSession, client_id: str, tg_id: int | None = None) -> Key | None:
    stmt = select(Key).where(Key.client_id == client_id)
    if tg_id is not None:
        stmt = stmt.where(Key.tg_id == tg_id)
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def get_key_expiry_presets(session: AsyncSession, email: str) -> tuple[str | None, list[int]]:
    key_obj = await get_key_by_email(session, email)
    if not key_obj:
        return None, []

    if not key_obj.tariff_id:
        return key_obj.client_id, []

    tariff = await session.execute(select(Tariff.group_code).where(Tariff.id == key_obj.tariff_id))
    group_code = tariff.scalar_one_or_none()
    if not group_code:
        return key_obj.client_id, []

    result = await session.execute(
        select(Tariff.duration_days)
        .where(Tariff.group_code == group_code, Tariff.is_active.is_(True))
        .order_by(Tariff.duration_days)
    )
    unique_durations: list[int] = []
    seen: set[int] = set()
    for (days,) in result.all():
        if days is None or days < 1 or days in seen:
            continue
        seen.add(int(days))
        unique_durations.append(int(days))

    return key_obj.client_id, unique_durations


async def get_key_details(session: AsyncSession, email: str) -> dict | None:
    """Возвращает подробную информацию о ключе по email. Горячие данные кэшируются в Redis."""
    ckey = cache_key("key_details", email)
    cached = await cache_get(ckey)
    if isinstance(cached, dict):
        return cached

    stmt = select(Key, User).join(User, Key.tg_id == User.tg_id).where(Key.email == email)
    result = await session.execute(stmt)
    row = result.first()
    if not row:
        return None

    key, user = row
    expiry_date = datetime.utcfromtimestamp(key.expiry_time / 1000)
    current_date = datetime.utcnow()
    time_left = expiry_date - current_date

    if time_left.total_seconds() <= 0:
        days_left_message = "<b>Ключ истек.</b>"
    elif time_left.days > 0:
        days_left_message = f"Осталось дней: <b>{time_left.days}</b>"
    else:
        hours_left = time_left.seconds // 3600
        days_left_message = f"Осталось часов: <b>{hours_left}</b>"

    out = {
        "key": key.key,
        "remnawave_link": key.remnawave_link,
        "server_id": key.server_id,
        "created_at": key.created_at,
        "expiry_time": key.expiry_time,
        "client_id": key.client_id,
        "tg_id": user.tg_id,
        "email": key.email,
        "is_frozen": key.is_frozen,
        "balance": user.balance,
        "alias": key.alias,
        "expiry_date": expiry_date.strftime("%d %B %Y года %H:%M"),
        "days_left_message": days_left_message,
        "link": key.key or key.remnawave_link,
        "cluster_name": key.server_id,
        "location_name": key.server_id,
        "tariff_id": key.tariff_id,
        "selected_device_limit": key.selected_device_limit,
        "selected_traffic_limit": key.selected_traffic_limit,
        "selected_price_rub": key.selected_price_rub,
        "current_device_limit": key.current_device_limit,
        "current_traffic_limit": key.current_traffic_limit,
    }
    await cache_set(ckey, out, KEY_DETAILS_CACHE_TTL_SEC)
    if key.client_id:
        await cache_set(cache_key("key_email", key.client_id), email, KEY_DETAILS_CACHE_TTL_SEC)
    return out


async def get_key_count(session: AsyncSession, tg_id: int) -> int:
    cached = await cache_get(cache_key("key_count", tg_id))
    if cached is not None:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass
    result = await session.execute(select(func.count()).select_from(Key).where(Key.tg_id == tg_id))
    count = result.scalar() or 0
    await cache_set(cache_key("key_count", tg_id), count, KEY_COUNT_CACHE_TTL_SEC)
    return count


async def delete_key(session: AsyncSession, identifier: int | str, commit: bool = True):
    tg_id_for_cache = None
    email_for_cache = None
    if isinstance(identifier, str):
        res = await session.execute(
            select(Key.tg_id, Key.email).where(Key.client_id == identifier).limit(1)
        )
        row = res.first()
        if row:
            tg_id_for_cache, email_for_cache = row[0], row[1]
        await cache_delete(cache_key("key_email", identifier))
    else:
        tg_id_for_cache = identifier
    stmt = delete(Key).where(Key.tg_id == identifier if isinstance(identifier, int) else Key.client_id == identifier)
    await session.execute(stmt)
    if commit:
        await session.commit()
    if tg_id_for_cache is not None:
        invalidate_user_snapshot(tg_id_for_cache)
        await invalidate_keys_list(tg_id_for_cache)
    if email_for_cache is not None:
        await invalidate_key_details(str(email_for_cache))
    logger.info(f"Ключ с идентификатором {identifier} удалён")


async def update_key_expiry(session: AsyncSession, client_id: str, new_expiry_time: int):
    await session.execute(update(Key).where(Key.client_id == client_id).values(expiry_time=new_expiry_time))
    await session.commit()
    await invalidate_key_details_by_client_id(session, client_id)
    logger.info(f"Срок действия ключа {client_id} обновлён до {new_expiry_time}")


async def get_client_id_by_email(session: AsyncSession, email: str):
    result = await session.execute(select(Key.client_id).where(Key.email == email))
    return result.scalar_one_or_none()


async def update_key_notified(session: AsyncSession, tg_id: int, client_id: str):
    await session.execute(update(Key).where(Key.tg_id == tg_id, Key.client_id == client_id).values(notified=True))
    await session.commit()
    await invalidate_keys_list(tg_id)
    await invalidate_key_details_by_client_id(session, client_id)


async def mark_key_as_frozen(session: AsyncSession, tg_id: int, client_id: str, time_left: int):
    await session.execute(
        text(
            """
            UPDATE keys
            SET expiry_time = :expiry,
                is_frozen = TRUE
            WHERE tg_id = :tg_id
              AND client_id = :client_id
            """
        ),
        {"expiry": time_left, "tg_id": tg_id, "client_id": client_id},
    )
    await invalidate_keys_list(tg_id)
    await invalidate_key_details_by_client_id(session, client_id)


async def mark_key_as_unfrozen(
    session: AsyncSession,
    tg_id: int,
    client_id: str,
    new_expiry_time: int,
):
    await session.execute(
        text(
            """
            UPDATE keys
            SET expiry_time = :expiry,
                is_frozen = FALSE
            WHERE tg_id = :tg_id
              AND client_id = :client_id
            """
        ),
        {"expiry": new_expiry_time, "tg_id": tg_id, "client_id": client_id},
    )
    await invalidate_keys_list(tg_id)
    await invalidate_key_details_by_client_id(session, client_id)


async def update_key_tariff(session: AsyncSession, client_id: str, tariff_id: int):
    await session.execute(update(Key).where(Key.client_id == client_id).values(tariff_id=tariff_id))
    await session.commit()
    await invalidate_key_details_by_client_id(session, client_id)
    logger.info(f"Тариф ключа {client_id} обновлён на {tariff_id}")


async def get_subscription_link(session: AsyncSession, email: str) -> str | None:
    result = await session.execute(select(func.coalesce(Key.key, Key.remnawave_link)).where(Key.email == email))
    return result.scalar_one_or_none()


async def update_key_client_id(session: AsyncSession, email: str, new_client_id: str):
    await session.execute(update(Key).where(Key.email == email).values(client_id=new_client_id))
    await session.commit()
    await invalidate_key_details(email)
    logger.info(f"client_id обновлён для {email} -> {new_client_id}")


async def update_key_link(session: AsyncSession, email: str, link: str) -> bool:
    q = update(Key).where(Key.email == email).values(key=link).returning(Key.client_id)
    res = await session.execute(q)
    await session.commit()
    ok = res.scalar_one_or_none() is not None
    if ok:
        await invalidate_key_details(email)
    return ok


async def update_key_subscription_links(session: AsyncSession, email: str, link: str) -> bool:
    stmt = (
        update(Key)
        .where(Key.email == email)
        .values(
            key=link,
            remnawave_link=link,
        )
        .returning(Key.client_id)
    )
    res = await session.execute(stmt)
    await session.commit()
    ok = res.scalar_one_or_none() is not None
    if ok:
        await invalidate_key_details(email)
    return ok


async def save_key_config_with_mode(
    session: AsyncSession,
    email: str,
    selected_devices: int | None,
    selected_traffic_gb: int | None,
    total_price: int,
    has_device_choice: bool,
    has_traffic_choice: bool,
    config_mode: str,
) -> None:
    values: dict = {}

    if config_mode == "pack":
        if has_device_choice and selected_devices is not None:
            values["current_device_limit"] = int(selected_devices)
        if has_traffic_choice and selected_traffic_gb is not None:
            values["current_traffic_limit"] = int(selected_traffic_gb)
    else:
        device_val = int(selected_devices) if selected_devices is not None and has_device_choice else None
        traffic_val = int(selected_traffic_gb) if selected_traffic_gb is not None and has_traffic_choice else None

        values["selected_device_limit"] = device_val
        values["selected_traffic_limit"] = traffic_val
        values["selected_price_rub"] = int(total_price)
        values["current_device_limit"] = device_val
        values["current_traffic_limit"] = traffic_val

    if not values:
        return

    await session.execute(update(Key).where(Key.email == email).values(**values))
    await invalidate_key_details(email)


async def reset_key_tariff_state(session: AsyncSession, tg_id: int, email: str, tariff_id: int) -> None:
    await session.execute(
        update(Key)
        .where(Key.tg_id == tg_id, Key.email == email)
        .values(
            tariff_id=tariff_id,
            selected_device_limit=None,
            current_device_limit=None,
            selected_traffic_limit=None,
            current_traffic_limit=None,
            selected_price_rub=None,
        )
    )
    await session.commit()
    await invalidate_keys_list(tg_id)
    await invalidate_key_details(email)


async def save_key_tariff_selection(
    session: AsyncSession,
    tg_id: int,
    email: str,
    tariff_id: int,
    selected_devices: int | None,
    selected_traffic_gb: int | None,
) -> None:
    selected_devices_val = int(selected_devices) if selected_devices is not None else None
    selected_traffic_val = int(selected_traffic_gb) if selected_traffic_gb is not None and int(selected_traffic_gb) > 0 else None

    await session.execute(
        update(Key)
        .where(Key.tg_id == tg_id, Key.email == email)
        .values(
            tariff_id=tariff_id,
            selected_device_limit=selected_devices_val,
            current_device_limit=selected_devices_val,
            selected_traffic_limit=selected_traffic_val,
            current_traffic_limit=selected_traffic_val,
            selected_price_rub=None,
        )
    )
    await session.commit()
    await invalidate_keys_list(tg_id)
    await invalidate_key_details(email)


async def save_admin_key_config(
    session: AsyncSession,
    email: str,
    base_devices: int,
    total_devices: int,
    base_traffic: int | None,
    total_traffic: int | None,
    selected_price: int | None,
) -> None:
    await session.execute(
        update(Key)
        .where(Key.email == email)
        .values(
            selected_device_limit=base_devices,
            current_device_limit=total_devices,
            selected_traffic_limit=base_traffic,
            current_traffic_limit=total_traffic,
            selected_price_rub=selected_price,
        )
    )
    await session.commit()
    await invalidate_key_details(email)


async def reset_key_current_limits_to_selected(session: AsyncSession, client_id: str):
    """Сбрасывает текущие лимиты к выбранным для ключа."""
    await session.execute(
        text(
            """
            UPDATE keys
            SET current_device_limit = selected_device_limit,
                current_traffic_limit = selected_traffic_limit
            WHERE client_id = :client_id
            """
        ),
        {"client_id": client_id},
    )
    await session.commit()
    await invalidate_key_details_by_client_id(session, client_id)
    logger.info(f"Текущие лимиты ключа {client_id} сброшены к выбранным")
