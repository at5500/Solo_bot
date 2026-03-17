import hashlib

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Header, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from audit import set_api_actor
from database import async_session_maker, identities as idb
from database.models import Admin


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def verify_admin_token(
    admin_id: int = Query(..., alias="tg_id"),
    token: str = Header(..., alias="X-Token"),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
) -> Admin:
    hashed = hash_token(token)
    result = await session.execute(select(Admin).where(Admin.tg_id == admin_id, Admin.token == hashed))
    admin = result.scalar_one_or_none()
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_api_actor(request, tg_id=admin.tg_id)
    return admin


async def verify_identity_token(
    x_identity_id: str = Header(..., alias="X-Identity-Id"),
    token: str = Header(..., alias="X-Token"),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
):
    """Проверяет пару identity_id + token; возвращает Identity. Для использования в API v2."""
    identity = await idb.verify_identity_token(session, x_identity_id, token)
    if not identity:
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return identity


async def verify_identity_admin(
    x_identity_id: str = Header(..., alias="X-Identity-Id"),
    token: str = Header(..., alias="X-Token"),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
):
    """Проверяет identity + token и что identity.is_admin; для админских ручек v2."""
    identity = await idb.verify_identity_token(session, x_identity_id, token)
    if not identity:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return identity


async def verify_identity_admin_short(
    x_identity_id: str = Header(..., alias="X-Identity-Id"),
    token: str = Header(..., alias="X-Token"),
    request: Request = None,
):
    """Проверка админа с короткой сессией (для broadcast и др.), чтобы не держать соединение с БД."""
    async with async_session_maker() as session:
        identity = await idb.verify_identity_token(session, x_identity_id, token)
        await session.commit()
    if not identity:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return identity


async def verify_admin_token_short(
    admin_id: int = Query(..., alias="tg_id"),
    token: str = Header(..., alias="X-Token"),
    request: Request = None,
) -> Admin:
    """Проверка админа с короткой сессией (для broadcast и др.), чтобы не держать соединение с БД."""
    hashed = hash_token(token)
    async with async_session_maker() as session:
        result = await session.execute(select(Admin).where(Admin.tg_id == admin_id, Admin.token == hashed))
        admin = result.scalar_one_or_none()
        await session.commit()
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    set_api_actor(request, tg_id=admin.tg_id)
    return admin
