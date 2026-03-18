from datetime import datetime, timedelta

from pytz import timezone
from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache_config import PAYMENT_PENDING_CACHE_TTL_SEC
from core.redis_cache import cache_delete, cache_get, cache_key, cache_set
from database.models import Payment
from logger import logger


MOSCOW_TZ = timezone("Europe/Moscow")


def _payment_cache_key(pid: str) -> str:
    return cache_key("payment_pending", pid)


async def register_pending_payment(
    payment_id: str,
    tg_id: int,
    amount: float,
    payment_system: str,
    *,
    currency: str = "RUB",
    metadata: dict | None = None,
    original_amount: float | None = None,
) -> bool:
    """Регистрирует ожидающий платёж только в Redis. В БД пишем при success/fail из вебхука."""
    data = {
        "tg_id": tg_id,
        "amount": amount,
        "currency": currency,
        "status": "pending",
        "payment_system": payment_system,
        "payment_id": payment_id,
        "metadata": metadata,
        "original_amount": original_amount,
    }
    ok = await cache_set(_payment_cache_key(payment_id), data, PAYMENT_PENDING_CACHE_TTL_SEC)
    if ok:
        logger.debug(f"[Payments] Pending в кэше: payment_id={payment_id}, tg_id={tg_id}")
    return ok


async def invalidate_payment_cache(payment_id: str) -> None:
    """Вызвать после сохранения платежа в БД (success/fail) из вебхука."""
    await cache_delete(_payment_cache_key(payment_id))


async def add_payment(
    session: AsyncSession,
    tg_id: int,
    amount: float,
    payment_system: str,
    *,
    status: str = "success",
    currency: str = "RUB",
    payment_id: str | None = None,
    metadata: dict | None = None,
    original_amount: float | None = None,
) -> int:
    try:
        now_moscow = datetime.now(MOSCOW_TZ).replace(tzinfo=None)
        stmt = (
            insert(Payment)
            .values(
                tg_id=tg_id,
                amount=amount,
                payment_system=payment_system,
                status=status,
                created_at=now_moscow,
                currency=currency,
                payment_id=payment_id,
                metadata_=metadata,
                original_amount=original_amount,
            )
            .returning(Payment.id)
        )
        result = await session.execute(stmt)
        internal_id = result.scalar_one()
        logger.info(
            f"Добавлен платёж id={internal_id}: tg_id={tg_id}, amount={amount}, system={payment_system}, status={status}"
        )
        return internal_id
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Ошибка при добавлении платежа: {e}")
        raise


async def get_last_payments(
    session: AsyncSession,
    tg_id: int,
    limit: int = 3,
    statuses: list[str] | None = None,
):
    query = select(Payment).where(Payment.tg_id == tg_id)

    if statuses:
        query = query.where(Payment.status.in_(statuses))

    query = query.order_by(Payment.created_at.desc()).limit(limit)

    result = await session.execute(query)
    payments = result.scalars().all()
    return [
        {
            "id": p.id,
            "tg_id": p.tg_id,
            "amount": p.amount,
            "currency": p.currency,
            "status": p.status,
            "payment_system": p.payment_system,
            "payment_id": p.payment_id,
            "created_at": p.created_at,
            "metadata": p.metadata_,
            "original_amount": p.original_amount,
        }
        for p in payments
    ]


async def get_payment_by_id(session: AsyncSession, internal_id: int) -> dict | None:
    try:
        result = await session.execute(select(Payment).where(Payment.id == internal_id).limit(1))
        payment = result.scalar_one_or_none()
        if not payment:
            return None
        return {
            "id": payment.id,
            "tg_id": payment.tg_id,
            "amount": payment.amount,
            "currency": payment.currency,
            "status": payment.status,
            "payment_system": payment.payment_system,
            "payment_id": payment.payment_id,
            "created_at": payment.created_at,
            "metadata": payment.metadata_,
            "original_amount": payment.original_amount,
        }
    except SQLAlchemyError as e:
        logger.error(f"Ошибка при поиске платежа id={internal_id}: {e}")
        await session.rollback()
        return None


async def update_payment_status(
    session: AsyncSession,
    internal_id: int,
    new_status: str,
    *,
    payment_id: str | None = None,
    metadata_patch: dict | None = None,
) -> bool:
    try:
        result = await session.execute(select(Payment).where(Payment.id == internal_id).limit(1))
        payment = result.scalar_one_or_none()
        if not payment:
            logger.info(f"Не удалось сменить статус: платёж id={internal_id} не найден")
            return False

        payment.status = new_status
        if payment_id is not None:
            payment.payment_id = payment_id
        base = payment.metadata_ or {}
        if new_status == "success" and "status_changed_at" not in base:
            base["status_changed_at"] = datetime.utcnow().replace(tzinfo=None).isoformat()
        if metadata_patch:
            base.update(metadata_patch)
        if base:
            payment.metadata_ = base

        await session.commit()
        logger.info(f"Статус платежа id={internal_id} изменён на {new_status}")
        return True
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Ошибка при смене статуса платежа id={internal_id}: {e}")
        return False


async def get_payment_by_payment_id(session: AsyncSession, pid: str) -> dict | None:
    """Сначала Redis (pending), затем БД. Из кэша возвращается запись без id — вебхук делает add_payment."""
    cached = await cache_get(_payment_cache_key(pid))
    if cached is not None:
        return {
            "id": None,
            "tg_id": cached["tg_id"],
            "amount": cached["amount"],
            "currency": cached.get("currency", "RUB"),
            "status": cached.get("status", "pending"),
            "payment_system": cached["payment_system"],
            "payment_id": cached["payment_id"],
            "created_at": None,
            "metadata": cached.get("metadata"),
            "original_amount": cached.get("original_amount"),
        }
    try:
        result = await session.execute(select(Payment).where(Payment.payment_id == pid).limit(1))
        payment = result.scalar_one_or_none()
        if not payment:
            return None
        return {
            "id": payment.id,
            "tg_id": payment.tg_id,
            "amount": payment.amount,
            "currency": payment.currency,
            "status": payment.status,
            "payment_system": payment.payment_system,
            "payment_id": payment.payment_id,
            "created_at": payment.created_at,
            "metadata": payment.metadata_,
            "original_amount": payment.original_amount,
        }
    except SQLAlchemyError as e:
        logger.error(f"Ошибка при поиске платежа payment_id={pid}: {e}")
        await session.rollback()
        return None


async def cancel_expired_pending_payments(session: AsyncSession) -> int:
    cutoff = datetime.now(MOSCOW_TZ).replace(tzinfo=None) - timedelta(minutes=60)
    stmt = (
        update(Payment)
        .where(
            and_(
                Payment.status.in_(("pending", "issued", "processing", "awaiting_choice")),
                Payment.created_at < cutoff,
            )
        )
        .values(status="cancelled")
    )
    res = await session.execute(stmt)
    await session.commit()
    affected = res.rowcount or 0
    return affected


async def get_all_payments(
    session: AsyncSession,
    tg_id: int,
    statuses: list[str] | None = None,
) -> list[dict]:
    query = select(Payment).where(Payment.tg_id == tg_id)

    if statuses:
        query = query.where(Payment.status.in_(statuses))

    query = query.order_by(Payment.created_at.desc())

    result = await session.execute(query)
    payments = result.scalars().all()
    return [
        {
            "id": p.id,
            "tg_id": p.tg_id,
            "amount": p.amount,
            "currency": p.currency,
            "status": p.status,
            "payment_system": p.payment_system,
            "payment_id": p.payment_id,
            "created_at": p.created_at,
            "metadata": p.metadata_,
            "original_amount": p.original_amount,
        }
        for p in payments
    ]
