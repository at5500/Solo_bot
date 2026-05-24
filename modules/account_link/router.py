"""Bot handler for `/start link_<token>` — consumes a link-token issued by
the Mini App and attaches the current Telegram user to the originating
web identity.

The matching token producer is the web app's
``POST /api/auth/link-tokens/telegram`` endpoint. Token format and Redis
storage live in ``utils/identity_link.py``.

For users who arrive at the bot for the very first time via this deeplink
(``web-first`` flow: registered via OTP on the website, never opened the
bot before), the handler also runs the standard ``/start`` onboarding so
the user lands on the main menu instead of just receiving the «✅
Аккаунты связаны» message with no further bot UI.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from database import async_session_maker
from database import identities as idb
from database.users import check_user_exists
from logger import logger
from utils.identity_link import LINK_KIND_TG, consume_link_token
from utils.photo_cache import invalidate_photo_cache


router = Router(name="account_link")

_PREFIX = "link_"


@router.message(F.text.startswith(f"/start {_PREFIX}"))
async def handle_account_link(message: Message, state: FSMContext) -> None:
    """Parses ``/start link_<token>``, consumes the token from Redis, and
    calls :func:`database.identities.attach_telegram` to merge the user's
    Telegram into the web-side identity.

    For brand-new bot users (registered via the web flow first) the
    handler also kicks off the standard ``/start`` onboarding after the
    successful attach so they land on the main menu rather than a dead
    end. ``state`` is injected by aiogram for that onboarding hand-off.
    """
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

    # Snapshot whether the user was already known to the bot *before* the
    # attach — ``merge_billing_user_into_telegram`` creates the ``users``
    # row as a side effect, so a post-attach check would always say "yes".
    is_existing_user = False

    async with async_session_maker() as session:
        try:
            is_existing_user = await check_user_exists(session, tg_id)
        except Exception as exc:
            # Non-fatal — worst case we run onboarding for someone who
            # already had a bot session, which is just a redundant menu.
            logger.info("[account_link] check_user_exists failed for tg_id=%s: %s", tg_id, type(exc).__name__)

        remote_id = await consume_link_token(LINK_KIND_TG, token)
        if not remote_id:
            await message.answer(
                "⏱ Ссылка для привязки устарела или уже использована.\n"
                "Откройте веб-приложение и нажмите «Войти через телеграм» ещё раз."
            )
            return
        merged = await idb.attach_telegram(session, remote_id, tg_id)
        await session.commit()

    if merged is None:
        logger.info("[account_link] refused for tg_id=%s remote=%s — both have keys", tg_id, remote_id)
        await message.answer(
            "❌ Не удалось связать аккаунты — на обоих оформлены подписки.\n"
            "Связать можно только если хотя бы один аккаунт ещё без подписки."
        )
        return

    # Drop the "no avatar" sentinel cached when the identity was web-only —
    # otherwise Profile keeps the default dino for ~50 minutes after linking.
    await invalidate_photo_cache(str(merged.id))

    logger.info("[account_link] linked tg_id=%s → identity=%s", tg_id, merged.id)
    await message.answer(
        "✅ Аккаунты связаны! Теперь все ваши подписки видны и в этом боте, "
        "и в личном кабинете."
    )

    if is_existing_user:
        return

    # Web-first user opening the bot for the first time — give them the
    # full /start onboarding (without captcha, since they already proved
    # legitimacy via Mini App OTP login). Wrapped in a fresh session
    # because ``async with`` above already committed and closed its own.
    try:
        from handlers.start import start_entry

        async with async_session_maker() as new_session:
            await start_entry(
                event=message,
                state=state,
                session=new_session,
                admin=False,
                captcha=False,
            )
            await new_session.commit()
    except Exception as exc:
        # Onboarding is best-effort — the link itself already succeeded,
        # so failing to render the main menu is not user-fatal. They can
        # still send `/start` manually.
        logger.warning(
            "[account_link] onboarding failed for tg_id=%s: %s",
            tg_id,
            type(exc).__name__,
        )
