import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import pytz
from aiogram import Bot, Router
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import (
    EXECUTOR_POOL_SIZE,
    NOTIFICATION_TIME,
    NOTIFY_10H_ENABLED,
    NOTIFY_10H_HOURS,
    NOTIFY_24H_ENABLED,
    NOTIFY_24H_HOURS,
    NOTIFY_DELETE_DELAY,
    NOTIFY_DELETE_KEY,
    NOTIFY_HOT_LEADS,
    NOTIFY_INACTIVE_TRAFFIC,
    NOTIFY_RENEW,
    NOTIFY_RENEW_EXPIRED,
    TRIAL_TIME_DISABLE,
)
from core.bootstrap import MODES_CONFIG, NOTIFICATIONS_CONFIG
from database import (
    add_notification,
    bulk_add_notifications,
    bulk_delete_notifications,
    check_notification_time,
    check_notification_time_bulk,
    check_notifications_bulk,
    delete_key,
    delete_notification,
    get_all_keys,
    get_balance,
    get_last_notification_time,
    get_last_notification_times_bulk,
    update_balance,
    update_key_expiry,
    update_key_tariff,
)
from database.models import Key, Tariff, User
from database.tariffs import (
    check_tariff_exists,
    get_tariff_by_id,
    get_tariffs_for_cluster,
)
from handlers.keys.operations import delete_key_from_cluster, renew_key_in_cluster
from handlers.notifications.notify_kb import (
    build_change_tariff_kb,
    build_notification_expired_kb,
    build_notification_kb,
)
from handlers.tariffs.tariff_display import GB, get_effective_limits_for_key, resolve_price_to_charge
from handlers.texts import (
    KEY_CANNOT_RENEW_CURRENT,
    KEY_DELETED_MSG,
    KEY_EXPIRED_DELAY_MSG,
    KEY_EXPIRED_NO_DELAY_MSG,
    KEY_EXPIRY,
    get_renewal_message,
)
from handlers.utils import format_hours, format_minutes, get_russian_month
from hooks.hooks import run_hooks
from logger import logger
from middlewares.session import release_session_early, wrap_session

from .hot_leads_notifications import notify_hot_leads
from .notify_utils import (
    NotificationRateLimiter,
    prepare_key_expiry_data,
    send_messages_with_limit,
    send_notification,
)
from .special_notifications import notify_inactive_trial_users, notify_users_no_traffic


router = Router()
moscow_tz = pytz.timezone("Europe/Moscow")
notification_lock = asyncio.Lock()


@dataclass
class NotificationContext:
    bot: Bot
    session: AsyncSession
    current_time: int
    preload_data: Optional[dict] = None
    bulk_updates: Optional[dict] = None

    def get_balance(self, tg_id: int) -> float:
        if self.preload_data and tg_id in self.preload_data.get("balances_cache", {}):
            return self.preload_data["balances_cache"][tg_id]
        return 0.0

    def get_tariff(self, tariff_id: int) -> Optional[dict]:
        if self.preload_data and tariff_id in self.preload_data.get("tariffs_cache", {}):
            return self.preload_data["tariffs_cache"][tariff_id]
        return None


async def preload_notification_data(session: AsyncSession) -> dict[str, Any]:
    stmt = (
        select(
            Key,
            Tariff,
            User.balance.label("user_balance"),
        )
        .outerjoin(Tariff, Key.tariff_id == Tariff.id)
        .outerjoin(User, Key.tg_id == User.tg_id)
        .where(Key.is_frozen.is_(False))
    )

    result = await session.execute(stmt)
    rows = result.all()

    keys_data = {}
    tariffs_cache = {}
    balances_cache = {}

    for row in rows:
        key = row[0]
        tariff = row[1]
        balance = row[2] or 0.0

        key_dict = {
            "key": key,
            "tariff": dict(tariff.__dict__) if tariff else None,
            "balance": float(balance),
        }
        keys_data[key.client_id] = key_dict

        if tariff and tariff.id not in tariffs_cache:
            tariffs_cache[tariff.id] = dict(tariff.__dict__)

        balances_cache[key.tg_id] = float(balance)

    return {
        "keys_data": keys_data,
        "tariffs_cache": tariffs_cache,
        "balances_cache": balances_cache,
    }


async def execute_bulk_updates(session: AsyncSession, bulk_updates: dict[str, Any]) -> None:
    try:
        balance_changes = bulk_updates.get("balance_changes") or {}
        if balance_changes:
            tg_ids = list(balance_changes.keys())
            changes = [balance_changes[tg_id] for tg_id in tg_ids]
            await session.execute(
                text(
                    "UPDATE users SET balance = balance + v.change FROM "
                    "(SELECT unnest(CAST(:tg_ids AS bigint[])) AS tg_id, unnest(CAST(:changes AS double precision[])) AS change) AS v "
                    "WHERE users.tg_id = v.tg_id"
                ),
                {"tg_ids": tg_ids, "changes": changes},
            )
            logger.info(f"Bulk: обновлено {len(balance_changes)} балансов")

        key_expiry = bulk_updates.get("key_expiry_updates") or []
        if key_expiry:
            await session.run_sync(
                lambda sync_sess: sync_sess.bulk_update_mappings(
                    Key,
                    [{"client_id": cid, "expiry_time": exp} for cid, exp in key_expiry],
                )
            )
            logger.info(f"Bulk: обновлено {len(key_expiry)} сроков действия ключей")

        key_tariff = bulk_updates.get("key_tariff_updates") or []
        if key_tariff:
            await session.run_sync(
                lambda sync_sess: sync_sess.bulk_update_mappings(
                    Key,
                    [{"client_id": cid, "tariff_id": tid} for cid, tid in key_tariff],
                )
            )
            logger.info(f"Bulk: обновлено {len(key_tariff)} тарифов ключей")

        to_add = bulk_updates.get("notifications_to_add") or []
        if to_add:
            await bulk_add_notifications(session, to_add, commit=False)

        to_delete = bulk_updates.get("notifications_to_delete") or []
        if to_delete:
            await bulk_delete_notifications(session, to_delete, commit=False)

        if to_add or to_delete:
            logger.info(
                f"Bulk: обработано {len(to_add)} добавлений и {len(to_delete)} удалений уведомлений"
            )

        await session.commit()

    except Exception as error:
        logger.error(f"Ошибка в bulk-обновлениях: {error}")
        await session.rollback()
        raise


async def send_expiry_warning(ctx: NotificationContext, key, hours_left: int, photo: str) -> bool:
    tg_id = key.tg_id
    email = key.email or ""

    expiry_data = await prepare_key_expiry_data(key, ctx.session, ctx.current_time)

    message_text = KEY_EXPIRY.format(
        email=email,
        hours_left_formatted=expiry_data["hours_left_formatted"],
        formatted_expiry_date=expiry_data["formatted_expiry_date"],
        tariff_name=expiry_data["tariff_name"],
        tariff_details=expiry_data["tariff_details"],
    )

    keyboard = build_notification_kb(email, getattr(key, "client_id", None))
    return await send_notification(ctx.bot, tg_id, photo, message_text, keyboard)


async def send_cannot_renew(ctx: NotificationContext, key, photo: str) -> bool:
    tg_id = key.tg_id
    email = key.email or ""

    expiry_data = await prepare_key_expiry_data(key, ctx.session, ctx.current_time)

    message_text = KEY_CANNOT_RENEW_CURRENT.format(
        email=email,
        hours_left_formatted=expiry_data["hours_left_formatted"],
        formatted_expiry_date=expiry_data["formatted_expiry_date"],
        tariff_name=expiry_data["tariff_name"],
        tariff_details=expiry_data["tariff_details"],
    )

    keyboard = build_change_tariff_kb(email, getattr(key, "client_id", None))
    return await send_notification(ctx.bot, tg_id, photo, message_text, keyboard)


async def send_expired_notification(ctx: NotificationContext, key, delay_minutes: int) -> bool:
    tg_id = key.tg_id
    email = key.email or ""

    if delay_minutes > 0:
        hours = delay_minutes // 60
        minutes = delay_minutes % 60
        if hours > 0 and minutes > 0:
            time_formatted = f"{format_hours(hours)} и {format_minutes(minutes)}"
        elif hours > 0:
            time_formatted = format_hours(hours)
        else:
            time_formatted = format_minutes(minutes)
        message_text = KEY_EXPIRED_DELAY_MSG.format(email=email, time_formatted=time_formatted)
    else:
        message_text = KEY_EXPIRED_NO_DELAY_MSG.format(email=email)

    keyboard = build_notification_kb(email, getattr(key, "client_id", None))
    return await send_notification(ctx.bot, tg_id, "notify_expired.jpg", message_text, keyboard)


async def send_deleted_notification(ctx: NotificationContext, key) -> bool:
    tg_id = key.tg_id
    email = key.email or ""

    message_text = KEY_DELETED_MSG.format(email=email)
    keyboard = build_notification_expired_kb()
    return await send_notification(ctx.bot, tg_id, "notify_expired.jpg", message_text, keyboard)


async def send_renewed_notification(ctx: NotificationContext, key, tariff: dict, new_expiry_time: int) -> bool:
    tg_id = key.tg_id
    email = key.email or ""

    selected_device_limit = getattr(key, "selected_device_limit", None)
    selected_traffic_limit = getattr(key, "selected_traffic_limit", None)
    selected_traffic_gb = int(selected_traffic_limit) if selected_traffic_limit is not None else None

    device_limit_effective, traffic_limit_bytes_effective = await get_effective_limits_for_key(
        session=ctx.session,
        tariff_id=int(tariff["id"]),
        selected_device_limit=int(selected_device_limit) if selected_device_limit is not None else None,
        selected_traffic_gb=selected_traffic_gb,
    )
    traffic_limit_gb = int(traffic_limit_bytes_effective / GB) if traffic_limit_bytes_effective else 0

    formatted_expiry_date = datetime.fromtimestamp(new_expiry_time / 1000, tz=moscow_tz).strftime("%d %B %Y, %H:%M")
    formatted_expiry_date = formatted_expiry_date.replace(
        datetime.fromtimestamp(new_expiry_time / 1000, tz=moscow_tz).strftime("%B"),
        get_russian_month(datetime.fromtimestamp(new_expiry_time / 1000, tz=moscow_tz)),
    )

    message_text = get_renewal_message(
        tariff_name=tariff["name"],
        traffic_limit=traffic_limit_gb,
        device_limit=device_limit_effective,
        expiry_date=formatted_expiry_date,
        subgroup_title=tariff.get("subgroup_title", ""),
    )

    keyboard = build_notification_expired_kb()
    result = await send_notification(ctx.bot, tg_id, "notify_expired.jpg", message_text, keyboard)

    if result:
        logger.info(f"✅ Уведомление о продлении подписки {email} отправлено пользователю {tg_id}.")
    else:
        logger.warning(f"📢 Не удалось отправить уведомление о продлении подписки {email} пользователю {tg_id}.")

    return result


async def try_auto_renew(ctx: NotificationContext, key) -> tuple[bool, Optional[dict], Optional[int]]:
    tg_id = key.tg_id
    email = key.email or ""
    renew_notification_id = f"{email}_renew"

    can_renew = await check_notification_time(ctx.session, tg_id, renew_notification_id, hours=24)
    if not can_renew:
        logger.debug(f"⏳ Подписка {email} уже продлевалась в течение последних 24 часов.")
        return False, None, None

    if ctx.preload_data and tg_id in ctx.preload_data.get("balances_cache", {}):
        balance = ctx.preload_data["balances_cache"][tg_id]
    else:
        balance = await get_balance(ctx.session, tg_id)

    server_id = key.server_id
    tariff_id = key.tariff_id

    tariffs = await get_tariffs_for_cluster(ctx.session, server_id)
    if not tariffs:
        logger.warning(f"⛔ Нет доступных тарифов для продления подписки {email}")
        return False, None, None

    current_tariff = None
    if tariff_id:
        current_tariff = ctx.get_tariff(tariff_id)
        if not current_tariff and await check_tariff_exists(ctx.session, tariff_id):
            current_tariff = await get_tariff_by_id(ctx.session, tariff_id)

    if not current_tariff:
        return False, None, None

    forbidden_groups = ["discounts", "discounts_max", "gifts", "trial"]
    try:
        hook_results = await run_hooks("renewal_forbidden_groups", chat_id=tg_id, admin=False, session=ctx.session)
        for hook_result in hook_results:
            additional_groups = hook_result.get("additional_groups", [])
            forbidden_groups.extend(additional_groups)
    except Exception as error:
        logger.warning(f"[AUTO_RENEW] Ошибка при получении дополнительных групп: {error}")

    if current_tariff["group_code"] in forbidden_groups:
        return False, None, None

    renewal_cost = await resolve_price_to_charge(
        ctx.session,
        {
            "tariff_id": current_tariff.get("id"),
            "selected_device_limit": getattr(key, "selected_device_limit", None),
            "selected_traffic_limit": getattr(key, "selected_traffic_limit", None),
            "selected_price_rub": getattr(key, "selected_price_rub", None),
        },
    )

    if renewal_cost is None or balance < renewal_cost:
        return False, None, None

    client_id = key.client_id
    current_expiry = key.expiry_time
    duration_days = current_tariff["duration_days"]

    selected_device_limit = getattr(key, "selected_device_limit", None)
    selected_traffic_limit = getattr(key, "selected_traffic_limit", None)
    selected_traffic_gb = int(selected_traffic_limit) if selected_traffic_limit is not None else None

    device_limit_effective, traffic_limit_bytes_effective = await get_effective_limits_for_key(
        session=ctx.session,
        tariff_id=int(current_tariff["id"]),
        selected_device_limit=int(selected_device_limit) if selected_device_limit is not None else None,
        selected_traffic_gb=selected_traffic_gb,
    )
    traffic_limit_gb = int(traffic_limit_bytes_effective / GB) if traffic_limit_bytes_effective else 0

    new_expiry_time = (
        current_expiry
        if current_expiry > datetime.utcnow().timestamp() * 1000
        else datetime.utcnow().timestamp() * 1000
    ) + duration_days * 24 * 60 * 60 * 1000

    logger.info(
        f"Продление подписки {email} на {duration_days} дней для пользователя {tg_id}. "
        f"Баланс: {balance}, списываем: {renewal_cost}"
    )

    key_subgroup = current_tariff.get("subgroup_title")

    await release_session_early(ctx.session)
    await renew_key_in_cluster(
        cluster_id=server_id,
        email=email,
        client_id=client_id,
        new_expiry_time=int(new_expiry_time),
        total_gb=traffic_limit_gb,
        hwid_device_limit=device_limit_effective,
        session=ctx.session,
        target_subgroup=key_subgroup,
        old_subgroup=key_subgroup,
        plan=current_tariff["id"],
    )

    new_tariff_device_limit = current_tariff.get("device_limit")
    new_tariff_traffic_limit = current_tariff.get("traffic_limit")
    reset_values = {
        "selected_device_limit": new_tariff_device_limit,
        "current_device_limit": new_tariff_device_limit,
        "selected_traffic_limit": new_tariff_traffic_limit,
        "current_traffic_limit": new_tariff_traffic_limit,
        "selected_price_rub": int(current_tariff["price_rub"]) if current_tariff.get("price_rub") is not None else None,
    }
    await ctx.session.execute(update(Key).where(Key.client_id == client_id).values(**reset_values))

    if ctx.bulk_updates is not None:
        if tg_id in ctx.bulk_updates["balance_changes"]:
            ctx.bulk_updates["balance_changes"][tg_id] -= renewal_cost
        else:
            ctx.bulk_updates["balance_changes"][tg_id] = -renewal_cost

        ctx.bulk_updates["key_expiry_updates"].append((client_id, int(new_expiry_time)))
        ctx.bulk_updates["key_tariff_updates"].append((client_id, current_tariff["id"]))
        ctx.bulk_updates["notifications_to_add"].append((tg_id, renew_notification_id))
    else:
        await update_balance(ctx.session, tg_id, -renewal_cost)
        await update_key_expiry(ctx.session, client_id, int(new_expiry_time))
        await update_key_tariff(ctx.session, client_id, current_tariff["id"])
        await add_notification(ctx.session, tg_id, renew_notification_id)

    return True, current_tariff, int(new_expiry_time)


async def notify_expiring_keys(
    ctx: NotificationContext,
    keys: list,
    min_hours: int,
    max_hours: int,
    notify_type: str,
    photo: str,
    notify_renew_enabled: bool,
    sessionmaker: Optional[async_sessionmaker] = None,
):
    if min_hours > 0:
        logger.info(f"Начало проверки подписок, истекающих через {min_hours}-{max_hours} часов.")
    else:
        logger.info(f"Начало проверки подписок, истекающих через {max_hours} часов.")

    min_threshold = int((datetime.now(moscow_tz) + timedelta(hours=min_hours)).timestamp() * 1000)
    max_threshold = int((datetime.now(moscow_tz) + timedelta(hours=max_hours)).timestamp() * 1000)
    expiring_keys = [key for key in keys if key.expiry_time and min_threshold < key.expiry_time <= max_threshold]

    if min_hours > 0:
        logger.info(f"Найдено {len(expiring_keys)} подписок, истекающих через {min_hours}-{max_hours} часов.")
    else:
        logger.info(f"Найдено {len(expiring_keys)} подписок, истекающих через {max_hours} часов.")

    tg_ids = [key.tg_id for key in expiring_keys]
    emails = [key.email or "" for key in expiring_keys]
    allowed = await check_notifications_bulk(ctx.session, notify_type, max_hours, tg_ids=tg_ids, emails=emails)
    allowed_set = {(user["tg_id"], user["email"]) for user in allowed}

    notify_pairs = [
        (key.tg_id, f"{(key.email or '')}_{notify_type}")
        for key in expiring_keys
        if (key.tg_id, key.email or "") in allowed_set
    ]
    can_notify_set = await check_notification_time_bulk(ctx.session, notify_pairs, max_hours)

    messages = []
    renew_candidates: list[tuple[Any, str]] = []

    for key in expiring_keys:
        tg_id = key.tg_id
        email = key.email or ""

        if (tg_id, email) not in allowed_set:
            continue

        notification_id = f"{email}_{notify_type}"
        if (tg_id, notification_id) not in can_notify_set:
            continue

        if notify_renew_enabled:
            renew_candidates.append((key, notification_id))
        else:
            expiry_data = await prepare_key_expiry_data(key, ctx.session, ctx.current_time)
            notification_text = KEY_EXPIRY.format(
                email=email,
                hours_left_formatted=expiry_data["hours_left_formatted"],
                formatted_expiry_date=expiry_data["formatted_expiry_date"],
                tariff_name=expiry_data["tariff_name"],
                tariff_details=expiry_data["tariff_details"],
            )
            keyboard = build_notification_kb(email, getattr(key, "client_id", None))
            messages.append({
                "tg_id": tg_id,
                "text": notification_text,
                "photo": photo,
                "keyboard": keyboard,
                "notification_id": notification_id,
                "email": email,
            })

    
    renew_results: list[tuple[Any, str, bool, Optional[dict], Optional[int]]] = [] 
    use_parallel = (
        notify_renew_enabled
        and renew_candidates
        and sessionmaker is not None
        and EXECUTOR_POOL_SIZE > 1
    )
    if use_parallel:
        semaphore = asyncio.Semaphore(EXECUTOR_POOL_SIZE)

        async def do_one_renew(key: Any, notification_id: str) -> tuple[Any, str, bool, Optional[dict], Optional[int]]:
            async with semaphore:
                async with sessionmaker() as session:
                    session = wrap_session(session, sessionmaker)
                    ctx_key = NotificationContext(
                        bot=ctx.bot,
                        session=session,
                        current_time=ctx.current_time,
                        preload_data=ctx.preload_data,
                        bulk_updates=None,
                    )
                    try:
                        renewed, tariff, new_expiry = await try_auto_renew(ctx_key, key)
                        await session.commit()
                        return (key, notification_id, bool(renewed), tariff, new_expiry)
                    except Exception as error:
                        logger.error(
                            "Ошибка авто-продления для пользователя %s (%s): %s",
                            key.tg_id,
                            getattr(key, "email", ""),
                            error,
                        )
                        return (key, notification_id, False, None, None)

        tasks = [do_one_renew(key, nid) for key, nid in renew_candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error("Ошибка в задаче продления: {}", r)
                continue
            renew_results.append(r)
    else:

        for key, notification_id in renew_candidates:
            tg_id = key.tg_id
            try:
                renewed, tariff, new_expiry = await try_auto_renew(ctx, key)
                renew_results.append((key, notification_id, bool(renewed), tariff, new_expiry))
            except Exception as error:
                logger.error(f"Ошибка авто-продления/уведомления для пользователя {tg_id}: {error}")
                renew_results.append((key, notification_id, False, None, None))

    renew_rate_limiter = NotificationRateLimiter(max_rate=30, window=1.0)
    for key, notification_id, renewed, tariff, new_expiry in renew_results:
        tg_id = key.tg_id
        await renew_rate_limiter.acquire()
        if renewed and tariff and new_expiry:
            await send_renewed_notification(ctx, key, tariff, new_expiry)
            await add_notification(ctx.session, tg_id, notification_id)
        else:
            await send_cannot_renew(ctx, key, photo)
            await add_notification(ctx.session, tg_id, notification_id)

    if messages:
        results = await send_messages_with_limit(ctx.bot, messages, session=ctx.session)
        sent_count = 0
        for msg, result in zip(messages, results, strict=False):
            await add_notification(ctx.session, msg["tg_id"], msg["notification_id"])
            if result:
                sent_count += 1
                logger.info(
                    f"Отправлено уведомление об истекающей подписке {msg['email']} пользователю {msg['tg_id']}."
                )
        logger.info(f"Отправлено {sent_count} уведомлений типа {notify_type}.")

    logger.info(f"Обработка уведомлений {notify_type} завершена.")
    await asyncio.sleep(1)


async def handle_expired_keys(ctx: NotificationContext, keys: list):
    logger.info("Начало обработки истекших ключей.")

    expired_keys = [key for key in keys if key.expiry_time and key.expiry_time < ctx.current_time]
    logger.info(f"Найдено {len(expired_keys)} истекших ключей.")

    tg_ids = [key.tg_id for key in expired_keys]
    emails = [key.email or "" for key in expired_keys]
    users = await check_notifications_bulk(ctx.session, "key_expired", 0, tg_ids=tg_ids, emails=emails)
    users_set = {(user["tg_id"], user["email"]) for user in users}

    notify_renew_expired_enabled = bool(NOTIFICATIONS_CONFIG.get("RENEW_EXPIRED_ENABLED", NOTIFY_RENEW_EXPIRED))
    notify_delete_key_enabled = bool(NOTIFICATIONS_CONFIG.get("DELETE_KEY_ENABLED", NOTIFY_DELETE_KEY))
    delete_key_delay_minutes = int(NOTIFICATIONS_CONFIG.get("DELETE_KEY_DELAY_MINUTES", NOTIFY_DELETE_DELAY))

    notification_pairs = [(key.tg_id, f"{key.email or ''}_key_expired") for key in expired_keys]
    last_times = await get_last_notification_times_bulk(ctx.session, notification_pairs)

    for key in expired_keys:
        tg_id = key.tg_id
        email = key.email or ""
        client_id = key.client_id
        server_id = key.server_id
        notification_id = f"{email}_key_expired"

        last_notification_time = last_times.get((tg_id, notification_id))

        if notify_renew_expired_enabled:
            try:
                renewed, tariff, new_expiry = await try_auto_renew(ctx, key)

                if renewed and tariff and new_expiry:
                    await send_renewed_notification(ctx, key, tariff, new_expiry)
                    if ctx.bulk_updates:
                        ctx.bulk_updates["notifications_to_delete"].append((tg_id, notification_id))
                    else:
                        await delete_notification(ctx.session, tg_id, notification_id)
                    continue

            except Exception as error:
                logger.error(f"Ошибка авто-продления для пользователя {tg_id}: {error}")
                continue

        if notify_delete_key_enabled:
            should_delete = False

            if delete_key_delay_minutes == 0:
                should_delete = True
            elif last_notification_time is not None:
                minutes_passed = (ctx.current_time - last_notification_time) / (1000 * 60)
                should_delete = minutes_passed >= delete_key_delay_minutes
                logger.info(f"Прошло минут={minutes_passed:.2f} DELETE_KEY_DELAY_MINUTES={delete_key_delay_minutes}")

            if should_delete:
                try:
                    await delete_key_from_cluster(server_id, email, client_id, ctx.session)
                    await delete_key(ctx.session, client_id)
                    logger.info(f"🗑 Ключ {client_id} для пользователя {tg_id} успешно удалён.")

                    await send_deleted_notification(ctx, key)
                except Exception as error:
                    logger.error(f"Ошибка удаления ключа {client_id} для пользователя {tg_id}: {error}")
                continue

        if last_notification_time is None and (tg_id, email) in users_set:
            await send_expired_notification(ctx, key, delete_key_delay_minutes)
            await add_notification(ctx.session, tg_id, notification_id)

    logger.info("Обработка истекших ключей завершена.")
    await asyncio.sleep(1)


async def periodic_notifications(bot: Bot, *, sessionmaker: async_sessionmaker):
    from middlewares.session import wrap_session

    while True:
        notification_interval = int(NOTIFICATIONS_CONFIG.get("BASE_NOTIFICATION_MINUTE", NOTIFICATION_TIME))

        if notification_lock.locked():
            logger.warning("Уведомления уже выполняются. Пропуск...")
            await asyncio.sleep(notification_interval)
            continue

        async with notification_lock:
            try:

                current_time = int(datetime.now(moscow_tz).timestamp() * 1000)
                start_time = datetime.now()
                preload_data = None
                keys = []
                bulk_updates = {
                    "balance_changes": {},
                    "key_expiry_updates": [],
                    "key_tariff_updates": [],
                    "notifications_to_add": [],
                    "notifications_to_delete": [],
                }

                async with sessionmaker() as preload_session:
                    logger.info("Запуск обработки уведомлений")
                    try:
                        preload_data = await preload_notification_data(preload_session)
                        keys_data = preload_data["keys_data"]
                        keys = [data["key"] for data in keys_data.values()]
                        preload_time = (datetime.now() - start_time).total_seconds()
                        logger.info(
                            f"Предзагружено данных: {len(keys)} ключей, "
                            f"{len(preload_data['tariffs_cache'])} тарифов за {preload_time:.2f}s"
                        )
                    except Exception as error:
                        logger.error(f"Ошибка при предварительной загрузке данных: {error}")
                        try:
                            keys = await get_all_keys(session=preload_session)
                            keys = [k for k in keys if not k.is_frozen]
                            preload_data = None
                            preload_time = (datetime.now() - start_time).total_seconds()
                            logger.info(f"Fallback: получено {len(keys)} ключей за {preload_time:.2f}s")
                        except Exception as fallback_error:
                            logger.error(f"Ошибка fallback получения ключей: {fallback_error}")
                            keys = []
                            preload_data = None

                async with sessionmaker() as session:
                    session = wrap_session(session, sessionmaker)
                    ctx = NotificationContext(
                        bot=bot,
                        session=session,
                        current_time=current_time,
                        preload_data=preload_data,
                        bulk_updates=bulk_updates,
                    )

                    trial_time_disable = bool(MODES_CONFIG.get("TRIAL_TIME_DISABLED", TRIAL_TIME_DISABLE))
                    if not trial_time_disable:
                        try:
                            await notify_inactive_trial_users(bot, session, sessionmaker=sessionmaker)
                        except Exception as error:
                            logger.error(f"Ошибка в notify_inactive_trial_users: {error}")

                    notify_24_enabled = bool(NOTIFICATIONS_CONFIG.get("EXPIRY_24H_ENABLED", NOTIFY_24H_ENABLED))
                    notify_24_hours = int(NOTIFICATIONS_CONFIG.get("EXPIRY_24H_BEFORE_HOURS", NOTIFY_24H_HOURS))
                    notify_10_enabled = bool(NOTIFICATIONS_CONFIG.get("EXPIRY_10H_ENABLED", NOTIFY_10H_ENABLED))
                    notify_10_hours = int(NOTIFICATIONS_CONFIG.get("EXPIRY_10H_BEFORE_HOURS", NOTIFY_10H_HOURS))
                    notify_renew_enabled = bool(NOTIFICATIONS_CONFIG.get("RENEW_ENABLED", NOTIFY_RENEW))
                    inactive_traffic_enabled = bool(
                        NOTIFICATIONS_CONFIG.get("INACTIVE_TRAFFIC_ENABLED", NOTIFY_INACTIVE_TRAFFIC)
                    )
                    notify_hot_leads_enabled = bool(NOTIFICATIONS_CONFIG.get("HOT_LEADS_ENABLED", NOTIFY_HOT_LEADS))

                    if notify_24_enabled:
                        try:
                            await notify_expiring_keys(
                                ctx,
                                keys,
                                min_hours=notify_10_hours if notify_10_enabled else 0,
                                max_hours=notify_24_hours,
                                notify_type="key_24h",
                                photo="notify_24h.jpg",
                                notify_renew_enabled=notify_renew_enabled,
                                sessionmaker=sessionmaker,
                            )
                        except Exception as error:
                            logger.error(f"Ошибка в notify_expiring_keys (24h): {error}")

                    if notify_10_enabled:
                        try:
                            await notify_expiring_keys(
                                ctx,
                                keys,
                                min_hours=0,
                                max_hours=notify_10_hours,
                                notify_type="key_10h",
                                photo="notify_10h.jpg",
                                notify_renew_enabled=notify_renew_enabled,
                                sessionmaker=sessionmaker,
                            )
                        except Exception as error:
                            logger.error(f"Ошибка в notify_expiring_keys (10h): {error}")

                    try:
                        await handle_expired_keys(ctx, keys)
                    except Exception as error:
                        logger.error(f"Ошибка в handle_expired_keys: {error}")

                    if inactive_traffic_enabled:
                        try:
                            await notify_users_no_traffic(bot, session, current_time, keys)
                        except Exception as error:
                            logger.error(f"Ошибка в notify_users_no_traffic: {error}")

                    try:
                        await run_hooks("periodic_notifications", bot=bot, session=session, keys=keys)
                    except Exception as error:
                        logger.error(f"Ошибка в хуках periodic_notifications: {error}")

                    if notify_hot_leads_enabled:
                        try:
                            await notify_hot_leads(bot, session)
                        except Exception as error:
                            logger.error(f"Ошибка в notify_hot_leads: {error}")

                    if bulk_updates:
                        bulk_start = datetime.now()
                        await execute_bulk_updates(session, bulk_updates)
                        bulk_time = (datetime.now() - bulk_start).total_seconds()
                        total_renewals = len(bulk_updates["balance_changes"])
                        total_key_updates = len(bulk_updates["key_expiry_updates"]) + len(
                            bulk_updates["key_tariff_updates"]
                        )
                        total_notification_updates = len(bulk_updates["notifications_to_add"]) + len(
                            bulk_updates["notifications_to_delete"]
                        )
                        logger.info(
                            f"Bulk-операции выполнены за {bulk_time:.2f}s. Обработано: {total_renewals} продлений, {total_key_updates} ключей, {total_notification_updates} уведомлений"
                        )

                    total_time = (datetime.now() - start_time).total_seconds()
                    logger.info(f"Уведомления завершены за {total_time:.2f}s")
                    await session.commit()

            except Exception as error:
                logger.error(f"Ошибка в periodic_notifications: {error}")

        await asyncio.sleep(notification_interval)
