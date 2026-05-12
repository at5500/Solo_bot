"""Per-key HWID device endpoints (/api/keys/{client_id}/devices*).

Registers handlers on ``user_router`` so the list and delete-by-hwid endpoints
sit next to the rest of the per-key actions (qr, hwid-reset, renew). Devices
themselves live in Remnawave — this module only owns ownership checks, the
cooldown logic, and translation to JSON for the Mini App.
"""

import asyncio

from .._common import *  # noqa: F401,F403 — pull every shared symbol
from .._common import (
    _resolve_billing_user_id,
    user_router,
)

try:
    from modules.devices.db import check_device_cooldown, update_device_cooldown
    from modules.devices.settings import DELETE_DEVICE_COOLDOWN_MINUTES
    _DEVICES_MODULE_AVAILABLE = True
except Exception:
    check_device_cooldown = None
    update_device_cooldown = None
    DELETE_DEVICE_COOLDOWN_MINUTES = 0
    _DEVICES_MODULE_AVAILABLE = False


async def _load_user_key(session: AsyncSession, billing_user_id: int, client_id: str) -> Key | None:
    return (
        await session.execute(
            select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1)
        )
    ).scalar_one_or_none()


def _device_to_item(device: dict) -> KeyDeviceItem:
    return KeyDeviceItem(
        hwid=str(device.get("hwid") or ""),
        device_model=device.get("deviceModel"),
        platform=device.get("platform"),
        os_version=device.get("osVersion"),
        user_agent=device.get("userAgent"),
        created_at=device.get("createdAt"),
        updated_at=device.get("updatedAt"),
    )


async def _fetch_tariff_device_limit(session: AsyncSession, tariff_id: int | None) -> int | None:
    if tariff_id is None:
        return None
    tariff = (
        await session.execute(select(Tariff).where(Tariff.id == int(tariff_id)).limit(1))
    ).scalar_one_or_none()
    if tariff is None:
        return None
    limit = getattr(tariff, "device_limit", None)
    return int(limit) if limit else None


@user_router.get("/{client_id}/devices", response_model=KeyDevicesResponse)
async def user_key_devices(
    client_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="devices_list", max_per_window=30, window_sec=60)

    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = await _load_user_key(session, billing_user_id, client_id)
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server_id = str(getattr(db_key, "server_id", "") or "")
    if not server_id:
        raise HTTPException(status_code=400, detail="У подписки не указан сервер")

    async def _fetch_devices(api):
        return await api.get_user_hwid_devices(client_id) or []

    devices, device_limit = await asyncio.gather(
        with_remnawave_api(session, server_id, _fetch_devices, fallback_any=True),
        _fetch_tariff_device_limit(session, getattr(db_key, "tariff_id", None)),
    )
    if devices is None:
        raise HTTPException(status_code=502, detail="Не удалось получить устройства из Remnawave")

    return KeyDevicesResponse(
        client_id=client_id,
        items=[_device_to_item(d) for d in devices],
        device_limit=device_limit,
    )


@user_router.delete("/{client_id}/devices/{hwid}", response_model=KeyDeviceDeleteResponse)
async def user_key_device_delete(
    client_id: str,
    hwid: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="devices_delete", max_per_window=10, window_sec=60)

    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = await _load_user_key(session, billing_user_id, client_id)
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    tg_id = int(getattr(db_key, "tg_id", 0) or 0)
    if tg_id <= 0:
        raise HTTPException(status_code=400, detail="Подписка не привязана к Telegram-аккаунту")

    server_id = str(getattr(db_key, "server_id", "") or "")
    if not server_id:
        raise HTTPException(status_code=400, detail="У подписки не указан сервер")

    if _DEVICES_MODULE_AVAILABLE and DELETE_DEVICE_COOLDOWN_MINUTES > 0:
        can_delete, remaining = await check_device_cooldown(session, tg_id, DELETE_DEVICE_COOLDOWN_MINUTES)
        if not can_delete:
            raise HTTPException(
                status_code=409,
                detail=f"Подождите {remaining} мин. перед следующим удалением",
                headers={"Retry-After": str(int(remaining) * 60)},
            )

    async def _delete_device(api):
        return bool(await api.delete_user_hwid_device(client_id, hwid))

    success = await with_remnawave_api(session, server_id, _delete_device, fallback_any=True)
    if not success:
        raise HTTPException(status_code=502, detail="Remnawave отказал в удалении устройства")

    if _DEVICES_MODULE_AVAILABLE:
        try:
            await update_device_cooldown(session, tg_id)
        except Exception:
            pass

    await invalidate_remnawave_profile(session, server_id, client_id, fallback_any=True)

    return KeyDeviceDeleteResponse(
        ok=True,
        message="Устройство удалено",
        client_id=client_id,
        hwid=hwid,
    )
