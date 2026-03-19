import asyncio

from logger import logger


async def notifications_loop(bot, sessionmaker) -> None:
    from handlers.notifications.general_notifications import periodic_notifications

    await periodic_notifications(bot, sessionmaker=sessionmaker)


async def scheduled_broadcasts_loop_task(bot, _sessionmaker) -> None:
    from handlers.admin.sender.scheduled_service import scheduled_broadcasts_loop

    await scheduled_broadcasts_loop(bot)


async def backup_loop(bot, _sessionmaker) -> None:
    from config import BACKUP_TIME
    from utils.backup import backup_database

    if BACKUP_TIME <= 0:
        await asyncio.Event().wait()
        return
    while True:
        error = await backup_database(bot_instance=bot)
        if error:
            logger.error("[Backup] Ошибка: {}", error)
        await asyncio.sleep(BACKUP_TIME)


def backup_thread_loop(stop_event, _bot, _sessionmaker) -> None:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from config import API_TOKEN, BACKUP_TIME
    from utils.backup import backup_database

    if BACKUP_TIME <= 0:
        stop_event.wait()
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    backup_bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        while not stop_event.is_set():
            error = loop.run_until_complete(backup_database(bot_instance=backup_bot))
            if error:
                logger.error("[Backup] Ошибка: {}", error)
            if stop_event.wait(BACKUP_TIME):
                break
    finally:
        loop.run_until_complete(backup_bot.session.close())
        loop.close()


async def server_checks_loop(_bot, sessionmaker) -> None:
    from config import PING_TIME
    from servers import check_servers

    if PING_TIME <= 0:
        await asyncio.Event().wait()
        return
    await check_servers(sessionmaker=sessionmaker)
