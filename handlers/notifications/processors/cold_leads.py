from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from core.bootstrap import NOTIFICATIONS_CONFIG
from database import add_notification, check_notification_time_bulk, get_cold_lead_notification_flags, get_cold_leads
from database.access.resolution import notify_telegram_chat_id
from handlers.admin.sender.sender_utils import is_telegram_chat_id
from handlers.notifications.keyboards import build_cold_lead_kb
from handlers.notifications.sender import send_notification
from handlers.texts import COLD_LEAD_FINAL_MESSAGE, COLD_LEAD_MESSAGE
from logger import logger

_DEFAULT_INTERVAL_HOURS = 48


async def process_cold_leads(bot: Bot, session: AsyncSession):
    logger.info("[ColdLeads] Запуск")

    interval = int(NOTIFICATIONS_CONFIG.get("COLD_LEADS_INTERVAL_HOURS", _DEFAULT_INTERVAL_HOURS))

    try:
        leads = await get_cold_leads(session)
        if not leads:
            return

        flags = await get_cold_lead_notification_flags(session, leads)
        can_send_step2 = await check_notification_time_bulk(
            session, [(tid, "cold_lead_step_1") for tid in leads], interval,
        )
        can_send_step3 = await check_notification_time_bulk(
            session, [(tid, "cold_lead_step_2") for tid in leads], interval,
        )

        notified = 0

        async def _send_to_user(user_id: int, text: str, keyboard) -> bool:
            chat_id = await notify_telegram_chat_id(session, user_id)
            if not is_telegram_chat_id(chat_id):
                return False
            return await send_notification(bot, chat_id, None, text, keyboard)

        for user_id in leads:
            step_flags = flags.get(user_id, set())
            has_step_1 = "cold_lead_step_1" in step_flags
            has_step_2 = "cold_lead_step_2" in step_flags
            has_step_3 = "cold_lead_step_3" in step_flags

            if not has_step_1:
                await add_notification(session, user_id, "cold_lead_step_1")
                continue

            if not has_step_2:
                if (user_id, "cold_lead_step_1") not in can_send_step2:
                    continue
                if await _send_to_user(user_id, COLD_LEAD_MESSAGE, build_cold_lead_kb()):
                    await add_notification(session, user_id, "cold_lead_step_2")
                    notified += 1
                continue

            if not has_step_3:
                if (user_id, "cold_lead_step_2") not in can_send_step3:
                    continue
                if await _send_to_user(user_id, COLD_LEAD_FINAL_MESSAGE, build_cold_lead_kb()):
                    await add_notification(session, user_id, "cold_lead_step_3")
                    notified += 1

        logger.info(f"[ColdLeads] Отправлено: {notified}")

    except Exception as e:
        logger.error(f"[ColdLeads] Ошибка: {e}")
