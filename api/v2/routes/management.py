import asyncio
import os
import re
import subprocess
import sys

from datetime import datetime, timedelta, timezone
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
from core.bootstrap import MANAGEMENT_CONFIG
from core.executor import run_io
from core.redis_cache import cache_incr
from core.settings.management_config import update_management_config
from core.settings.modes_config import resolve_protect_content
from database import async_session_maker
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


async def _admin_rate_limit(request_or_identity, action: str, max_calls: int, window_sec: int) -> None:
    identity_id = getattr(request_or_identity, "id", "unknown")
    key = f"admin_rl:{action}:{identity_id}"
    count = await cache_incr(key, window_sec)
    if count > max_calls:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте позже.")


class MaintenanceUpdate(BaseModel):
    enabled: bool


class DomainChange(BaseModel):
    domain: str


class BroadcastLaunchPayload(BaseModel):
    send_to: Literal["all", "subscribed", "unsubscribed", "untrial", "trial", "hotleads", "cluster"] = "all"
    channel: Literal["bot", "site", "both"] = "both"
    text: str
    photo: str | None = None
    cluster_name: str | None = None
    workers: int = 5
    messages_per_second: int = 35


class ScheduledBroadcastCreatePayload(BroadcastLaunchPayload):
    scheduled_for: datetime


class ScheduledBroadcastUpdatePayload(BaseModel):
    send_to: Literal["all", "subscribed", "unsubscribed", "untrial", "trial", "hotleads", "cluster"] | None = None
    channel: Literal["bot", "site", "both"] | None = None
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
        _broadcast_bot = Bot(
            token=API_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, protect_content=resolve_protect_content()),
        )
    elif _broadcast_bot.default is not None:
        _broadcast_bot.default.protect_content = resolve_protect_content()
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
    text_changed = "text" in fields
    send_to = payload.send_to if "send_to" in fields else current.send_to
    channel = payload.channel if "channel" in fields else current.channel
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
        channel=channel,
    )
    if not text_changed:
        prepared["text"] = current.text
        prepared["keyboard_json"] = current.keyboard_json
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


class BulkFilterPayload(BaseModel):
    filter_type: Literal["tariff", "cluster", "created", "expiry"]
    tariff_id: int | None = None
    cluster_name: str | None = None
    created_days: int | None = None
    created_dir: Literal["older", "newer"] | None = None
    expiry_kind: Literal["expired", "active", "soon"] | None = None
    expiry_days: int | None = None


class BulkApplyPayload(BulkFilterPayload):
    action: Literal["add_days", "add_gb", "freeze", "unfreeze", "delete"]
    value: int = 0


@router.post("/bulk/preview")
async def bulk_preview(
    payload: BulkFilterPayload,
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Сколько ключей попадает под фильтр (без действия)."""
    from handlers.admin.bulk.query import fetch_matching_keys

    keys = await fetch_matching_keys(session, payload.model_dump(exclude_none=True))
    return {"count": len(keys)}


@router.post("/bulk/apply")
async def bulk_apply(
    payload: BulkApplyPayload,
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Массовое действие над ключами по фильтру."""
    from handlers.admin.bulk.operations import (
        bulk_add_days,
        bulk_add_gb,
        bulk_delete,
        bulk_freeze,
        bulk_unfreeze,
    )
    from handlers.admin.bulk.query import fetch_matching_keys

    data = payload.model_dump(exclude_none=True)
    keys = await fetch_matching_keys(session, data)
    if not keys:
        return {"matched": 0, "ok": 0, "failed": 0}

    if payload.action == "add_days":
        ok, failed, _ = await bulk_add_days(session, keys, int(payload.value))
    elif payload.action == "add_gb":
        ok, failed, _ = await bulk_add_gb(session, keys, int(payload.value))
    elif payload.action == "freeze":
        ok, failed, _ = await bulk_freeze(session, keys)
    elif payload.action == "unfreeze":
        ok, failed, _ = await bulk_unfreeze(session, keys)
    elif payload.action == "delete":
        ok, failed, _ = await bulk_delete(session, keys)
    else:
        raise HTTPException(status_code=400, detail="Unknown action")

    return {"matched": len(keys), "ok": int(ok), "failed": int(failed)}


@router.get("/dashboard")
async def get_dashboard(
    days: int = Query(30, ge=1, le=365),
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Сводка для дашборда: юзеры, ключи, активные подписки, выручка, новые за период."""
    from database.models import Coupon, Gift, Payment

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since = datetime.utcnow() - timedelta(days=days)
    since_ms = int(since.timestamp() * 1000)
    soon_ms = now_ms + 7 * 86_400_000

    total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0
    total_keys = (await session.execute(select(func.count()).select_from(Key))).scalar() or 0
    active_keys = (
        await session.execute(
            select(func.count()).select_from(Key).where(Key.expiry_time > now_ms, Key.is_frozen.is_(False))
        )
    ).scalar() or 0
    new_users = (
        await session.execute(select(func.count()).select_from(User).where(User.created_at >= since))
    ).scalar() or 0
    revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == "success", Payment.created_at >= since
            )
        )
    ).scalar() or 0
    revenue_total = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "success")
        )
    ).scalar() or 0

    daily_rows = (
        await session.execute(
            select(func.date(Payment.created_at).label("d"), func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.status == "success", Payment.created_at >= since)
            .group_by(func.date(Payment.created_at))
            .order_by(func.date(Payment.created_at))
        )
    ).all()
    revenue_series = [{"date": str(d), "amount": float(a or 0)} for d, a in daily_rows]

    frozen_keys = (
        await session.execute(select(func.count()).select_from(Key).where(Key.is_frozen.is_(True)))
    ).scalar() or 0
    trial_users = (
        await session.execute(select(func.count()).select_from(User).where(User.trial > 0))
    ).scalar() or 0
    new_keys = (
        await session.execute(select(func.count()).select_from(Key).where(Key.created_at >= since_ms))
    ).scalar() or 0
    expiring_soon = (
        await session.execute(
            select(func.count())
            .select_from(Key)
            .where(Key.expiry_time > now_ms, Key.expiry_time <= soon_ms, Key.is_frozen.is_(False))
        )
    ).scalar() or 0
    payments_count = (
        await session.execute(
            select(func.count()).select_from(Payment).where(Payment.status == "success", Payment.created_at >= since)
        )
    ).scalar() or 0
    gifts_total = (await session.execute(select(func.count()).select_from(Gift))).scalar() or 0
    coupons_total = (await session.execute(select(func.count()).select_from(Coupon))).scalar() or 0
    try:
        from database.models import WebErrorReport

        errors_open = (
            await session.execute(
                select(func.count()).select_from(WebErrorReport).where(WebErrorReport.resolved.is_(False))
            )
        ).scalar() or 0
    except Exception:
        errors_open = 0
    avg_check = float(revenue) / int(payments_count) if payments_count else 0.0

    sys_rows = (
        await session.execute(
            select(Payment.payment_system, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.status == "success", Payment.created_at >= since)
            .group_by(Payment.payment_system)
            .order_by(func.coalesce(func.sum(Payment.amount), 0).desc())
        )
    ).all()
    revenue_by_system = [{"name": (s or "—"), "amount": float(a or 0)} for s, a in sys_rows]

    user_rows = (
        await session.execute(
            select(func.date(User.created_at).label("d"), func.count())
            .where(User.created_at >= since)
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
    ).all()
    users_series = [{"date": str(d), "count": int(c or 0)} for d, c in user_rows]

    return {
        "period_days": days,
        "total_users": int(total_users),
        "new_users": int(new_users),
        "total_keys": int(total_keys),
        "active_keys": int(active_keys),
        "frozen_keys": int(frozen_keys),
        "trial_users": int(trial_users),
        "new_keys": int(new_keys),
        "expiring_soon": int(expiring_soon),
        "payments_count": int(payments_count),
        "gifts_total": int(gifts_total),
        "coupons_total": int(coupons_total),
        "errors_open": int(errors_open),
        "avg_check": float(avg_check),
        "revenue_period": float(revenue),
        "revenue_total": float(revenue_total),
        "revenue_series": revenue_series,
        "revenue_by_system": revenue_by_system,
        "users_series": users_series,
    }


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
    await _admin_rate_limit(identity, "restart", max_calls=3, window_sec=60)
    background.add_task(_restart_bot)
    return {"status": "restarting"}


@router.post("/change-domain")
async def change_domain(
    payload: DomainChange,
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Массовая замена домена в ключах и remnawave_link."""
    await _admin_rate_limit(identity, "change_domain", max_calls=3, window_sec=300)
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
    return {"updated": result.rowcount or 0}


@router.post("/restore-trials")
async def restore_trials(
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Сбрасывает trial=0 у пользователей без ключей."""
    await _admin_rate_limit(identity, "restore_trials", max_calls=3, window_sec=300)
    stmt = (
        update(User)
        .where(
            User.trial == 1,
            ~exists(select(Key.user_id).where(Key.user_id == User.id)),
        )
        .values(trial=0)
    )
    result = await session.execute(stmt)
    return {"restored": result.rowcount or 0}


@router.post("/backup")
async def trigger_backup(identity=Depends(verify_identity_admin)):
    """Запуск бэкапа БД в фоне."""
    await _admin_rate_limit(identity, "backup", max_calls=2, window_sec=300)

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
        logger.warning("audit-drain failed: {}", exc)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка при дренаже аудита") from exc


@router.post("/broadcast")
async def launch_broadcast(
    payload: BroadcastLaunchPayload,
    identity=Depends(verify_identity_admin_short),
):
    """Запуск рассылки по выбранной аудитории. Сессия БД не держится на время рассылки."""
    await _admin_rate_limit(identity, "broadcast", max_calls=5, window_sec=300)
    try:
        prepared = prepare_broadcast_payload(
            send_to=payload.send_to,
            text=payload.text,
            photo=payload.photo,
            cluster_name=payload.cluster_name,
            workers=payload.workers,
            messages_per_second=payload.messages_per_second,
            channel=payload.channel,
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
            channel=payload.channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    broadcast = await create_scheduled_broadcast(
        session,
        created_by_tg_id=getattr(identity, "tg_id", None),
        send_to=prepared["send_to"],
        channel=prepared["channel"],
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
    await _admin_rate_limit(identity, "broadcast_now", max_calls=5, window_sec=300)
    item = await start_scheduled_broadcast(session, broadcast_id)
    if item is None:
        raise HTTPException(status_code=409, detail="Scheduled broadcast can no longer be sent now")
    try:
        result = await execute_scheduled_broadcast(item, bot=_get_broadcast_bot())
    except Exception as exc:
        logger.error("[Broadcast] send-now failed for {}: {}", broadcast_id, exc)
        await mark_scheduled_broadcast_failed(session, broadcast_id, str(exc))
        raise HTTPException(status_code=500, detail="Ошибка при выполнении рассылки") from exc
    if result.get("success"):
        item = await mark_scheduled_broadcast_sent(session, broadcast_id, result)
    else:
        item = await mark_scheduled_broadcast_failed(session, broadcast_id, result.get("message", "Broadcast failed"))
    return {"success": bool(result.get("success")), "item": scheduled_broadcast_to_dict(item), "result": result}
