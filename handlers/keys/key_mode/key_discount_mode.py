from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import DISCOUNT_ACTIVE_HOURS
from core.bootstrap import NOTIFICATIONS_CONFIG
from database import get_keys, get_tariffs, get_tariffs_for_cluster
from database.models import Notification
from handlers.buttons import MAIN_MENU, RENEW_KEY_NOTIFICATION
from handlers.keys.utils import build_key_callback
from handlers.notifications.notify_kb import build_tariffs_keyboard
from handlers.tariffs.buy.key_tariffs import select_tariff_plan
from handlers.texts import DISCOUNT_TARIFF, DISCOUNT_TARIFF_MAX
from handlers.utils import format_discount_time_left, get_least_loaded_cluster
from logger import logger


router = Router()


@router.callback_query(F.data == "hot_lead_discount")
async def handle_discount_entry(callback: CallbackQuery, session: AsyncSession):
    tg_id = callback.from_user.id

    result = await session.execute(
        select(Notification.last_notification_time).where(
            Notification.tg_id == tg_id,
            Notification.notification_type == "hot_lead_step_2",
        )
    )
    last_time = result.scalar_one_or_none()

    if not last_time:
        await callback.message.edit_text("❌ Скидка недоступна.")
        return

    discount_active_hours = int(NOTIFICATIONS_CONFIG.get("DISCOUNT_ACTIVE_HOURS", DISCOUNT_ACTIVE_HOURS))

    now = datetime.utcnow()
    if now - last_time > timedelta(hours=discount_active_hours):
        await callback.message.edit_text("⏳ Срок действия скидки истёк.")
        return

    keys = await get_keys(session, tg_id)

    if keys and len(keys) > 0:
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=RENEW_KEY_NOTIFICATION,
                callback_data=build_key_callback("renew_key", keys[0].client_id, keys[0].email),
            )
        )
        builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))

        expires_at = last_time + timedelta(hours=discount_active_hours)
        await callback.message.edit_text(
            "🎯 <b>ЭКСКЛЮЗИВНОЕ ПРЕДЛОЖЕНИЕ!</b>\n\n<blockquote>"
            "💎 <b>Специальные тарифы</b> — доступные только для вас!\n"
            "🚀 <b>Получите максимум возможностей</b> по выгодной цене!\n"
            "</blockquote>\n"
            f"⏰ <b>Предложение действует всего: {format_discount_time_left(expires_at, discount_active_hours)} — не упустите свой шанс!</b>",
            reply_markup=builder.as_markup(),
        )
    else:
        tariffs = await get_tariffs(session=session, group_code="discounts")
        if not tariffs:
            try:
                cluster_name = await get_least_loaded_cluster(session)
                cluster_tariffs = await get_tariffs_for_cluster(session, cluster_name)
                if cluster_tariffs:
                    group_code = cluster_tariffs[0].get("group_code")
                    if group_code:
                        logger.warning(f"[DISCOUNT] Нет тарифов discounts, fallback на {group_code}")
                        tariffs = await get_tariffs(session=session, group_code=group_code)
            except Exception as e:
                logger.error(f"[DISCOUNT] Не удалось получить обычные тарифы: {e}")

            if not tariffs:
                await callback.message.edit_text("❌ Тарифы временно недоступны.")
                return

        await callback.message.edit_text(
            DISCOUNT_TARIFF,
            reply_markup=build_tariffs_keyboard(tariffs, prefix="discount_tariff"),
        )


@router.callback_query(F.data.startswith("discount_tariff|"))
async def handle_discount_tariff_selection(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    try:
        tariff_id = int(callback.data.split("|")[1])
        original_data = callback.data
        object.__setattr__(callback, "data", f"select_tariff_plan|{tariff_id}")
        try:
            await select_tariff_plan(callback, session=session, state=state)
        finally:
            object.__setattr__(callback, "data", original_data)
    except Exception as e:
        logger.error(f"Ошибка при выборе скидочного тарифа: {e}")
        await callback.message.answer("❌ Произошла ошибка при выборе тарифа.")


@router.callback_query(F.data == "hot_lead_final_discount")
async def handle_ultra_discount(callback: CallbackQuery, session: AsyncSession):
    tg_id = callback.from_user.id

    result = await session.execute(
        select(Notification.last_notification_time).where(
            Notification.tg_id == tg_id,
            Notification.notification_type == "hot_lead_step_3",
        )
    )
    last_time = result.scalar_one_or_none()

    if not last_time:
        await callback.message.edit_text("❌ Скидка недоступна.")
        return

    discount_active_hours = int(NOTIFICATIONS_CONFIG.get("DISCOUNT_ACTIVE_HOURS", DISCOUNT_ACTIVE_HOURS))

    now = datetime.utcnow()
    if now - last_time > timedelta(hours=discount_active_hours):
        await callback.message.edit_text("⏳ Срок действия финальной скидки истёк.")
        return

    keys = await get_keys(session, tg_id)

    if keys and len(keys) > 0:
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=RENEW_KEY_NOTIFICATION,
                callback_data=build_key_callback("renew_key", keys[0].client_id, keys[0].email),
            )
        )
        builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))

        expires_at = last_time + timedelta(hours=discount_active_hours)
        await callback.message.edit_text(
            "🎯 <b>УНИКАЛЬНОЕ ФИНАЛЬНОЕ ПРЕДЛОЖЕНИЕ!</b>\n\n<blockquote>"
            "💎 <b>Доступ к тарифам с МАКСИМАЛЬНОЙ выгодой</b> — только для вас!\n"
            "🚀 <b>Уникальные условия</b> — получите максимум преимуществ по минимальной цене!\n"
            "</blockquote>\n"
            f"⏰ <b>Время ограничено: {format_discount_time_left(expires_at, discount_active_hours)} — не упустите шанс!</b>",
            reply_markup=builder.as_markup(),
        )
    else:
        tariffs = await get_tariffs(session=session, group_code="discounts_max")
        if not tariffs:
            try:
                cluster_name = await get_least_loaded_cluster(session)
                cluster_tariffs = await get_tariffs_for_cluster(session, cluster_name)
                if cluster_tariffs:
                    group_code = cluster_tariffs[0].get("group_code")
                    if group_code:
                        logger.warning(f"[DISCOUNT_MAX] Нет тарифов discounts_max, fallback на {group_code}")
                        tariffs = await get_tariffs(session=session, group_code=group_code)
            except Exception as e:
                logger.error(f"[DISCOUNT_MAX] Не удалось получить обычные тарифы: {e}")

            if not tariffs:
                await callback.message.edit_text("❌ Тарифы временно недоступны.")
                return

        await callback.message.edit_text(
            DISCOUNT_TARIFF_MAX,
            reply_markup=build_tariffs_keyboard(tariffs, prefix="discount_tariff"),
        )
