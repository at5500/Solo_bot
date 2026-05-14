from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import (
    bind_identity_actor,
    clear_auth_cookie,
    get_request_actor,
    get_session,
    hash_token,
    verify_identity_token,
)
from api.depends import AUTH_COOKIE_NAME
from api.v2.routes.auth._common import _resolve_partner_snapshot
from api.v2.schemas.identities import (
    ChangePasswordRequest,
    IdentityResponse,
    IdentitySessionItem,
    IdentitySessionsResponse,
    SetPasswordRequest,
)
from api.v2.schemas.web_public import AccountSummaryResponse
from database import (
    get_balance,
    get_keys,
    get_trial,
    identities as idb,
    identity_sessions as idsess,
)
from database.models import CouponUsage, Gift, GiftUsage
from database.referrals import get_referral_stats
from database.web_notifications import count_unread_for_identity
from utils.referral_codes import encode_referral_code


router = APIRouter()


@router.get("/me", response_model=IdentityResponse)
async def me(
    identity=Depends(verify_identity_token),
):
    """Текущая идентичность по HttpOnly cookie `auth_token`."""
    return IdentityResponse.model_validate(identity)


class DebugCookieResponse(BaseModel):
    has_cookie: bool
    cookie_length: int = 0
    cookie_prefix: str | None = None


@router.get("/debug-cookie", response_model=DebugCookieResponse)
async def debug_cookie(request: Request):
    """Diagnostic endpoint: reports whether the auth_token cookie reached the
    backend. Returns only the length and the first few characters, never the
    full token, so it can be safely called from a Mini App debug screen.

    REMOVE AFTER COOKIE FLOW IS VALIDATED IN PRODUCTION.
    """
    raw = request.cookies.get(AUTH_COOKIE_NAME)
    if not raw or not raw.strip():
        return DebugCookieResponse(has_cookie=False)
    raw = raw.strip()
    return DebugCookieResponse(
        has_cookie=True,
        cookie_length=len(raw),
        cookie_prefix=raw[:6] if raw else None,
    )


def _current_token_hash(request: Request) -> str | None:
    raw = request.cookies.get(AUTH_COOKIE_NAME)
    if not raw or not raw.strip():
        return None
    return hash_token(raw.strip())


@router.get("/sessions", response_model=IdentitySessionsResponse)
async def list_my_sessions(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Возвращает активные сессии текущей identity (все устройства)."""
    current_hash = _current_token_hash(request)
    rows = await idsess.list_sessions_for_identity(session, identity.id)
    items = [
        IdentitySessionItem(
            id=row.id,
            device_label=row.device_label,
            ip=row.ip,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            is_current=bool(current_hash and row.token_hash == current_hash),
        )
        for row in rows
    ]
    return IdentitySessionsResponse(sessions=items)


@router.delete("/sessions/{session_id}")
async def revoke_my_session(
    session_id: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Удаляет одну сессию текущей identity. Если удалена текущая — очищаем cookie."""
    ok = await idsess.delete_session_by_id(
        session, session_id=session_id, identity_id=identity.id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    current_hash = _current_token_hash(request)
    rows = await idsess.list_sessions_for_identity(session, identity.id)
    if current_hash and not any(r.token_hash == current_hash for r in rows):
        clear_auth_cookie(response, request)
    return {"ok": True}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Удаляет все сессии текущей identity кроме текущей."""
    current_hash = _current_token_hash(request)
    if not current_hash:
        raise HTTPException(status_code=400, detail="Текущая сессия не определена")
    removed = await idsess.delete_other_sessions(
        session, identity_id=identity.id, keep_token_hash=current_hash
    )
    return {"ok": True, "removed": removed}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Удаляет текущую сессию из БД и очищает auth cookie. Не требует валидной сессии."""
    raw = request.cookies.get(AUTH_COOKIE_NAME)
    if raw and raw.strip():
        try:
            await idsess.delete_session_by_token_hash(session, hash_token(raw.strip()))
        except Exception:
            pass
    clear_auth_cookie(response, request)
    return {"ok": True}


@router.post("/me/onboarding/complete", response_model=IdentityResponse)
async def onboarding_complete(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Отмечает, что админ прошёл/скипнул онбординг-тур."""
    from datetime import datetime as _dt

    if identity.onboarding_completed_at is None:
        identity.onboarding_completed_at = _dt.utcnow()
    return IdentityResponse.model_validate(identity)


@router.post("/me/onboarding/reset", response_model=IdentityResponse)
async def onboarding_reset(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Сбрасывает флаг онбординга — туториал запустится снова."""
    identity.onboarding_completed_at = None
    identity.onboarding_stage = "landing"
    return IdentityResponse.model_validate(identity)


_ONBOARDING_STAGES = {"landing", "header", "cabinet", "flow", "elements", "done"}


@router.post("/me/onboarding/stage", response_model=IdentityResponse)
async def onboarding_set_stage(
    body: dict,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Переводит админа на указанный этап онбординга."""
    from datetime import datetime as _dt

    stage = str(body.get("stage") or "").strip()
    if stage not in _ONBOARDING_STAGES:
        raise HTTPException(status_code=400, detail="Неизвестный этап онбординга")
    identity.onboarding_stage = stage
    if stage == "done" and identity.onboarding_completed_at is None:
        identity.onboarding_completed_at = _dt.utcnow()
    return IdentityResponse.model_validate(identity)


@router.get("/summary", response_model=AccountSummaryResponse)
async def auth_summary(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    actor = get_request_actor(request)
    billing_user_id = actor.billing_user_id if actor and actor.billing_user_id is not None else None
    if billing_user_id is None:
        billing_user_id = await idb.ensure_billing_user_for_identity(session, identity)
    balance = float(await get_balance(session, billing_user_id))
    trial_status = await get_trial(session, billing_user_id)
    keys = await get_keys(session, billing_user_id)
    keys_total = len(keys) if keys else 0
    gifts_sent_r = await session.execute(
        select(func.count()).select_from(Gift).where(Gift.sender_user_id == billing_user_id)
    )
    gifts_sent = gifts_sent_r.scalar_one() or 0
    gifts_claimed_r = await session.execute(
        select(func.count()).select_from(GiftUsage).where(GiftUsage.user_id == billing_user_id)
    )
    gifts_claimed = gifts_claimed_r.scalar_one() or 0
    coupons_r = await session.execute(
        select(func.count()).select_from(CouponUsage).where(CouponUsage.user_id == billing_user_id)
    )
    coupons_used = coupons_r.scalar_one() or 0
    ref = await get_referral_stats(session, billing_user_id)
    partner = await _resolve_partner_snapshot(session, int(billing_user_id))
    unread_notifications = await count_unread_for_identity(session, identity.id)
    return AccountSummaryResponse(
        identity_id=identity.id,
        email=identity.email,
        tg_id=identity.tg_id,
        linked_telegram=identity.tg_id is not None,
        referral_code=encode_referral_code(int(billing_user_id)),
        balance=balance,
        trial_status=int(trial_status),
        keys_total=keys_total,
        referrals_total=int(ref.get("total_referrals") or 0),
        referrals_active=int(ref.get("active_referrals") or 0),
        referral_bonus_total=float(ref.get("total_referral_bonus") or 0),
        gifts_sent=int(gifts_sent),
        gifts_claimed=int(gifts_claimed),
        coupons_used=int(coupons_used),
        partner_enabled=bool(partner.get("partner_enabled", False)),
        partner_code=str(partner.get("partner_code") or ""),
        partner_balance=float(partner.get("partner_balance") or 0.0),
        partner_percent=float(partner.get("partner_percent") or 0.0),
        partner_percent_custom=bool(partner.get("partner_percent_custom", False)),
        partner_referred_total=int(partner.get("partner_referred_total") or 0),
        partner_payout_method=partner.get("partner_payout_method"),
        partner_payout_destination=partner.get("partner_payout_destination"),
        partner_last_payout=partner.get("partner_last_payout"),
        unread_notifications=int(unread_notifications),
    )


@router.post("/set-password")
async def set_password(
    body: SetPasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="Пароли не совпадают")
    updated = await idb.set_initial_password(session, identity.id, body.password)
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Пароль уже установлен или аккаунт недоступен",
        )
    await bind_identity_actor(request, session, updated)
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="Новые пароли не совпадают")
    err = await idb.change_identity_password(
        session,
        identity.id,
        body.current_password,
        body.password,
    )
    if err == "no_password":
        raise HTTPException(
            status_code=409,
            detail="Пароль ещё не установлен. Сначала задайте пароль в кабинете.",
        )
    if err == "wrong_password":
        raise HTTPException(status_code=401, detail="Неверный текущий пароль")
    refreshed = await idb.get_identity_by_id(session, identity.id)
    if refreshed:
        await bind_identity_actor(request, session, refreshed)
    return {"ok": True}
