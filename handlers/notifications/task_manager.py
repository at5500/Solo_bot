import asyncio
import os

from apscheduler.triggers.cron import CronTrigger

from core.periodic_manager import periodic_task_manager
from database import async_session_maker, cancel_expired_pending_payments
from hooks.hooks import register_hook
from logger import logger


_TASKS_REGISTERED = False


async def _backup_loop(_bot, _sessionmaker) -> None:
    from config import BACKUP_TIME
    from utils.backup import backup_database

    if BACKUP_TIME <= 0:
        await asyncio.Event().wait()
        return
    while True:
        error = await backup_database()
        if error:
            logger.error("[Backup] Ошибка: {}", error)
        await asyncio.sleep(BACKUP_TIME)


async def _server_checks_loop(_bot, sessionmaker) -> None:
    from config import PING_TIME
    from servers import check_servers

    if PING_TIME <= 0:
        await asyncio.Event().wait()
        return
    await check_servers(sessionmaker=sessionmaker)


async def _scheduled_audit_drain() -> None:
    from audit import drain_audit_redis_to_db

    try:
        drained = await drain_audit_redis_to_db(async_session_maker)
        logger.info("[AuditDrain] Ночной drain завершён, записано событий: {}", drained)
    except Exception as error:
        logger.error("[AuditDrain] Ошибка ночного drain: {}", error)


async def _scheduled_stats_report() -> None:
    from handlers.admin.stats.stats_handler import send_daily_stats_report

    async with async_session_maker() as session:
        await send_daily_stats_report(session)


async def _sweep_stale_payments_job() -> None:
    async with async_session_maker() as session:
        await cancel_expired_pending_payments(session)


def _register_periodic_tasks() -> None:
    global _TASKS_REGISTERED
    if _TASKS_REGISTERED:
        return
    from handlers.admin.sender.scheduled_service import scheduled_broadcasts_loop
    from handlers.notifications.general_notifications import periodic_notifications

    periodic_task_manager.register_loop_task(
        "notifications",
        lambda bot, sessionmaker: periodic_notifications(bot, sessionmaker=sessionmaker),
    )
    periodic_task_manager.register_loop_task("scheduled_broadcasts", lambda bot, sessionmaker: scheduled_broadcasts_loop(bot))
    periodic_task_manager.register_loop_task("backup", _backup_loop)
    periodic_task_manager.register_loop_task("server_checks", _server_checks_loop)
    periodic_task_manager.register_cron_task(
        "audit_drain_midnight",
        _scheduled_audit_drain,
        CronTrigger(hour=0, minute=0, timezone="Europe/Moscow"),
    )
    periodic_task_manager.register_cron_task(
        "daily_stats_report",
        _scheduled_stats_report,
        CronTrigger(hour=0, minute=1, timezone="Europe/Moscow"),
    )
    periodic_task_manager.register_cron_task(
        "sweep_stale_payments",
        _sweep_stale_payments_job,
        CronTrigger(minute=0, timezone="Europe/Moscow"),
    )
    _TASKS_REGISTERED = True


def _should_start_manager() -> bool:
    if os.environ.get("NOTIFICATION_WORKER", "").strip() == "1":
        return True
    if os.environ.get("NOTIFICATION_WORKER_SEPARATE", "").strip() == "1":
        return False
    return True


async def ensure_periodic_task_manager_started(bot, sessionmaker) -> None:
    _register_periodic_tasks()
    if not _should_start_manager():
        logger.info("[PeriodicManager] Пропуск старта в текущем процессе")
        return
    await periodic_task_manager.start(bot, sessionmaker)


async def ensure_periodic_task_manager_stopped() -> None:
    await periodic_task_manager.stop()


@register_hook("startup")
async def start_periodic_task_manager(bot, sessionmaker, **_kwargs):
    await ensure_periodic_task_manager_started(bot, sessionmaker)


@register_hook("shutdown")
async def stop_periodic_task_manager(**_kwargs):
    await ensure_periodic_task_manager_stopped()
