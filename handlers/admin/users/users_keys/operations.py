from ._common import *  # noqa: F401,F403
from .edit import handle_key_edit


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_traffic"),
    IsAdminFilter(),
)
async def handle_user_traffic(
    callback_query: types.CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_obj = await resolve_callback_key(session, tg_id, callback_data.data)
    if not key_obj:
        await callback_query.message.edit_text("❌ Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        return
    email = key_obj.email

    await callback_query.message.edit_text("⏳ Получаем данные о трафике, пожалуйста, подождите...")

    traffic_data = await get_user_traffic(session, tg_id, email)

    if traffic_data["status"] == "error":
        await callback_query.message.edit_text(
            traffic_data["message"],
            reply_markup=build_editor_kb(tg_id, True),
        )
        return

    total_traffic = 0
    result_text = f"📊 <b>Трафик подписки {email}:</b>\n\n"

    for server, traffic in traffic_data["traffic"].items():
        if isinstance(traffic, str):
            result_text += f"❌ {server}: {traffic}\n"
        else:
            result_text += f"🌍 {server}: <b>{traffic} ГБ</b>\n"
            total_traffic += traffic

    result_text += f"\n🔢 <b>Общий трафик:</b> {total_traffic:.2f} ГБ"

    await callback_query.message.edit_text(
        result_text,
        reply_markup=build_editor_kb(tg_id, True),
    )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_reset_traffic"),
    IsAdminFilter(),
)
async def handle_reset_traffic(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_obj = await resolve_callback_key(session, tg_id, callback_data.data)
    if not key_obj:
        await callback_query.message.edit_text(
            "❌ Ключ не найден в базе данных.",
            reply_markup=build_editor_kb(tg_id),
        )
        return

    email = key_obj.email
    cluster_id = key_obj.server_id

    try:
        await reset_traffic_in_cluster(cluster_id, email, session)
        await callback_query.message.edit_text(
            f"✅ Трафик для ключа <b>{email}</b> успешно сброшен.",
            reply_markup=build_editor_kb(tg_id),
        )
    except Exception as e:
        logger.error(f"Ошибка при сбросе трафика: {e}")
        await callback_query.message.edit_text(
            "❌ Произошла ошибка при сбросе трафика. Попробуйте позже.",
            reply_markup=build_editor_kb(tg_id),
        )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_freeze"),
    IsAdminFilter(),
)
async def handle_admin_freeze_subscription(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_obj = await resolve_callback_key(session, tg_id, callback_data.data)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return
    email = key_obj.email

    try:
        record = await get_key_details(session, email)
        if not record:
            await callback_query.message.edit_text(
                text="🚫 Информация о ключе не найдена.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        client_id = record["client_id"]
        cluster_id = record["server_id"]

        result = await toggle_client_on_cluster(cluster_id, email, client_id, enable=False, session=session)
        if result["status"] != "success":
            text_error = (
                f"Произошла ошибка при отключении подписки.\nДетали: {result.get('error') or result.get('results')}"
            )
            await callback_query.message.edit_text(
                text_error,
                reply_markup=build_editor_kb(tg_id, True),
            )
            return

        now_ms = int(time.time() * 1000)
        time_left = record["expiry_time"] - now_ms
        if time_left < 0:
            time_left = 0

        await mark_key_as_frozen(session, record["tg_id"], client_id, time_left)
        session.expire_all()

        await callback_query.answer("✅ Подписка отключена")

        await handle_key_edit(
            callback_query=callback_query,
            callback_data=callback_data,
            session=session,
            update=False,
        )
    except Exception as e:
        await handle_error(tg_id, callback_query, f"Ошибка при отключении подписки: {e}")


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_unfreeze"),
    IsAdminFilter(),
)
async def handle_admin_unfreeze_subscription(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_obj = await resolve_callback_key(session, tg_id, callback_data.data)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return
    email = key_obj.email

    try:
        record = await get_key_details(session, email)
        if not record:
            await callback_query.message.edit_text(
                text="🚫 Информация о ключе не найдена.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        client_id = record["client_id"]
        cluster_id = record["server_id"]

        result = await toggle_client_on_cluster(cluster_id, email, client_id, enable=True, session=session)
        if result["status"] != "success":
            text_error = (
                f"Произошла ошибка при включении подписки.\nДетали: {result.get('error') or result.get('results')}"
            )
            await callback_query.message.edit_text(
                text_error,
                reply_markup=build_editor_kb(tg_id, True),
            )
            return

        tariff = await get_tariff_by_id(session, record["tariff_id"]) if record.get("tariff_id") else None
        if not tariff:
            total_gb = 0
            hwid_limit = 0
        else:
            total_gb = int(tariff.get("traffic_limit") or 0)
            hwid_limit = int(tariff.get("device_limit") or 0)

        if record.get("current_traffic_limit") is not None:
            total_gb = record["current_traffic_limit"]
        if record.get("current_device_limit") is not None:
            hwid_limit = record["current_device_limit"]

        now_ms = int(time.time() * 1000)
        leftover = record["expiry_time"]
        if leftover < 0:
            leftover = 0
        new_expiry_time = leftover if leftover > now_ms else now_ms + leftover

        await mark_key_as_unfrozen(session, record["tg_id"], client_id, new_expiry_time)
        session.expire_all()
        await release_session_early(session)

        await renew_key_in_cluster(
            cluster_id=cluster_id,
            email=email,
            client_id=client_id,
            new_expiry_time=new_expiry_time,
            total_gb=total_gb,
            session=session,
            hwid_device_limit=hwid_limit,
            reset_traffic=False,
            plan=record.get("tariff_id"),
        )

        await callback_query.answer("✅ Подписка включена")

        await handle_key_edit(
            callback_query=callback_query,
            callback_data=callback_data,
            session=session,
            update=False,
        )
    except Exception as e:
        await handle_error(tg_id, callback_query, f"Ошибка при включении подписки: {e}")
