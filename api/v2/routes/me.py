"""Public user-facing endpoints for Telegram Mini App (/api/me/...)."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from api.v2.schemas.me import (
    MeDiscountInfo,
    MeKeyDetails,
    MeKeyShort,
    MePaymentResponse,
    MeProfileResponse,
    MePurchaseRequest,
    MePurchaseResponse,
    MeReferralStats,
    MeRenewRequest,
    MeRenewResponse,
    MeTariffGroup,
    MeTariffItem,
    MeTariffsResponse,
    MeTrialResponse,
)
from config import USERNAME_BOT
from database import keys as kdb, payments as pdb, referrals as rdb
from database.models import Tariff, User
from database.tariffs import get_tariff_by_id
from database.users import get_balance, get_balance_trial_key_count


router = APIRouter(prefix="/me", tags=["Me (Public)"])


async def _resolve_tg_id(identity) -> int:
    """Extract tg_id from identity; raise 403 if Telegram is not linked."""
    if not identity.tg_id:
        raise HTTPException(status_code=403, detail="Telegram not linked to this identity")
    return identity.tg_id


@router.get("/profile", response_model=MeProfileResponse)
async def get_my_profile(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns the authenticated user's profile: balance, trial status, key count."""
    tg_id = await _resolve_tg_id(identity)
    balance, trial, key_count = await get_balance_trial_key_count(session, tg_id)

    result = await session.execute(
        select(
            User.username,
            User.first_name,
            User.preferred_currency,
            User.created_at,
        ).where(User.tg_id == tg_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return MeProfileResponse(
        tg_id=tg_id,
        username=row.username,
        first_name=row.first_name,
        balance=balance,
        trial=trial,
        key_count=key_count,
        preferred_currency=row.preferred_currency or "RUB",
        created_at=row.created_at,
    )


@router.get("/keys", response_model=list[MeKeyShort])
async def get_my_keys(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns a list of all subscription keys owned by the user."""
    tg_id = await _resolve_tg_id(identity)
    keys = await kdb.get_keys(session, tg_id)
    return [
        MeKeyShort(
            email=k.email,
            client_id=k.client_id,
            server_id=k.server_id,
            alias=getattr(k, "alias", None),
            expiry_time=int(k.expiry_time) if k.expiry_time else 0,
            is_frozen=bool(getattr(k, "is_frozen", False)),
            tariff_id=getattr(k, "tariff_id", None),
        )
        for k in keys
    ]


@router.get("/keys/{email}", response_model=MeKeyDetails)
async def get_my_key_details(
    email: str,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns full details for a specific key including the subscription link and live panel data."""
    from panels.remnawave_runtime import get_remnawave_profile

    tg_id = await _resolve_tg_id(identity)
    details = await kdb.get_key_details(session, email)
    if not details:
        raise HTTPException(status_code=404, detail="Key not found")
    if details.get("tg_id") != tg_id:
        raise HTTPException(status_code=403, detail="Key does not belong to this user")

    expiry_ms = details.get("expiry_time") or 0
    expiry_dt = datetime.utcfromtimestamp(expiry_ms / 1000) if expiry_ms else None
    now = datetime.utcnow()

    expired = False
    days_left = None
    hours_left = None
    if expiry_dt:
        delta = expiry_dt - now
        total_seconds = delta.total_seconds()
        if total_seconds <= 0:
            expired = True
        else:
            days_left = delta.days if delta.days > 0 else None
            if delta.days == 0:
                hours_left = int(total_seconds // 3600)

    traffic_used_gb = None
    devices_connected = None
    client_id = details.get("client_id")
    server_id = details.get("server_id")
    if client_id and server_id and not expired:
        try:
            profile = await get_remnawave_profile(session, server_id, client_id, fallback_any=True)
            if profile:
                traffic_used_gb = profile.get("used_gb")
                devices_connected = profile.get("hwid_count")
        except Exception:
            pass

    return MeKeyDetails(
        email=details.get("email"),
        client_id=client_id,
        server_id=server_id,
        alias=details.get("alias"),
        created_at=details.get("created_at"),
        expiry_time=expiry_ms,
        is_frozen=bool(details.get("is_frozen")),
        tariff_id=details.get("tariff_id"),
        link=details.get("link"),
        expiry_date=details.get("expiry_date"),
        days_left=days_left,
        hours_left=hours_left,
        expired=expired,
        selected_device_limit=details.get("selected_device_limit"),
        selected_traffic_limit=details.get("selected_traffic_limit"),
        current_device_limit=details.get("current_device_limit"),
        current_traffic_limit=details.get("current_traffic_limit"),
        traffic_used_gb=traffic_used_gb,
        devices_connected=devices_connected,
    )


@router.post("/trial", response_model=MeTrialResponse)
async def activate_trial(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Activate a free trial subscription.

    Finds the first active trial tariff, creates a key on the least loaded
    cluster.  Returns error if user already used their trial.
    """
    import uuid

    from database.tariffs import get_tariffs
    from database.users import get_trial, update_trial
    from handlers.keys.operations.creation import create_key_on_cluster
    from handlers.utils import generate_random_email, get_least_loaded_cluster

    tg_id = await _resolve_tg_id(identity)

    trial_status = await get_trial(session, tg_id)
    if trial_status not in (0, -1):
        return MeTrialResponse(activated=False, error="Trial already used")

    tariffs = await get_tariffs(session, group_code="trial")
    active_trials = [t for t in tariffs if t.get("is_active")]
    if not active_trials:
        return MeTrialResponse(activated=False, error="No trial tariffs available")

    tariff = active_trials[0]

    try:
        cluster_id = await get_least_loaded_cluster(session)
    except ValueError as e:
        return MeTrialResponse(activated=False, error=str(e))

    email = await generate_random_email(session=session)
    client_id = str(uuid.uuid4())
    duration_days = tariff["duration_days"]
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    expiry_ms = int(now_ms + duration_days * 86400 * 1000)

    await create_key_on_cluster(
        cluster_id=cluster_id,
        tg_id=tg_id,
        client_id=client_id,
        email=email,
        expiry_timestamp=expiry_ms,
        plan=tariff["id"],
        session=session,
        is_trial=True,
    )

    await update_trial(session, tg_id, 1)

    key_details = await kdb.get_key_details(session, email)
    link = key_details.get("link") if key_details else None

    return MeTrialResponse(
        activated=True,
        email=email,
        client_id=client_id,
        link=link,
        expiry_time=expiry_ms,
    )


HIDDEN_GROUPS = {"trial", "gifts"}
DISCOUNT_GROUPS = {"discounts", "discounts_max"}


async def _check_hot_lead_discount_eager(session: AsyncSession, tg_id: int) -> dict:
    """Check hot lead discount, looking ahead if bot hasn't sent a notification yet.

    First uses the standard check (bot already wrote step_2/step_3).
    If nothing found, computes whether the user *would* qualify based on
    step_1 timing — without writing anything to DB, so the bot still
    sends the Telegram notification on its own schedule.
    """
    from datetime import timedelta

    from config import DISCOUNT_ACTIVE_HOURS, HOT_LEAD_INTERVAL_HOURS
    from core.bootstrap import NOTIFICATIONS_CONFIG
    from database.hot_leads import get_hot_leads
    from database.models import Notification
    from database.notifications import check_hot_lead_discount

    discount = await check_hot_lead_discount(session, tg_id)
    if discount.get("available"):
        return discount

    leads = await get_hot_leads(session)
    if tg_id not in leads:
        return {"available": False}

    interval_hours = int(NOTIFICATIONS_CONFIG.get("HOT_LEADS_INTERVAL_HOURS", HOT_LEAD_INTERVAL_HOURS))
    discount_hours = int(NOTIFICATIONS_CONFIG.get("DISCOUNT_ACTIVE_HOURS", DISCOUNT_ACTIVE_HOURS))
    now = datetime.utcnow()

    step1_row = await session.execute(
        select(Notification.last_notification_time)
        .where(Notification.tg_id == tg_id, Notification.notification_type == "hot_lead_step_1")
    )
    step1_time = step1_row.scalar_one_or_none()
    if not step1_time:
        return {"available": False}

    step2_row = await session.execute(
        select(Notification.last_notification_time)
        .where(Notification.tg_id == tg_id, Notification.notification_type == "hot_lead_step_2")
    )
    step2_time = step2_row.scalar_one_or_none()

    if step2_time is None:
        virtual_start = step1_time + timedelta(hours=interval_hours)
        virtual_end = virtual_start + timedelta(hours=discount_hours)
        if virtual_start <= now <= virtual_end:
            return {
                "available": True,
                "type": "hot_lead_step_2",
                "tariff_group": "discounts",
                "expires_at": virtual_end,
            }
        return {"available": False}

    step3_row = await session.execute(
        select(Notification.last_notification_time)
        .where(Notification.tg_id == tg_id, Notification.notification_type == "hot_lead_step_3")
    )
    step3_time = step3_row.scalar_one_or_none()

    if step3_time is None:
        step2_expired = now > step2_time + timedelta(hours=discount_hours)
        virtual_start = step2_time + timedelta(hours=interval_hours)
        virtual_end = virtual_start + timedelta(hours=discount_hours)
        if step2_expired and virtual_start <= now <= virtual_end:
            return {
                "available": True,
                "type": "hot_lead_step_3",
                "tariff_group": "discounts_max",
                "expires_at": virtual_end,
            }

    return {"available": False}


def _tariff_to_item(t) -> MeTariffItem:
    return MeTariffItem(
        id=t.id,
        name=t.name,
        group_code=t.group_code,
        duration_days=t.duration_days,
        price_rub=t.price_rub,
        traffic_limit=t.traffic_limit,
        device_limit=t.device_limit,
        subgroup_title=t.subgroup_title,
        vless=bool(t.vless),
        configurable=bool(t.configurable),
    )


@router.get("/tariffs", response_model=MeTariffsResponse)
async def get_available_tariffs(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns active tariffs grouped by group_code.

    Discount tariffs (``discounts``, ``discounts_max``) are only included
    when the user has an active hot-lead discount.  ``trial`` and ``gifts``
    groups are always excluded.
    """
    from collections import defaultdict

    tg_id = await _resolve_tg_id(identity)

    result = await session.execute(
        select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.sort_order.asc().nullslast(), Tariff.id)
    )
    all_tariffs = result.scalars().all()

    discount_info = await _check_hot_lead_discount_eager(session, tg_id)
    active_discount_group = discount_info.get("tariff_group") if discount_info.get("available") else None

    groups: dict[str, list[MeTariffItem]] = defaultdict(list)
    for t in all_tariffs:
        gc = (t.group_code or "").lower()
        if gc in HIDDEN_GROUPS:
            continue
        if gc in DISCOUNT_GROUPS and gc != active_discount_group:
            continue
        groups[gc].append(_tariff_to_item(t))

    discount = None
    if discount_info.get("available"):
        discount = MeDiscountInfo(
            type=discount_info["type"],
            tariff_group=discount_info["tariff_group"],
            expires_at=discount_info["expires_at"],
        )

    return MeTariffsResponse(
        groups={k: MeTariffGroup(tariffs=v) for k, v in groups.items()},
        discount=discount,
    )


@router.get("/referrals", response_model=MeReferralStats)
async def get_my_referrals(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns referral statistics for the authenticated user."""
    tg_id = await _resolve_tg_id(identity)
    stats = await rdb.get_referral_stats(session, tg_id)

    referral_link = None
    if USERNAME_BOT:
        referral_link = f"https://t.me/{USERNAME_BOT}?start=referral_{tg_id}"

    return MeReferralStats(
        total_referrals=stats.get("total_referrals", 0),
        active_referrals=stats.get("active_referrals", 0),
        total_bonus=stats.get("total_referral_bonus", 0.0),
        referral_link=referral_link,
    )


@router.get("/payments", response_model=list[MePaymentResponse])
async def get_my_payments(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns payment history for the authenticated user."""
    tg_id = await _resolve_tg_id(identity)
    payments = await pdb.get_last_payments(session, tg_id, limit=limit, statuses=["success"])
    return [
        MePaymentResponse(
            id=p.get("id"),
            amount=p.get("amount", 0),
            currency=p.get("currency", "RUB"),
            status=p.get("status", ""),
            payment_system=p.get("payment_system", ""),
            created_at=p.get("created_at"),
        )
        for p in payments
    ]


@router.post("/keys/purchase", response_model=MePurchaseResponse)
async def purchase_key(
    body: MePurchaseRequest,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Purchase a new subscription key.

    Server determines the price from the tariff, picks the least loaded
    cluster, generates internal identifiers (email / client_id) and
    provisions the key on the VPN panel.  If the user's balance is
    insufficient a payment link is returned instead.
    """
    import uuid
    from math import ceil

    from database.users import update_balance
    from handlers.keys.operations.creation import create_key_on_cluster
    from handlers.payments.payment_links import PaymentLinkRequest, create_payment_link
    from handlers.utils import generate_random_email, get_least_loaded_cluster

    tg_id = await _resolve_tg_id(identity)

    tariff = await get_tariff_by_id(session, body.tariff_id)
    if not tariff or not tariff.get("is_active"):
        raise HTTPException(status_code=400, detail="Tariff not found or inactive")

    gc = (tariff.get("group_code") or "").lower()
    if gc in DISCOUNT_GROUPS:
        discount_info = await _check_hot_lead_discount_eager(session, tg_id)
        if not discount_info.get("available") or discount_info.get("tariff_group") != gc:
            raise HTTPException(status_code=403, detail="Discount not available")
    if gc in HIDDEN_GROUPS:
        raise HTTPException(status_code=403, detail="This tariff group is not available for purchase")

    cost = float(tariff["price_rub"])
    balance = await get_balance(session, tg_id)

    if balance < cost:
        missing = ceil(cost - balance)
        if not body.provider_id:
            return MePurchaseResponse(
                created=False,
                payment_required=True,
                missing_amount=float(missing),
                error="Insufficient balance. Provide provider_id to create a payment link.",
            )

        link_request = PaymentLinkRequest(
            tg_id=tg_id,
            amount=missing,
            currency="RUB",
            provider_id=body.provider_id,
            success_url=body.success_url,
            failure_url=body.failure_url,
            metadata={"purchase_tariff_id": body.tariff_id},
        )
        result = await create_payment_link(session, link_request)
        if not result.success:
            return MePurchaseResponse(created=False, error=result.error)

        return MePurchaseResponse(
            created=False,
            payment_required=True,
            payment_url=result.payment_url,
            payment_id=result.payment_id,
            missing_amount=float(missing),
        )

    try:
        cluster_id = await get_least_loaded_cluster(session)
    except ValueError as e:
        return MePurchaseResponse(created=False, error=str(e))

    email = await generate_random_email(session=session)
    client_id = str(uuid.uuid4())
    duration_days = tariff["duration_days"]
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    expiry_ms = int(now_ms + duration_days * 86400 * 1000)

    await create_key_on_cluster(
        cluster_id=cluster_id,
        tg_id=tg_id,
        client_id=client_id,
        email=email,
        expiry_timestamp=expiry_ms,
        plan=body.tariff_id,
        session=session,
    )

    await update_balance(session, tg_id, -cost)

    return MePurchaseResponse(
        created=True,
        email=email,
        client_id=client_id,
    )


@router.post("/keys/{email}/renew", response_model=MeRenewResponse)
async def renew_my_key(
    email: str,
    body: MeRenewRequest,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Renew a subscription key.

    If balance is sufficient the key is renewed immediately and cost is
    deducted.  Otherwise a payment link is created for the missing amount
    (``provider_id`` is required in that case).
    """
    from datetime import timedelta
    from math import ceil

    from handlers.keys.key_renew import complete_key_renewal, resolve_cluster_name
    from handlers.payments.payment_links import PaymentLinkRequest, create_payment_link

    tg_id = await _resolve_tg_id(identity)

    details = await kdb.get_key_details(session, email)
    if not details:
        raise HTTPException(status_code=404, detail="Key not found")
    if details.get("tg_id") != tg_id:
        raise HTTPException(status_code=403, detail="Key does not belong to this user")

    tariff = await get_tariff_by_id(session, body.tariff_id)
    if not tariff or not tariff.get("is_active"):
        raise HTTPException(status_code=400, detail="Tariff not found or inactive")

    gc = (tariff.get("group_code") or "").lower()
    if gc in DISCOUNT_GROUPS:
        disc = await _check_hot_lead_discount_eager(session, tg_id)
        if not disc.get("available") or disc.get("tariff_group") != gc:
            raise HTTPException(status_code=403, detail="Discount not available")
    if gc in HIDDEN_GROUPS:
        raise HTTPException(status_code=403, detail="This tariff group is not available for renewal")

    cost = float(tariff["price_rub"])
    balance = await get_balance(session, tg_id)

    if balance < cost:
        missing = ceil(cost - balance)
        if not body.provider_id:
            return MeRenewResponse(
                renewed=False,
                payment_required=True,
                missing_amount=float(missing),
                error="Insufficient balance. Provide provider_id to create a payment link.",
            )

        link_request = PaymentLinkRequest(
            tg_id=tg_id,
            amount=missing,
            currency="RUB",
            provider_id=body.provider_id,
            success_url=body.success_url,
            failure_url=body.failure_url,
            metadata={"renewal_email": email, "tariff_id": body.tariff_id},
        )
        result = await create_payment_link(session, link_request)
        if not result.success:
            return MeRenewResponse(renewed=False, error=result.error)

        return MeRenewResponse(
            renewed=False,
            payment_required=True,
            payment_url=result.payment_url,
            payment_id=result.payment_id,
            missing_amount=float(missing),
        )

    duration_days = tariff["duration_days"]
    total_gb = tariff.get("traffic_limit") or 0
    client_id = details["client_id"]

    expiry_ms = details.get("expiry_time") or 0
    current_ms = int(datetime.utcnow().timestamp() * 1000)
    base_ms = max(expiry_ms, current_ms)
    new_expiry_time = int(base_ms + timedelta(days=duration_days).total_seconds() * 1000)

    await complete_key_renewal(
        session=session,
        tg_id=tg_id,
        client_id=client_id,
        email=email,
        new_expiry_time=new_expiry_time,
        total_gb=total_gb,
        cost=cost,
        callback_query=None,
        tariff_id=body.tariff_id,
    )

    return MeRenewResponse(renewed=True, new_expiry_time=new_expiry_time)
