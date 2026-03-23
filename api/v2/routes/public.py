"""Anonymous public endpoints for landing page purchases (/api/public/...)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session
from api.v2.schemas.public import (
    PublicPurchaseRequest,
    PublicPurchaseResponse,
    PublicStatusResponse,
    PublicTariffItem,
)
from database.models import Tariff


router = APIRouter(prefix="/public", tags=["Public (Anonymous)"])

EXCLUDED_GROUPS = {"trial", "gifts", "discounts", "discounts_max"}


@router.get("/tariffs", response_model=list[PublicTariffItem])
async def get_public_tariffs(
    session: AsyncSession = Depends(get_session),
):
    """Returns active tariffs for the landing page. No auth required."""
    result = await session.execute(
        select(Tariff)
        .where(Tariff.is_active.is_(True))
        .order_by(Tariff.sort_order.asc().nullslast(), Tariff.id)
    )
    tariffs = result.scalars().all()
    return [
        PublicTariffItem(
            id=t.id,
            name=t.name,
            duration_days=t.duration_days,
            price_rub=t.price_rub,
            traffic_limit=t.traffic_limit,
            device_limit=t.device_limit,
        )
        for t in tariffs
        if (t.group_code or "").lower() not in EXCLUDED_GROUPS
    ]


@router.post("/purchase", response_model=PublicPurchaseResponse)
async def public_purchase(
    body: PublicPurchaseRequest,
    session: AsyncSession = Depends(get_session),
):
    """Create a payment link for an anonymous buyer.

    Finds or creates an identity by email, looks up the tariff price,
    and returns a payment URL for redirect.
    """
    from database import identities as idb
    from database.tariffs import get_tariff_by_id
    from handlers.payments.payment_links import PaymentLinkRequest, create_payment_link

    email = body.email.strip().lower()
    if not email or "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    tariff = await get_tariff_by_id(session, body.tariff_id)
    if not tariff or not tariff.get("is_active"):
        raise HTTPException(status_code=400, detail="Tariff not found or inactive")

    gc = (tariff.get("group_code") or "").lower()
    if gc in EXCLUDED_GROUPS:
        raise HTTPException(status_code=400, detail="This tariff is not available")

    identity = await idb.get_identity_by_email(session, email)
    if not identity:
        identity, _ = await idb.create_identity_with_token(session, email=email)

    if not identity.tg_id:
        import hashlib as _hl
        from database.users import add_user

        digest = int(_hl.sha256(email.encode()).hexdigest()[:15], 16)
        synthetic_tg_id = -(digest % (10**12) + 1)
        await add_user(session, tg_id=synthetic_tg_id, commit=False)
        identity = await idb.attach_telegram(session, identity.id, synthetic_tg_id)
        await session.commit()

    cost = float(tariff["price_rub"])

    link_request = PaymentLinkRequest(
        tg_id=identity.tg_id,
        amount=cost,
        currency="RUB",
        provider_id=body.provider_id,
        success_url=body.success_url,
        failure_url=body.failure_url,
        metadata={
            "public_purchase": True,
            "tariff_id": body.tariff_id,
            "email": email,
        },
    )
    result = await create_payment_link(session, link_request)
    if not result.success:
        return PublicPurchaseResponse(error=result.error)

    return PublicPurchaseResponse(
        payment_url=result.payment_url,
        payment_id=result.payment_id,
    )


@router.get("/status/{payment_id}", response_model=PublicStatusResponse)
async def get_payment_status(
    payment_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Poll payment status after redirect from payment gateway."""
    from database.keys import get_keys
    from database.payments import get_payment_by_payment_id

    payment = await get_payment_by_payment_id(session, payment_id)
    if not payment:
        return PublicStatusResponse(status="pending")

    status = payment.get("status", "pending")
    if status != "success":
        return PublicStatusResponse(status=status)

    tg_id = payment.get("tg_id")
    metadata = payment.get("metadata") or {}
    buyer_email = metadata.get("email")

    link = None
    if tg_id:
        keys = await get_keys(session, tg_id)
        if keys:
            last_key = keys[-1]
            link = getattr(last_key, "key", None) or getattr(last_key, "remnawave_link", None)

    if not link:
        return PublicStatusResponse(status="paid", email=buyer_email)

    return PublicStatusResponse(status="ready", link=link, email=buyer_email)
