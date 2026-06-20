from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from core.bootstrap import NOTIFICATIONS_CONFIG
from database import add_notification
from database.access.resolution import notify_telegram_chat_id
from database.returning import RETURNING_NOTIFICATION_TYPE, get_returning_targets
from handlers.admin.sender.sender_utils import is_telegram_chat_id
from handlers.notifications.keyboards import build_cold_lead_kb
from handlers.notifications.sender import send_notification
from handlers.texts import RETURNING_MESSAGE
from logger import logger

_DEFAULT_MIN_DAYS = 60
_DEFAULT_MAX_DAYS = 180


async def process_returning(bot: Bot, session: AsyncSession):
    """Возврат давно ушедших («второй эшелон» после горячих лидов): тем, кто ушёл
    давно (60–180 дней назад) и не вернулся, — мягкое напоминание без скидки."""
    logger.info("[Returning] Запуск")
    min_days = int(NOTIFICATIONS_CONFIG.get("RETURNING_MIN_DAYS", _DEFAULT_MIN_DAYS))
    max_days = int(NOTIFICATIONS_CONFIG.get("RETURNING_MAX_DAYS", _DEFAULT_MAX_DAYS))

    try:
        targets = await get_returning_targets(session, min_days, max_days)
        if not targets:
            return

        notified = 0
        for user_id in targets:
            chat_id = await notify_telegram_chat_id(session, user_id)
            if not is_telegram_chat_id(chat_id):
                continue
            if await send_notification(bot, chat_id, None, RETURNING_MESSAGE, build_cold_lead_kb()):
                await add_notification(session, user_id, RETURNING_NOTIFICATION_TYPE)
                notified += 1

        logger.info(f"[Returning] Отправлено: {notified}")

    except Exception as e:
        logger.error(f"[Returning] Ошибка: {e}")
