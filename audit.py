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
    await cache_rpush(AUDIT_REDIS_FLUSH_KEY, record)
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
    """Пишет событие telegram_access: в Redis-буфер (если включён) или в БД в отдельной сессии.
    При ошибке логирует и не пробрасывает исключение."""
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
        except Exception as exc:
            logger.warning("[Audit] Запись в Redis-буфер не удалась: %s", exc)
        return
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
