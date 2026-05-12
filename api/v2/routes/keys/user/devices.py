"""Per-key HWID device endpoints (/api/keys/{client_id}/devices*).

Registers handlers on ``user_router`` so the list and delete-by-hwid endpoints
sit next to the rest of the per-key actions (qr, hwid-reset, renew). Devices
themselves live in Remnawave — this module only owns ownership checks, the
cooldown logic, and translation to JSON for the Mini App.
"""

from .._common import *  # noqa: F401,F403 — pull every shared symbol
from .._common import (
    _resolve_billing_user_id,
    user_router,
)

from database.servers import get_servers

try:
    from modules.devices.db import check_device_cooldown, update_device_cooldown
    from modules.devices.settings import DELETE_DEVICE_COOLDOWN_MINUTES
    _DEVICES_MODULE_AVAILABLE = True
except Exception:
    check_device_cooldown = None
    update_device_cooldown = None
    DELETE_DEVICE_COOLDOWN_MINUTES = 0
    _DEVICES_MODULE_AVAILABLE = False


async def _resolve_remnawave_server(session: AsyncSession) -> dict | None:
    """Picks the first Remnawave-typed server across all clusters.

    The devices module relies on this same lookup; we duplicate it instead of
    importing from ``modules.devices.router`` to avoid pulling aiogram into
    the API layer.

    :param session: SQLAlchemy session.
    :returns: Server dict (with ``api_url``) or ``None`` if none configured.
    """
    servers = await get_servers(session=session)
    for cluster_servers in servers.values():
        for server in cluster_servers:
            if server.get("panel_type", "") == "remnawave":
                return server
    return None


async def _load_user_key(session: AsyncSession, billing_user_id: int, client_id: str) -> Key | None:
    """Owner-scoped key lookup.

    :param session: SQLAlchemy session.
    :param billing_user_id: User ID resolved from the identity cookie.
    :param client_id: Key UUID from the URL path.
    :returns: The Key row or ``None`` if not found / not owned by this user.
    """
    return (
        await session.execute(
            select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1)
        )
    ).scalar_one_or_none()


def _device_to_item(device: dict) -> KeyDeviceItem:
    """Normalizes a Remnawave HWID payload into our API schema."""
    return KeyDeviceItem(
        hwid=str(device.get("hwid") or ""),
        device_model=device.get("deviceModel"),
        platform=device.get("platform"),
        os_version=device.get("osVersion"),
        user_agent=device.get("userAgent"),
        created_at=device.get("createdAt"),
        updated_at=device.get("updatedAt"),
    )


@user_router.get("/{client_id}/devices", response_model=KeyDevicesResponse)
async def user_key_devices(
    client_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns active HWID devices for a key owned by the caller.

    :param client_id: Key UUID.
    :returns: ``KeyDevicesResponse`` with normalized device list and the
              tariff-side device_limit when available.
    :raises HTTPException: 404 if the key is not found or not owned by the
                           caller, 503 if no Remnawave server is configured.
    """
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="devices_list", max_per_window=30, window_sec=60)

    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = await _load_user_key(session, billing_user_id, client_id)
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    remna_server = await _resolve_remnawave_server(session)
    if remna_server is None:
        raise HTTPException(status_code=503, detail="Сервер Remnawave не настроен")

    api = RemnawaveAPI(remna_server["api_url"])
    if not await api.login(REMNAWAVE_LOGIN, REMNAWAVE_PASSWORD):
        raise HTTPException(status_code=502, detail="Не удалось авторизоваться в Remnawave")

    devices = await api.get_user_hwid_devices(client_id) or []

    device_limit: int | None = None
    tariff_id = getattr(db_key, "tariff_id", None)
    if tariff_id is not None:
        tariff = (
            await session.execute(select(Tariff).where(Tariff.id == int(tariff_id)).limit(1))
        ).scalar_one_or_none()
        if tariff is not None and getattr(tariff, "device_limit", None):
            device_limit = int(tariff.device_limit)

    return KeyDevicesResponse(
        client_id=client_id,
        items=[_device_to_item(d) for d in devices],
        device_limit=device_limit,
        hwid_limit_enabled=None,
    )


@user_router.delete("/{client_id}/devices/{hwid}", response_model=KeyDeviceDeleteResponse)
async def user_key_device_delete(
    client_id: str,
    hwid: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Removes a single HWID device after enforcing ownership and cooldown.

    On success the user-level cooldown timestamp is bumped (one cooldown shared
    across all of the caller's keys, matching the bot's UX).

    :param client_id: Key UUID.
    :param hwid: Device HWID returned by Remnawave.
    :returns: ``KeyDeviceDeleteResponse`` with remaining device count.
    :raises HTTPException: 404 if key not found, 409 if cooldown is still
                           active (``Retry-After`` is set in seconds), 503 if
                           Remnawave is unreachable.
    """
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="devices_delete", max_per_window=10, window_sec=60)

    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = await _load_user_key(session, billing_user_id, client_id)
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    tg_id = int(getattr(db_key, "tg_id", 0) or 0)
    if tg_id <= 0:
        raise HTTPException(status_code=400, detail="Подписка не привязана к Telegram-аккаунту")

    if _DEVICES_MODULE_AVAILABLE and DELETE_DEVICE_COOLDOWN_MINUTES > 0:
        can_delete, remaining = await check_device_cooldown(session, tg_id, DELETE_DEVICE_COOLDOWN_MINUTES)
        if not can_delete:
            raise HTTPException(
                status_code=409,
                detail=f"Подождите {remaining} мин. перед следующим удалением",
                headers={"Retry-After": str(int(remaining) * 60)},
            )

    remna_server = await _resolve_remnawave_server(session)
    if remna_server is None:
        raise HTTPException(status_code=503, detail="Сервер Remnawave не настроен")

    api = RemnawaveAPI(remna_server["api_url"])
    if not await api.login(REMNAWAVE_LOGIN, REMNAWAVE_PASSWORD):
        raise HTTPException(status_code=502, detail="Не удалось авторизоваться в Remnawave")

    success = bool(await api.delete_user_hwid_device(client_id, hwid))
    if not success:
        raise HTTPException(status_code=502, detail="Remnawave отказал в удалении устройства")

    if _DEVICES_MODULE_AVAILABLE:
        try:
            await update_device_cooldown(session, tg_id)
        except Exception:
            pass

    remaining_devices = await api.get_user_hwid_devices(client_id) or []
    return KeyDeviceDeleteResponse(
        ok=True,
        message="Устройство удалено",
        client_id=client_id,
        hwid=hwid,
        remaining_devices=len(remaining_devices),
    )
