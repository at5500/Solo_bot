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
from database import async_session_maker
from core.bootstrap import MANAGEMENT_CONFIG
from core.executor import run_io
from core.settings.management_config import update_management_config
from database.models import Key, ScheduledBroadcast, Server, User
from database.scheduled_broadcasts import (
    cancel_scheduled_broadcast,
    create_scheduled_broadcast,
    get_scheduled_broadcast,
    list_scheduled_broadcasts,
    mark_scheduled_broadcast_failed,
    mark_scheduled_broadcast_sent,
    start_scheduled_broadcast,
    update_scheduled_broadcast,
)
from handlers.admin.sender.scheduled_service import (
    ensure_utc_datetime,
    execute_broadcast_payload,
    execute_scheduled_broadcast,
    prepare_broadcast_payload,
    scheduled_broadcast_to_dict,
)
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


class ScheduledBroadcastCreatePayload(BroadcastLaunchPayload):
    scheduled_for: datetime


class ScheduledBroadcastUpdatePayload(BaseModel):
    send_to: Literal["all", "subscribed", "unsubscribed", "untrial", "trial", "hotleads", "cluster"] | None = None
    text: str | None = None
    photo: str | None = None
    cluster_name: str | None = None
    workers: int | None = None
    messages_per_second: int | None = None
    scheduled_for: datetime | None = None


_broadcast_bot: Bot | None = None


def _get_broadcast_bot() -> Bot:
    """Возвращает экземпляр бота для рассылки."""
    global _broadcast_bot
    if _broadcast_bot is None:
        _broadcast_bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return _broadcast_bot


def _require_future_schedule(value: datetime) -> datetime:
    scheduled_for = ensure_utc_datetime(value)
    if scheduled_for <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="scheduled_for must be in the future")
    return scheduled_for


def _resolve_update_payload(
    payload: ScheduledBroadcastUpdatePayload,
    current: ScheduledBroadcast,
) -> dict:
    fields = payload.model_fields_set
    send_to = payload.send_to if "send_to" in fields else current.send_to
    text = payload.text if "text" in fields else current.text
    photo = payload.photo if "photo" in fields else current.photo
    cluster_name = payload.cluster_name if "cluster_name" in fields else current.cluster_name
    workers = payload.workers if "workers" in fields else current.workers
    messages_per_second = (
        payload.messages_per_second if "messages_per_second" in fields else current.messages_per_second
    )
    prepared = prepare_broadcast_payload(
        send_to=send_to,
        text=text,
        photo=photo,
        cluster_name=cluster_name,
        workers=workers,
        messages_per_second=messages_per_second,
    )
    if "scheduled_for" in fields:
        prepared["scheduled_for"] = _require_future_schedule(payload.scheduled_for)
    return prepared


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
    try:
        prepared = prepare_broadcast_payload(
            send_to=payload.send_to,
            text=payload.text,
            photo=payload.photo,
            cluster_name=payload.cluster_name,
            workers=payload.workers,
            messages_per_second=payload.messages_per_second,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await execute_broadcast_payload(prepared, bot=_get_broadcast_bot())


@router.post("/broadcast/scheduled")
async def create_broadcast_schedule(
    payload: ScheduledBroadcastCreatePayload,
    identity=Depends(verify_identity_admin_short),
    session: AsyncSession = Depends(get_session),
):
    try:
        prepared = prepare_broadcast_payload(
            send_to=payload.send_to,
            text=payload.text,
            photo=payload.photo,
            cluster_name=payload.cluster_name,
            workers=payload.workers,
            messages_per_second=payload.messages_per_second,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    broadcast = await create_scheduled_broadcast(
        session,
        created_by_tg_id=getattr(identity, "tg_id", None),
        send_to=prepared["send_to"],
        cluster_name=prepared["cluster_name"],
        text=prepared["text"],
        photo=prepared["photo"],
        keyboard_json=prepared["keyboard_json"],
        scheduled_for=_require_future_schedule(payload.scheduled_for),
        workers=prepared["workers"],
        messages_per_second=prepared["messages_per_second"],
    )
    return {"success": True, "item": scheduled_broadcast_to_dict(broadcast)}


@router.get("/broadcast/scheduled")
async def list_broadcast_schedules(
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
    status: str | None = Query(None, description="Фильтр статусов через запятую"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    statuses = [item.strip() for item in (status or "").split(",") if item.strip()] or None
    items = await list_scheduled_broadcasts(session, statuses=statuses, limit=limit, offset=offset)
    return {"items": [scheduled_broadcast_to_dict(item) for item in items], "limit": limit, "offset": offset}


@router.get("/broadcast/scheduled/{broadcast_id}")
async def get_broadcast_schedule(
    broadcast_id: str,
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    item = await get_scheduled_broadcast(session, broadcast_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Scheduled broadcast not found")
    return {"item": scheduled_broadcast_to_dict(item)}


@router.patch("/broadcast/scheduled/{broadcast_id}")
async def update_broadcast_schedule(
    broadcast_id: str,
    payload: ScheduledBroadcastUpdatePayload,
    identity=Depends(verify_identity_admin_short),
    session: AsyncSession = Depends(get_session),
):
    current = await get_scheduled_broadcast(session, broadcast_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Scheduled broadcast not found")
    try:
        values = _resolve_update_payload(payload, current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await update_scheduled_broadcast(session, broadcast_id, **values)
    if updated is None:
        raise HTTPException(status_code=409, detail="Scheduled broadcast can no longer be edited")
    return {"success": True, "item": scheduled_broadcast_to_dict(updated)}


@router.post("/broadcast/scheduled/{broadcast_id}/cancel")
async def cancel_broadcast_schedule(
    broadcast_id: str,
    identity=Depends(verify_identity_admin_short),
    session: AsyncSession = Depends(get_session),
):
    item = await cancel_scheduled_broadcast(session, broadcast_id)
    if item is None:
        raise HTTPException(status_code=409, detail="Scheduled broadcast can no longer be cancelled")
    return {"success": True, "item": scheduled_broadcast_to_dict(item)}


@router.post("/broadcast/scheduled/{broadcast_id}/send-now")
async def send_broadcast_schedule_now(
    broadcast_id: str,
    identity=Depends(verify_identity_admin_short),
    session: AsyncSession = Depends(get_session),
):
    item = await start_scheduled_broadcast(session, broadcast_id)
    if item is None:
        raise HTTPException(status_code=409, detail="Scheduled broadcast can no longer be sent now")
    try:
        result = await execute_scheduled_broadcast(item, bot=_get_broadcast_bot())
    except Exception as exc:
        await mark_scheduled_broadcast_failed(session, broadcast_id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.get("success"):
        item = await mark_scheduled_broadcast_sent(session, broadcast_id, result)
    else:
        item = await mark_scheduled_broadcast_failed(session, broadcast_id, result.get("message", "Broadcast failed"))
    return {"success": bool(result.get("success")), "item": scheduled_broadcast_to_dict(item), "result": result}
