from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from api.v2.schemas.payment_links import PaymentLinkCreateRequest, PaymentLinkCreateResponse
from database import identities as idb
from handlers.payments import create_payment_link
from handlers.payments.payment_links import PaymentLinkRequest

router = APIRouter(tags=["PaymentLinks"])


async def _resolve_tg_id(body: PaymentLinkCreateRequest, session: AsyncSession) -> int:
    """Возвращает tg_id из body.tg_id или из identity_id; иначе исключение."""
    if body.tg_id is not None:
        return body.tg_id
    if body.identity_id:
        tg_id = await idb.resolve_tg_id(session, body.identity_id)
        if tg_id is not None:
            return tg_id
        raise HTTPException(
            status_code=400,
            detail="У идентичности не привязан Telegram. Привяжите tg_id для создания платёжной ссылки.",
        )
    raise HTTPException(status_code=400, detail="Укажите tg_id или identity_id")


@router.post("/", response_model=PaymentLinkCreateResponse)
async def create_link(
    body: PaymentLinkCreateRequest,
    http_request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Создаёт платёжную ссылку через выбранную кассу (единая точка входа). Принимает identity_id или tg_id."""
    try:
        tg_id = await _resolve_tg_id(body, session)
    except HTTPException:
        raise
    payment_request = PaymentLinkRequest(
        tg_id=tg_id,
        amount=body.amount,
        currency=body.currency or "RUB",
        provider_id=body.provider_id,
        success_url=body.success_url,
        failure_url=body.failure_url,
        metadata=body.metadata,
    )
    result = await create_payment_link(session, payment_request)
    return PaymentLinkCreateResponse(
        success=result.success,
        payment_id=result.payment_id,
        payment_url=result.payment_url,
        error=result.error,
    )
