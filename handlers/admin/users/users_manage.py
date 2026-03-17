import pytz

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.formatting import BlockQuote, Bold, Text
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    get_key_details,
    update_trial,
)
from database.models import Admin, Key, ManualBan, Payment, Referral, User
from filters.admin import IsAdminFilter
from handlers.utils import sanitize_key_name
from utils.csv_export import export_referrals_csv

from ..panel.keyboard import (
    AdminPanelCallback,
    build_admin_back_btn,
    build_admin_back_kb,
)
from .keyboard import (
    AdminUserEditorCallback,
    build_editor_kb,
    build_user_edit_kb,
)
from .users_states import UserEditorState


MOSCOW_TZ = pytz.timezone("Europe/Moscow")

router = Router()


@router.callback_query(
    AdminPanelCallback.filter(F.action == "search_user"),
    IsAdminFilter(),
)
async def handle_search_user(callback_query: CallbackQuery, state: FSMContext):
    text = (
        "<b>🔍 Поиск пользователя</b>"
        "\n\n📌 Введите ID, Username или перешлите сообщение пользователя."
        "\n\n🆔 ID - числовой айди"
        "\n📝 Username - юзернейм пользователя"
        "\n\n<i>✉️ Для поиска, вы можете просто переслать сообщение от пользователя.</i>"
    )

    await state.set_state(UserEditorState.waiting_for_user_data)
    await callback_query.message.edit_text(text=text, reply_markup=build_admin_back_kb())


@router.callback_query(
    AdminPanelCallback.filter(F.action == "search_key"),
    IsAdminFilter(),
)
async def handle_search_key(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(UserEditorState.waiting_for_key_name)
    await callback_query.message.edit_text(
        text="🔑 Введите имя ключа для поиска:",
        reply_markup=build_admin_back_kb(),
    )


@router.message(UserEditorState.waiting_for_key_name, IsAdminFilter())
async def handle_key_name_input(message: Message, state: FSMContext, session: AsyncSession):
    kb = build_admin_back_kb()

    if not message.text:
        await message.answer(text="🚫 Пожалуйста, отправьте текстовое сообщение.", reply_markup=kb)
        return

    key_name = sanitize_key_name(message.text)
    key_details = await get_key_details(session, key_name)

    if not key_details:
        await message.answer(
            text="🚫 Пользователь с указанным именем ключа не найден.",
            reply_markup=kb,
        )
        return

    await process_user_search(message, state, session, key_details["tg_id"], actor_tg_id=message.from_user.id)


@router.message(UserEditorState.waiting_for_user_data, IsAdminFilter())
async def handle_user_data_input(message: Message, state: FSMContext, session: AsyncSession):
    kb = build_admin_back_kb()

    if message.forward_from:
        tg_id = message.forward_from.id
        await process_user_search(message, state, session, tg_id, actor_tg_id=message.from_user.id)
        return

    if not message.text:
        await message.answer(text="🚫 Пожалуйста, отправьте текстовое сообщение.", reply_markup=kb)
        return

    if message.text.isdigit():
        tg_id = int(message.text)
    else:
        username = message.text.strip().lstrip("@").replace("https://t.me/", "")

        stmt = (
            select(User.tg_id)
            .where(func.lower(User.username) == func.lower(username))
            .order_by(User.updated_at.desc())
            .limit(1)
        )
        tg_id = (await session.execute(stmt)).scalar_one_or_none()

        if tg_id is None:
            await message.answer(
                text="🚫 Пользователь с указанным Username не найден!",
                reply_markup=kb,
            )
            return

    await process_user_search(message, state, session, tg_id, actor_tg_id=message.from_user.id)


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_send_message"),
    IsAdminFilter(),
)
async def handle_send_message(
    callback_query: types.CallbackQuery,
    callback_data: AdminUserEditorCallback,
    state: FSMContext,
):
    tg_id = callback_data.tg_id

    await callback_query.message.edit_text(
        text=(
            "✉️ Введите текст сообщения, которое вы хотите отправить пользователю:\n\n"
            "Поддерживается только Telegram-форматирование — <b>жирный</b>, <i>курсив</i> и другие стили через редактор Telegram.\n\n"
            "Вы можете отправить:\n"
            "• Только <b>текст</b>\n"
            "• Только <b>картинку</b>\n"
            "• <b>Текст + картинку</b>"
        ),
        reply_markup=build_editor_kb(tg_id),
    )

    await state.update_data(tg_id=tg_id)
    await state.set_state(UserEditorState.waiting_for_message_text)


@router.message(UserEditorState.waiting_for_message_text, IsAdminFilter())
async def handle_message_text_input(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("tg_id")
    text_message = message.html_text or message.text or message.caption or ""
    photo = message.photo[-1].file_id if message.photo else None

    max_len = 1024 if photo else 4096
    if len(text_message) > max_len:
        await message.answer(
            f"⚠️ Сообщение слишком длинное.\nМаксимум: <b>{max_len}</b> символов, сейчас: <b>{len(text_message)}</b>.",
            reply_markup=build_editor_kb(tg_id),
        )
        await state.clear()
        return

    await state.update_data(text=text_message, photo=photo)
    await state.set_state(UserEditorState.preview_message)

    if photo:
        await message.answer_photo(photo=photo, caption=text_message, parse_mode="HTML")
    else:
        await message.answer(text=text_message, parse_mode="HTML")

    await message.answer(
        "👀 Это предпросмотр сообщения. Отправить?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📤 Отправить", callback_data="send_user_message"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_user_message"),
                ]
            ]
        ),
    )


@router.callback_query(
    F.data == "send_user_message",
    IsAdminFilter(),
    UserEditorState.preview_message,
)
async def handle_send_user_message(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("tg_id")
    text_message = data.get("text")
    photo = data.get("photo")

    try:
        if photo:
            await callback_query.bot.send_photo(
                chat_id=tg_id,
                photo=photo,
                caption=text_message,
                parse_mode="HTML",
            )
        else:
            await callback_query.bot.send_message(
                chat_id=tg_id,
                text=text_message,
                parse_mode="HTML",
            )
        await callback_query.message.edit_text(
            text="✅ Сообщение успешно отправлено.",
            reply_markup=build_editor_kb(tg_id),
        )
    except Exception as e:
        await callback_query.message.edit_text(
            text=f"❌ Не удалось отправить сообщение: {e}",
            reply_markup=build_editor_kb(tg_id),
        )
    await state.clear()


@router.callback_query(
    F.data == "cancel_user_message",
    IsAdminFilter(),
    UserEditorState.preview_message,
)
async def handle_cancel_user_message(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("tg_id")
    await callback_query.message.edit_text(
        text="🚫 Отправка сообщения отменена.",
        reply_markup=build_editor_kb(tg_id),
    )
    await state.clear()


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_trial_restore"),
    IsAdminFilter(),
)
async def handle_trial_restore(
    callback_query: types.CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    tg_id = callback_data.tg_id

    await update_trial(session, tg_id, 0)
    await callback_query.message.edit_text(
        text="✅ Триал успешно восстановлен!",
        reply_markup=build_editor_kb(tg_id),
    )


@router.callback_query(
    AdminPanelCallback.filter(F.action == "restore_trials"),
    IsAdminFilter(),
)
async def confirm_restore_trials(callback_query: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить",
        callback_data=AdminPanelCallback(action="confirm_restore_trials").pack(),
    )
    builder.row(build_admin_back_btn())

    await callback_query.message.edit_text(
        text=(
            "⚠ Вы уверены, что хотите восстановить пробники для пользователей? \n\n"
            "Только для тех, у кого нет подписок (активных или истекших)!"
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(
    AdminPanelCallback.filter(F.action == "confirm_restore_trials"),
    IsAdminFilter(),
)
async def restore_trials(callback_query: types.CallbackQuery, session: AsyncSession):
    stmt = (
        update(User)
        .where(
            User.trial == 1,
            ~exists(select(Key.tg_id).where(Key.tg_id == User.tg_id)),
        )
        .values(trial=0)
    )
    result = await session.execute(stmt)
    await session.commit()

    builder = InlineKeyboardBuilder()
    builder.row(build_admin_back_btn())

    await callback_query.message.edit_text(
        text=f"✅ Пробники восстановлены для {result.rowcount} пользователей без подписок.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_export_referrals"),
    IsAdminFilter(),
)
async def handle_users_export_referrals(
    callback_query: types.CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
):
    referrer_tg_id = callback_data.tg_id

    csv_file = await export_referrals_csv(referrer_tg_id, session)

    if csv_file is None:
        await callback_query.message.answer("У пользователя нет рефералов.")
        return

    await callback_query.message.answer_document(
        document=csv_file,
        caption=f"Список рефералов для пользователя {referrer_tg_id}.",
    )


async def process_user_search(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
    tg_id: int,
    edit: bool = False,
    actor_tg_id: int | None = None,
) -> None:
    await state.clear()

    stmt_user = select(User.username, User.balance, User.created_at, User.updated_at, User.trial).where(
        User.tg_id == tg_id
    )
    result_user = await session.execute(stmt_user)
    user_data = result_user.first()

    if not user_data:
        await message.answer(
            text="🚫 Пользователь с указанным ID не найден!",
            reply_markup=build_admin_back_kb(),
        )
        return

    username, balance, created_at, updated_at, trial = user_data
    balance = int(balance or 0)
    created_at_str = created_at.replace(tzinfo=pytz.UTC).astimezone(MOSCOW_TZ).strftime("%H:%M:%S %d.%m.%Y")
    updated_at_str = updated_at.replace(tzinfo=pytz.UTC).astimezone(MOSCOW_TZ).strftime("%H:%M:%S %d.%m.%Y")

    trial_status = "использован" if trial == 1 else "доступен"

    stmt_ref_count = select(func.count()).select_from(Referral).where(Referral.referrer_tg_id == tg_id)
    result_ref = await session.execute(stmt_ref_count)
    referral_count = result_ref.scalar_one()

    stmt_ref_by = select(Referral.referrer_tg_id).where(Referral.referred_tg_id == tg_id).limit(1)
    result_ref_by = await session.execute(stmt_ref_by)
    referrer_tg_id = result_ref_by.scalar_one_or_none()

    referrer_text = None
    if referrer_tg_id:
        stmt_referrer = select(User.username).where(User.tg_id == referrer_tg_id)
        result_referrer = await session.execute(stmt_referrer)
        ref_username = result_referrer.scalar_one_or_none()
        if ref_username:
            referrer_text = f"🤝 Пригласил: @{ref_username} ({referrer_tg_id})"
        else:
            referrer_text = f"🤝 Пригласил: {referrer_tg_id}"

    stmt = select(
        func.count(Payment.id),
        func.coalesce(func.sum(Payment.amount), 0),
    ).where(
        Payment.status == "success",
        Payment.tg_id == tg_id,
        Payment.payment_system != "admin",
    )
    result = await session.execute(stmt)
    topups_amount, topups_sum = result.one_or_none() or (0, 0)

    stmt_keys = select(Key).where(Key.tg_id == tg_id)
    result_keys = await session.execute(stmt_keys)
    key_records = result_keys.scalars().all()

    stmt_ban = select(ManualBan).where(ManualBan.tg_id == tg_id).limit(1)
    result_ban = await session.execute(stmt_ban)
    ban_record = result_ban.scalar_one_or_none()

    ban_info = None
    ban_reason = None
    is_banned = ban_record is not None
    if ban_record:
        if ban_record.reason == "shadow":
            ban_info = "🚫 Блокировка: 👻 Теневой бан"
        elif ban_record.until:
            until_str = ban_record.until.replace(tzinfo=pytz.UTC).astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
            ban_info = f"🚫 Блокировка: до {until_str}"
            if ban_record.reason:
                ban_reason = ban_record.reason
        else:
            ban_info = "🚫 Блокировка: навсегда"
            if ban_record.reason:
                ban_reason = ban_record.reason

    body = Text(
        f"🆔 ID: {tg_id}\n",
        f"📄 Логин: @{username}\n" if username else "📄 Логин: —\n",
        f"📅 Дата регистрации: {created_at_str}\n",
        f"🏃 Дата активности: {updated_at_str}\n",
        f"💰 Баланс: {balance} Р.\n",
        f"💳 Пополнения: {topups_sum} Р. ({topups_amount} шт.)\n",
        f"👥 Количество рефералов: {referral_count}\n",
        f"🎁 Триал: {trial_status}\n",
    )

    if referrer_text:
        body += Text(referrer_text, "\n")

    if ban_info:
        body += Text(ban_info, "\n")
        if ban_reason:
            body += Text(f"📝 Причина: {ban_reason}\n")

    text_builder = Text(Bold("📊 Информация о пользователе"), "\n\n", BlockQuote(body))

    text = text_builder.as_html()

    effective_actor_tg_id = actor_tg_id or (message.from_user.id if message.from_user else None)
    admin_role = None
    if effective_actor_tg_id is not None:
        admin_role = await session.scalar(select(Admin.role).where(Admin.tg_id == effective_actor_tg_id))

    kb = await build_user_edit_kb(tg_id, key_records, is_banned=is_banned, admin_role=admin_role)

    if edit:
        try:
            await message.edit_text(text=text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass
    else:
        await message.answer(text=text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(
    AdminUserEditorCallback.filter(F.action == "users_editor"),
    IsAdminFilter(),
)
async def handle_users_editor(
    callback: CallbackQuery,
    callback_data: AdminUserEditorCallback,
    session: AsyncSession,
    state: FSMContext,
):
    await process_user_search(
        callback.message,
        state=state,
        session=session,
        tg_id=callback_data.tg_id,
        edit=callback_data.edit,
        actor_tg_id=callback.from_user.id,
    )
