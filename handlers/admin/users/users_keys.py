import asyncio
import time
import uuid

from datetime import datetime, timedelta, timezone
from handlers.buttons import BACK

import pytz

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import REMNAWAVE_LOGIN, REMNAWAVE_PASSWORD, REMNAWAVE_TOKEN_LOGIN_ENABLED, USE_COUNTRY_SELECTION
from core.bootstrap import MODES_CONFIG
from database import (
    check_server_name_by_cluster,
    delete_key,
    delete_user_data,
    get_active_tariffs_by_group_code,
    get_key_by_email,
    get_key_details,
    get_keys,
    get_server_names,
    get_servers,
    get_tariff_by_id,
    get_tariffs_for_cluster,
    mark_key_as_frozen,
    mark_key_as_unfrozen,
    save_admin_key_config,
    update_key_subscription_links,
    update_key_expiry,
)
from database.models import Key
from filters.admin import IsAdminFilter
from middlewares.session import release_session_early
from handlers.keys.operations import (
    create_key_on_cluster,
    delete_key_from_cluster,
    get_user_traffic,
    renew_key_in_cluster,
    reset_traffic_in_cluster,
    toggle_client_on_cluster,
    update_subscription,
)
from handlers.utils import generate_random_email, handle_error
from hooks.hook_buttons import insert_hook_buttons
from hooks.processors import process_admin_key_edit_menu
from logger import logger
from panels.remnawave import RemnawaveAPI

from ..panel.keyboard import AdminPanelCallback, build_admin_back_btn, build_admin_back_kb
from .keyboard import (
    AdminUserEditorCallback,
    AdminUserKeyEditorCallback,
    build_cluster_selection_kb,
    build_editor_kb,
    build_key_delete_kb,
    build_key_edit_kb,
    build_reissue_menu_kb,
    build_user_delete_kb,
    build_users_key_expiry_kb,
    build_users_key_show_kb,
)
from .utils import resolve_admin_key
from .users_states import RenewTariffState, UserEditorState


MOSCOW_TZ = pytz.timezone("Europe/Moscow")

router = Router()


async def _resolve_callback_key(
    session: AsyncSession,
    tg_id: int,
    key_ref: str | int | None,
) -> Key | None:
    return await resolve_admin_key(session, tg_id, key_ref)


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_key_edit"),
    IsAdminFilter(),
)
async def handle_key_edit(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback | AdminUserKeyEditorCallback,
    session: AsyncSession,
    update: bool = False,
):
    key_ref = callback_data.data
    key_obj = await _resolve_callback_key(session, callback_data.tg_id, key_ref)

    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о подписке не найдена.",
            reply_markup=build_editor_kb(callback_data.tg_id),
        )
        return

    email = key_obj.email
    key_details = await get_key_details(session, email)
    is_frozen = bool(key_details.get("is_frozen")) if key_details else bool(getattr(key_obj, "is_frozen", False))

    key_value = key_obj.key or key_obj.remnawave_link or "—"
    alias_part = f" (<i>{key_obj.alias}</i>)" if key_obj.alias else ""

    if key_obj.created_at:
        created_at_dt = datetime.fromtimestamp(int(key_obj.created_at) / 1000, tz=MOSCOW_TZ)
        created_at = created_at_dt.strftime("%d %B %Y года %H:%M")
    else:
        created_at = "—"

    if is_frozen:
        frozen_left_ms = int((key_details or {}).get("expiry_time") or 0)
        total_minutes = max(frozen_left_ms // 60000, 0)
        days, rem_minutes = divmod(total_minutes, 24 * 60)
        hours, minutes = divmod(rem_minutes, 60)
        frozen_parts: list[str] = []
        if days:
            frozen_parts.append(f"{days} дн.")
        if hours:
            frozen_parts.append(f"{hours} ч.")
        if minutes or not frozen_parts:
            frozen_parts.append(f"{minutes} мин.")
        expiry_label = "⏳ <b>Остаток:</b>"
        expiry_date = " ".join(frozen_parts)
    elif key_obj.expiry_time:
        expiry_dt = datetime.fromtimestamp(int(key_obj.expiry_time) / 1000, tz=MOSCOW_TZ)
        expiry_label = "⏰ <b>Истекает:</b>"
        expiry_date = expiry_dt.strftime("%d %B %Y года %H:%M")
    else:
        expiry_label = "⏰ <b>Истекает:</b>"
        expiry_date = "—"

    tariff_name = "—"
    subgroup_title = "—"
    group_code = "—"
    base_devices = None
    base_traffic = None
    is_configurable = False
    if key_obj.tariff_id:
        tariff = await get_tariff_by_id(session, key_obj.tariff_id)
        if tariff:
            tariff_name = tariff.get("name", "—")
            subgroup_title = tariff.get("subgroup_title") or "—"
            group_code = tariff.get("group_code") or "—"
            base_devices = tariff.get("device_limit")
            base_traffic = tariff.get("traffic_limit")
            is_configurable = bool(tariff.get("configurable"))

    devices_line = ""
    traffic_line = ""
    if is_configurable:
        sel_dev, cur_dev = key_obj.selected_device_limit, key_obj.current_device_limit
        if sel_dev is not None or cur_dev is not None:
            base_dev = sel_dev if sel_dev is not None else (base_devices if base_devices is not None else cur_dev)
            extra = (
                f" + {cur_dev - base_dev} (докуплено)"
                if (base_dev is not None and cur_dev is not None and cur_dev > base_dev)
                else ""
            )
            devices_line = f"📱 <b>Устройства:</b> {base_dev}{extra}\n"

        sel_traf, cur_traf = key_obj.selected_traffic_limit, key_obj.current_traffic_limit
        if sel_traf is not None or cur_traf is not None:
            base_traf = sel_traf if sel_traf is not None else (base_traffic if base_traffic is not None else cur_traf)
            extra = (
                f" + {cur_traf - base_traf} ГБ (докуплено)"
                if (base_traf is not None and cur_traf is not None and cur_traf > base_traf)
                else ""
            )
            traffic_line = f"📊 <b>Трафик:</b> {base_traf} ГБ{extra}\n"

    text = (
        "<b>🔑 Информация о подписке</b>\n\n"
        "<blockquote>"
        f"🔗 <b>Ключ{alias_part}:</b> <code>{key_value}</code>\n"
        f"📆 <b>Создан:</b> {created_at} (МСК)\n"
        f"{'⛔ <b>Статус:</b> отключена\n' if is_frozen else ''}"
        f"{expiry_label} {expiry_date}{' (МСК)' if not is_frozen and expiry_date != '—' else ''}\n"
        f"🌐 <b>Кластер:</b> {key_obj.server_id or '—'}\n"
        f"🆔 <b>ID клиента:</b> {key_obj.tg_id or '—'}\n"
        f"🏷️ <b>Тарифная группа:</b> {group_code}\n"
        f"📁 <b>Подгруппа:</b> {subgroup_title}\n"
        f"📦 <b>Тариф:</b> {tariff_name}\n"
        f"{devices_line}"
        f"{traffic_line}"
        "</blockquote>"
    )

    if not update or not getattr(callback_data, "edit", False):
        kb_key_details = dict(key_obj.__dict__)
        kb_key_details["is_frozen"] = is_frozen
        kb_markup = build_key_edit_kb(kb_key_details, email, is_configurable=is_configurable, key_ref=str(key_ref))
        kb_builder = InlineKeyboardBuilder.from_markup(kb_markup)
        hook_buttons = await process_admin_key_edit_menu(
            email=email,
            session=session,
            client_id=key_obj.client_id,
            tg_id=key_obj.tg_id,
        )
        kb_builder = insert_hook_buttons(kb_builder, hook_buttons)
        try:
            await callback_query.message.edit_text(
                text=text,
                reply_markup=kb_builder.as_markup(),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    else:
        try:
            await callback_query.message.edit_text(
                text=text,
                reply_markup=await build_users_key_expiry_kb(
                    session,
                    callback_data.tg_id,
                    email,
                    key_ref=str(key_ref),
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_expiry_edit"),
    IsAdminFilter(),
)
async def handle_change_expiry(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return
    email = key_obj.email

    await callback_query.message.edit_reply_markup(
        reply_markup=await build_users_key_expiry_kb(session, tg_id, email, key_ref=key_ref)
    )


@router.callback_query(
    AdminUserKeyEditorCallback.filter(F.action == "add"),
    IsAdminFilter(),
)
async def handle_expiry_add(
    callback_query: CallbackQuery,
    callback_data: AdminUserKeyEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return
    email = key_obj.email
    days = callback_data.month

    key_details = await get_key_details(session, email)

    if not key_details:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return

    if days:
        await change_expiry_time(key_details["expiry_time"] + days * 24 * 3600 * 1000, email, session)
        await handle_key_edit(callback_query, callback_data, session, True)
        return

    await state.update_data(tg_id=tg_id, email=email, key_ref=key_ref, op_type="add")
    await state.set_state(UserEditorState.waiting_for_expiry_time)

    await callback_query.message.edit_text(
        text="✍️ Введите количество дней, которое хотите добавить к времени действия ключа:",
        reply_markup=build_users_key_show_kb(tg_id, key_ref),
    )


@router.callback_query(
    AdminUserKeyEditorCallback.filter(F.action == "take"),
    IsAdminFilter(),
)
async def handle_expiry_take(
    callback_query: CallbackQuery,
    callback_data: AdminUserKeyEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return
    email = key_obj.email

    await state.update_data(tg_id=tg_id, email=email, key_ref=key_ref, op_type="take")
    await state.set_state(UserEditorState.waiting_for_expiry_time)

    await callback_query.message.edit_text(
        text="✍️ Введите количество дней, которое хотите вычесть из времени действия ключа:",
        reply_markup=build_users_key_show_kb(tg_id, key_ref),
    )


@router.callback_query(
    AdminUserKeyEditorCallback.filter(F.action == "set"),
    IsAdminFilter(),
)
async def handle_expiry_set(
    callback_query: CallbackQuery,
    callback_data: AdminUserKeyEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return
    email = key_obj.email

    key_details = await get_key_details(session, email)

    if not key_details:
        await callback_query.message.edit_text(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return

    await state.update_data(tg_id=tg_id, email=email, key_ref=key_ref, op_type="set")
    await state.set_state(UserEditorState.waiting_for_expiry_time)

    text = (
        "✍️ Введите новое время действия ключа:"
        "\n\n📌 Формат: <b>год-месяц-день час:минута</b>"
        f"\n\n📄 Текущая дата: {datetime.fromtimestamp(key_details['expiry_time'] / 1000, tz=MOSCOW_TZ).strftime('%Y-%m-%d %H:%M')} (МСК)"
    )

    await callback_query.message.edit_text(
        text=text,
        reply_markup=build_users_key_show_kb(tg_id, key_ref),
    )


@router.message(UserEditorState.waiting_for_expiry_time, IsAdminFilter())
async def handle_expiry_time_input(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    tg_id = data.get("tg_id")
    email = data.get("email")
    key_ref = data.get("key_ref")
    op_type = data.get("op_type")

    if op_type != "set" and (not message.text.isdigit() or int(message.text) < 0):
        await message.answer(
            text="🚫 Пожалуйста, введите корректное количество дней!",
            reply_markup=build_users_key_show_kb(tg_id, key_ref) if key_ref else build_editor_kb(tg_id),
        )
        return

    key_details = await get_key_details(session, email)

    if not key_details:
        await message.answer(
            text="🚫 Информация о ключе не найдена.",
            reply_markup=build_editor_kb(tg_id),
        )
        return

    try:
        current_expiry_time = datetime.fromtimestamp(
            key_details["expiry_time"] / 1000,
            tz=MOSCOW_TZ,
        )

        if op_type == "add":
            days = int(message.text)
            new_expiry_time = current_expiry_time + timedelta(days=days)
            text = f"✅ Ко времени действия ключа добавлено <b>{days} дн.</b>"
        elif op_type == "take":
            days = int(message.text)
            new_expiry_time = current_expiry_time - timedelta(days=days)
            text = f"✅ Из времени действия ключа вычтено <b>{days} дн.</b>"
        else:
            new_expiry_time = datetime.strptime(message.text, "%Y-%m-%d %H:%M")
            new_expiry_time = MOSCOW_TZ.localize(new_expiry_time)
            text = f"✅ Время действия ключа изменено на <b>{message.text} (МСК)</b>"

        new_expiry_timestamp = int(new_expiry_time.timestamp() * 1000)
        await change_expiry_time(new_expiry_timestamp, email, session)
    except ValueError:
        text = "🚫 Пожалуйста, используйте корректный формат даты (ГГГГ-ММ-ДД ЧЧ:ММ)!"
    except Exception as e:
        text = f"❗ Произошла ошибка во время изменения времени действия ключа: {e}"

    await message.answer(
        text=text,
        reply_markup=build_users_key_show_kb(tg_id, key_ref) if key_ref else build_editor_kb(tg_id),
    )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_reissue_menu"),
    IsAdminFilter(),
)
async def handle_reissue_menu(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text("🚫 Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        return

    text = (
        "<b>🔄 Перевыпуск подписки</b>\n\n"
        "<b>📦 Полный перевыпуск</b>\n"
        "<i>Пересоздаёт подписку на сервере с возможностью выбора кластера. "
        "Используйте для переноса на другой сервер или обновления данных.</i>\n\n"
        "<b>🔗 Сменить ссылку</b>\n"
        "<i>Генерирует новую ссылку подписки. Старая ссылка перестанет работать. "
        "Все данные подписки сохранятся.</i>"
    )

    await callback_query.message.edit_text(
        text=text,
        reply_markup=build_reissue_menu_kb(key_ref, tg_id),
    )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_update_key"),
    IsAdminFilter(),
)
async def handle_update_key(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text("🚫 Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        return
    email = key_obj.email

    await callback_query.message.edit_text(
        text=f"📡 Выберите кластер, на котором пересоздать ключ <b>{email}</b>:",
        reply_markup=await build_cluster_selection_kb(
            session,
            tg_id,
            key_ref,
            action="confirm_admin_key_reissue",
        ),
    )


@router.callback_query(F.data.startswith("confirm_admin_key_reissue|"), IsAdminFilter())
async def confirm_admin_key_reissue(callback_query: CallbackQuery, session: AsyncSession, state: FSMContext):
    _, tg_id, key_ref, cluster_id = callback_query.data.split("|")
    tg_id = int(tg_id)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text("🚫 Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        return
    email = key_obj.email

    try:
        servers = await get_servers(session)
        cluster_servers = servers.get(cluster_id, [])

        tariffs = await get_tariffs_for_cluster(session, cluster_id)
        if not tariffs:
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(
                    text="🔗 Привязать тариф",
                    callback_data=AdminPanelCallback(action="clusters").pack(),
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text=BACK,
                    callback_data=AdminUserEditorCallback(
                        action="users_key_edit",
                        tg_id=tg_id,
                        data=key_ref,
                    ).pack(),
                )
            )
            await callback_query.message.edit_text(
                f"🚫 <b>Невозможно пересоздать подписку</b>\n\n"
                f"📊 <b>Информация о кластере:</b>\n<blockquote>"
                f"🌐 <b>Кластер:</b> <code>{cluster_id}</code>\n"
                f"⚠️ <b>Статус:</b> Нет привязанного тарифа\n</blockquote>"
                f"💡 <b>Привяжите тариф к кластеру</b>",
                reply_markup=builder.as_markup(),
            )
            return

        use_country_selection = bool(MODES_CONFIG.get("COUNTRY_SELECTION_ENABLED", USE_COUNTRY_SELECTION))

        if use_country_selection:
            unique_countries = {srv["server_name"] for srv in cluster_servers}
            await state.update_data(tg_id=tg_id, email=email, key_ref=key_ref, cluster_id=cluster_id)
            builder = InlineKeyboardBuilder()
            for country in sorted(unique_countries):
                builder.button(
                    text=country,
                    callback_data=f"admin_reissue_country|{tg_id}|{key_ref}|{country}",
                )
            builder.row(
                InlineKeyboardButton(
                    text=BACK,
                    callback_data=AdminUserEditorCallback(
                        action="users_key_edit",
                        tg_id=tg_id,
                        data=key_ref,
                    ).pack(),
                )
            )
            await callback_query.message.edit_text(
                "🌍 Выберите сервер (страну) для пересоздания подписки:",
                reply_markup=builder.as_markup(),
            )
            return

        key_link = await get_key_by_email(session, email)
        remnawave_link = key_link.remnawave_link if key_link else None

        await update_subscription(
            tg_id,
            email,
            session,
            cluster_override=cluster_id,
            remnawave_link=remnawave_link,
        )

        await handle_key_edit(
            callback_query,
            AdminUserEditorCallback(tg_id=tg_id, data=key_ref, action="view_key"),
            session,
            True,
        )
    except Exception as e:
        logger.error(f"Ошибка при перевыпуске ключа {email}: {e}")
        await callback_query.message.answer(f"❗ Ошибка: {e}")


@router.callback_query(F.data.startswith("admin_reissue_country|"), IsAdminFilter())
async def admin_reissue_country(callback_query: CallbackQuery, session: AsyncSession, state: FSMContext):
    _, tg_id, key_ref, country = callback_query.data.split("|")
    tg_id = int(tg_id)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)
    if not key_obj:
        await callback_query.message.edit_text("🚫 Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        return
    email = key_obj.email

    try:
        data = await state.get_data()
        cluster_id = data.get("cluster_id")

        if cluster_id:
            tariffs = await get_tariffs_for_cluster(session, cluster_id)
            if not tariffs:
                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(
                        text="🔗 Привязать тариф",
                        callback_data=AdminPanelCallback(action="clusters").pack(),
                    )
                )
                builder.row(
                    InlineKeyboardButton(
                        text=BACK,
                        callback_data=AdminUserEditorCallback(
                            action="users_key_edit",
                            tg_id=tg_id,
                            data=key_ref,
                        ).pack(),
                    )
                )
                await callback_query.message.edit_text(
                    f"🚫 <b>Невозможно пересоздать подписку</b>\n\n"
                    f"📊 <b>Информация о кластере:</b>\n<blockquote>"
                    f"🌐 <b>Кластер:</b> <code>{cluster_id}</code>\n"
                    f"⚠️ <b>Статус:</b> Нет привязанного тарифа\n</blockquote>"
                    f"💡 <b>Привяжите тариф к кластеру</b>",
                    reply_markup=builder.as_markup(),
                )
                return

        key_link = await get_key_by_email(session, email)
        remnawave_link = key_link.remnawave_link if key_link else None

        await update_subscription(
            tg_id=tg_id,
            email=email,
            session=session,
            country_override=country,
            remnawave_link=remnawave_link,
        )

        await handle_key_edit(
            callback_query,
            AdminUserEditorCallback(tg_id=tg_id, data=key_ref, action="view_key"),
            session,
            True,
        )
    except Exception as e:
        logger.error(f"Ошибка при перевыпуске ключа для страны {country}: {e}")
        await callback_query.message.answer(f"❗ Ошибка: {e}")


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_recreate_key"),
    IsAdminFilter(),
)
async def handle_recreate_key_start(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    key_ref = str(callback_data.data)
    key_obj = await _resolve_callback_key(session, tg_id, key_ref)

    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Ключ не найден.",
            reply_markup=build_editor_kb(tg_id),
        )
        return

    email = key_obj.email

    tariff_name = "—"
    if key_obj.tariff_id:
        tariff = await get_tariff_by_id(session, key_obj.tariff_id)
        if tariff:
            tariff_name = tariff.get("name", "—")

    text = (
        "<b>🔁 Пересоздание ссылки подписки</b>\n\n"
        f"📦 <b>Тариф:</b> {tariff_name}\n\n"
        "⚠️ <b>Будет сгенерирована новая ссылка подписки.</b>\n"
        "Старая ссылка перестанет работать.\n\n"
        "✅ <i>Все данные подписки сохранятся.</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Пересоздать",
            callback_data=f"confirm_recreate|{tg_id}|{key_ref}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=BACK,
            callback_data=AdminUserEditorCallback(action="users_key_edit", tg_id=tg_id, data=key_ref).pack(),
        )
    )

    await callback_query.message.edit_text(text=text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("confirm_recreate|"), IsAdminFilter())
async def handle_recreate_key_confirm(
    callback_query: CallbackQuery,
    session: AsyncSession,
):
    _, tg_id, key_ref = callback_query.data.split("|")
    tg_id = int(tg_id)

    try:
        key_obj = await _resolve_callback_key(session, tg_id, key_ref)

        if not key_obj:
            await callback_query.message.edit_text(
                text="🚫 Ключ не найден.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        old_email = key_obj.email

        await callback_query.message.edit_text("⏳ Пересоздание ссылки подписки...")

        client_id = key_obj.client_id
        cluster_id = key_obj.server_id
        old_link = key_obj.remnawave_link or key_obj.key

        servers = await get_servers(session)
        cluster = servers.get(cluster_id)

        if not cluster:
            for _, server_list in servers.items():
                for server_info in server_list:
                    if server_info.get("server_name", "").lower() == cluster_id.lower():
                        cluster = [server_info]
                        break
                if cluster:
                    break

        if not cluster:
            await callback_query.message.edit_text(
                text=f"❗ Кластер {cluster_id} не найден.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        remnawave_servers = [s for s in cluster if s.get("panel_type", "3x-ui").lower() == "remnawave"]

        if not remnawave_servers:
            await callback_query.message.edit_text(
                text="❗ Revoke доступен только для Remnawave. Для 3x-ui используйте перевыпуск.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        api_url = remnawave_servers[0].get("api_url")
        if not api_url:
            await callback_query.message.edit_text(
                text="❗ У Remnawave сервера не задан api_url.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        api = RemnawaveAPI(api_url)
        try:
            if not REMNAWAVE_TOKEN_LOGIN_ENABLED:
                await api.login(REMNAWAVE_LOGIN, REMNAWAVE_PASSWORD)

            user_data = await api.revoke_user_subscription(client_id)
        finally:
            await api.aclose()

        if not user_data:
            await callback_query.message.edit_text(
                text="❗ Не удалось выполнить revoke. Проверьте логи.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        new_link = user_data.get("subscriptionUrl")

        if not new_link:
            await callback_query.message.edit_text(
                text="❗ Revoke выполнен, но новая ссылка не получена.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        await update_key_subscription_links(session, old_email, new_link)

        try:
            user_text = (
                "🔄 <b>Ваша подписка была перевыпущена</b>\n\n"
                f"🔗 <b>Новая ссылка подписки:</b>\n<code>{new_link}</code>\n\n"
                "<i>Старая ссылка больше не работает.</i>"
            )
            user_kb = InlineKeyboardBuilder()
            user_kb.row(
                InlineKeyboardButton(
                    text="📱 Мои подписки",
                    callback_data="view_keys",
                )
            )
            user_kb.row(
                InlineKeyboardButton(
                    text="👤 Личный кабинет",
                    callback_data="profile",
                )
            )

            await callback_query.bot.send_message(
                chat_id=tg_id,
                text=user_text,
                reply_markup=user_kb.as_markup(),
            )
            notification_sent = True
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление клиенту {tg_id}: {e}")
            notification_sent = False

        text = (
            "✅ <b>Ссылка подписки пересоздана</b>\n\n"
            f"🔗 <b>Старая ссылка:</b>\n<code>{old_link}</code>\n\n"
            f"🔗 <b>Новая ссылка:</b>\n<code>{new_link}</code>\n\n"
        )
        if notification_sent:
            text += "📨 <i>Клиент уведомлён о новой ссылке.</i>"
        else:
            text += "⚠️ <i>Не удалось уведомить клиента.</i>"

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=BACK,
                callback_data=AdminUserEditorCallback(
                    action="users_key_edit",
                    tg_id=tg_id,
                    data=key_ref,
                ).pack(),
            )
        )

        await callback_query.message.edit_text(
            text=text,
            reply_markup=builder.as_markup(),
        )

    except Exception as e:
        logger.error(f"Ошибка при revoke ключа {old_email}: {e}")
        await callback_query.message.edit_text(
            text=f"❗ Ошибка при пересоздании: {e}",
            reply_markup=build_editor_kb(tg_id),
        )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_delete_key"),
    IsAdminFilter(),
)
async def handle_delete_key(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    key_obj = await _resolve_callback_key(session, callback_data.tg_id, callback_data.data)
    if not key_obj:
        await callback_query.message.edit_text(
            text="🚫 Ключ не найден!",
            reply_markup=build_editor_kb(callback_data.tg_id),
        )
        return

    email = key_obj.email
    client_id = key_obj.client_id

    if client_id is None:
        await callback_query.message.edit_text(
            text="🚫 Ключ не найден!",
            reply_markup=build_editor_kb(callback_data.tg_id),
        )
        return

    await state.set_state(UserEditorState.confirm_delete_key)
    await state.update_data(
        delete_key_email=email,
        delete_key_tg_id=int(callback_data.tg_id),
        delete_key_client_id=client_id,
    )

    await callback_query.message.edit_text(
        text="❓ Вы уверены, что хотите удалить ключ?",
        reply_markup=build_key_delete_kb(callback_data.tg_id),
    )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_delete_key_confirm"),
    UserEditorState.confirm_delete_key,
    IsAdminFilter(),
)
async def handle_delete_key_confirm(
    callback_query: types.CallbackQuery,
    callback_data: AdminUserEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    data = await state.get_data()
    email = data.get("delete_key_email")
    expected_tg_id = data.get("delete_key_tg_id")
    client_id = data.get("delete_key_client_id")
    await state.clear()

    if not email or int(expected_tg_id or 0) != int(callback_data.tg_id):
        await callback_query.answer("Данные устарели", show_alert=True)
        return

    if not client_id:
        key_obj = await get_key_by_email(session, email, int(callback_data.tg_id))
        client_id = key_obj.client_id if key_obj else None

    kb = build_editor_kb(callback_data.tg_id)

    if client_id:
        clusters = await get_servers(session=session)
        await release_session_early(session)

        async def delete_key_from_servers():
            tasks = []
            for cluster_name, cluster_servers in clusters.items():
                for _ in cluster_servers:
                    tasks.append(delete_key_from_cluster(cluster_name, email, client_id, session))
            await asyncio.gather(*tasks, return_exceptions=True)

        await delete_key_from_servers()
        await delete_key(session, client_id)

        await callback_query.message.edit_text(text="✅ Ключ успешно удален.", reply_markup=kb)
    else:
        await callback_query.message.edit_text(
            text="🚫 Ключ не найден или уже удален.",
            reply_markup=kb,
        )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_delete_user"),
    IsAdminFilter(),
)
async def handle_delete_user(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
):
    tg_id = callback_data.tg_id
    await callback_query.message.edit_text(
        text=f"❗️ Вы уверены, что хотите удалить пользователя с ID {tg_id}?",
        reply_markup=build_user_delete_kb(tg_id),
    )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_delete_user_confirm"),
    IsAdminFilter(),
)
async def handle_delete_user_confirm(
    callback_query: types.CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id

    key_records = [(row.email, row.client_id) for row in await get_keys(session, tg_id)]
    await release_session_early(session)

    async def delete_keys_from_servers():
        try:
            tasks = []
            servers = await get_servers(session=session)
            for email, client_id in key_records:
                for cluster_id, _cluster in servers.items():
                    tasks.append(delete_key_from_cluster(cluster_id, email, client_id, session))
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Ошибка при удалении ключей с серверов для пользователя {tg_id}: {e}")

    await delete_keys_from_servers()

    try:
        await delete_user_data(session, tg_id)
        await callback_query.message.edit_text(
            text=f"🗑️ Пользователь с ID {tg_id} был удален.",
            reply_markup=build_admin_back_kb(),
        )
    except Exception as e:
        logger.error(f"Ошибка при удалении данных из базы данных для пользователя {tg_id}: {e}")
        await callback_query.message.edit_text(
            text=f"❌ Произошла ошибка при удалении пользователя с ID {tg_id}. Попробуйте снова.",
            reply_markup=build_admin_back_kb(),
        )


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
    key_obj = await _resolve_callback_key(session, tg_id, callback_data.data)
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
    AdminUserEditorCallback.filter(F.action == "users_create_key"),
    IsAdminFilter(),
)
async def handle_create_key_start(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id
    await state.update_data(tg_id=tg_id)

    use_country_selection = bool(MODES_CONFIG.get("COUNTRY_SELECTION_ENABLED", USE_COUNTRY_SELECTION))

    if use_country_selection:
        await state.set_state(UserEditorState.selecting_country)

        countries = await get_server_names(session)

        if not countries:
            await callback_query.message.edit_text(
                "❌ Нет доступных стран для создания ключа.",
                reply_markup=build_editor_kb(tg_id),
            )
            return

        builder = InlineKeyboardBuilder()
        for country in countries:
            builder.button(text=country, callback_data=country)
        builder.adjust(1)
        builder.row(build_admin_back_btn())

        await callback_query.message.edit_text(
            "🌍 <b>Выберите страну для создания ключа:</b>",
            reply_markup=builder.as_markup(),
        )
        return

    await state.set_state(UserEditorState.selecting_cluster)

    servers = await get_servers(session=session)
    cluster_names = list(servers.keys())

    if not cluster_names:
        await callback_query.message.edit_text(
            "❌ Нет доступных кластеров для создания ключа.",
            reply_markup=build_editor_kb(tg_id),
        )
        return

    builder = InlineKeyboardBuilder()
    for cluster in cluster_names:
        builder.button(text=f"🌐 {cluster}", callback_data=cluster)
    builder.adjust(2)
    builder.row(build_admin_back_btn())

    await callback_query.message.edit_text(
        "🌐 <b>Выберите кластер для создания ключа:</b>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(UserEditorState.selecting_country, IsAdminFilter())
async def handle_create_key_country(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    country = callback_query.data
    await state.update_data(country=country)
    await state.set_state(UserEditorState.selecting_duration)

    builder = InlineKeyboardBuilder()

    cluster_info = await check_server_name_by_cluster(session, country)

    if not cluster_info:
        await callback_query.message.edit_text("❌ Сервер не найден.")
        return

    cluster_name = cluster_info["cluster_name"]
    await state.update_data(cluster_name=cluster_name)

    tariffs = await get_tariffs_for_cluster(session, cluster_name)

    for tariff in tariffs:
        if tariff["duration_days"] < 1:
            continue
        builder.button(
            text=f"{tariff['name']} — {tariff['price_rub']}₽",
            callback_data=f"tariff_{tariff['id']}",
        )

    builder.adjust(1)
    builder.row(build_admin_back_btn())

    await callback_query.message.edit_text(
        text=f"🕒 <b>Выберите срок действия ключа для страны <code>{country}</code>:</b>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(UserEditorState.selecting_cluster, IsAdminFilter())
async def handle_create_key_cluster(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    cluster_name = callback_query.data

    data = await state.get_data()
    tg_id = data.get("tg_id")

    if not tg_id:
        await callback_query.message.edit_text("❌ Ошибка: tg_id клиента не найден.")
        return

    await state.update_data(cluster_name=cluster_name)
    await state.set_state(UserEditorState.selecting_duration)

    tariffs = await get_tariffs_for_cluster(session, cluster_name)

    builder = InlineKeyboardBuilder()
    for tariff in tariffs:
        if tariff["duration_days"] < 1:
            continue
        builder.button(
            text=f"{tariff['name']} — {tariff['price_rub']}₽",
            callback_data=f"tariff_{tariff['id']}",
        )

    builder.adjust(1)
    builder.row(build_admin_back_btn())

    await callback_query.message.edit_text(
        text=f"🕒 <b>Выберите срок действия ключа для кластера <code>{cluster_name}</code>:</b>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(UserEditorState.selecting_duration, IsAdminFilter())
async def handle_create_key_duration(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    tg_id = data.get("tg_id", callback_query.from_user.id)

    use_country_selection = bool(MODES_CONFIG.get("COUNTRY_SELECTION_ENABLED", USE_COUNTRY_SELECTION))

    try:
        if not callback_query.data.startswith("tariff_"):
            raise ValueError("Некорректный callback_data")
        tariff_id = int(callback_query.data.replace("tariff_", ""))

        tariff = await get_tariff_by_id(session, tariff_id)
        if not tariff:
            raise ValueError("Тариф не найден.")

        duration_days = tariff["duration_days"]
        client_id = str(uuid.uuid4())
        email = await generate_random_email(session=session)
        expiry = datetime.now(tz=timezone.utc) + timedelta(days=duration_days)
        expiry_ms = int(expiry.timestamp() * 1000)

        if use_country_selection and "country" in data:
            country = data["country"]
            await create_key_on_cluster(
                country,
                tg_id,
                client_id,
                email,
                expiry_ms,
                plan=tariff_id,
                session=session,
            )

            await state.clear()
            await callback_query.message.edit_text(
                f"✅ Ключ успешно создан для страны <b>{country}</b> на {duration_days} дней.",
                reply_markup=build_editor_kb(tg_id),
            )
        elif "cluster_name" in data:
            cluster_name = data["cluster_name"]
            await create_key_on_cluster(
                cluster_name,
                tg_id,
                client_id,
                email,
                expiry_ms,
                plan=tariff_id,
                session=session,
            )

            await state.clear()
            await callback_query.message.edit_text(
                f"✅ Ключ успешно создан в кластере <b>{cluster_name}</b> на {duration_days} дней.",
                reply_markup=build_editor_kb(tg_id),
            )
        else:
            await callback_query.message.edit_text("❌ Не удалось определить источник — страна или кластер.")
    except Exception as e:
        logger.error(f"[CreateKey] Ошибка при создании ключа: {e}")
        await callback_query.message.edit_text(
            "❌ Не удалось создать ключ. Попробуйте позже.",
            reply_markup=build_editor_kb(tg_id),
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
    key_obj = await _resolve_callback_key(session, tg_id, callback_data.data)
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
    key_obj = await _resolve_callback_key(session, tg_id, callback_data.data)
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
        await session.commit()
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
    key_obj = await _resolve_callback_key(session, tg_id, callback_data.data)
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
        new_expiry_time = now_ms + leftover

        await mark_key_as_unfrozen(session, record["tg_id"], client_id, new_expiry_time)
        await session.commit()
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


async def change_expiry_time(expiry_time: int, email: str, session: AsyncSession) -> Exception | None:
    key_obj = await get_key_by_email(session, email)
    if not key_obj:
        return ValueError(f"User with email {email} was not found")

    client_id = key_obj.client_id
    tariff_id = key_obj.tariff_id
    server_id = key_obj.server_id
    key_device_limit = key_obj.current_device_limit
    key_traffic_limit = key_obj.current_traffic_limit
    if server_id is None:
        return ValueError(f"Key with client_id {client_id} was not found")

    traffic_limit = 0
    device_limit = None
    key_subgroup = None
    if tariff_id:
        tariff = await get_tariff_by_id(session, tariff_id)
        if tariff:
            traffic_limit = int(tariff.get("traffic_limit") or 0)
            raw_device_limit = tariff.get("device_limit")
            device_limit = int(raw_device_limit) if raw_device_limit is not None else 0
            key_subgroup = tariff.get("subgroup_title")

    if key_device_limit is not None:
        device_limit = key_device_limit
    if key_traffic_limit is not None:
        traffic_limit = key_traffic_limit

    servers = await get_servers(session=session)

    if server_id in servers:
        target_cluster = server_id
    else:
        target_cluster = None
        for cluster_name, cluster_servers in servers.items():
            if any(s.get("server_name") == server_id for s in cluster_servers):
                target_cluster = cluster_name
                break

        if not target_cluster:
            return ValueError(f"No suitable cluster found for server {server_id}")

    await release_session_early(session)

    await renew_key_in_cluster(
        cluster_id=target_cluster,
        email=email,
        client_id=client_id,
        new_expiry_time=expiry_time,
        total_gb=traffic_limit,
        session=session,
        hwid_device_limit=device_limit,
        reset_traffic=False,
        target_subgroup=key_subgroup,
        old_subgroup=key_subgroup,
        plan=tariff_id,
    )

    await update_key_expiry(session, client_id, expiry_time)
    return None


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_edit_config"),
    IsAdminFilter(),
)
async def handle_edit_config_start(
    callback_query: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    state: FSMContext,
    session: AsyncSession,
):
    key_ref = str(callback_data.data)
    tg_id = callback_data.tg_id

    key_obj = await _resolve_callback_key(session, tg_id, key_ref)

    if not key_obj:
        await callback_query.message.edit_text("❌ Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        return

    email = key_obj.email

    if not key_obj.tariff_id:
        await callback_query.message.edit_text(
            "❌ У ключа не назначен тариф.",
            reply_markup=build_key_edit_kb(key_obj.__dict__, email),
        )
        return

    tariff = await get_tariff_by_id(session, key_obj.tariff_id)
    if not tariff or not tariff.get("configurable"):
        await callback_query.message.edit_text(
            "❌ Тариф не поддерживает конфигурацию.",
            reply_markup=build_key_edit_kb(key_obj.__dict__, email),
        )
        return

    base_devices = key_obj.selected_device_limit or tariff.get("device_limit") or 1
    current_devices = key_obj.current_device_limit or base_devices
    extra_devices = max(0, current_devices - base_devices)

    base_traffic = key_obj.selected_traffic_limit
    current_traffic = key_obj.current_traffic_limit
    extra_traffic = max(0, (current_traffic or 0) - (base_traffic or 0)) if current_traffic and base_traffic else 0

    await state.set_state(UserEditorState.config_menu)
    await state.update_data(
        email=email,
        key_ref=key_ref,
        tg_id=tg_id,
        tariff_id=key_obj.tariff_id,
        cfg_base_devices=base_devices,
        cfg_extra_devices=extra_devices,
        cfg_base_traffic=base_traffic,
        cfg_extra_traffic=extra_traffic,
    )

    await render_config_menu(callback_query, state, session)


async def render_config_menu(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    email = data.get("email")
    key_ref = data.get("key_ref")
    tg_id = data.get("tg_id")
    tariff_id = data.get("tariff_id")

    tariff = await get_tariff_by_id(session, tariff_id)
    if not tariff:
        await callback_query.message.edit_text("❌ Тариф не найден.")
        await state.clear()
        return

    base_devices = data.get("cfg_base_devices") or 1
    extra_devices = data.get("cfg_extra_devices") or 0
    base_traffic = data.get("cfg_base_traffic")
    extra_traffic = data.get("cfg_extra_traffic") or 0

    traffic_to_show = base_traffic
    if traffic_to_show is None and email:
        key_obj = await get_key_by_email(session, email)
        if key_obj:
            traffic_to_show = key_obj.selected_traffic_limit or key_obj.current_traffic_limit
    if traffic_to_show is None and tariff:
        raw = tariff.get("traffic_limit")
        if raw is not None:
            try:
                val = int(raw)
                if val > 0:
                    traffic_to_show = val
            except (TypeError, ValueError):
                pass

    text = (
        f"<b>⚙️ Конфигурация ключа</b>\n\n"
        f"🔑 <b>Ключ:</b> <code>{email}</code>\n"
        f"📦 <b>Тариф:</b> {tariff.get('name')}\n\n"
    )

    extra_dev_str = f" + {extra_devices} (докуплено)" if extra_devices > 0 else ""
    text += f"📱 <b>Устройства:</b> {base_devices}{extra_dev_str}\n"

    if traffic_to_show:
        extra_traf_str = f" + {extra_traffic} ГБ (докуплено)" if extra_traffic > 0 else ""
        text += f"📊 <b>Трафик:</b> {traffic_to_show} ГБ{extra_traf_str}\n"
    else:
        text += "📊 <b>Трафик:</b> безлимит\n"

    text += "\n<i>Выберите что редактировать:</i>"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📦 Тариф (база)", callback_data="cfg_edit_base"),
        InlineKeyboardButton(text="➕ Докупка", callback_data="cfg_edit_addon"),
    )
    builder.row(InlineKeyboardButton(text="💾 Сохранить", callback_data="cfg_save"))
    builder.row(
        InlineKeyboardButton(
            text=BACK,
            callback_data=AdminUserEditorCallback(action="users_key_edit", data=key_ref, tg_id=tg_id).pack(),
        )
    )

    await state.set_state(UserEditorState.config_menu)
    await callback_query.message.edit_text(text=text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "cfg_edit_base", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_edit_base(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    tariff = await get_tariff_by_id(session, data.get("tariff_id"))
    device_options = tariff.get("device_options") or [] if tariff else []
    traffic_options = tariff.get("traffic_options_gb") or [] if tariff else []

    builder = InlineKeyboardBuilder()
    if device_options:
        builder.row(InlineKeyboardButton(text="📱 Устройства", callback_data="cfg_base_devices"))
    if traffic_options:
        builder.row(InlineKeyboardButton(text="📊 Трафик", callback_data="cfg_base_traffic"))
    builder.row(InlineKeyboardButton(text=BACK, callback_data="cfg_back_menu"))

    await callback_query.message.edit_text(
        "<b>📦 Редактирование базы тарифа</b>\n\nВыберите параметр:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "cfg_edit_addon", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_edit_addon(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    tariff = await get_tariff_by_id(session, data.get("tariff_id"))
    device_options = tariff.get("device_options") or [] if tariff else []
    traffic_options = tariff.get("traffic_options_gb") or [] if tariff else []

    builder = InlineKeyboardBuilder()
    if device_options:
        builder.row(InlineKeyboardButton(text="📱 Устройства", callback_data="cfg_addon_devices"))
    if traffic_options:
        builder.row(InlineKeyboardButton(text="📊 Трафик", callback_data="cfg_addon_traffic"))
    builder.row(InlineKeyboardButton(text=BACK, callback_data="cfg_back_menu"))

    await callback_query.message.edit_text(
        "<b>➕ Редактирование докупки</b>\n\nВыберите параметр:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "cfg_back_menu", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_back_menu(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    await render_config_menu(callback_query, state, session)


@router.callback_query(F.data == "cfg_base_devices", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_base_devices(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    tariff = await get_tariff_by_id(session, data.get("tariff_id"))
    device_options = tariff.get("device_options") or [] if tariff else []
    base_devices = data.get("cfg_base_devices") or 1

    builder = InlineKeyboardBuilder()
    for opt in sorted(device_options):
        mark = " ✅" if int(opt) == int(base_devices) else ""
        builder.button(text=f"{opt} устр.{mark}", callback_data=f"cfg_set_base_dev:{opt}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text=BACK, callback_data="cfg_back_menu"))

    await state.set_state(UserEditorState.config_select_base)
    await state.update_data(cfg_param="devices")
    await callback_query.message.edit_text(
        "<b>📱 Выберите базу устройств:</b>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "cfg_base_traffic", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_base_traffic(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    tariff = await get_tariff_by_id(session, data.get("tariff_id"))
    traffic_options = tariff.get("traffic_options_gb") or [] if tariff else []
    base_traffic = data.get("cfg_base_traffic")

    builder = InlineKeyboardBuilder()
    for opt in sorted(traffic_options):
        is_sel = (base_traffic is None and opt == 0) or (base_traffic is not None and int(opt) == int(base_traffic))
        mark = " ✅" if is_sel else ""
        label = "безлимит" if opt == 0 else f"{opt} ГБ"
        builder.button(text=f"{label}{mark}", callback_data=f"cfg_set_base_traf:{opt}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text=BACK, callback_data="cfg_back_menu"))

    await state.set_state(UserEditorState.config_select_base)
    await state.update_data(cfg_param="traffic")
    await callback_query.message.edit_text(
        "<b>📊 Выберите базу трафика:</b>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("cfg_set_base_dev:"), UserEditorState.config_select_base, IsAdminFilter())
async def handle_cfg_set_base_dev(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    base_devices = int(callback_query.data.split(":")[1])
    await state.update_data(cfg_base_devices=base_devices)
    await callback_query.answer(f"✅ База устройств: {base_devices}")
    await state.set_state(UserEditorState.config_menu)
    await render_config_menu(callback_query, state, session)


@router.callback_query(F.data.startswith("cfg_set_base_traf:"), UserEditorState.config_select_base, IsAdminFilter())
async def handle_cfg_set_base_traf(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    traffic_gb = int(callback_query.data.split(":")[1])
    await state.update_data(cfg_base_traffic=traffic_gb if traffic_gb > 0 else None)
    label = "безлимит" if traffic_gb == 0 else f"{traffic_gb} ГБ"
    await callback_query.answer(f"✅ База трафика: {label}")
    await state.set_state(UserEditorState.config_menu)
    await render_config_menu(callback_query, state, session)


@router.callback_query(F.data == "cfg_addon_devices", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_addon_devices(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    extra_devices = data.get("cfg_extra_devices") or 0

    await state.set_state(UserEditorState.config_input_addon)
    await state.update_data(cfg_param="devices")

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="cfg_cancel_input"))

    await callback_query.message.edit_text(
        f"<b>📱 Докупка устройств</b>\n\n"
        f"Текущее значение: <b>{extra_devices}</b>\n\n"
        f"Введите новое количество докупленных устройств (число):",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "cfg_addon_traffic", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_addon_traffic(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    extra_traffic = data.get("cfg_extra_traffic") or 0

    await state.set_state(UserEditorState.config_input_addon)
    await state.update_data(cfg_param="traffic")

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="cfg_cancel_input"))

    await callback_query.message.edit_text(
        f"<b>📊 Докупка трафика</b>\n\n"
        f"Текущее значение: <b>{extra_traffic} ГБ</b>\n\n"
        f"Введите новое количество докупленного трафика в ГБ (число):",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "cfg_cancel_input", UserEditorState.config_input_addon, IsAdminFilter())
async def handle_cfg_cancel_input(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.set_state(UserEditorState.config_menu)
    await render_config_menu(callback_query, state, session)


@router.message(UserEditorState.config_input_addon, IsAdminFilter())
async def handle_cfg_input_addon(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    param = data.get("cfg_param")
    email = data.get("email")
    key_ref = data.get("key_ref")
    tg_id = data.get("tg_id")
    tariff_id = data.get("tariff_id")

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Введите корректное число.")
        return

    value = int(message.text)
    if value < 0:
        await message.answer("❌ Значение не может быть отрицательным.")
        return

    if param == "devices":
        await state.update_data(cfg_extra_devices=value)
    else:
        await state.update_data(cfg_extra_traffic=value)

    await state.set_state(UserEditorState.config_menu)

    data = await state.get_data()
    tariff = await get_tariff_by_id(session, tariff_id)

    base_devices = data.get("cfg_base_devices") or 1
    extra_devices = data.get("cfg_extra_devices") or 0
    base_traffic = data.get("cfg_base_traffic")
    extra_traffic = data.get("cfg_extra_traffic") or 0

    text = (
        f"<b>⚙️ Конфигурация ключа</b>\n\n"
        f"🔑 <b>Ключ:</b> <code>{email}</code>\n"
        f"📦 <b>Тариф:</b> {tariff.get('name') if tariff else '—'}\n\n"
    )

    extra_dev_str = f" + {extra_devices} (докуплено)" if extra_devices > 0 else ""
    text += f"📱 <b>Устройства:</b> {base_devices}{extra_dev_str}\n"

    if base_traffic:
        extra_traf_str = f" + {extra_traffic} ГБ (докуплено)" if extra_traffic > 0 else ""
        text += f"📊 <b>Трафик:</b> {base_traffic} ГБ{extra_traf_str}\n"
    else:
        text += "📊 <b>Трафик:</b> безлимит\n"

    text += "\n<i>Выберите что редактировать:</i>"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📦 Тариф (база)", callback_data="cfg_edit_base"),
        InlineKeyboardButton(text="➕ Докупка", callback_data="cfg_edit_addon"),
    )
    builder.row(InlineKeyboardButton(text="💾 Сохранить", callback_data="cfg_save"))
    builder.row(
        InlineKeyboardButton(
            text=BACK,
            callback_data=AdminUserEditorCallback(action="users_key_edit", data=key_ref, tg_id=tg_id).pack(),
        )
    )

    await message.answer(text=text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "cfg_save", UserEditorState.config_menu, IsAdminFilter())
async def handle_cfg_save(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    email = data.get("email")
    tg_id = data.get("tg_id")
    tariff_id = data.get("tariff_id")

    base_devices = data.get("cfg_base_devices") or 1
    extra_devices = data.get("cfg_extra_devices") or 0
    total_devices = base_devices + extra_devices

    base_traffic = data.get("cfg_base_traffic")
    extra_traffic = data.get("cfg_extra_traffic") or 0
    total_traffic = (base_traffic + extra_traffic) if base_traffic else None

    tariff = await get_tariff_by_id(session, tariff_id)
    selected_price = None
    if tariff:
        base_price = tariff.get("price_rub") or 0

        device_step = tariff.get("device_step_rub") or 0
        tariff_base_devices = tariff.get("device_limit") or 1
        extra_base_devices = max(0, base_devices - tariff_base_devices)
        devices_extra_price = extra_base_devices * device_step

        traffic_step = tariff.get("traffic_step_rub") or 0
        tariff_base_traffic = tariff.get("traffic_limit") or 0
        extra_base_traffic = max(0, (base_traffic or 0) - tariff_base_traffic) if base_traffic else 0
        traffic_extra_price = extra_base_traffic * traffic_step

        selected_price = base_price + devices_extra_price + traffic_extra_price

    key_obj = await get_key_by_email(session, email)

    if not key_obj:
        await callback_query.message.edit_text("❌ Ключ не найден.", reply_markup=build_editor_kb(tg_id))
        await state.clear()
        return

    try:
        await release_session_early(session)
        await renew_key_in_cluster(
            cluster_id=key_obj.server_id,
            email=email,
            client_id=key_obj.client_id,
            new_expiry_time=key_obj.expiry_time,
            total_gb=total_traffic or 0,
            session=session,
            hwid_device_limit=total_devices,
            reset_traffic=False,
            plan=tariff_id,
        )

        await save_admin_key_config(
            session,
            email=email,
            base_devices=base_devices,
            total_devices=total_devices,
            base_traffic=base_traffic,
            total_traffic=total_traffic,
            selected_price=selected_price,
        )

        await state.clear()
        await callback_query.answer("✅ Конфигурация сохранена", show_alert=True)

        callback_data_back = AdminUserEditorCallback(action="users_key_edit", data=email, tg_id=tg_id)
        await handle_key_edit(
            callback_query=callback_query,
            callback_data=callback_data_back,
            session=session,
            update=False,
        )

    except Exception as e:
        logger.error(f"[EditConfig] Ошибка при сохранении конфигурации: {e}")
        await callback_query.message.edit_text(
            "❌ Не удалось сохранить конфигурацию. Попробуйте позже.",
            reply_markup=build_editor_kb(tg_id),
        )
        await state.clear()


@router.callback_query(F.data == "cfg_back_menu", IsAdminFilter())
async def handle_cfg_back_menu_any(callback_query: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.set_state(UserEditorState.config_menu)
    await render_config_menu(callback_query, state, session)
