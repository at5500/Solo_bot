from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_admin
from api.v2.base_crud import generate_crud_router
from api.v2.schemas import (
    BlockedUserResponse,
    ManualBanResponse,
    NotificationResponse,
    PaymentResponse,
    TemporaryDataResponse,
    TrackingSourceResponse,
)
from database import get_tracking_source_stats
from database.access.resolution import resolve_user_optional
from database.models import (
    BlockedUser,
    ManualBan,
    Notification,
    Payment,
    TemporaryData,
    TrackingSource,
)


router = APIRouter()

router.include_router(
    generate_crud_router(
        model=Payment,
        schema_response=PaymentResponse,
        schema_create=None,
        schema_update=None,
        identifier_field="id",
        enabled_methods=["get_all", "get_one", "delete"],
    ),
    prefix="/payments",
    tags=["Payments"],
    dependencies=[Depends(verify_identity_admin)],
)


@router.get("/payments/by_tg_id/{tg_id}", response_model=list[PaymentResponse], tags=["Payments"])
async def get_payments_by_tg_id(
    tg_id: int = Path(...),
    identity=Depends(verify_identity_admin),
    session: AsyncSession = Depends(get_session),
):
    """Список платежей по tg_id пользователя."""
    u = await resolve_user_optional(session, tg_id)
    if u is None:
        raise HTTPException(status_code=404, detail="Payments not found")
    result = await session.execute(select(Payment).where(Payment.user_id == u.id))
    payments = result.scalars().all()
    if not payments:
        raise HTTPException(status_code=404, detail="Payments not found")
    return payments


router.include_router(
    generate_crud_router(
        model=Notification,
        schema_response=NotificationResponse,
        schema_create=None,
        schema_update=None,
        identifier_field="user_id",
        parameter_name="tg_id",
        telegram_path_to_user_id=True,
        enabled_methods=["get_all", "get_one", "delete"],
    ),
    prefix="/admin/notifications",
    tags=["Notifications"],
    dependencies=[Depends(verify_identity_admin)],
)

router.include_router(
    generate_crud_router(
        model=ManualBan,
        schema_response=ManualBanResponse,
        schema_create=None,
        schema_update=None,
        identifier_field="user_id",
        parameter_name="tg_id",
        telegram_path_to_user_id=True,
        enabled_methods=["get_all", "get_one", "delete"],
    ),
    prefix="/manual-bans",
    tags=["Bans"],
    dependencies=[Depends(verify_identity_admin)],
)

router.include_router(
    generate_crud_router(
        model=BlockedUser,
        schema_response=BlockedUserResponse,
        schema_create=None,
        schema_update=None,
        identifier_field="user_id",
        parameter_name="tg_id",
        telegram_path_to_user_id=True,
        enabled_methods=["get_all", "get_one", "delete"],
    ),
    prefix="/blocked-users",
    tags=["Bans"],
    dependencies=[Depends(verify_identity_admin)],
)

router.include_router(
    generate_crud_router(
        model=TemporaryData,
        schema_response=TemporaryDataResponse,
        schema_create=None,
        schema_update=None,
        identifier_field="user_id",
        parameter_name="tg_id",
        telegram_path_to_user_id=True,
        enabled_methods=["get_all", "get_one", "delete"],
    ),
    prefix="/temporary-data",
    tags=["TemporaryData"],
    dependencies=[Depends(verify_identity_admin)],
)

router.include_router(
    generate_crud_router(
        model=TrackingSource,
        schema_response=TrackingSourceResponse,
        schema_create=None,
        schema_update=None,
        identifier_field="id",
        enabled_methods=["get_all", "delete"],
    ),
    prefix="/tracking-sources",
    tags=["TrackingSources"],
    dependencies=[Depends(verify_identity_admin)],
)


@router.get(
    "/tracking-sources/{code}", response_model=TrackingSourceResponse, dependencies=[Depends(verify_identity_admin)]
)
async def get_tracking_source_with_stats(
    code: str,
    session: AsyncSession = Depends(get_session),
):
    """Источник по коду со статистикой регистраций и платежей."""
    result = await session.execute(select(TrackingSource).where(TrackingSource.code == code))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Tracking source not found")
    stats = await get_tracking_source_stats(session, code)
    return TrackingSourceResponse(
        id=source.id,
        name=source.name,
        code=source.code,
        type=source.type,
        created_by=source.created_by,
        created_at=source.created_at,
        registrations=(stats["registrations"] if stats else 0),
        trials=(stats["trials"] if stats else 0),
        payments=(stats["payments"] if stats else 0),
        total_amount=(float(stats["total_amount"]) if stats else 0.0),
        monthly=(stats["monthly"] if stats and "monthly" in stats else []),
    )
