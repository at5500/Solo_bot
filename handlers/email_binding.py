"""Bot-side «Привязать почту» flow with OTP verification (audit F-NEW-tg-03).

UPSTREAM-MERGE NOTE
-------------------
This file was rewritten in commit 528752a1 to close audit F-NEW-tg-03
(email squat — the bot used to attach any email without verification).
On the next merge with upstream-https/dev: if Vladless landed OTP
verification himself, prefer his version — it will integrate better
with the obfuscated module ecosystem and our fork here becomes
redundant. If upstream did not touch this file, keep the current
implementation.


Mirrors the webapp `/auth/link-email/send-code` + `/auth/link-email/confirm`
pair so the bot can't be used to squat an email the attacker doesn't own.

State machine:

1. ``waiting_for_email`` — user sent the address; we run collision checks,
   send a 6-digit OTP via SMTP and switch state to ``waiting_for_code``.
   No DB write yet.
2. ``waiting_for_code`` — user typed the code; we verify against Redis,
   re-check collision (a parallel flow could've touched the row), and
   only then write ``identity.email``.

Same Redis keys / cooldowns as webapp (`utils/web_email_link_code`), so
the per-email limits are shared between both surfaces — flood through
bot doesn't burn the webapp budget separately.
"""

import re
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.settings.web_config import is_email_binding_enabled
from database.identities import attach_email, get_identity_by_email, get_or_create_identity_for_tg
from handlers.buttons import BACK
from handlers.utils import edit_or_send_message
from logger import logger
from mail import send_email_link_code_email, smtp_configured
from utils import web_email_link_code as email_link_code


router = Router(name="email_binding")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CODE_RE = re.compile(r"^\d{6}$")


class EmailBindingState(StatesGroup):
    waiting_for_email = State()
    waiting_for_code = State()


def _back_keyboard():
    """Returns the inline keyboard with a single «Назад» button — same
    shape every step of the flow uses."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=BACK, callback_data="profile"))
    return builder.as_markup()


@router.callback_query(F.data == "bind_email")
async def prompt_email(callback: CallbackQuery, state: FSMContext, session) -> None:
    """First step — show the prompt and switch into ``waiting_for_email``.
    Clears any leftover state from a previous aborted run so re-entering
    the flow always starts fresh."""
    if not is_email_binding_enabled():
        await callback.answer("Привязка почты отключена", show_alert=True)
        return

    identity = await get_or_create_identity_for_tg(session, callback.from_user.id)
    if identity.email:
        await callback.answer("Почта уже привязана", show_alert=True)
        return

    await state.clear()
    await edit_or_send_message(
        target_message=callback.message,
        text=(
            "📧 <b>Привязка почты</b>\n\n"
            "Укажите email — он понадобится для входа на сайт, "
            "если возникнут проблемы с Telegram.\n\n"
            "Отправьте адрес сообщением."
        ),
        reply_markup=_back_keyboard(),
    )
    await state.set_state(EmailBindingState.waiting_for_email)
    await callback.answer()


@router.message(EmailBindingState.waiting_for_email)
async def receive_email(message: Message, state: FSMContext, session) -> None:
    """Validates the email, runs collision checks, sends an OTP and
    switches into ``waiting_for_code``. The actual attach happens only
    after the user types the code back — without this gate, an attacker
    could squat any email not yet in our DB and intercept the victim's
    future login (audit F-NEW-tg-03)."""
    raw = (message.text or "").strip().lower()
    if not EMAIL_RE.match(raw) or len(raw) > 255:
        await message.answer("❌ Неверный формат email. Попробуйте ещё раз.")
        return

    email_norm = email_link_code.normalize_email(raw)

    existing = await get_identity_by_email(session, email_norm)
    identity = await get_or_create_identity_for_tg(session, message.from_user.id)
    if existing and existing.id != identity.id:
        # Refuse only when another LIVE Telegram account owns this email.
        # An email-only leftover (existing.tg_id is None — e.g. a web account
        # whose key was later deleted) is mergeable: fall through and send the
        # OTP. The actual merge happens in receive_code via attach_email,
        # after the user proves ownership with the code — so the F-NEW-tg-03
        # anti-squat property is preserved. Mirrors webapp
        # /auth/link-email/send-code.
        if existing.tg_id is not None and int(existing.tg_id) != int(message.from_user.id):
            await message.answer("❌ Этот email уже занят другим пользователем.")
            return
        logger.info(
            "email_binding: mergeable email collision tg_id=%s email=%s leftover identity_id=%s",
            message.from_user.id,
            email_norm,
            existing.id,
        )

    if not smtp_configured():
        await message.answer(
            "❌ Отправка кодов на email сейчас не настроена. "
            "Обратитесь в поддержку."
        )
        return

    if not await email_link_code.redis_ready():
        await message.answer("❌ Сервис временно недоступен. Попробуйте позже.")
        return

    if not await email_link_code.try_consume_email_send_budget(email_norm):
        await message.answer(
            "❌ Слишком много запросов для этого адреса. Попробуйте позже."
        )
        return
    if not await email_link_code.try_acquire_cooldown(email_norm):
        await message.answer(
            "⏱ Код уже отправлен. Подождите перед повторной отправкой."
        )
        return

    code = "".join(secrets.choice("0123456789") for _ in range(6))
    if not await email_link_code.store_code(email_norm, code):
        await email_link_code.release_cooldown(email_norm)
        await message.answer("❌ Не удалось сохранить код. Попробуйте позже.")
        return

    try:
        await send_email_link_code_email(email_norm, code)
    except Exception as exc:
        logger.warning(
            "email_binding: send code failed for %s: %s",
            email_norm,
            type(exc).__name__,
        )
        await email_link_code.release_cooldown(email_norm)
        await email_link_code.delete_code(email_norm)
        await message.answer("❌ Не удалось отправить письмо. Попробуйте позже.")
        return

    await state.update_data(pending_email=email_norm)
    await state.set_state(EmailBindingState.waiting_for_code)
    await message.answer(
        f"📨 Код подтверждения отправлен на <code>{email_norm}</code>.\n\n"
        "Введите 6-значный код из письма.",
        reply_markup=_back_keyboard(),
    )


@router.message(EmailBindingState.waiting_for_code)
async def receive_code(message: Message, state: FSMContext, session) -> None:
    """Verifies the OTP and writes the email to the identity. Re-checks
    collision after verification — between send-code and confirm a
    parallel flow could've touched the row."""
    code = (message.text or "").strip()
    if not CODE_RE.match(code):
        await message.answer("❌ Неверный формат кода. Введите 6 цифр.")
        return

    data = await state.get_data()
    email_norm = (data.get("pending_email") or "").strip().lower()
    if not email_norm:
        await state.clear()
        await message.answer("❌ Сессия истекла. Начните заново.")
        return

    # Without this guard a Redis outage between send-code and confirm
    # would make ``verify_and_consume_code`` silently return False
    # (cache_get → None) and the user would see «Неверный код»
    # forever instead of a transient «недоступен» he can react to.
    if not await email_link_code.redis_ready():
        await state.clear()
        await message.answer(
            "❌ Сервис временно недоступен. Попробуйте позже."
        )
        return

    if not await email_link_code.try_consume_email_verify_budget(email_norm):
        await state.clear()
        await message.answer("❌ Слишком много попыток. Запросите новый код.")
        return

    if not await email_link_code.verify_and_consume_code(email_norm, code):
        await message.answer(
            "❌ Неверный код или срок действия истёк. Попробуйте ещё раз."
        )
        return

    # Re-check collision after verification — parallel flow could've
    # touched the row between send-code and confirm.
    identity = await get_or_create_identity_for_tg(session, message.from_user.id)
    if identity.email:
        await state.clear()
        await message.answer("ℹ️ Почта уже была привязана.")
        return

    # Attach — merging an email-only leftover account if one holds this
    # address (transfers its keys/balance/trial, deletes the empty
    # identity). Returns None only when the email belongs to a DIFFERENT
    # live account. Mirrors webapp /auth/link-email/confirm.
    result = await attach_email(session, identity.id, email_norm)
    if not result:
        await state.clear()
        await message.answer("❌ Этот email уже занят другим пользователем.")
        return
    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👤 В кабинет", callback_data="profile"))
    await message.answer(
        f"✅ Почта <code>{email_norm}</code> привязана.",
        reply_markup=builder.as_markup(),
    )
