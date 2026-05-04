"""User-facing key endpoints (/api/keys/*).

Регистрирует эндпоинты на ``user_router`` из ``_common``. Импорт этого модуля
из ``__init__.py`` запускает регистрацию декораторов.
"""

from .._common import *  # noqa: F401,F403 — подтягиваем все имена для endpoints
from .._common import (
    _key_actions_config,
    _normalize_expiry_ms,
    _resolve_available_location_servers,
    _resolve_billing_user_id,
    _resolve_default_web_payment_provider,
    _resolve_public_base_url,
    router,
    user_router,
)


@user_router.get("", response_model=list[AccountKeyResponse])
async def user_keys(
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    keys = await get_keys(session, billing_user_id)
    tariff_ids = {int(t) for t in (getattr(k, "tariff_id", None) for k in keys) if t is not None}
    trial_tariff_ids: set[int] = set()
    if tariff_ids:
        rows = (
            await session.execute(select(Tariff.id).where(Tariff.id.in_(tariff_ids), Tariff.price_rub == 0))
        ).scalars().all()
        trial_tariff_ids = {int(r) for r in rows}
    result: list[AccountKeyResponse] = []
    for key in keys:
        key_actions = AccountKeyActionsAvailability()
        try:
            key_ref = str(getattr(key, "client_id", "") or getattr(key, "email", "") or "")
            _, markup, _ = await build_key_view_payload(session, int(billing_user_id), key_ref)
            key_actions = _extract_key_actions_from_markup(markup)
        except Exception:
            key_actions = AccountKeyActionsAvailability()
        tariff_id_value = getattr(key, "tariff_id", None)
        result.append(
            AccountKeyResponse(
                email=str(getattr(key, "email", "") or ""),
                alias=getattr(key, "alias", None),
                client_id=str(getattr(key, "client_id", "") or ""),
                tariff_id=tariff_id_value,
                server_id=str(getattr(key, "server_id", "") or ""),
                created_at=int(getattr(key, "created_at", 0) or 0),
                expiry_time=int(getattr(key, "expiry_time", 0) or 0),
                key=getattr(key, "key", None),
                remnawave_link=getattr(key, "remnawave_link", None),
                is_frozen=bool(getattr(key, "is_frozen", False)),
                is_trial=bool(tariff_id_value is not None and int(tariff_id_value) in trial_tariff_ids),
                actions=key_actions,
            )
        )
    return result


@user_router.get("/actions-config", response_model=AccountKeyActionsConfigResponse)
async def user_keys_actions_config(
    identity=Depends(verify_identity_token),
):
    _ = identity
    return _key_actions_config()


@user_router.get("/{client_id}/details", response_model=AccountKeyDetailsResponse)
async def user_key_details(
    client_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = (
        await session.execute(select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1))
    ).scalar_one_or_none()
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    key_details = await get_key_details(session, str(getattr(db_key, "email", "") or ""))
    if not key_details:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    tariff_name = ""
    subgroup_title = ""
    traffic_limit_gb = 0
    device_limit = 0
    is_tariff_configurable = False
    addons_devices_enabled = False
    addons_traffic_enabled = False
    (
        tariff_name,
        subgroup_title,
        traffic_limit_gb,
        device_limit,
        _,
        is_tariff_configurable,
        addons_devices_enabled,
        addons_traffic_enabled,
    ) = await get_key_tariff_addons_state(
        session=session,
        key_record=key_details,
        db_key=db_key,
    )
    connected_devices = 0
    used_traffic_gb = None
    try:
        profile = await get_remnawave_profile(
            session,
            str(getattr(db_key, "server_id", "") or ""),
            client_id,
            fallback_any=True,
        )
        if profile:
            connected_devices = int(profile.get("hwid_count") or 0)
            used_raw = profile.get("used_gb")
            used_traffic_gb = float(used_raw) if used_raw is not None else None
            traffic_limit_bytes_actual = profile.get("traffic_limit_bytes")
            if traffic_limit_bytes_actual is not None:
                try:
                    traffic_limit_bytes_actual = int(traffic_limit_bytes_actual)
                    traffic_limit_gb = int(traffic_limit_bytes_actual / GB) if traffic_limit_bytes_actual > 0 else 0
                except (TypeError, ValueError):
                    pass
    except Exception:
        connected_devices = 0
        used_traffic_gb = None
    return AccountKeyDetailsResponse(
        client_id=str(getattr(db_key, "client_id", "") or ""),
        email=str(getattr(db_key, "email", "") or ""),
        alias=getattr(db_key, "alias", None),
        expiry_time=int(getattr(db_key, "expiry_time", 0) or 0),
        is_frozen=bool(getattr(db_key, "is_frozen", False)),
        tariff_name=str(tariff_name or ""),
        subgroup_title=str(subgroup_title or ""),
        traffic_limit_gb=int(traffic_limit_gb or 0),
        used_traffic_gb=used_traffic_gb,
        device_limit=int(device_limit or 0),
        connected_devices=int(connected_devices or 0),
        is_tariff_configurable=bool(is_tariff_configurable),
        addons_devices_enabled=bool(addons_devices_enabled),
        addons_traffic_enabled=bool(addons_traffic_enabled),
    )


@user_router.get("/{client_id}/qr", response_model=AccountKeyQrResponse)
async def user_key_qr(
    client_id: str,
    request: Request,
    force_web: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="key_qr", max_per_window=30, window_sec=60)
    actions = _key_actions_config()
    if not force_web and not actions.qr_enabled:
        raise HTTPException(status_code=403, detail="QR для подписок отключен в настройках")
    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = (
        await session.execute(select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1))
    ).scalar_one_or_none()
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    qr_data = str(getattr(db_key, "key", "") or "").strip() or str(getattr(db_key, "remnawave_link", "") or "").strip()
    if not qr_data:
        raise HTTPException(status_code=400, detail="Ссылка для подключения отсутствует")
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    image_data = b64encode(buffer.getvalue()).decode("ascii")
    return AccountKeyQrResponse(
        ok=True,
        message="QR-код готов",
        link=qr_data,
        image_data_url=f"data:image/png;base64,{image_data}",
    )


@user_router.patch("/{client_id}/alias", response_model=AccountKeyResponse)
async def user_key_update_alias(
    client_id: str,
    body: AccountKeyAliasUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="key_alias", max_per_window=20, window_sec=60)
    alias = str(body.alias or "").strip()
    if not alias:
        raise HTTPException(status_code=400, detail="Укажите alias")
    if len(alias) > 10:
        raise HTTPException(status_code=400, detail="Alias должен быть не длиннее 10 символов")
    if not re.match(r"^[a-zA-Zа-яА-ЯёЁ0-9@._-]+$", alias):
        raise HTTPException(status_code=400, detail="Alias содержит недопустимые символы")
    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = (
        await session.execute(select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1))
    ).scalar_one_or_none()
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    db_key.alias = alias
    tariff_id_value = getattr(db_key, "tariff_id", None)
    is_trial_value = False
    if tariff_id_value is not None:
        price = (
            await session.execute(select(Tariff.price_rub).where(Tariff.id == int(tariff_id_value)).limit(1))
        ).scalar_one_or_none()
        is_trial_value = price == 0
    return AccountKeyResponse(
        email=str(getattr(db_key, "email", "") or ""),
        alias=getattr(db_key, "alias", None),
        client_id=str(getattr(db_key, "client_id", "") or ""),
        tariff_id=tariff_id_value,
        server_id=str(getattr(db_key, "server_id", "") or ""),
        created_at=int(getattr(db_key, "created_at", 0) or 0),
        expiry_time=int(getattr(db_key, "expiry_time", 0) or 0),
        key=getattr(db_key, "key", None),
        remnawave_link=getattr(db_key, "remnawave_link", None),
        is_frozen=bool(getattr(db_key, "is_frozen", False)),
        is_trial=is_trial_value,
    )


@user_router.delete("/{client_id}", response_model=AccountKeyActionResponse)
async def user_key_delete(
    client_id: str,
    request: Request,
    force_web: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    from api.ratelimit import enforce_rate_limit
    await enforce_rate_limit(request, session, bucket="key_delete", max_per_window=10, window_sec=60)
    actions = _key_actions_config()
    if not force_web and not actions.delete_enabled:
        raise HTTPException(status_code=403, detail="Удаление подписки отключено в настройках")
    billing_user_id = await _resolve_billing_user_id(request, identity, session)
    db_key = (
        await session.execute(select(Key).where(Key.user_id == billing_user_id, Key.client_id == client_id).limit(1))
    ).scalar_one_or_none()
    if db_key is None:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    cluster_id = str(getattr(db_key, "server_id", "") or "")
    email = str(getattr(db_key, "email", "") or "")
    if cluster_id and email:
        await delete_key_from_cluster(
            cluster_id=cluster_id,
            email=email,
            client_id=client_id,
            session=session,
        )
    await session.delete(db_key)
    return AccountKeyActionResponse(ok=True, message="Подписка удалена")
