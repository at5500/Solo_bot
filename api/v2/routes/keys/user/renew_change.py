"""Mini App-only renew endpoint that supports switching the tariff.

The stock ``renew.py`` endpoint takes ``tariff_id`` from the existing DB
key and ignores anything the caller might pass — the bot, by contrast,
allows users to renew onto a different tariff (any duration from the same
``basic`` group). This file adds a parallel endpoint that mirrors the bot
behaviour without touching ``renew.py``, so future upstream merges of
Solo_bot don't conflict with project-local changes.

* ``POST /api/keys/{client_id}/renew/{tariff_id}`` — renew an existing
  key onto an explicitly chosen tariff. Equivalent to picking «продление»
  in the bot and selecting a different duration. The chosen tariff
  replaces ``Key.tariff_id`` via the same ``execute_renewal`` service the
  bot already uses, so balance/coupons/cluster sync match exactly.

The route is registered through a one-line addition to ``__init__.py``
and reuses :data:`api.v2.routes.keys._common.user_router`.
"""

from datetime import datetime

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, validate_redirect_url, verify_identity_token
from api.v2.schemas.web_public import AccountKeyRenewRequest, AccountKeyRenewResponse
from database import get_tariff_by_id
from database.models import Key
from database.temporary_data import create_temporary_data
from services.payments.payment_links import PaymentLinkRequest, create_payment_link

from .._common import (
    _key_actions_config,
    _normalize_expiry_ms,
    _resolve_billing_user_id,
    _resolve_default_web_payment_provider,
    _resolve_public_base_url,
    user_router,
)


@user_router.post(
    "/{client_id}/renew/{new_tariff_id}",
    response_model=AccountKeyRenewResponse,
)
async def user_key_renew_change_tariff(
    client_id: str,
    new_tariff_id: int,
    body: AccountKeyRenewRequest,
    request: Request,
    preview: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Renews an existing key onto an explicitly chosen tariff.

    Behaviour mirrors the bot's renew-with-tariff flow: the caller picks
    any active tariff (typically a different duration from the same
    subscription group), and the service layer rewrites ``Key.tariff_id``
    while extending expiry, applying coupons, and syncing the cluster.

    Args:
        client_id: UUID of the key being renewed.
        new_tariff_id: ID of the tariff to switch the key onto. Path-only
            to keep the request body identical to the stock renew endpoint.
        body: Standard renew payload — provider, redirect URLs, coupon.
        request: Injected FastAPI request (for IP / base-URL resolution).
        preview: If true, only the pricing calculation is performed —
            no payment link or DB mutation.
        session: SQLAlchemy session.
        identity: Authenticated caller (cookie-based).

    Returns:
        ``AccountKeyRenewResponse`` — either an immediate ok with balance
        update or a payment-required redirect (same shape as ``renew.py``).
    """
    from api.ratelimit import enforce_rate_limit
    from services.errors import ServiceError
    from services.keys import calculate_renewal_pricing, execute_renewal

    if not preview:
        await enforce_rate_limit(
            request,
            session,
            bucket="key_renew",
            max_per_window=10,
            window_sec=60,
        )

    actions = _key_actions_config()
    if not actions.renew_enabled:
        raise HTTPException(status_code=403, detail="Продление подписки отключено в настройках")

    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = (
        await session.execute(
            select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1)
        )
    ).scalar_one_or_none()
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    if bool(getattr(db_key, "is_frozen", False)):
        raise HTTPException(status_code=400, detail="Продление для замороженной подписки недоступно")

    current_tariff_id = getattr(db_key, "tariff_id", None)
    if not current_tariff_id:
        raise HTTPException(status_code=400, detail="Для подписки не назначен тариф")

    # Verify the requested tariff exists and is active. The cross-group
    # guard we used to have here blocked the legitimate trial → paid
    # upgrade — trials live in their own ``group_code`` and a user
    # buying their first paid subscription always switches groups.
    # The bot's renew flow doesn't enforce same-group either; access
    # control is already done by ``/tariffs/public`` (it only exposes
    # tariffs the user is allowed to pick), so the extra check here
    # was redundant and harmful.
    current_tariff = await get_tariff_by_id(session, int(current_tariff_id))
    new_tariff = await get_tariff_by_id(session, int(new_tariff_id))
    if not current_tariff or not new_tariff:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    if not new_tariff.get("is_active", True):
        raise HTTPException(status_code=400, detail="Выбранный тариф недоступен")

    key_email = str(getattr(db_key, "email", "") or "")
    key_server_id = str(getattr(db_key, "server_id", "") or "")

    try:
        pricing = await calculate_renewal_pricing(
            session=session,
            billing_user_id=int(billing_user_id),
            key_email=key_email,
            tariff_id=int(new_tariff_id),
            coupon_code=body.coupon_code,
        )
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=e.message) from None

    if preview:
        return AccountKeyRenewResponse(
            ok=True,
            message="Расчет обновлен",
            client_id=str(client_id),
            tariff_id=int(new_tariff_id),
            charged_rub=0,
            balance_rub=pricing.balance,
            base_price_rub=pricing.base_price_rub,
            discount_rub=pricing.discount_rub,
            final_price_rub=pricing.final_price_rub,
            applied_coupon_code=pricing.applied_coupon_code,
            payment_required=pricing.payment_required,
            required_amount_rub=pricing.required_amount,
            payment_id=None,
            payment_url=None,
        )

    if pricing.payment_required:
        provider_id = str(body.provider_id or _resolve_default_web_payment_provider() or "").strip().upper()
        if not provider_id:
            raise HTTPException(status_code=503, detail="Нет доступных провайдеров оплаты")
        base_url = _resolve_public_base_url(request)
        success_url = validate_redirect_url(str(body.success_url or ""), f"{base_url}/payment-success")
        failure_url = validate_redirect_url(str(body.failure_url or ""), f"{base_url}/payment-failure")
        payment_request = PaymentLinkRequest(
            legacy_user_ref=int(billing_user_id),
            amount=pricing.required_amount,
            currency="RUB",
            provider_id=provider_id,
            success_url=success_url,
            failure_url=failure_url,
            metadata={
                "payment_flow": "key_renewal",
                "tariff_id": int(new_tariff_id),
                "client_id": str(client_id),
                "email": key_email,
                "cost": pricing.final_price_rub,
                "selected_duration_days": pricing.duration_days,
                "selected_device_limit": pricing.selected_device_limit,
                "selected_traffic_limit": pricing.selected_traffic_limit,
                "selected_price_rub": pricing.final_price_rub,
                "total_gb": pricing.total_gb,
                "base_price_rub": pricing.base_price_rub,
                "discount_rub": pricing.discount_rub,
                "applied_coupon_code": pricing.applied_coupon_code,
                "coupon_id": pricing.coupon_id,
            },
        )
        payment_result = await create_payment_link(session, payment_request)
        if not payment_result.success or not payment_result.payment_url or not payment_result.payment_id:
            raise HTTPException(
                status_code=400,
                detail=payment_result.error or "Не удалось создать ссылку оплаты",
            )
        await create_temporary_data(
            session,
            int(billing_user_id),
            "waiting_for_renewal_payment",
            {
                "tariff_id": int(new_tariff_id),
                "client_id": str(client_id),
                "email": key_email,
                "cost": pricing.final_price_rub,
                "required_amount": pricing.required_amount,
                "selected_duration_days": pricing.duration_days,
                "selected_device_limit": pricing.selected_device_limit,
                "selected_traffic_limit": pricing.selected_traffic_limit,
                "selected_price_rub": pricing.final_price_rub,
                "total_gb": pricing.total_gb,
                "base_price_rub": pricing.base_price_rub,
                "discount_rub": pricing.discount_rub,
                "applied_coupon_code": pricing.applied_coupon_code,
                "coupon_id": pricing.coupon_id,
            },
        )
        return AccountKeyRenewResponse(
            ok=True,
            message="Требуется оплата для продления подписки",
            client_id=str(client_id),
            tariff_id=int(new_tariff_id),
            charged_rub=0,
            balance_rub=pricing.balance,
            base_price_rub=pricing.base_price_rub,
            discount_rub=pricing.discount_rub,
            final_price_rub=pricing.final_price_rub,
            applied_coupon_code=pricing.applied_coupon_code,
            payment_required=True,
            required_amount_rub=pricing.required_amount,
            payment_id=payment_result.payment_id,
            payment_url=payment_result.payment_url,
        )

    expiry_raw = _normalize_expiry_ms(getattr(db_key, "expiry_time", None))
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    base_expiry = now_ms if expiry_raw <= now_ms else expiry_raw
    new_expiry_time = int(base_expiry + pricing.duration_days * 24 * 60 * 60 * 1000)
    if not key_email or not key_server_id:
        raise HTTPException(status_code=400, detail="Некорректные данные подписки")

    try:
        result = await execute_renewal(
            session=session,
            billing_user_id=int(billing_user_id),
            client_id=str(client_id),
            key_email=key_email,
            key_server_id=key_server_id,
            tariff_id=int(new_tariff_id),
            new_expiry_time=new_expiry_time,
            total_gb=pricing.total_gb,
            cost=float(pricing.final_price_rub),
            selected_device_limit=pricing.selected_device_limit,
            selected_traffic_limit=pricing.selected_traffic_limit,
            selected_price_rub=pricing.final_price_rub,
            coupon_id=pricing.coupon_id,
        )
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=e.message) from None

    return AccountKeyRenewResponse(
        ok=True,
        message="Подписка продлена",
        client_id=result.client_id,
        tariff_id=result.tariff_id,
        charged_rub=result.charged_rub,
        balance_rub=result.balance_rub,
        base_price_rub=pricing.base_price_rub,
        discount_rub=pricing.discount_rub,
        final_price_rub=pricing.final_price_rub,
        applied_coupon_code=pricing.applied_coupon_code,
    )
