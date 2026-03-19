import asyncio

from apscheduler.triggers.cron import CronTrigger

from database import async_session_maker, cancel_expired_pending_payments
from logger import logger


async def scheduled_audit_drain() -> None:
    from audit import drain_audit_redis_to_db

    try:
        drained = await drain_audit_redis_to_db(async_session_maker)
        logger.info("[AuditDrain] Ночной drain завершён, записано событий: {}", drained)
    except Exception as error:
        logger.error("[AuditDrain] Ошибка ночного drain: {}", error)


async def scheduled_stats_report() -> None:
    from handlers.admin.stats.stats_handler import send_daily_stats_report

    async with async_session_maker() as session:
        await send_daily_stats_report(session)


async def sweep_stale_payments_job() -> None:
    async with async_session_maker() as session:
        await cancel_expired_pending_payments(session)


def scheduled_audit_drain_process_runner() -> None:
    asyncio.run(scheduled_audit_drain())


def scheduled_stats_report_process_runner() -> None:
    asyncio.run(scheduled_stats_report())


def sweep_stale_payments_process_runner() -> None:
    asyncio.run(sweep_stale_payments_job())


AUDIT_DRAIN_TRIGGER = CronTrigger(hour=0, minute=0, timezone="Europe/Moscow")
DAILY_STATS_REPORT_TRIGGER = CronTrigger(hour=0, minute=1, timezone="Europe/Moscow")
STALE_PAYMENTS_SWEEP_TRIGGER = CronTrigger(minute=0, timezone="Europe/Moscow")
