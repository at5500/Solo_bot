from __future__ import annotations

import json
import uuid

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable

from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject, User
from fastapi import Request
from sqlalchemy import and_, delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AuditEvent
from logger import logger

def _get_bot_webhook_path() -> str:
    """Путь вебхука бота из конфига (для исключения из шага «успешная оплата»)."""
    try:
        from config import WEBHOOK_PATH
        return ((WEBHOOK_PATH or "").strip().lower()) or ""
    except ImportError:
        return ""


def _is_bot_webhook_path(path: str) -> bool:
    """True только если path — именно вебхук бота (точное совпадение сегмента пути), не касса."""
    bot_path = _get_bot_webhook_path()
    if not bot_path:
        return False
    p = (path or "").strip().lower()
    path_segment = p.split(" ", 1)[1] if " " in p else p
    return path_segment == bot_path or path_segment.rstrip("/") == bot_path.rstrip("/")

try:
    from core.cache_config import (
        AUDIT_REDIS_BUFFER_ENABLED,
        AUDIT_REDIS_DRAIN_BATCH,
        AUDIT_REDIS_FLUSH_KEY,
        AUDIT_REDIS_IDENTITY_PREFIX,
        AUDIT_REDIS_USER_PREFIX,
        AUDIT_REDIS_USER_TTL_SEC,
    )
except ImportError:
    AUDIT_REDIS_BUFFER_ENABLED = False
    AUDIT_REDIS_FLUSH_KEY = "audit:flush"
    AUDIT_REDIS_USER_PREFIX = "audit:user:tg:"
    AUDIT_REDIS_IDENTITY_PREFIX = "audit:user:identity:"
    AUDIT_REDIS_USER_TTL_SEC = 25 * 3600
    AUDIT_REDIS_DRAIN_BATCH = 1000

_MAX_TEXT_LEN = 160
_AUDIT_TABLE_READY = False


@dataclass
class AuditContext:
    request_id: str
    channel: str
    path_or_handler: str
    actor_identity_id: str | None = None
    actor_tg_id: int | None = None


def new_request_id() -> str:
    return uuid.uuid4().hex


def _naive_utc(dt: datetime) -> datetime:
    """Приводит datetime к naive UTC для запросов к колонкам DateTime (без timezone)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def _trim(value: Any, limit: int = _MAX_TEXT_LEN) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return _trim(value, 500)


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True)


def _message_text(event: TelegramObject) -> str | None:
    if isinstance(event, Message):
        return _trim(event.text or event.caption)
    if isinstance(event, CallbackQuery):
        return _trim(event.data)
    if isinstance(event, InlineQuery):
        return _trim(event.query)
    return None


def _event_user(event: TelegramObject) -> User | None:
    if hasattr(event, "from_user") and isinstance(event.from_user, User):
        return event.from_user
    return None


def describe_telegram_event(event: TelegramObject) -> str:
    if isinstance(event, Message):
        return f"message:{_message_text(event) or '-'}"
    if isinstance(event, CallbackQuery):
        return f"callback:{_message_text(event) or '-'}"
    if isinstance(event, InlineQuery):
        return f"inline:{_message_text(event) or '-'}"
    return type(event).__name__


def ensure_api_context(request: Request) -> AuditContext:
    context = getattr(request.state, "audit_context", None)
    if isinstance(context, AuditContext):
        return context

    path_or_handler = request.url.path
    if request.url.query:
        path_or_handler = f"{path_or_handler}?{request.url.query}"

    context = AuditContext(
        request_id=new_request_id(),
        channel="api",
        path_or_handler=path_or_handler,
    )
    request.state.audit_context = context
    request.state.audit_request_id = context.request_id
    return context


def get_api_context(request: Request | None) -> AuditContext | None:
    if request is None:
        return None
    context = getattr(request.state, "audit_context", None)
    if isinstance(context, AuditContext):
        return context
    return None


def set_api_actor(
    request: Request,
    *,
    identity_id: str | None = None,
    tg_id: int | None = None,
) -> AuditContext:
    context = ensure_api_context(request)
    if identity_id is not None:
        context.actor_identity_id = identity_id
    if tg_id is not None:
        context.actor_tg_id = tg_id
    return context


def ensure_telegram_context(
    data: dict[str, Any] | None,
    event: TelegramObject,
) -> AuditContext:
    if data is not None:
        existing = data.get("audit_context")
        if isinstance(existing, AuditContext):
            return existing

    user = _event_user(event)
    context = AuditContext(
        request_id=new_request_id(),
        channel="telegram",
        path_or_handler=describe_telegram_event(event),
        actor_tg_id=user.id if user else None,
    )
    if data is not None:
        data["audit_context"] = context
        data["audit_request_id"] = context.request_id
    return context


def set_telegram_actor(
    audit_context: AuditContext | dict[str, Any] | None,
    *,
    identity_id: str | None = None,
    tg_id: int | None = None,
) -> AuditContext | None:
    context: AuditContext | None
    if isinstance(audit_context, AuditContext):
        context = audit_context
    elif isinstance(audit_context, dict):
        context = audit_context.get("audit_context")
    else:
        context = None

    if not isinstance(context, AuditContext):
        return None
    if identity_id is not None:
        context.actor_identity_id = identity_id
    if tg_id is not None:
        context.actor_tg_id = tg_id
    return context


def get_telegram_context(audit_context: AuditContext | dict[str, Any] | None) -> AuditContext | None:
    if isinstance(audit_context, AuditContext):
        return audit_context
    if isinstance(audit_context, dict):
        context = audit_context.get("audit_context")
        if isinstance(context, AuditContext):
            return context
    return None


def log_api_access(
    request: Request,
    *,
    status_code: int,
    duration_ms: int,
    result: str,
    reason: str | None = None,
) -> None:
    context = ensure_api_context(request)
    client_ip = request.client.host if request.client else "-"
    logger.debug(
        f"[AUDIT_ACCESS] {_serialize({
            'request_id': context.request_id,
            'channel': 'api',
            'method': request.method,
            'path': context.path_or_handler,
            'status_code': status_code,
            'duration_ms': duration_ms,
            'result': result,
            'reason': reason,
            'client_ip': client_ip,
            'actor_identity_id': context.actor_identity_id,
            'actor_tg_id': context.actor_tg_id,
        })}"
    )


def log_telegram_access(
    event: TelegramObject,
    *,
    audit_context: AuditContext | None,
    result: str,
    reason: str | None = None,
) -> None:
    context = audit_context or AuditContext(
        request_id=new_request_id(),
        channel="telegram",
        path_or_handler=describe_telegram_event(event),
    )
    user = _event_user(event)
    logger.debug(
        f"[AUDIT_ACCESS] {_serialize({
            'request_id': context.request_id,
            'channel': 'telegram',
            'path_or_handler': context.path_or_handler,
            'event_type': type(event).__name__,
            'message': _message_text(event),
            'result': result,
            'reason': reason,
            'actor_identity_id': context.actor_identity_id,
            'actor_tg_id': context.actor_tg_id or (user.id if user else None),
            'username': getattr(user, 'username', None) if user else None,
        })}"
    )


async def record_audit_event(
    session: AsyncSession,
    *,
    event_type: str,
    channel: str,
    path_or_handler: str,
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    result: str = "success",
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    await ensure_audit_table(session)
    event = AuditEvent(
        event_type=event_type,
        channel=channel,
        actor_identity_id=actor_identity_id,
        actor_tg_id=actor_tg_id,
        path_or_handler=_trim(path_or_handler, 255) or channel,
        entity_type=_trim(entity_type, 64),
        entity_id=_trim(entity_id, 255),
        result=_trim(result, 32) or "success",
        reason=_trim(reason, 1000),
        metadata_=_jsonable(metadata) if metadata else None,
        request_id=_trim(request_id, 64),
    )
    session.add(event)
    await session.flush()
    logger.debug(
        f"[AUDIT_EVENT] {_serialize({
            'id': event.id,
            'request_id': event.request_id,
            'channel': event.channel,
            'event_type': event.event_type,
            'actor_identity_id': event.actor_identity_id,
            'actor_tg_id': event.actor_tg_id,
            'path_or_handler': event.path_or_handler,
            'entity_type': event.entity_type,
            'entity_id': event.entity_id,
            'result': event.result,
            'reason': event.reason,
            'metadata': event.metadata_,
        })}"
    )
    return event


async def safe_record_audit_event(session: AsyncSession, **kwargs: Any) -> AuditEvent | None:
    try:
        return await record_audit_event(session, **kwargs)
    except Exception as exc:
        logger.warning(f"[Audit] Не удалось записать событие {kwargs.get('event_type')}: {exc}")
        return None


def _telegram_access_payload(
    audit_context: AuditContext | None,
    event: TelegramObject,
    *,
    result: str = "success",
    reason: str | None = None,
) -> dict[str, Any]:
    """Собирает payload для записи telegram_access (для фоновой задачи или синхронной)."""
    ctx = get_telegram_context(audit_context)
    user = _event_user(event)
    path = describe_telegram_event(event)
    if ctx is None:
        ctx = AuditContext(
            request_id=new_request_id(),
            channel="telegram",
            path_or_handler=path,
            actor_tg_id=user.id if user else None,
        )
    return {
        "request_id": ctx.request_id,
        "path_or_handler": path,
        "actor_identity_id": ctx.actor_identity_id,
        "actor_tg_id": ctx.actor_tg_id or (user.id if user else None),
        "result": result,
        "reason": reason,
    }


async def record_telegram_access_event(
    session: AsyncSession,
    audit_context: AuditContext | None,
    event: TelegramObject,
    *,
    result: str = "success",
    reason: str | None = None,
) -> AuditEvent | None:
    """Пишет в БД одно событие «обработчик Telegram вызван» для полного следа пользователя."""
    payload = _telegram_access_payload(audit_context, event, result=result, reason=reason)
    return await safe_record_audit_event(
        session,
        event_type="telegram_access",
        channel="telegram",
        path_or_handler=payload["path_or_handler"],
        actor_identity_id=payload["actor_identity_id"],
        actor_tg_id=payload["actor_tg_id"],
        result=payload["result"],
        reason=payload["reason"],
        request_id=payload["request_id"],
    )


def _audit_record_for_redis(
    *,
    event_type: str = "telegram_access",
    channel: str = "telegram",
    path_or_handler: str,
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    result: str = "success",
    reason: str | None = None,
    request_id: str | None = None,
    metadata_: dict | None = None,
) -> dict[str, Any]:
    """Формирует запись для буфера Redis (с created_at в ISO)."""
    return {
        "event_type": event_type,
        "channel": channel,
        "path_or_handler": _trim(path_or_handler, 255) or channel,
        "actor_identity_id": actor_identity_id,
        "actor_tg_id": actor_tg_id,
        "entity_type": _trim(entity_type, 64) if entity_type else None,
        "entity_id": _trim(str(entity_id), 255) if entity_id is not None else None,
        "result": _trim(result, 32) or "success",
        "reason": _trim(reason, 1000) if reason else None,
        "request_id": _trim(request_id, 64) if request_id else None,
        "metadata_": _jsonable(metadata_) if metadata_ else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def record_audit_event_to_redis(
    *,
    request_id: str | None = None,
    path_or_handler: str = "",
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    result: str = "success",
    reason: str | None = None,
) -> None:
    """Пишет событие telegram_access в буфер Redis (списки для выгрузки в БД и для чтения по пользователю)."""
    from core.redis_cache import cache_expire, cache_rpush

    record = _audit_record_for_redis(
        path_or_handler=path_or_handler,
        actor_identity_id=actor_identity_id,
        actor_tg_id=actor_tg_id,
        result=result,
        reason=reason,
        request_id=request_id,
    )
    n = await cache_rpush(AUDIT_REDIS_FLUSH_KEY, record)
    if n == 0:
        raise RuntimeError("Redis unavailable (cache_rpush returned 0)")
    if actor_tg_id is not None:
        user_key = f"{AUDIT_REDIS_USER_PREFIX}{actor_tg_id}"
        await cache_rpush(user_key, record)
        await cache_expire(user_key, AUDIT_REDIS_USER_TTL_SEC)
    if actor_identity_id:
        identity_key = f"{AUDIT_REDIS_IDENTITY_PREFIX}{actor_identity_id}"
        await cache_rpush(identity_key, record)
        await cache_expire(identity_key, AUDIT_REDIS_USER_TTL_SEC)


async def record_api_access_event_to_redis(
    *,
    request_id: str | None = None,
    path_or_handler: str = "",
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    result: str = "success",
    reason: str | None = None,
) -> None:
    """Пишет событие api_access в буфер Redis (как telegram_access)."""
    from core.redis_cache import cache_expire, cache_rpush

    record = _audit_record_for_redis(
        event_type="api_access",
        channel="api",
        path_or_handler=path_or_handler,
        actor_identity_id=actor_identity_id,
        actor_tg_id=actor_tg_id,
        result=result,
        reason=reason,
        request_id=request_id,
    )
    await cache_rpush(AUDIT_REDIS_FLUSH_KEY, record)
    if actor_tg_id is not None:
        user_key = f"{AUDIT_REDIS_USER_PREFIX}{actor_tg_id}"
        await cache_rpush(user_key, record)
        await cache_expire(user_key, AUDIT_REDIS_USER_TTL_SEC)
    if actor_identity_id:
        identity_key = f"{AUDIT_REDIS_IDENTITY_PREFIX}{actor_identity_id}"
        await cache_rpush(identity_key, record)
        await cache_expire(identity_key, AUDIT_REDIS_USER_TTL_SEC)


async def record_api_access_event_background(
    session_factory: Any,
    request: Request,
    *,
    result: str = "success",
    reason: str | None = None,
    status_code: int = 200,
) -> None:
    """Пишет одно событие api_access в Redis-буфер или в БД в фоне (после обработки запроса).
    Вызывается из middleware; actor берётся из request.state (set_api_actor в эндпоинтах)."""
    context = ensure_api_context(request)
    path_or_handler = f"{request.method} {request.url.path}"
    if request.url.query:
        path_or_handler = f"{path_or_handler}?{request.url.query}"
    path_or_handler = _trim(path_or_handler, 255) or "api"
    if AUDIT_REDIS_BUFFER_ENABLED:
        try:
            await record_api_access_event_to_redis(
                request_id=context.request_id,
                path_or_handler=path_or_handler,
                actor_identity_id=context.actor_identity_id,
                actor_tg_id=context.actor_tg_id,
                result=result,
                reason=reason,
            )
        except Exception as exc:
            logger.warning("[Audit] Запись api_access в Redis-буфер не удалась: %s", exc)
        return
    try:
        async with session_factory() as session:
            await ensure_audit_table(session)
            await record_audit_event(
                session,
                event_type="api_access",
                channel="api",
                path_or_handler=path_or_handler,
                actor_identity_id=context.actor_identity_id,
                actor_tg_id=context.actor_tg_id,
                result=result,
                reason=reason,
                request_id=_trim(context.request_id, 64),
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "[Audit] Фоновая запись api_access не удалась: %s",
            exc,
            extra={"path_or_handler": path_or_handler[:80] if path_or_handler else None},
        )


async def record_telegram_access_event_background(
    session_factory: Any,
    *,
    request_id: str | None,
    path_or_handler: str,
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    result: str = "success",
    reason: str | None = None,
) -> None:
    """Пишет событие telegram_access: в Redis-буфер (если включён), иначе в БД.
    При сбое записи в Redis — fallback в БД, чтобы события не терялись."""
    if AUDIT_REDIS_BUFFER_ENABLED:
        try:
            await record_audit_event_to_redis(
                request_id=request_id,
                path_or_handler=path_or_handler,
                actor_identity_id=actor_identity_id,
                actor_tg_id=actor_tg_id,
                result=result,
                reason=reason,
            )
            return
        except Exception as exc:
            logger.warning(
                f"[Audit] Запись в Redis-буфер не удалась, пишем в БД: {exc}"
            )
    try:
        async with session_factory() as session:
            await ensure_audit_table(session)
            await record_audit_event(
                session,
                event_type="telegram_access",
                channel="telegram",
                path_or_handler=_trim(path_or_handler, 255) or "telegram",
                actor_identity_id=actor_identity_id,
                actor_tg_id=actor_tg_id,
                result=result,
                reason=reason,
                request_id=_trim(request_id, 64),
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "[Audit] Фоновая запись telegram_access не удалась: %s",
            exc,
            extra={"path_or_handler": path_or_handler[:80] if path_or_handler else None},
        )


async def safe_record_api_event(
    session: AsyncSession,
    request: Request,
    *,
    event_type: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    result: str = "success",
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    path_or_handler: str | None = None,
) -> AuditEvent | None:
    context = ensure_api_context(request)
    return await safe_record_audit_event(
        session,
        event_type=event_type,
        channel="api",
        path_or_handler=path_or_handler or context.path_or_handler,
        actor_identity_id=actor_identity_id if actor_identity_id is not None else context.actor_identity_id,
        actor_tg_id=actor_tg_id if actor_tg_id is not None else context.actor_tg_id,
        entity_type=entity_type,
        entity_id=entity_id,
        result=result,
        reason=reason,
        metadata=metadata,
        request_id=context.request_id,
    )


async def safe_record_telegram_event(
    session: AsyncSession,
    audit_context: AuditContext | dict[str, Any] | None,
    *,
    event_type: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    result: str = "success",
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_identity_id: str | None = None,
    actor_tg_id: int | None = None,
    path_or_handler: str | None = None,
) -> AuditEvent | None:
    context = get_telegram_context(audit_context)
    return await safe_record_audit_event(
        session,
        event_type=event_type,
        channel="telegram",
        path_or_handler=path_or_handler or (context.path_or_handler if context else "telegram"),
        actor_identity_id=actor_identity_id if actor_identity_id is not None else (context.actor_identity_id if context else None),
        actor_tg_id=actor_tg_id if actor_tg_id is not None else (context.actor_tg_id if context else None),
        entity_type=entity_type,
        entity_id=entity_id,
        result=result,
        reason=reason,
        metadata=metadata,
        request_id=context.request_id if context else None,
    )


def _redis_record_to_event_like(rec: dict[str, Any]) -> SimpleNamespace:
    """Превращает запись из Redis в объект с теми же атрибутами, что и AuditEvent."""
    created = rec.get("created_at")
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            created = datetime.now(timezone.utc)
    elif created is None:
        created = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=None,
        event_type=rec.get("event_type", "telegram_access"),
        channel=rec.get("channel", "telegram"),
        path_or_handler=rec.get("path_or_handler") or "",
        actor_identity_id=rec.get("actor_identity_id"),
        actor_tg_id=rec.get("actor_tg_id"),
        entity_type=rec.get("entity_type"),
        entity_id=rec.get("entity_id"),
        result=rec.get("result", "success"),
        reason=rec.get("reason"),
        metadata_=rec.get("metadata_"),
        request_id=rec.get("request_id"),
        created_at=created,
    )


async def _list_audit_events_from_redis(
    tg_id: int | None,
    identity_id: str | None,
    channel: str | None,
    event_types: list[str] | None,
    max_events: int = 3000,
) -> list[SimpleNamespace]:
    """Читает события пользователя из Redis-буфера (для слияния с БД)."""
    if not AUDIT_REDIS_BUFFER_ENABLED:
        return []
    from core.redis_cache import cache_lrange

    out: list[SimpleNamespace] = []
    seen: set[tuple[str, str]] = set()
    keys_to_read = []
    if tg_id is not None:
        keys_to_read.append(f"{AUDIT_REDIS_USER_PREFIX}{tg_id}")
    if identity_id:
        keys_to_read.append(f"{AUDIT_REDIS_IDENTITY_PREFIX}{identity_id}")
    for key in keys_to_read:
        raw = await cache_lrange(key, -max_events, -1)
        for rec in reversed(raw):
            if not isinstance(rec, dict):
                continue
            created = rec.get("created_at")
            rid = rec.get("request_id") or ""
            if (created, rid) in seen:
                continue
            if channel and rec.get("channel") != channel:
                continue
            if event_types and rec.get("event_type") not in event_types:
                continue
            seen.add((str(created), rid))
            out.append(_redis_record_to_event_like(rec))
    out.sort(key=lambda e: e.created_at, reverse=True)
    return out[:max_events]


async def list_audit_events_from_redis_buffer(max_events: int = 5000) -> list[SimpleNamespace]:
    """Читает последние события из глобального буфера Redis (audit:flush). Не удаляет записи."""
    if not AUDIT_REDIS_BUFFER_ENABLED:
        return []
    from core.redis_cache import cache_lrange

    raw = await cache_lrange(AUDIT_REDIS_FLUSH_KEY, -max_events, -1)
    out = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        out.append(_redis_record_to_event_like(rec))
    out.sort(key=lambda e: e.created_at)
    return out


def _aggregate_audit_rows(
    rows: list[tuple[Any, Any, Any, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], set[tuple[str, int | str]]]:
    """Собирает by_step, by_path_list, all_actors из строк (path, result, actor_tg_id, actor_identity_id)."""
    by_step: dict[str, dict[str, Any]] = {}
    total_events = 0
    all_actors: set[tuple[str, int | str]] = set()

    for path, res, tg_id, identity_id in rows:
        total_events += 1
        step = _normalize_path_to_step(path or "")
        actor = (identity_id or "", tg_id or 0)
        if identity_id or tg_id:
            all_actors.add(actor)
        if step not in by_step:
            by_step[step] = {"total": 0, "success": 0, "fail": 0, "actors": set()}
        by_step[step]["total"] += 1
        if res == "success":
            by_step[step]["success"] += 1
        else:
            by_step[step]["fail"] += 1
        if identity_id or tg_id:
            by_step[step]["actors"].add(actor)

    by_path_list = []
    for step, data in sorted(by_step.items(), key=lambda x: -x[1]["total"]):
        total = data["total"]
        fail = data["fail"]
        unique = len(data["actors"])
        fail_rate = round(100.0 * fail / total, 1) if total else 0
        by_path_list.append({
            "step": step,
            "label": AUDIT_STEP_LABELS.get(step, step),
            "total": total,
            "success": data["success"],
            "fail": fail,
            "unique_users": unique,
            "fail_rate_pct": fail_rate,
        })
    return by_step, by_path_list, all_actors


async def list_audit_events(
    session: AsyncSession,
    *,
    identity_id: str | None = None,
    tg_id: int | None = None,
    channel: str | None = None,
    event_type: str | None = None,
    event_types: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditEvent | SimpleNamespace]:
    """Список событий аудита. При включённом Redis-буфере объединяет данные из БД и Redis."""
    await ensure_audit_table(session)
    event_types_list = sorted(event_types) if event_types else None

    if not AUDIT_REDIS_BUFFER_ENABLED:
        stmt = select(AuditEvent)
        actor_filters = []
        if identity_id:
            actor_filters.append(AuditEvent.actor_identity_id == identity_id)
            actor_filters.append(and_(AuditEvent.entity_type == "identity", AuditEvent.entity_id == identity_id))
        if tg_id is not None:
            tg_id_str = str(tg_id)
            actor_filters.append(AuditEvent.actor_tg_id == tg_id)
            actor_filters.append(and_(AuditEvent.entity_type == "user", AuditEvent.entity_id == tg_id_str))
            actor_filters.append(and_(AuditEvent.entity_type == "telegram_user", AuditEvent.entity_id == tg_id_str))
        if actor_filters:
            stmt = stmt.where(or_(*actor_filters))
        if channel:
            stmt = stmt.where(AuditEvent.channel == channel)
        if event_type:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        if event_types_list:
            stmt = stmt.where(AuditEvent.event_type.in_(event_types_list))
        stmt = stmt.order_by(desc(AuditEvent.created_at), desc(AuditEvent.id)).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    redis_events = await _list_audit_events_from_redis(
        tg_id, identity_id, channel, event_types_list, max_events=3000
    )
    need = offset + limit + len(redis_events)
    stmt = select(AuditEvent)
    actor_filters = []
    if identity_id:
        actor_filters.append(AuditEvent.actor_identity_id == identity_id)
        actor_filters.append(and_(AuditEvent.entity_type == "identity", AuditEvent.entity_id == identity_id))
    if tg_id is not None:
        tg_id_str = str(tg_id)
        actor_filters.append(AuditEvent.actor_tg_id == tg_id)
        actor_filters.append(and_(AuditEvent.entity_type == "user", AuditEvent.entity_id == tg_id_str))
        actor_filters.append(and_(AuditEvent.entity_type == "telegram_user", AuditEvent.entity_id == tg_id_str))
    if actor_filters:
        stmt = stmt.where(or_(*actor_filters))
    if channel:
        stmt = stmt.where(AuditEvent.channel == channel)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if event_types_list:
        stmt = stmt.where(AuditEvent.event_type.in_(event_types_list))
    stmt = stmt.order_by(desc(AuditEvent.created_at), desc(AuditEvent.id)).limit(min(5000, need)).offset(0)
    result = await session.execute(stmt)
    db_events = list(result.scalars().all())
    merged = redis_events + db_events
    merged.sort(key=lambda e: (e.created_at, getattr(e, "id", 0)), reverse=True)
    return merged[offset : offset + limit]


async def ensure_audit_table(session: AsyncSession) -> None:
    global _AUDIT_TABLE_READY
    if _AUDIT_TABLE_READY:
        return
    connection = await session.connection()
    await connection.run_sync(AuditEvent.__table__.create, checkfirst=True)
    _AUDIT_TABLE_READY = True


async def delete_old_audit_events(
    session: AsyncSession,
    *,
    older_than_days: int = 90,
) -> int:
    """Удаляет события старше N дней. Вызывать по крону/периодике при больших наплывах.
    Возвращает количество удалённых строк."""
    await ensure_audit_table(session)
    threshold = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    stmt = delete(AuditEvent).where(AuditEvent.created_at < threshold)
    result = await session.execute(stmt)
    return result.rowcount or 0


AUDIT_STEP_LABELS: dict[str, str] = {
    "start": "Старт",
    "profile": "Профиль",
    "view_keys": "Мои ключи",
    "key_create": "Подписка оформлена",
    "pay_start": "Начало оплаты (ссылка создана)",
    "pay": "Успешная оплата (пополнение)",
    "key_view": "Ключ (карточка)",
    "connect": "Подписка подключена",
    "renew": "Продление",
    "addons": "Аддоны",
    "referral": "Рефералы",
    "coupons": "Купоны",
    "register": "Регистрация (API)",
    "login": "Вход (API)",
    "api_other": "API прочее",
    "admin": "Админ-панель",
    "other": "Прочее",
}


DEFAULT_FUNNEL_STEPS = ("start", "profile", "view_keys", "key_create", "pay_start", "pay", "key_view", "connect")


_CALLBACK_EXACT: dict[str, str] = {
    "profile": "profile",
    "view_keys": "view_keys",
    "create_key": "key_create",
    "buy": "key_create",
    "pay": "pay_start",
    "balance": "pay_start",
}


_CALLBACK_PREFIX: list[tuple[str, str]] = [
    ("view_key|", "key_view"),
    ("view_keys|", "view_keys"),
    ("connect_device|", "connect"),
    ("connect_router|", "connect"),
    ("connect_tv|", "connect"),
    ("connect_pc|", "connect"),
    ("connect_ios|", "connect"),
    ("connect_android|", "connect"),
    ("show_qr|", "connect"),
    ("continue_tv|", "connect"),
    ("pay_currency", "pay_start"),
    ("balance_history", "pay_start"),
    ("cfg_user_confirm|", "key_create"),
    ("choose_payment_provider|", "pay_start"),
    ("cfg_renew", "renew"),
    ("key_addons", "addons"),
    ("extend_key", "coupons"),
]


_CALLBACK_CONTAINS: list[tuple[str, str]] = [
    ("connect_", "connect"),
    ("renew", "renew"),
    ("addon", "addons"),
    ("referral", "referral"),
    ("invite", "referral"),
    ("coupon", "coupons"),
    ("users_audit", "admin"),
    ("users_editor", "admin"),
    ("search_user", "admin"),
    ("admin_panel", "admin"),
]

_MESSAGE_START: set[str] = {"/start", "start"}

_HANDLER_CONTAINS: list[tuple[str, str] | tuple[str, str, str]] = [
    ("process_start", "start"),
    ("start_entry", "start"),
    ("show_start_menu", "start"),
    ("process_callback_view_profile", "profile"),
    ("process_callback_or_message_view_keys", "view_keys"),
    ("key_view", "key_view", "key_create"),
    ("key_create", "key_create"),
    ("handle_key_creation", "key_create"),
    ("confirm_create", "key_create"),
    ("complete_key_renewal", "renew"),
    ("handle_connect_device", "connect"),
    ("process_connect_", "connect"),
    ("process_callback_connect", "connect"),
    ("process_continue_tv", "connect"),
    ("show_qr_code", "connect"),
    ("pay", "pay_start"),
    ("balance", "pay_start"),
    ("renew", "renew"),
    ("addon", "addons"),
    ("referral", "referral"),
    ("refferal", "referral"),
    ("coupon", "coupons"),
    ("admin_panel", "admin"),
    ("users_audit", "admin"),
    ("users_editor", "admin"),
    ("search_user", "admin"),
    ("auth/register", "register"),
    ("auth/login", "login"),
    ("auth/send-login", "login"),
    ("auth/login-by-code", "login"),
    ("auth/login-telegram", "login"),
]

def _funnel_step_counts(path: str, result: str, step: str) -> bool:
    """Решает, считать ли событие достижением шага воронки.
    pay_start: любое успешное «начало оплаты» (меню, валюта, создание ссылки).
    pay: вебхук с «webhook» в path, кроме пути вебхука бота из конфига (WEBHOOK_PATH).
    key_create: только факт создания ключа (cfg_user_confirm или /keys/create).
    connect: любое успешное подключение."""
    if result != "success":
        return False
    p = (path or "").lower()
    if step == "pay_start":
        return True
    if step == "pay":
        if "webhook" not in p:
            return False
        if _is_bot_webhook_path(path or ""):
            return False
        return True
    if step == "key_create":
        return "cfg_user_confirm" in p or "/keys/create" in p
    return True


def _normalize_path_to_step(path: str) -> str:
    """Сводит path_or_handler к шагу по правилам из маппингов.
    Вебхук с «webhook» в path → pay, кроме пути вебхука бота из конфига (WEBHOOK_PATH)."""
    if not path:
        return "other"
    p = path.lower().strip()
    if "webhook" in p:
        if _is_bot_webhook_path(path):
            pass 
        else:
            return "pay"

    if p.startswith("callback:"):
        callback_data = p.split(":", 1)[-1] 
        data = callback_data.split("|")[0]  
        step = _CALLBACK_EXACT.get(data)
        if step:
            return step
        for prefix, step in _CALLBACK_PREFIX:
            if callback_data.startswith(prefix):
                return step
        for item in _CALLBACK_CONTAINS:
            substr, step = item[0], item[1]
            if substr in callback_data:
                return step
        return "other"

    if p.startswith("message:"):
        text = (p.split(":", 1)[-1] or "").strip()
        return "start" if any(s in text for s in _MESSAGE_START) else "other"

    if p.startswith("post ") or p.startswith("get "):
        if "auth/register" in p:
            return "register"
        if "auth/" in p and "login" in p:
            return "login"
        if "payment-links" in p or ("payment" in p and "webhook" not in p):
            return "pay_start"
        return "api_other"

    for item in _HANDLER_CONTAINS:
        if len(item) == 3:
            substr, step, exclude = item[0], item[1], item[2]
            if substr in p and exclude not in p:
                return step
        else:
            substr, step = item[0], item[1]
            if substr in p:
                return step
    return "other"


async def get_audit_stats(
    session: AsyncSession,
    *,
    date_from: datetime,
    date_to: datetime,
    max_events: int = 100_000,
) -> dict[str, Any]:
    """Агрегаты по аудиту за период (только БД): по шагам объём, успехи/ошибки, уникальные пользователи.
    Удобно для «какие пути хорошо отрабатывают, какие нет». Данные из Redis в расчёт не берутся."""
    await ensure_audit_table(session)
    d_from = _naive_utc(date_from)
    d_to = _naive_utc(date_to)
    stmt = (
        select(
            AuditEvent.path_or_handler,
            AuditEvent.result,
            AuditEvent.actor_tg_id,
            AuditEvent.actor_identity_id,
        )
        .where(
            AuditEvent.created_at >= d_from,
            AuditEvent.created_at < d_to,
        )
        .limit(max_events)
    )
    result = await session.execute(stmt)
    rows = result.all()
    by_step, by_path_list, all_actors = _aggregate_audit_rows(rows)
    return {
        "summary": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "total_events": sum(d["total"] for d in by_step.values()),
            "unique_users": len(all_actors),
        },
        "by_path": by_path_list,
    }


async def get_audit_stats_from_redis(max_events: int = 5000) -> dict[str, Any] | None:
    """Агрегаты по аудиту из буфера Redis (без БД). Возвращает None, если буфер выключен."""
    if not AUDIT_REDIS_BUFFER_ENABLED:
        return None
    events = await list_audit_events_from_redis_buffer(max_events=max_events)
    rows = [(e.path_or_handler, e.result, e.actor_tg_id, e.actor_identity_id) for e in events]
    by_step, by_path_list, all_actors = _aggregate_audit_rows(rows)
    return {
        "summary": {
            "source": "redis",
            "total_events": len(events),
            "unique_users": len(all_actors),
        },
        "by_path": by_path_list,
    }


async def get_audit_funnel(
    session: AsyncSession,
    *,
    date_from: datetime,
    date_to: datetime,
    steps_ordered: tuple[str, ...] | None = None,
    max_events: int = 50_000,
) -> list[dict[str, Any]]:
    """Воронка: сколько уникальных пользователей достигли каждого шага.
    Шаг pay = только вебхук кассы (пополнение). Шаг key_create = только факт создания ключа. Остальное — по result success."""
    await ensure_audit_table(session)
    steps = steps_ordered or DEFAULT_FUNNEL_STEPS
    d_from = _naive_utc(date_from)
    d_to = _naive_utc(date_to)
    stmt = (
        select(
            AuditEvent.path_or_handler,
            AuditEvent.result,
            AuditEvent.actor_tg_id,
            AuditEvent.actor_identity_id,
        )
        .where(
            AuditEvent.created_at >= d_from,
            AuditEvent.created_at < d_to,
        )
        .limit(max_events)
    )
    result = await session.execute(stmt)
    rows = result.all()
    return _funnel_from_rows(rows, steps)


def _funnel_from_rows(
    rows: list[tuple[Any, ...]],
    steps_ordered: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Воронка по строкам: (path, result, actor_tg_id, actor_identity_id). Учитываются только завершающие события."""
    steps = steps_ordered or DEFAULT_FUNNEL_STEPS
    actor_steps: dict[tuple[str, int], set[str]] = {}
    for row in rows:
        if len(row) >= 4:
            path, result, tg_id, identity_id = row[0], row[1], row[2], row[3]
        else:
            path, tg_id, identity_id = row[0], row[1], row[2]
            result = "success"
        step = _normalize_path_to_step(path or "")
        if not _funnel_step_counts(path or "", str(result or ""), step):
            continue
        key = (str(identity_id or ""), int(tg_id or 0))
        if key == ("", 0):
            continue
        if key not in actor_steps:
            actor_steps[key] = set()
        actor_steps[key].add(step)

    step_index = {s: i for i, s in enumerate(steps)}
    reached: list[int] = [0] * len(steps)
    for _actor, reached_steps in actor_steps.items():
        max_idx = -1
        for st in reached_steps:
            if st in step_index and step_index[st] > max_idx:
                max_idx = step_index[st]
        for i in range(max_idx + 1):
            reached[i] += 1

    funnel_list = []
    prev_count = None
    for i, step in enumerate(steps):
        count = reached[i]
        conversion = round(100.0 * count / prev_count, 1) if prev_count and prev_count > 0 else 100.0
        funnel_list.append({
            "step": step,
            "label": AUDIT_STEP_LABELS.get(step, step),
            "count": count,
            "conversion_from_prev_pct": conversion if prev_count else None,
        })
        prev_count = count
    return funnel_list


async def get_audit_funnel_from_redis(
    max_events: int = 5000,
    steps_ordered: tuple[str, ...] | None = None,
) -> list[dict[str, Any]] | None:
    """Воронка по событиям из буфера Redis. None, если буфер выключен."""
    if not AUDIT_REDIS_BUFFER_ENABLED:
        return None
    events = await list_audit_events_from_redis_buffer(max_events=max_events)
    rows = [(e.path_or_handler, e.result, e.actor_tg_id, e.actor_identity_id) for e in events]
    return _funnel_from_rows(rows, steps_ordered)


async def drain_audit_redis_to_db(session_factory: Any) -> int:
    """Выгружает буфер аудита из Redis в БД батчами. Вызывать по крону (например в 00:00).
    Возвращает количество записанных событий."""
    from core.redis_cache import cache_lpop_batch

    total = 0
    while True:
        batch = await cache_lpop_batch(AUDIT_REDIS_FLUSH_KEY, AUDIT_REDIS_DRAIN_BATCH)
        if not batch:
            break
        try:
            async with session_factory() as session:
                await ensure_audit_table(session)
                for rec in batch:
                    created = rec.get("created_at")
                    if isinstance(created, str):
                        try:
                            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        except Exception:
                            created = datetime.now(timezone.utc)
                    elif created is None:
                        created = datetime.now(timezone.utc)
                    event = AuditEvent(
                        event_type=rec.get("event_type", "telegram_access"),
                        channel=rec.get("channel", "telegram"),
                        path_or_handler=rec.get("path_or_handler") or "telegram",
                        actor_identity_id=rec.get("actor_identity_id"),
                        actor_tg_id=rec.get("actor_tg_id"),
                        entity_type=rec.get("entity_type"),
                        entity_id=rec.get("entity_id"),
                        result=rec.get("result", "success"),
                        reason=rec.get("reason"),
                        metadata_=rec.get("metadata_"),
                        request_id=rec.get("request_id"),
                        created_at=created,
                    )
                    session.add(event)
                await session.commit()
                total += len(batch)
        except Exception as exc:
            logger.warning("[Audit] drain_audit_redis_to_db батч не записан: %s", exc)
            break
    return total
