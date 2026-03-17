from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session
from api.v2.schemas import TariffBase, TariffResponse, TariffUpdate
from api.v2.schemas.tariffs import TariffGroup, TariffPublic
from api.v2.base_crud import generate_crud_router
from database.models import Tariff


def _tariff_to_public(t: Tariff) -> TariffPublic:
    return TariffPublic(
        id=t.id,
        name=t.name or "",
        group_code=t.group_code or "",
        duration_days=t.duration_days or 0,
        price_rub=t.price_rub or 0,
        traffic_limit=t.traffic_limit,
        device_limit=t.device_limit,
        subgroup_title=t.subgroup_title,
        sort_order=t.sort_order,
        vless=bool(getattr(t, "vless", False)),
    )


public_router = APIRouter()


@public_router.get("/groups", response_model=list[TariffGroup])
async def get_tariff_groups(session: AsyncSession = Depends(get_session)):
    """Публичный список групп тарифов — уникальные значения колонки group_code."""
    q = (
        select(Tariff.group_code)
        .where(Tariff.is_active == True, Tariff.group_code.isnot(None), Tariff.group_code != "")
        .distinct()
        .order_by(Tariff.group_code)
    )
    result = await session.execute(q)
    values = result.scalars().all()
    return [TariffGroup(group_code=v or "") for v in values]


@public_router.get("/public", response_model=list[TariffPublic])
async def get_tariffs_public(
    group_code: str | None = Query(None, description="Фильтр по группе тарифов"),
    tariff_ids: str | None = Query(None, description="ID тарифов через запятую (приоритет над группой)"),
    filter_vless: str | None = Query(
        None,
        description="vless: только для роутера (vless=True), app: только для приложения (vless=False), иначе все",
    ),
    session: AsyncSession = Depends(get_session),
):
    """Публичный список активных тарифов (без авторизации)."""
    q = select(Tariff).where(Tariff.is_active == True).order_by(Tariff.sort_order.asc().nulls_last(), Tariff.price_rub.asc())
    if tariff_ids:
        try:
            ids = [int(x.strip()) for x in tariff_ids.split(",") if x.strip()]
            if ids:
                q = q.where(Tariff.id.in_(ids))
        except ValueError:
            pass
    elif group_code:
        q = q.where(Tariff.group_code == group_code)
    if filter_vless == "router":
        q = q.where(Tariff.vless == True)
    elif filter_vless == "app":
        q = q.where(Tariff.vless == False)
    result = await session.execute(q)
    rows = result.scalars().all()
    return [_tariff_to_public(t) for t in rows]


router = generate_crud_router(
    model=Tariff,
    schema_response=TariffResponse,
    schema_create=TariffBase,
    schema_update=TariffUpdate,
    identifier_field="name",
    parameter_name="name",
    enabled_methods=["get_all", "get_one", "create", "update", "delete"],
)
