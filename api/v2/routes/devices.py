"""Device-related endpoints that aren't tied to a single key (/api/devices/*).

Per-key list / delete-by-hwid live alongside the rest of the key actions in
``routes/keys/user/devices.py``. This file owns the cooldown-status probe used
by the Mini App to keep its delete button correctly disabled.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from api.v2.schemas.web_public import DeviceCooldownResponse
from database import identities as idb
from database.models import User


try:
    from modules.devices.db import check_device_cooldown
    from modules.devices.settings import DELETE_DEVICE_COOLDOWN_MINUTES
    _DEVICES_MODULE_AVAILABLE = True
except Exception:
    check_device_cooldown = None
    DELETE_DEVICE_COOLDOWN_MINUTES = 0
    _DEVICES_MODULE_AVAILABLE = False


router = APIRouter()


@router.get("/cooldown", response_model=DeviceCooldownResponse)
async def device_cooldown(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns the caller's device-deletion cooldown state.

    The cooldown is per Telegram user (not per key) — one record per ``tg_id``.

    :returns: ``DeviceCooldownResponse`` with the configured cooldown (in
              minutes), whether deletion is currently allowed, and how many
              minutes are still left.
    """
    _ = request
    billing_user_id = await idb.ensure_billing_user_for_identity(session, identity)
    cooldown_minutes = int(DELETE_DEVICE_COOLDOWN_MINUTES or 0)

    if not _DEVICES_MODULE_AVAILABLE or cooldown_minutes <= 0:
        return DeviceCooldownResponse(
            cooldown_minutes=cooldown_minutes,
            can_delete=True,
            remaining_minutes=0,
        )

    tg_id_value = (
        await session.execute(select(User.tg_id).where(User.id == int(billing_user_id)).limit(1))
    ).scalar_one_or_none()
    if tg_id_value is None:
        raise HTTPException(status_code=400, detail="Аккаунт не привязан к Telegram")

    can_delete, remaining = await check_device_cooldown(session, int(tg_id_value), cooldown_minutes)
    return DeviceCooldownResponse(
        cooldown_minutes=cooldown_minutes,
        can_delete=bool(can_delete),
        remaining_minutes=int(remaining),
    )
