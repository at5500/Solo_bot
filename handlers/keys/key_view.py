import asyncio
import html
import os
import re

from datetime import datetime

import pytz

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    ENABLE_DELETE_KEY_BUTTON,
    HAPP_CRYPTOLINK,
    HWID_RESET_BUTTON,
    QRCODE,
    REMNAWAVE_WEBAPP,
    REMNAWAVE_WEBAPP_OPEN_IN_BROWSER,
    USE_COUNTRY_SELECTION,
)
from core.bootstrap import BUTTONS_CONFIG, MODES_CONFIG
from panels.remnawave_runtime import (
    get_remnawave_profile,
    invalidate_remnawave_profile,
    resolve_remnawave_api_url,
    with_remnawave_api,
)
from database import get_key_details, get_keys
from database.models import Key
from handlers.buttons import (
    ADDONS_BUTTON_DEVICES,
    ADDONS_BUTTON_DEVICES_TRAFFIC,
    ADDONS_BUTTON_TRAFFIC,
    ALIAS,
    BACK,
    CHANGE_LOCATION,
    CONNECT_DEVICE,
    DELETE,
    HWID_BUTTON,
    MAIN_MENU,
    QR,
    RENEW_KEY,
    ROUTER_BUTTON,
    TV_BUTTON,
)
from database import get_vless_enabled_batch
from handlers.tariffs.tariff_display import GB, get_key_tariff_addons_state
from handlers.texts import (
    DAYS_LEFT_MESSAGE,
    FROZEN_SUBSCRIPTION_MSG,
    KEYS_FOOTER,
    KEYS_HEADER,
    NO_SUBSCRIPTIONS_MSG,
    RENAME_KEY_PROMPT,
    key_message,
)
from handlers.keys.utils import build_key_callback, build_key_ref, key_owned_by_user, resolve_key
from handlers.utils import (
    edit_or_send_message,
    format_days,
    format_hours,
    format_minutes,
    get_russian_month,
    is_full_remnawave_cluster,
    safe_answer_callback,
)
from hooks.hook_buttons import insert_hook_buttons
from hooks.processors import (
    process_after_hwid_reset,
    process_remnawave_webapp_override,
    process_view_key_menu,
)
from logger import logger
router = Router()
moscow_tz = pytz.timezone("Europe/Moscow")


class RenameKeyState(StatesGroup):
    waiting_for_new_alias = State()


@router.callback_query(F.data == "view_keys")
@router.message(F.text == "/subs")
async def process_callback_or_message_view_keys(
    callback_query_or_message: Message | CallbackQuery,
    session: AsyncSession,
    page: int = 0,
):
    if isinstance(callback_query_or_message, CallbackQuery):
        target_message = callback_query_or_message.message
    else:
        target_message = callback_query_or_message

    tg_id = callback_query_or_message.from_user.id

    try:
        records = await get_keys(session, tg_id)

        if records and len(records) == 1:
            key_ref = build_key_ref(records[0].client_id, records[0].email)
            image_path = os.path.join("img", "pic_view.jpg")
            await render_key_info(target_message, session, key_ref, image_path)
            return

        inline_keyboard, response_message = await build_keys_response(records, session, page=page)
        image_path = os.path.join("img", "pic_keys.jpg")

        await edit_or_send_message(
            target_message=target_message,
            text=response_message,
            reply_markup=inline_keyboard,
            media_path=image_path,
        )
    except Exception as error:
        error_message = f"Ошибка при получении ключей: {error}"
        await target_message.answer(text=error_message)


@router.callback_query(F.data.startswith("view_keys|"))
async def process_callback_view_keys_paged(
    callback_query: CallbackQuery,
    session: AsyncSession,
):
    parts = callback_query.data.split("|")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    await process_callback_or_message_view_keys(callback_query, session, page=page)


async def build_keys_response(records: list[Key] | None, session: AsyncSession, page: int = 0):
    builder = InlineKeyboardBuilder()

    page_size = 5
    records = sorted(
        records or [],
        key=lambda r: r.created_at or 0,
    )

    total = len(records)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    if total:
        response_message = KEYS_HEADER

        start = page * page_size
        end = start + page_size
        page_records = records[start:end]

        tariff_ids = []
        for record in page_records:
            tid = getattr(record, "tariff_id", None)
            if tid is not None:
                try:
                    tariff_ids.append(int(tid))
                except (TypeError, ValueError):
                    pass
        vless_by_tariff = await get_vless_enabled_batch(session, tariff_ids) if tariff_ids else {}

        for record in page_records:
            alias = record.alias
            email = record.email
            client_id = record.client_id
            expiry_time = record.expiry_time

            key_display = html.escape(alias.strip() if alias else email)

            if expiry_time:
                expiry_date_full = datetime.fromtimestamp(expiry_time / 1000, tz=moscow_tz)
                formatted_date_full = expiry_date_full.strftime("до %d.%m.%y, %H:%M")
            else:
                formatted_date_full = "без срока действия"

            tid = getattr(record, "tariff_id", None)
            is_vless = vless_by_tariff.get(int(tid), False) if tid is not None else False

            icon = "📶" if is_vless else "🔑"

            key_button = InlineKeyboardButton(
                text=f"{icon} {key_display}",
                callback_data=build_key_callback("view_key", client_id, email),
            )
            rename_button = InlineKeyboardButton(
                text=ALIAS,
                callback_data=f"rename_key|{client_id}",
            )
            builder.row(key_button, rename_button)

            response_message += f"• <b>{key_display}</b> ({formatted_date_full})\n"

        response_message += KEYS_FOOTER

        if total_pages > 1:
            nav_row = []

            if page > 0:
                nav_row.append(
                    InlineKeyboardButton(
                        text="⬅️ Пред.",
                        callback_data=f"view_keys|{page - 1}",
                    )
                )

            nav_row.append(
                InlineKeyboardButton(
                    text=f"({page + 1}/{total_pages})",
                    callback_data=" ",
                )
            )

            if page < total_pages - 1:
                nav_row.append(
                    InlineKeyboardButton(
                        text="След. ➡️",
                        callback_data=f"view_keys|{page + 1}",
                    )
                )

            builder.row(*nav_row)
    else:
        response_message = NO_SUBSCRIPTIONS_MSG

    builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))

    return builder.as_markup(), response_message


@router.callback_query(F.data.startswith("rename_key|"))
async def handle_rename_key(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    client_id = callback.data.split("|")[1]
    key_row = (await session.execute(select(Key).where(Key.client_id == client_id))).scalar_one_or_none()
    if not key_row or key_row.tg_id != callback.from_user.id:
        await safe_answer_callback(callback, "Доступ запрещён.", show_alert=True)
        return
    await state.set_state(RenameKeyState.waiting_for_new_alias)
    await state.update_data(client_id=client_id)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=BACK, callback_data="cancel_and_back_to_view_keys"))

    await edit_or_send_message(
        target_message=callback.message,
        text=RENAME_KEY_PROMPT,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "cancel_and_back_to_view_keys")
async def cancel_and_back(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.clear()
    await process_callback_or_message_view_keys(callback, session)


@router.message(F.text, RenameKeyState.waiting_for_new_alias)
async def handle_new_alias_input(message: Message, state: FSMContext, session: AsyncSession):
    alias = message.text.strip()

    if len(alias) > 10:
        await message.answer("❌ Имя слишком длинное. Введите до 10 символов.\nПовторите ввод.")
        return

    if not alias or not re.match(r"^[a-zA-Zа-яА-ЯёЁ0-9@._-]+$", alias):
        await message.answer(
            "❌ Введены недопустимые символы или имя пустое. Используйте только буквы, цифры и @._-\nПовторите ввод."
        )
        return

    data = await state.get_data()
    client_id = data.get("client_id")

    try:
        await session.execute(
            update(Key).where(Key.tg_id == message.chat.id, Key.client_id == client_id).values(alias=alias)
        )
        await session.commit()
    except Exception as error:
        await message.answer("❌ Не удалось переименовать подписку.")
        logger.error(f"Ошибка при обновлении alias: {error}")
    finally:
        await state.clear()

    await process_callback_or_message_view_keys(message, session)


@router.callback_query(F.data.startswith("view_key|"))
async def process_callback_view_key(callback_query: CallbackQuery, session: AsyncSession):
    key_ref = callback_query.data.split("|", 1)[1]
    key_obj = await resolve_key(session, callback_query.from_user.id, key_ref)
    record = await get_key_details(session, key_obj.email) if key_obj else None
    if not key_owned_by_user(record, callback_query.from_user.id):
        await safe_answer_callback(callback_query, "Доступ запрещён.", show_alert=True)
        return
    image_path = os.path.join("img", "pic_view.jpg")
    await render_key_info(callback_query.message, session, key_ref, image_path)


async def build_key_view_payload(session: AsyncSession, tg_id: int, key_ref_or_email: str):
    key_obj = await resolve_key(session, tg_id, key_ref_or_email)
    key_name = key_obj.email if key_obj else key_ref_or_email
    record = await get_key_details(session, key_name)
    if not record:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))
        return "<b>Информация о подписке не найдена.</b>", builder.as_markup(), False

    db_key = key_obj

    is_frozen = record["is_frozen"]
    client_id = record.get("client_id")
    final_link = record.get("link")

    builder = InlineKeyboardBuilder()

    if is_frozen:
        builder.row(InlineKeyboardButton(text=BACK, callback_data="view_keys"))
        builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))
        return FROZEN_SUBSCRIPTION_MSG, builder.as_markup(), True

    expiry_time = record["expiry_time"]
    server_name = record["server_id"]
    expiry_date = datetime.utcfromtimestamp(expiry_time / 1000)
    now = datetime.utcnow()
    time_left = expiry_date - now

    if time_left.total_seconds() <= 0:
        days_left_message = DAYS_LEFT_MESSAGE
    else:
        total_seconds = int(time_left.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        days_left_message = (
            f"⏳ Осталось: <b>{format_days(days)}</b>, <b>{format_hours(hours)}</b>, <b>{format_minutes(minutes)}</b>"
        )

    formatted_expiry_date = (
        f"{expiry_date.strftime('%d')} {get_russian_month(expiry_date)} {expiry_date.strftime('%Y')} года"
    )

    is_full_task = asyncio.create_task(is_full_remnawave_cluster(server_name, session))

    tariff_name = ""
    subgroup_title = ""
    traffic_limit_gb = 0
    device_limit = 0
    vless_enabled = False
    is_tariff_configurable = False
    addons_devices_enabled = False
    addons_traffic_enabled = False

    if record.get("tariff_id"):
        (
            tariff_name,
            subgroup_title,
            traffic_limit_gb,
            device_limit,
            vless_enabled,
            is_tariff_configurable,
            addons_devices_enabled,
            addons_traffic_enabled,
        ) = await get_key_tariff_addons_state(
            session=session,
            key_record=record,
            db_key=db_key,
        )

    is_full_remnawave = await is_full_task

    hwid_count = 0
    remna_used_gb = None
    if is_full_remnawave and client_id:
        profile = await get_remnawave_profile(session, str(server_name), client_id)
        if profile:
            hwid_count = int(profile.get("hwid_count") or 0)
            remna_used_gb = profile.get("used_gb")
            traffic_limit_bytes_actual = profile.get("traffic_limit_bytes")
            if traffic_limit_bytes_actual is not None:
                try:
                    traffic_limit_bytes_actual = int(traffic_limit_bytes_actual)
                    traffic_limit_gb = int(traffic_limit_bytes_actual / GB) if traffic_limit_bytes_actual > 0 else 0
                except (TypeError, ValueError):
                    pass

    country_selection_enabled = bool(MODES_CONFIG.get("COUNTRY_SELECTION_ENABLED", USE_COUNTRY_SELECTION))
    remnawave_webapp_enabled = bool(MODES_CONFIG.get("REMNAWAVE_WEBAPP_ENABLED", REMNAWAVE_WEBAPP))
    open_in_browser = bool(MODES_CONFIG.get("REMNAWAVE_WEBAPP_OPEN_IN_BROWSER", REMNAWAVE_WEBAPP_OPEN_IN_BROWSER))
    happ_cryptolink_enabled = bool(MODES_CONFIG.get("HAPP_CRYPTOLINK_ENABLED", HAPP_CRYPTOLINK))

    response_message = key_message(
        final_link,
        formatted_expiry_date,
        days_left_message,
        server_name,
        server_name if country_selection_enabled else None,
        hwid_count=hwid_count if device_limit is not None else 0,
        tariff_name=tariff_name,
        traffic_limit=traffic_limit_gb,
        device_limit=device_limit,
        subgroup_title=subgroup_title,
        is_remnawave=is_full_remnawave,
        remna_used_gb=remna_used_gb,
    )

    use_webapp = remnawave_webapp_enabled
    if is_full_remnawave and final_link and remnawave_webapp_enabled and not happ_cryptolink_enabled:
        use_webapp = await process_remnawave_webapp_override(
            remnawave_webapp=remnawave_webapp_enabled,
            final_link=final_link,
            session=session,
        )

    tv_button_enabled = bool(BUTTONS_CONFIG.get("ANDROID_TV_BUTTON_ENABLE"))

    if is_full_remnawave and final_link and use_webapp and not happ_cryptolink_enabled:
        if vless_enabled:
            builder.row(
                InlineKeyboardButton(
                    text=ROUTER_BUTTON,
                    callback_data=build_key_callback("connect_router", client_id, key_name),
                )
            )
        elif open_in_browser:
            builder.row(InlineKeyboardButton(text=CONNECT_DEVICE, url=final_link))
            if tv_button_enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=TV_BUTTON,
                        callback_data=build_key_callback("connect_tv", client_id, key_name),
                    )
                )
        else:
            builder.row(InlineKeyboardButton(text=CONNECT_DEVICE, web_app=WebAppInfo(url=final_link)))
            if tv_button_enabled:
                builder.row(
                    InlineKeyboardButton(
                        text=TV_BUTTON,
                        callback_data=build_key_callback("connect_tv", client_id, key_name),
                    )
                )
    else:
        if vless_enabled:
            builder.row(
                InlineKeyboardButton(
                    text=ROUTER_BUTTON,
                    callback_data=build_key_callback("connect_router", client_id, key_name),
                )
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=CONNECT_DEVICE,
                    callback_data=build_key_callback("connect_device", client_id, key_name),
                )
            )

    builder.row(InlineKeyboardButton(text=RENEW_KEY, callback_data=build_key_callback("renew_key", client_id, key_name)))

    if is_tariff_configurable and (addons_devices_enabled or addons_traffic_enabled):
        if addons_devices_enabled and addons_traffic_enabled:
            addons_text = ADDONS_BUTTON_DEVICES_TRAFFIC
        elif addons_devices_enabled:
            addons_text = ADDONS_BUTTON_DEVICES
        else:
            addons_text = ADDONS_BUTTON_TRAFFIC
        builder.row(InlineKeyboardButton(text=addons_text, callback_data=build_key_callback("key_addons", client_id, key_name)))

    hwid_reset_enabled = bool(BUTTONS_CONFIG.get("HWID_RESET_BUTTON_ENABLE", HWID_RESET_BUTTON))
    qrcode_enabled = bool(BUTTONS_CONFIG.get("QRCODE_BUTTON_ENABLE", QRCODE))
    delete_key_enabled = bool(BUTTONS_CONFIG.get("DELETE_KEY_BUTTON_ENABLE", ENABLE_DELETE_KEY_BUTTON))
    if hwid_reset_enabled and hwid_count > 0:
        builder.row(InlineKeyboardButton(text=HWID_BUTTON, callback_data=build_key_callback("reset_hwid", client_id, key_name)))

    if qrcode_enabled:
        builder.row(InlineKeyboardButton(text=QR, callback_data=build_key_callback("show_qr", client_id, key_name)))

    if delete_key_enabled:
        builder.row(InlineKeyboardButton(text=DELETE, callback_data=build_key_callback("delete_key", client_id, key_name)))

    if country_selection_enabled:
        builder.row(
            InlineKeyboardButton(text=CHANGE_LOCATION, callback_data=build_key_callback("change_location", client_id, key_name))
        )

    builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))

    module_buttons = await process_view_key_menu(key_name=key_name, session=session)
    builder = insert_hook_buttons(builder, module_buttons)

    return response_message, builder.as_markup(), False


async def build_key_view_message(session: AsyncSession, email: str):
    text, reply_markup, _ = await build_key_view_payload(session, 0, email)
    return text, reply_markup


async def render_key_info(message: Message, session: AsyncSession, key_ref_or_email: str, image_path: str):
    text, reply_markup, _ = await build_key_view_payload(session, message.chat.id, key_ref_or_email)
    await edit_or_send_message(
        target_message=message,
        text=text,
        reply_markup=reply_markup,
        media_path=image_path,
    )


@router.callback_query(F.data.startswith("reset_hwid|"))
async def handle_reset_hwid(callback_query: CallbackQuery, session: AsyncSession):
    key_ref = callback_query.data.split("|", 1)[1]
    key_obj = await resolve_key(session, callback_query.from_user.id, key_ref)
    key_name = key_obj.email if key_obj else key_ref
    record = await get_key_details(session, key_name)
    if not record:
        await safe_answer_callback(callback_query, "❌ Ключ не найден.", show_alert=True)
        return
    if not key_owned_by_user(record, callback_query.from_user.id):
        await safe_answer_callback(callback_query, "Доступ запрещён.", show_alert=True)
        return

    client_id = record.get("client_id")
    if not client_id:
        await safe_answer_callback(callback_query, "❌ У ключа отсутствует client_id.", show_alert=True)
        return

    remna_api_url = await resolve_remnawave_api_url(session, str(record.get("server_id") or ""), fallback_any=True)
    if not remna_api_url:
        await safe_answer_callback(callback_query, "❌ Remnawave-сервер не найден.", show_alert=True)
        return

    async def _reset_devices(api):
        devices = await api.get_user_hwid_devices(client_id)
        if not devices:
            return 0, 0
        deleted_local = 0
        for device in devices:
            if await api.delete_user_hwid_device(client_id, device["hwid"]):
                deleted_local += 1
        return len(devices), deleted_local

    reset_result = await with_remnawave_api(
        session,
        str(record.get("server_id") or ""),
        _reset_devices,
        fallback_any=True,
        timeout_sec=12.0,
    )
    if reset_result is None:
        await safe_answer_callback(callback_query, "❌ Авторизация в Remnawave не удалась.", show_alert=True)
        return

    total, deleted = reset_result
    await invalidate_remnawave_profile(
        session,
        str(record.get("server_id") or ""),
        str(client_id),
        fallback_any=True,
    )
    if total == 0:
        await safe_answer_callback(callback_query, "✅ Устройства не были привязаны.", show_alert=True)
    else:
        await safe_answer_callback(callback_query, f"✅ Устройства сброшены ({deleted})", show_alert=True)

    if await process_after_hwid_reset(
        chat_id=callback_query.from_user.id,
        admin=False,
        session=session,
        key_name=key_name,
    ):
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))
        if callback_query.message.text:
            await callback_query.message.edit_text("✅ Устройства сброшены", reply_markup=builder.as_markup())
        else:
            await callback_query.message.edit_caption(
                caption="✅ Устройства сброшены",
                reply_markup=builder.as_markup(),
            )
        return

    image_path = os.path.join("img", "pic_view.jpg")
    await render_key_info(callback_query.message, session, key_ref, image_path)
