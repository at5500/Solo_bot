from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_request_actor, get_session, verify_identity_token
from api.v2.base_crud import generate_crud_router
from api.v2.schemas import CouponBase, CouponResponse, CouponUpdate
from api.v2.schemas.web_public import CouponApplyRequest, CouponApplyResponse
from database import identities as idb
from database.models import Coupon
from services.coupons import apply_fixed_coupon
from services.errors import LimitExceededError, NotFoundError, ServiceError, ValidationError


router = generate_crud_router(
    model=Coupon,
    schema_response=CouponResponse,
    schema_create=CouponBase,
    schema_update=CouponUpdate,
    identifier_field="code",
    parameter_name="code",
    enabled_methods=["get_all", "get_one", "create", "update", "delete"],
)


async def _resolve_coupon_user_id(session: AsyncSession, request: Request, identity) -> tuple[int, int | None]:
    actor = get_request_actor(request)
    billing_user_id = actor.billing_user_id if actor and actor.billing_user_id is not None else None
    if billing_user_id is None:
        billing_user_id = await idb.ensure_billing_user_for_identity(session, identity)
    tg_id = actor.telegram_chat_id if actor else None
    return int(billing_user_id), tg_id


def _service_error_to_http(e: ServiceError) -> HTTPException:
    status_map = {
        "not_found": 404,
        "limit_exceeded": 409,
        "validation_error": 400,
        "forbidden": 403,
    }
    return HTTPException(status_code=status_map.get(e.code, 400), detail=e.message)


@router.post("/apply", response_model=CouponApplyResponse)
async def apply_coupon(
    body: CouponApplyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    from api.ratelimit import enforce_rate_limit

    await enforce_rate_limit(request, session, bucket="coupon_apply", max_per_window=10, window_sec=60)
    user_id, tg_id = await _resolve_coupon_user_id(session, request, identity)
    try:
        result = await apply_fixed_coupon(
            session=session,
            user_id=user_id,
            tg_id=tg_id,
            code=str(body.code or ""),
        )
        return CouponApplyResponse(
            ok=True,
            message="Купон успешно активирован",
            coupon_code=result.coupon_code,
            amount=result.amount,
            balance=result.balance,
        )
    except ServiceError as e:
        await session.rollback()
        raise _service_error_to_http(e)
    except Exception:
        await session.rollback()
        raise HTTPException(status_code=500, detail="Ошибка активации купона")
