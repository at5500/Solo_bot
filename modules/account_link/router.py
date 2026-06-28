"""Bot handler for `/start link_<token>` — consent-gated attach of the
caller's Telegram to a web-side identity.

A web user mints a token via ``POST /api/auth/link-tokens/telegram`` and
shares the bot deeplink. Anyone who clicks it lands here. We **do not**
attach immediately — that would let an attacker «kidnap» a victim's
Telegram into their own web account by simply sending the URL ([audit
F-NEW-tg-01]).

Flow:

1. Click → ``handle_account_link`` peeks the token (read-only) and shows
   a confirmation message naming the web identity behind the link
   (masked email + registration date) with explicit ✅/❌ buttons.
2. ``link_confirm:<consent_id>`` callback → consume the token and run
   :func:`database.identities.attach_telegram`. Onboarding for brand-new
   bot users runs here, after the user has explicitly approved.
3. ``link_reject:<consent_id>`` callback → drop the token entirely so a
   leaked URL can't be re-used later.

The consent envelope is stored in Redis under a short, random id with a
5-minute TTL — the original token sits there too, intact and unattach'd
until either button is pressed.

The matching token producer is ``POST /api/auth/link-tokens/telegram``.
Token storage and lifecycle live in ``utils/identity_link.py``.
"""

import json
import secrets
import string

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.redis_cache import cache_delete, cache_get, cache_set
from database import async_session_maker
from database import identities as idb
from database.users import check_user_exists
from logger import logger
from utils.identity_link import (
    LINK_KIND_TG,
    consume_link_token,
    drop_link_token,
    peek_link_token,
)
from utils.photo_cache import invalidate_photo_cache


router = Router(name="account_link")

_PREFIX = "link_"
_CONSENT_PREFIX = "link_consent:"
_CONSENT_TTL_SEC = 5 * 60  # User has 5 minutes to accept or reject.
_CONSENT_ID_LEN = 12
_CONSENT_ALPHABET = string.ascii_letters + string.digits


def _mask_email(email: str | None) -> str:
    """Renders ``i***e@example.com`` for a recognizable but redacted view.

    Args:
        email: Raw email or ``None``.

    Returns:
        Masked string safe to surface in a TG message. ``"—"`` when the
        input doesn't look like an email.
    """
    if not email or "@" not in email:
        return "—"
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    if len(local) <= 3:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def _generate_consent_id() -> str:
    """Returns a short random id used as the callback-data discriminator
    for the consent envelope in Redis."""
    return "".join(secrets.choice(_CONSENT_ALPHABET) for _ in range(_CONSENT_ID_LEN))


async def _store_consent(token: str, remote_id: str, tg_id: int) -> str:
    """Stashes a pending consent in Redis under a fresh consent id.

    Args:
        token: The original ``link_<token>`` payload — kept intact in
            the envelope so we can consume it atomically on approval.
        remote_id: The web identity behind the token (the side that
            wants the caller's Telegram).
        tg_id: The caller's Telegram user id — verified again on the
            callback so a forwarded inline message can't be clicked by
            a different user to steal the consent.

    Returns:
        The fresh consent id (used as the callback-data tail).
    """
    cid = _generate_consent_id()
    payload = json.dumps({"token": token, "remote_id": remote_id, "tg_id": int(tg_id)})
    await cache_set(_CONSENT_PREFIX + cid, payload, _CONSENT_TTL_SEC)
    return cid


def _parse_consent(raw: object) -> dict | None:
    """Decodes a raw cache value into a consent envelope, or ``None`` if
    the value is missing / not a JSON dict."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


async def _peek_consent(cid: str) -> dict | None:
    """Reads a consent envelope *without* deleting it.

    Used by both callbacks to verify ``tg_id`` *before* the one-shot
    pop — otherwise a clicked button on a forwarded inline message
    would burn the original recipient's consent (DoS on the flow).
    """
    raw = await cache_get(_CONSENT_PREFIX + cid)
    return _parse_consent(raw)


async def _pop_consent(cid: str) -> dict | None:
    """Reads a consent envelope and deletes it (one-shot).

    Only call after ``_peek_consent`` confirmed the caller is the legit
    recipient — see the call sites in the confirm/reject handlers.
    """
    raw = await cache_get(_CONSENT_PREFIX + cid)
    if not raw:
        return None
    await cache_delete(_CONSENT_PREFIX + cid)
    return _parse_consent(raw)


@router.message(F.text.startswith(f"/start {_PREFIX}"))
async def handle_account_link(message: Message, state: FSMContext) -> None:
    """First step of the consent flow.

    Peeks the token (without consuming), looks up the web identity
    behind it, and shows a confirmation message with ✅/❌ buttons.
    The actual attach is performed only after the user explicitly
    presses ✅ in :func:`link_confirm_callback`.

    ``state`` is forwarded to the confirm callback indirectly: brand-new
    bot users go through onboarding *there*, after the attach has been
    approved — running it pre-consent would leak the bot UI to whoever
    clicked the malicious URL.
    """
    del state  # onboarding runs in the confirm callback now
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return
    payload = parts[1].strip()
    if not payload.startswith(_PREFIX):
        return
    token = payload[len(_PREFIX):]
    if not token:
        return

    user = message.from_user
    if not user or not user.id:
        return
    tg_id = int(user.id)

    async with async_session_maker() as session:
        remote_id = await peek_link_token(LINK_KIND_TG, token)
        if not remote_id:
            await message.answer(
                "⏱ Ссылка для привязки устарела или уже использована.\n"
                "Откройте веб-приложение и нажмите «Войти через телеграм» ещё раз."
            )
            return
        remote_identity = await idb.get_identity_by_id(session, remote_id)

    if remote_identity is None:
        # Token points at a deleted identity — clean up and treat as expired.
        await drop_link_token(LINK_KIND_TG, token)
        await message.answer(
            "⏱ Ссылка для привязки больше не действительна.\n"
            "Откройте веб-приложение и нажмите «Войти через телеграм» ещё раз."
        )
        return

    email_mask = _mask_email(getattr(remote_identity, "email", None))
    created_at = getattr(remote_identity, "created_at", None)
    created_label = created_at.strftime("%d.%m.%Y") if created_at else "—"

    cid = await _store_consent(token, str(remote_identity.id), tg_id)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, привязать", callback_data=f"link_confirm:{cid}")],
            [InlineKeyboardButton(text="❌ Нет, это не я", callback_data=f"link_reject:{cid}")],
        ]
    )
    await message.answer(
        "🔗 <b>Подтвердите привязку Telegram</b>\n\n"
        "Кто-то хочет привязать ваш Telegram к веб-аккаунту:\n"
        f"📧 <code>{email_mask}</code>\n"
        f"📅 Зарегистрирован: {created_label}\n\n"
        "Если это <b>вы</b> — нажмите «Да, привязать».\n"
        "Если <b>не вы</b> — нажмите «Нет, это не я». "
        "Возможно, кто-то пытается получить доступ к вашему аккаунту.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("link_confirm:"))
async def link_confirm_callback(query: CallbackQuery, state: FSMContext) -> None:
    """Approval handler — consumes the original token and performs the
    actual ``attach_telegram``. Brand-new bot users get a «отправьте
    /start» tail in the success message rather than an auto-onboarding —
    callbacks don't carry the user's own message context (``query.message``
    is the bot's confirmation bubble), so ``handlers.start.start_entry``
    would parse a wrong ``from_user`` if we called it here."""
    del state
    cid = (query.data or "").split(":", 1)[1]
    message = query.message
    caller_tg_id = int(query.from_user.id) if query.from_user else 0

    # Peek first, tg_id check second, pop last — if we popped before
    # verifying the caller, a clicked button on a forwarded inline
    # message would burn the original recipient's consent.
    consent = await _peek_consent(cid)
    if not consent:
        await query.answer("Запрос устарел", show_alert=False)
        if message is not None:
            try:
                await message.edit_text(
                    "⏱ Запрос на привязку устарел. Откройте веб-приложение "
                    "и попробуйте ещё раз.",
                    reply_markup=None,
                )
            except Exception:
                pass
        return

    if int(consent.get("tg_id", 0)) != caller_tg_id:
        # Inline message was forwarded — only the original recipient
        # whose tg_id we stashed in the envelope can approve. Consent
        # stays intact in Redis for the real owner.
        await query.answer("Это подтверждение не для вас", show_alert=True)
        return

    token = str(consent.get("token") or "")
    expected_remote_id = str(consent.get("remote_id") or "")
    if not token or not expected_remote_id:
        await query.answer("Запрос повреждён", show_alert=False)
        return

    # Acknowledge the click now — Telegram surfaces «no response from
    # bot» under the button after ~15 s and the DB work below can
    # easily take longer under load.
    await query.answer()

    # Now that we know the caller is legit, atomically pop the consent
    # so a concurrent click can't run the attach twice.
    if await _pop_consent(cid) is None:
        # Another click won the race between peek and pop. Treat as
        # already used.
        if message is not None:
            try:
                await message.edit_text(
                    "⏱ Ссылка для привязки уже использована.",
                    reply_markup=None,
                )
            except Exception:
                pass
        return

    is_existing_user = False
    merged = None
    async with async_session_maker() as session:
        try:
            is_existing_user = await check_user_exists(session, caller_tg_id)
        except Exception as exc:
            logger.info(
                "[account_link] check_user_exists failed for tg_id=%s: %s",
                caller_tg_id,
                type(exc).__name__,
            )

        # Consume locks in the operation: if the same token races into a
        # parallel /start in another tab, only one wins. Mismatch with
        # the envelope's remote_id means someone re-stored a fresh token
        # under the same value (effectively impossible — 8-char base62) —
        # bail out defensively rather than attach into a wrong identity.
        remote_id = await consume_link_token(LINK_KIND_TG, token)
        if not remote_id or remote_id != expected_remote_id:
            if message is not None:
                try:
                    await message.edit_text(
                        "⏱ Ссылка для привязки устарела или уже использована.",
                        reply_markup=None,
                    )
                except Exception:
                    pass
            return

        merged = await idb.attach_telegram(session, remote_id, caller_tg_id)
        await session.commit()

    if merged is None:
        logger.info(
            "[account_link] refused for tg_id=%s remote=%s — both have keys",
            caller_tg_id,
            expected_remote_id,
        )
        if message is not None:
            try:
                await message.edit_text(
                    "❌ Не удалось связать аккаунты — на обоих оформлены подписки.\n"
                    "Связать можно только если хотя бы один аккаунт ещё без подписки.",
                    reply_markup=None,
                )
            except Exception:
                pass
        return

    await invalidate_photo_cache(str(merged.id))

    logger.info("[account_link] linked tg_id=%s → identity=%s", caller_tg_id, merged.id)
    success_text = (
        "✅ Аккаунты связаны! Теперь все ваши подписки видны и в этом боте, "
        "и в личном кабинете."
    )
    if not is_existing_user:
        # Web-first user opening the bot for the first time — point them
        # at the standard onboarding entry so they don't dead-end on the
        # success message. Running ``start_entry`` from a callback would
        # parse the bot's own confirmation bubble as the user message
        # and fall apart, so we hand the user the explicit command.
        success_text += "\n\nЧтобы открыть меню бота, отправьте /start."
    if message is not None:
        try:
            await message.edit_text(success_text, reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("link_reject:"))
async def link_reject_callback(query: CallbackQuery, state: FSMContext) -> None:
    """Rejection handler — drops the underlying link token outright so a
    leaked URL can't be reused later, and tells the user it was a
    likely phishing attempt."""
    del state
    cid = (query.data or "").split(":", 1)[1]
    message = query.message
    caller_tg_id = int(query.from_user.id) if query.from_user else 0

    # Same peek-then-pop guard as confirm: a forwarded ❌ click from a
    # third party must not destroy the original recipient's consent
    # and underlying token (DoS).
    consent = await _peek_consent(cid)
    if not consent:
        await query.answer("Запрос устарел", show_alert=False)
        if message is not None:
            try:
                await message.edit_text(
                    "⏱ Запрос на привязку устарел.",
                    reply_markup=None,
                )
            except Exception:
                pass
        return

    if int(consent.get("tg_id", 0)) != caller_tg_id:
        await query.answer("Это подтверждение не для вас", show_alert=True)
        return

    await query.answer()
    if await _pop_consent(cid) is None:
        return
    token = str(consent.get("token") or "")
    await drop_link_token(LINK_KIND_TG, token)
    if message is not None:
        try:
            await message.edit_text(
                "❌ Запрос на привязку отклонён. Если кто-то пытался "
                "получить доступ к вашему аккаунту — никаких "
                "дополнительных действий не требуется.",
                reply_markup=None,
            )
        except Exception:
            pass
