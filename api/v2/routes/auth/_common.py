from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.v2.schemas.identities import IdentityResponse, LoginResponse
from config import API_TOKEN_TTL_DAYS
from logger import logger
from utils.referral_codes import encode_partner_code


TOKEN_TTL_HINT = "бессрочно" if API_TOKEN_TTL_DAYS is None else f"{API_TOKEN_TTL_DAYS} дн."
TELEGRAM_LOGIN_MAX_AGE = 86400


def build_login_response(identity) -> LoginResponse:
    return LoginResponse(
        identity_id=identity.id,
        identity=IdentityResponse.model_validate(identity),
    )

_TRUSTED_PROXY_CIDRS: list[str] = []


def _client_ip(request: Request) -> str:
    client_host = (request.client.host if request.client else "") or ""
    forwarded = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if not forwarded:
        return client_host
    if not _TRUSTED_PROXY_CIDRS and client_host not in ("127.0.0.1", "::1"):
        return client_host
    return forwarded.split(",")[0].strip() or client_host


def _mask_payout_destination(method: str | None, destination: str | None) -> str | None:
    """Masks payout requisites for display: cards show the last 4 digits,
    crypto addresses show a head…tail fragment. Returns None when empty."""
    raw = (destination or "").strip()
    if not raw:
        return None
    code = (method or "").strip().lower()
    if code in ("card", "sbp"):
        tail = raw[-4:]
        return f"*{tail}" if len(raw) > 4 else raw
    if len(raw) > 10:
        return f"{raw[:4]}…{raw[-4:]}"
    return raw


async def _resolve_partner_snapshot(session: AsyncSession, billing_user_id: int) -> dict[str, object]:
    partner_feature_enabled = False
    default_percent = 0.0
    try:
        from modules.partner_program import settings as partner_settings

        partner_feature_enabled = True
        raw_percent = getattr(partner_settings, "PARTNER_BONUS_PERCENTAGES", {}).get(1, 0.0)
        default_percent = float(raw_percent) * 100.0
    except Exception:
        partner_feature_enabled = False
        default_percent = 0.0
    from api.v2.routes.partners import partners_table_exists

    partner_table_ok = await partners_table_exists(session)
    if not partner_table_ok:
        partner_feature_enabled = False
    payload: dict[str, object] = {
        "partner_enabled": partner_feature_enabled,
        "partner_code": "",
        "partner_balance": 0.0,
        "partner_percent": default_percent,
        "partner_percent_custom": False,
        "partner_referred_total": 0,
        "partner_referred_paid": 0,
        "partner_payout_method": None,
        "partner_payout_destination": None,
        "partner_last_payout": None,
    }
    try:
        partner_row = (
            await session.execute(
                text(
                    """
                    SELECT
                        tg_id,
                        COALESCE(partner_balance, 0),
                        partner_percent,
                        COALESCE(partner_percent_custom, false),
                        partner_code,
                        payout_method,
                        card_number
                    FROM users
                    WHERE id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": int(billing_user_id)},
            )
        ).first()
    except Exception:
        partner_row = None
    if partner_row is None:
        return payload
    tg_id = int(partner_row[0]) if partner_row[0] is not None else None
    balance = float(partner_row[1] or 0.0)
    percent_raw = partner_row[2]
    percent_custom = bool(partner_row[3])
    percent_value = float(percent_raw) if (percent_custom and percent_raw is not None) else float(default_percent)
    code = str(partner_row[4] or "").strip()
    if (not code or code.isdigit() or code.startswith("r1_")) and int(billing_user_id) > 0:
        generated_code = encode_partner_code(int(billing_user_id))
        code = generated_code
        try:
            await session.execute(
                text("UPDATE users SET partner_code = :code WHERE id = :id"),
                {"code": generated_code, "id": int(billing_user_id)},
            )
            await session.flush()
        except Exception as e:
            logger.warning("[Auth] Ошибка сохранения partner_code для billing_user_id={}: {}", billing_user_id, e)
    payout_method = str(partner_row[5] or "").strip() or None
    payout_destination_raw = str(partner_row[6] or "").strip() or None
    payout_destination = _mask_payout_destination(payout_method, payout_destination_raw)
    referred_total = 0
    referred_paid = 0
    last_payout: dict[str, object] | None = None
    if tg_id is not None:
        try:
            referred_total = int(
                (
                    await session.execute(
                        text("SELECT COUNT(*) FROM partners WHERE partner_tg_id = :tg_id"),
                        {"tg_id": int(tg_id)},
                    )
                ).scalar()
                or 0
            )
        except Exception:
            referred_total = 0
        try:
            payout_row = (
                await session.execute(
                    text(
                        """
                        SELECT id, amount, status, created_at, method, destination
                        FROM payout_requests
                        WHERE tg_id = :tg_id
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {"tg_id": int(tg_id)},
                )
            ).first()
            if payout_row is not None:
                created_at = payout_row[3]
                last_payout = {
                    "id": int(payout_row[0]),
                    "amount_rub": float(payout_row[1] or 0.0),
                    "status": str(payout_row[2] or ""),
                    "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
                    "method": payout_row[4] or payout_method,
                    "destination": _mask_payout_destination(
                        payout_row[4] or payout_method, payout_row[5] or payout_destination_raw
                    ),
                }
        except Exception:
            last_payout = None
        try:
            referred_paid = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(DISTINCT pr.joined_tg_id) "
                            "FROM partners pr "
                            "WHERE pr.partner_tg_id = :tg_id "
                            "AND EXISTS ("
                            "  SELECT 1 FROM payments pay "
                            "  WHERE pay.tg_id = pr.joined_tg_id "
                            "  AND lower(pay.status) = 'success'"
                            ")"
                        ),
                        {"tg_id": int(tg_id)},
                    )
                ).scalar()
                or 0
            )
        except Exception:
            referred_paid = 0
    payload.update({
        "partner_enabled": bool(partner_table_ok and (partner_feature_enabled or code or referred_total > 0 or balance > 0)),
        "partner_code": code,
        "partner_balance": balance,
        "partner_percent": percent_value,
        "partner_percent_custom": percent_custom,
        "partner_referred_total": referred_total,
        "partner_referred_paid": referred_paid,
        "partner_payout_method": payout_method,
        "partner_payout_destination": payout_destination,
        "partner_last_payout": last_payout,
    })
    return payload
