import html
import re
import time
import traceback

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import BufferedInputFile, ErrorEvent
from aiogram.utils.markdown import hbold
from sqlalchemy.exc import (
    InterfaceError as SQLAlchemyInterfaceError,
    OperationalError as SQLAlchemyOperationalError,
)

from config import ADMIN_ID
from database import async_session_maker
from logger import logger


_OBFUSCATED_MIN_SEQ = 15
_PLACEHOLDER = "<obfuscated>"

# Strings that must never reach admin DMs or log sinks: bot API tokens that
# aiogram occasionally surfaces in ``TelegramAPIError.__str__``, and the
# one-shot ``link_<token>`` payloads echoed by `/start link_…` errors.
_BOT_TOKEN_RE = re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]+", re.IGNORECASE)
_LINK_PAYLOAD_RE = re.compile(r"\blink_[A-Za-z0-9]{6,}\b")

_ERROR_LOG_THROTTLE: dict[str, float] = {}
_ERROR_LOG_THROTTLE_WINDOW_SEC = 60
_ERROR_LOG_THROTTLE_MAX_KEYS = 500


def _should_log_error(key: str, window_sec: float = _ERROR_LOG_THROTTLE_WINDOW_SEC) -> bool:
    """Возвращает True, если эту ошибку ещё можно залогировать (не троттлим). Обновляет время последнего лога."""
    now = time.monotonic()
    if len(_ERROR_LOG_THROTTLE) >= _ERROR_LOG_THROTTLE_MAX_KEYS:
        by_ts = sorted((v, k) for k, v in _ERROR_LOG_THROTTLE.items())
        for _, k in by_ts[: _ERROR_LOG_THROTTLE_MAX_KEYS // 2]:
            _ERROR_LOG_THROTTLE.pop(k, None)
    last = _ERROR_LOG_THROTTLE.get(key, 0.0)
    if now - last >= window_sec:
        _ERROR_LOG_THROTTLE[key] = now
        return True
    return False


def _sanitize_traceback(text: str) -> str:
    """Strips long obfuscated byte sequences AND any embedded secrets.

    The original purpose was cosmetic — collapsing long ``\\xHH`` runs that
    show up when aiogram serialises binary payloads. Extended to also redact
    bot API tokens and one-shot link-token payloads so that admin DMs and
    log sinks never carry live credentials.

    Args:
        text: Raw traceback text from :func:`traceback.format_exc`.

    Returns:
        Sanitised text with obfuscated sequences and secrets replaced.
    """
    out = re.sub(r"(\\x[0-9a-fA-F]{2}){" + str(_OBFUSCATED_MIN_SEQ) + r",}", _PLACEHOLDER, text)
    out = _BOT_TOKEN_RE.sub("bot<redacted>", out)
    out = _LINK_PAYLOAD_RE.sub("link_<redacted>", out)
    return out


def _safe_update_repr(update) -> str:
    """Compact, secret-free representation of an aiogram ``Update``.

    Replaces the default ``repr`` (which includes the raw message text and
    therefore any ``/start link_<token>`` payload) with just the
    ``update_id`` plus the event kind. The full update is still available
    via the sanitised traceback if needed for debugging.

    Args:
        update: ``aiogram.types.Update`` instance from ``ErrorEvent``.

    Returns:
        A short string suitable for log lines.
    """
    update_id = getattr(update, "update_id", None)
    kind = "unknown"
    if getattr(update, "message", None):
        kind = "message"
    elif getattr(update, "callback_query", None):
        kind = "callback_query"
    elif getattr(update, "inline_query", None):
        kind = "inline_query"
    elif getattr(update, "edited_message", None):
        kind = "edited_message"
    return f"Update(id={update_id}, kind={kind})"


def setup_error_handlers(dp: Dispatcher) -> None:
    @dp.errors(ExceptionTypeFilter(Exception))
    async def errors_handler(event: ErrorEvent, bot: Bot) -> bool:
        if isinstance(event.exception, TelegramForbiddenError):
            user = None
            if event.update.message and event.update.message.from_user:
                user = event.update.message.from_user
            elif event.update.callback_query and event.update.callback_query.from_user:
                user = event.update.callback_query.from_user
            if user:
                logger.info(f"User {user.id} заблокировал бота.")
            else:
                logger.info("Пользователь заблокировал бота.")
            return True

        if isinstance(event.exception, TelegramRetryAfter):
            e = event.exception
            chat_id = None
            if event.update.message:
                chat_id = event.update.message.chat.id
            elif event.update.callback_query and event.update.callback_query.message:
                chat_id = event.update.callback_query.message.chat.id
            if _should_log_error("TelegramRetryAfter", 30):
                logger.warning(
                    "Flood control (TelegramRetryAfter): retry_after={} сек, chat_id={}",
                    e.retry_after,
                    chat_id,
                )
            if chat_id:
                try:
                    await bot.send_message(
                        chat_id,
                        f"⏳ Слишком много запросов. Повторите через {int(e.retry_after) + 1} сек.",
                    )
                except Exception:
                    pass
            return True

        if isinstance(event.exception, OSError) and getattr(event.exception, "errno", None) == 24:
            chat_id = None
            if event.update.message:
                chat_id = event.update.message.chat.id
            elif event.update.callback_query and event.update.callback_query.message:
                chat_id = event.update.callback_query.message.chat.id
            if _should_log_error("OSError24", 15):
                logger.warning(
                    "OSError(24) Too many open files при обработке запроса (chat_id={})",
                    chat_id,
                )
            if chat_id:
                try:
                    await bot.send_message(
                        chat_id,
                        "⚠️ Временная перегрузка. Попробуйте ещё раз через пару секунд.",
                    )
                except Exception:
                    pass
            return True

        if isinstance(
            event.exception,
            SQLAlchemyInterfaceError | SQLAlchemyOperationalError,
        ):
            chat_id = None
            if event.update.message:
                chat_id = event.update.message.chat.id
            elif event.update.callback_query and event.update.callback_query.message:
                chat_id = event.update.callback_query.message.chat.id
            if _should_log_error("DB_Interface_Operational", 30):
                logger.warning(
                    "Ошибка соединения с БД (InterfaceError/OperationalError): {}",
                    _sanitize_traceback(str(event.exception)[:200]),
                )
            if chat_id:
                try:
                    await bot.send_message(
                        chat_id,
                        "⚠️ Временная ошибка связи с базой данных. Попробуйте ещё раз через пару секунд.",
                    )
                except Exception:
                    pass
            return True

        if isinstance(event.exception, TelegramBadRequest):
            error_message = str(event.exception)

            if (
                "message is not modified" in error_message
                or "message to edit not found" in error_message
                or "message can't be edited" in error_message.lower()
            ):
                logger.debug(
                    "TelegramBadRequest (edit/delete): {}",
                    error_message[:150],
                )
                return True

            if (
                "query is too old and response timeout expired or query ID is invalid" in error_message
                or "message can't be deleted for everyone" in error_message
                or "message to delete not found" in error_message
            ):
                log_key = "TelegramBadRequest_old_or_delete"
                if _should_log_error(log_key, 45):
                    try:
                        tb = _sanitize_traceback(
                            "".join(
                                traceback.format_exception(
                                    type(event.exception),
                                    event.exception,
                                    event.exception.__traceback__,
                                )
                            )
                        )
                        logger.warning("Показываем стартовое меню из-за TelegramBadRequest: {}", error_message[:200])
                        logger.error("Traceback:\n{}", tb)

                        if ADMIN_ID:
                            if "query is too old and response timeout expired or query ID is invalid" in error_message:
                                caption = (
                                    f"{hbold('TelegramBadRequest: устаревший callback-запрос')}\n\n"
                                    "Что произошло:\n"
                                    "• Пользователь нажал старую кнопку, или\n"
                                    "• Telegram обработал callback уже после истечения таймаута.\n\n"
                                    "Описание:\n"
                                    "Такое может происходить из-за временной недоступности Telegram или "
                                    "нестабильного подключения сервера к API (задержки, потери пакетов, очереди запросов).\n\n"
                                    "Действия:\n"
                                    "• Проверить стабильность интернет-соединения сервера.\n"
                                    "• Оценить задержки/нагрузку на бота и частоту callback-запросов.\n"
                                    "• При необходимости оптимизировать обработку или уменьшить время между нажатием кнопки и ответом."
                                )
                            else:
                                safe_msg = _sanitize_traceback(error_message[:1021])
                                caption = f"{hbold(type(event.exception).__name__)}: {safe_msg}..."

                            for admin_id in ADMIN_ID:
                                await bot.send_document(
                                    chat_id=admin_id,
                                    document=BufferedInputFile(
                                        tb.encode(),
                                        filename=f"error_{event.update.update_id}.txt",
                                    ),
                                    caption=caption[:1024],
                                )
                    except Exception as e:
                        if _should_log_error("error_handler_admin_send", 60):
                            logger.error("Сбой при логировании/отправке ошибки админу: {}", e, exc_info=True)

                try:
                    from handlers.start import start_entry

                    if event.update.message:
                        fsm_context = dp.fsm.get_context(
                            bot=bot,
                            chat_id=event.update.message.chat.id,
                            user_id=event.update.message.from_user.id,
                        )
                        async with async_session_maker() as session:
                            await start_entry(
                                event=event.update.message,
                                state=fsm_context,
                                session=session,
                                admin=False,
                                captcha=False,
                            )
                            await session.commit()
                    elif event.update.callback_query:
                        fsm_context = dp.fsm.get_context(
                            bot=bot,
                            chat_id=event.update.callback_query.message.chat.id,
                            user_id=event.update.callback_query.from_user.id,
                        )
                        async with async_session_maker() as session:
                            await start_entry(
                                event=event.update.callback_query,
                                state=fsm_context,
                                session=session,
                                admin=False,
                                captcha=False,
                            )
                            await session.commit()
                except Exception as e:
                    if _should_log_error("error_handler_start_menu", 60):
                        logger.error("Ошибка при показе стартового меню после ошибки: {}", e, exc_info=True)

                return True

        exc = event.exception
        # Hash the raw ``str(exc)`` into the throttle key (cheap dedupe) but
        # never log it as-is — exceptions from aiogram/aiohttp can embed the
        # bot API token in their stringification.
        generic_key = "err:{}:{}".format(type(exc).__name__, str(exc)[:80].replace("\n", " "))
        if _should_log_error(generic_key, _ERROR_LOG_THROTTLE_WINDOW_SEC):
            safe_exc = _sanitize_traceback(repr(exc))
            logger.exception("Update: {}\nException: {}", _safe_update_repr(event.update), safe_exc)

        if not ADMIN_ID:
            return True

        try:
            if _should_log_error("generic_admin_doc", 60):
                tb_text = _sanitize_traceback(traceback.format_exc())
                exc_text = html.escape(_sanitize_traceback(str(event.exception)[:1021]))
                for admin_id in ADMIN_ID:
                    await bot.send_document(
                        chat_id=admin_id,
                        document=BufferedInputFile(
                            tb_text.encode(),
                            filename=f"error_{event.update.update_id}.txt",
                        ),
                        caption=f"{hbold(type(event.exception).__name__)}: {exc_text}...",
                    )

            from handlers.start import start_entry

            if event.update.message:
                fsm_context = dp.fsm.get_context(
                    bot=bot,
                    chat_id=event.update.message.chat.id,
                    user_id=event.update.message.from_user.id,
                )
                async with async_session_maker() as session:
                    await start_entry(
                        event=event.update.message,
                        state=fsm_context,
                        session=session,
                        admin=False,
                        captcha=False,
                    )
                    await session.commit()
            elif event.update.callback_query:
                fsm_context = dp.fsm.get_context(
                    bot=bot,
                    chat_id=event.update.callback_query.message.chat.id,
                    user_id=event.update.callback_query.from_user.id,
                )
                async with async_session_maker() as session:
                    await start_entry(
                        event=event.update.callback_query,
                        state=fsm_context,
                        session=session,
                        admin=False,
                        captcha=False,
                    )
                    await session.commit()

        except TelegramBadRequest as exception:
            if _should_log_error("error_handler_telegram_bad", 60):
                logger.warning("Не удалось отправить детали ошибки: {}", exception)
        except Exception as exception:
            if _should_log_error("error_handler_unexpected", 60):
                logger.error("Неожиданная ошибка в error handler: {}", exception)

        return True
