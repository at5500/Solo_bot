import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from typing import Literal

import psutil
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import distinct, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_admin, verify_identity_admin_short
from api.v2.schemas.audit import (
    AuditEventListResponse,
    AuditEventResponse,
    AuditStatsResponse,
)
from audit import drain_audit_redis_to_db, get_audit_funnel, get_audit_stats, list_audit_events
from config import API_TOKEN, BOT_SERVICE
from database import async_session_maker, save_blocked_user_ids
from core.bootstrap import MANAGEMENT_CONFIG
from core.executor import run_io
from core.settings.management_config import update_management_config
from database.models import Key, Server, User
from handlers.admin.sender.sender_service import BroadcastService
from handlers.admin.sender.sender_utils import get_recipients, parse_message_buttons
from logger import logger
from utils.backup import backup_database

router = APIRouter()


class MaintenanceUpdate(BaseModel):
    enabled: bool


class DomainChange(BaseModel):
    domain: str


class BroadcastLaunchPayload(BaseModel):
    send_to: Literal["all", "subscribed", "unsubscribed", "untrial", "trial", "hotleads", "cluster"] = "all"
    text: str
    photo: str | None = None
    cluster_name: str | None = None
    workers: int = 5
    messages_per_second: int = 35


_broadcast_bot: Bot | None = None


def _get_broadcast_bot() -> Bot:
    """Возвращает экземпляр бота для рассылки."""
    global _broadcast_bot
    if _broadcast_bot is None:
        _broadcast_bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _broadcast_bot


async def _restart_bot() -> None:
    """Перезапуск процесса бота (systemctl или execv)."""
    await asyncio.sleep(1)
    try:
        parent = psutil.Process(os.getpid()).parent()
        is_systemd = parent and "systemd" in parent.name().lower()
        if is_systemd:
            await run_io(lambda: subprocess.run(["sudo", "systemctl", "restart", BOT_SERVICE], check=True))
        else:
            python_exe = sys.executable
            script_path = os.path.abspath(sys.argv[0])
            os.execv(python_exe, [python_exe, script_path] + sys.argv[1:])
    except Exception:
        os._exit(1)


@router.get("/status")
async def get_status(identity=Depends(verify_identity_admin)):
    """Текущий статус: maintenance и management config."""
    return {
        "maintenance_enabled": bool(MANAGEMENT_CONFIG.get("MAINTENANCE_ENABLED", False)),
        "management": dict(MANAGEMENT_CONFIG or {}),
    }


@router.post("/maintenance")
async def set_maintenance(
    payload: MaintenanceUpdate,
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Включение/выключение режима обслуживания."""
    current_config = dict(MANAGEMENT_CONFIG or {})
    current_config["MAINTENANCE_ENABLED"] = bool(payload.enabled)
    await update_management_config(session, current_config)
    return {"maintenance_enabled": bool(MANAGEMENT_CONFIG.get("MAINTENANCE_ENABLED", False))}


@router.post("/restart")
async def restart_bot(
    background: BackgroundTasks,
    identity=Depends(verify_identity_admin),
):
    """Запуск перезапуска бота в фоне."""
    background.add_task(_restart_bot)
    return {"status": "restarting"}


@router.post("/change-domain")
async def change_domain(
    payload: DomainChange,
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Массовая замена домена в ключах и remnawave_link."""
    domain = payload.domain.strip()
    if not domain or " " in domain or not re.fullmatch(r"[a-zA-Z0-9.-]+", domain):
        raise HTTPException(status_code=400, detail="Invalid domain")
    new_domain_url = f"https://{domain}"
    stmt = (
        update(Key)
        .values(
            key=func.regexp_replace(Key.key, r"^https://[^/]+", new_domain_url),
            remnawave_link=func.regexp_replace(Key.remnawave_link, r"^https://[^/]+", new_domain_url),
        )
        .where(
            (Key.key.startswith("https://") & ~Key.key.startswith(new_domain_url))
            | (Key.remnawave_link.startswith("https://") & ~Key.remnawave_link.startswith(new_domain_url))
        )
    )
    result = await session.execute(stmt)
    await session.commit()
    return {"updated": result.rowcount or 0}


@router.post("/restore-trials")
async def restore_trials(
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Сбрасывает trial=0 у пользователей без ключей."""
    stmt = (
        update(User)
        .where(
            User.trial == 1,
            ~exists(select(Key.tg_id).where(Key.tg_id == User.tg_id)),
        )
        .values(trial=0)
    )
    result = await session.execute(stmt)
    await session.commit()
    return {"restored": result.rowcount or 0}


@router.post("/backup")
async def trigger_backup(identity=Depends(verify_identity_admin)):
    """Запуск бэкапа БД в фоне."""

    async def _run_backup() -> None:
        exception = await backup_database()
        if exception:
            logger.error(f"[Management] Backup finished with error: {exception}")

    asyncio.create_task(_run_backup())
    return {"status": "backup_started"}


@router.get("/broadcast/clusters")
async def get_broadcast_clusters(
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Список кластеров для рассылки по кластеру."""
    result = await session.execute(select(distinct(Server.cluster_name)).where(Server.cluster_name.is_not(None)))
    clusters = sorted([row[0] for row in result.all() if row and row[0]])
    return {"clusters": clusters}


def _parse_date_range(
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[datetime, datetime]:
    """Возвращает (date_from, date_to) в UTC. Либо date=YYYY-MM-DD (один день), либо date_from + date_to."""
    tz = timezone.utc
    if date:
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
            start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
            end = start + timedelta(days=1)
            return start, end
        except ValueError:
            raise HTTPException(status_code=400, detail="date должен быть YYYY-MM-DD")
    if date_from and date_to:
        try:
            start = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
            end = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            if start >= end:
                raise HTTPException(status_code=400, detail="date_from должен быть раньше date_to")
            return start, end
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Неверный формат дат: {e}")
    end = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start, end


@router.get("/audit-stats", response_model=AuditStatsResponse)
async def get_audit_stats_endpoint(
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
    date: str | None = Query(None, description="Один день: YYYY-MM-DD"),
    date_from: str | None = Query(None, description="Начало периода (ISO)"),
    date_to: str | None = Query(None, description="Конец периода (ISO)"),
):
    """Статистика аудита за период: какие пути отрабатывают хорошо/плохо, воронка старт→оплата.
    Данные только из БД (события из Redis учитываются после drain)."""
    start, end = _parse_date_range(date=date, date_from=date_from, date_to=date_to)
    stats = await get_audit_stats(session, date_from=start, date_to=end)
    funnel = await get_audit_funnel(session, date_from=start, date_to=end)
    return AuditStatsResponse(
        summary=stats["summary"],
        by_path=stats["by_path"],
        funnel=funnel,
    )


@router.get("/audit-events", response_model=AuditEventListResponse)
async def get_audit_events_history(
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
    identity_id: str | None = Query(None, description="Фильтр по identity_id"),
    tg_id: int | None = Query(None, description="Фильтр по Telegram user id"),
    channel: str | None = Query(None, description="api или telegram"),
    event_type: str | None = Query(None, description="Точный event_type"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """История аудита клиента по identity_id и/или tg_id."""
    if identity_id is None and tg_id is None:
        raise HTTPException(status_code=400, detail="Укажите identity_id или tg_id")

    events = await list_audit_events(
        session,
        identity_id=identity_id,
        tg_id=tg_id,
        channel=channel,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
    return AuditEventListResponse(
        items=[
            AuditEventResponse(
                id=getattr(event, "id", None),
                event_type=event.event_type,
                channel=event.channel,
                actor_identity_id=event.actor_identity_id,
                actor_tg_id=event.actor_tg_id,
                path_or_handler=event.path_or_handler,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                result=event.result,
                reason=event.reason,
                metadata=event.metadata_,
                request_id=event.request_id,
                created_at=event.created_at,
            )
            for event in events
        ],
        limit=limit,
        offset=offset,
    )


@router.post("/audit-drain")
async def post_audit_drain(identity=Depends(verify_identity_admin_short)):
    """Выгружает буфер аудита из Redis в БД. Для вызова по крону (например 0 0 * * * в 00:00)."""
    try:
        count = await drain_audit_redis_to_db(async_session_maker)
        return {"success": True, "drained": count}
    except Exception as exc:
        logger.warning("audit-drain failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/broadcast")
async def launch_broadcast(
    payload: BroadcastLaunchPayload,
    identity=Depends(verify_identity_admin_short),
):
    """Запуск рассылки по выбранной аудитории. Сессия БД не держится на время рассылки."""
    text_raw = (payload.text or "").strip()
    if not text_raw:
        raise HTTPException(status_code=400, detail="Broadcast text is required")
    if payload.send_to == "cluster" and not (payload.cluster_name or "").strip():
        raise HTTPException(status_code=400, detail="Cluster name is required for cluster broadcast")
    clean_text, keyboard = parse_message_buttons(text_raw)
    max_len = 1024 if payload.photo else 4096
    if len(clean_text) > max_len:
        raise HTTPException(status_code=400, detail=f"Message too long. Max {max_len} symbols")
    async with async_session_maker() as session:
        tg_ids, total_users = await get_recipients(session, payload.send_to, (payload.cluster_name or None))
        await session.commit()
    if not tg_ids:
        return {"success": False, "message": "No recipients found", "stats": {"total_messages": 0}}
    bot = _get_broadcast_bot()
    messages = [{"tg_id": tg_id, "text": clean_text, "photo": payload.photo, "keyboard": keyboard} for tg_id in tg_ids]
    workers = max(1, min(int(payload.workers or 5), 30))
    rate = max(1, min(int(payload.messages_per_second or 35), 60))
    broadcast_service = BroadcastService(bot=bot, session=None, messages_per_second=rate)
    stats = await broadcast_service.broadcast(messages, workers=workers)
    blocked_ids = stats.get("blocked_user_ids") or []
    if blocked_ids:
        async with async_session_maker() as session:
            try:
                await save_blocked_user_ids(session, blocked_ids)
            except Exception:
                pass
    return {
        "success": True,
        "message": "Broadcast completed",
        "recipients": total_users,
        "stats": stats,
    }
