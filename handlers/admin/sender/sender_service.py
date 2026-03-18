import asyncio
import time

from collections.abc import Awaitable, Callable
from collections import deque
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker, save_blocked_user_ids
from logger import logger


def run_broadcast_in_thread(
    api_token: str,
    tg_ids: list[int],
    text_message: str,
    photo: str | None,
    keyboard_data: dict | None,
    progress_cb: Callable[[int, int, int, int], None] | None = None,
) -> dict:
    """
    Синхронная обёртка: запускает рассылку в отдельном event loop в текущем потоке.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = None
    try:
        bot = Bot(token=api_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        keyboard = InlineKeyboardMarkup.model_validate(keyboard_data) if keyboard_data else None
        messages = [
            {"tg_id": tg_id, "text": text_message, "photo": photo, "keyboard": keyboard}
            for tg_id in tg_ids
        ]
        service = BroadcastService(bot=bot, session=None, messages_per_second=35)

        async def on_progress(completed: int, total: int, sent: int, failed: int) -> None:
            if progress_cb:
                progress_cb(completed, total, sent, failed)

        return loop.run_until_complete(
            service.broadcast(
                messages,
                workers=5,
                on_progress=on_progress,
                progress_interval=2.0,
            )
        )
    finally:
        if bot is not None and bot.session is not None:
            try:
                loop.run_until_complete(bot.session.close())
            except Exception:
                pass
        loop.close()


class BroadcastMessage:
    def __init__(self, tg_id: int, text: str, photo: str | None = None, keyboard: Any = None) -> None:
        self.tg_id = tg_id
        self.text = text
        self.photo = photo
        self.keyboard = keyboard
        self.retry_after = None
        self.attempts = 0


class RateLimiter:
    def __init__(self, max_rate: int = 35, window: float = 1.0) -> None:
        self.max_rate = max_rate
        self.window = window
        self.send_times = deque()
        self.lock = asyncio.Lock()

    def _clean_old_timestamps(self, current_time: float):
        cutoff_time = current_time - self.window
        while self.send_times and self.send_times[0] <= cutoff_time:
            self.send_times.popleft()

    async def acquire(self):
        async with self.lock:
            while True:
                now = time.time()

                self._clean_old_timestamps(now)

                if len(self.send_times) < self.max_rate:
                    self.send_times.append(now)
                    return

                oldest_timestamp = self.send_times[0]
                time_to_wait = (oldest_timestamp + self.window) - now

                if time_to_wait > 0:
                    await asyncio.sleep(time_to_wait + 0.001)


class BroadcastService:
    def __init__(
        self,
        bot: Bot,
        session: AsyncSession | None = None,
        messages_per_second: int = 35,
    ) -> None:
        self.bot = bot
        self._session = session
        self.rate_limiter = RateLimiter(max_rate=messages_per_second)
        self.blocked_users = set()
        self.queue = asyncio.Queue()
        self.delayed_queue = asyncio.Queue()
        self.results = []
        self.total_sent = 0
        self.start_time = None
        self.is_running = False

    async def _send_single_message(self, msg: BroadcastMessage) -> bool:
        try:
            await self.rate_limiter.acquire()

            if msg.photo:
                await self.bot.send_photo(
                    chat_id=msg.tg_id, photo=msg.photo, caption=msg.text, parse_mode="HTML", reply_markup=msg.keyboard
                )
            else:
                await self.bot.send_message(
                    chat_id=msg.tg_id, text=msg.text, parse_mode="HTML", reply_markup=msg.keyboard
                )

            return True

        except TelegramRetryAfter as e:
            msg.retry_after = e.retry_after
            msg.attempts += 1
            logger.warning(
                f"⚠️ Flood control для {msg.tg_id}: повтор через {e.retry_after} сек. (попытка {msg.attempts})"
            )
            await self.delayed_queue.put(msg)
            return False

        except TelegramForbiddenError:
            logger.warning(f"🚫 Бот заблокирован пользователем {msg.tg_id}")
            self.blocked_users.add(msg.tg_id)
            return False

        except TelegramBadRequest as e:
            error_msg = str(e).lower()
            if "chat not found" in error_msg:
                logger.warning(f"🚫 Чат не найден для пользователя {msg.tg_id}")
                self.blocked_users.add(msg.tg_id)
            else:
                logger.warning(f"📩 Не удалось отправить сообщение пользователю {msg.tg_id}: {e}")
            return False

        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения пользователю {msg.tg_id}: {e}")
            return False

    async def _process_delayed_messages(self):
        while self.is_running:
            try:
                if not self.delayed_queue.empty():
                    msg = await asyncio.wait_for(self.delayed_queue.get(), timeout=0.1)

                    if msg.retry_after:
                        await asyncio.sleep(msg.retry_after)
                        msg.retry_after = None

                    if msg.attempts < 3:
                        await self.queue.put(msg)
                    else:
                        logger.error(f"❌ Достигнут лимит попыток для {msg.tg_id}")
                        self.results.append(False)
                else:
                    await asyncio.sleep(0.1)

            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"❌ Ошибка в обработчике отложенных сообщений: {e}")
                await asyncio.sleep(0.1)

    async def _worker(self):
        while self.is_running:
            try:
                msg = await asyncio.wait_for(self.queue.get(), timeout=0.1)

                success = await self._send_single_message(msg)

                if success:
                    self.total_sent += 1
                    self.results.append(True)
                elif msg.attempts == 0:
                    self.results.append(False)

                self.queue.task_done()

            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"❌ Ошибка в воркере рассылки: {e}")
                await asyncio.sleep(0.1)

    async def _save_blocked_users(self) -> None:
        if not self.blocked_users:
            return
        try:
            if self._session is not None:
                await save_blocked_user_ids(self._session, list(self.blocked_users))
            else:
                async with async_session_maker() as session:
                    await save_blocked_user_ids(session, list(self.blocked_users))
        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении заблокированных пользователей: {e}")
            if self._session is not None:
                await self._session.rollback()

    async def _progress_loop(
        self,
        total: int,
        on_progress: Callable[[int, int, int, int], Awaitable[None]],
        interval: float,
        progress_every: int,
    ) -> None:
        """Периодически вызывает on_progress(completed, total, sent, failed)."""
        last_reported = 0
        while self.is_running:
            await asyncio.sleep(interval)
            if not self.is_running:
                break
            completed = len(self.results)
            if completed - last_reported < progress_every:
                continue
            sent = self.total_sent
            failed = completed - sent
            try:
                await on_progress(completed, total, sent, failed)
                last_reported = completed
            except Exception as e:
                logger.debug(f"[Broadcast] Ошибка обновления прогресса: {e}")

    async def broadcast(
        self,
        messages: list[dict],
        workers: int = 20,
        on_progress: Callable[[int, int, int, int], Awaitable[None]] | None = None,
        progress_interval: float = 2.0,
        progress_every: int = 50,
    ) -> dict:
        self.is_running = True
        self.start_time = time.time()
        self.results = []
        self.total_sent = 0
        self.blocked_users = set()

        for msg_data in messages:
            msg = BroadcastMessage(
                tg_id=msg_data["tg_id"],
                text=msg_data["text"],
                photo=msg_data.get("photo"),
                keyboard=msg_data.get("keyboard"),
            )
            await self.queue.put(msg)

        logger.info(f"📤 Начата рассылка на {len(messages)} пользователей с {workers} воркерами")

        total = len(messages)
        progress_task = None
        if on_progress and total > 0:
            progress_task = asyncio.create_task(
                self._progress_loop(total, on_progress, progress_interval, max(1, progress_every)),
            )

        worker_tasks = [asyncio.create_task(self._worker()) for _ in range(workers)]

        delayed_task = asyncio.create_task(self._process_delayed_messages())

        await self.queue.join()

        await asyncio.sleep(1)
        while not self.delayed_queue.empty():
            await asyncio.sleep(1)

        self.is_running = False

        if progress_task is not None:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
            completed = len(self.results)
            try:
                await on_progress(
                    completed,
                    total,
                    self.total_sent,
                    completed - self.total_sent,
                )
            except Exception as e:
                logger.debug(f"[Broadcast] Финальное обновление прогресса: {e}")

        for task in worker_tasks:
            task.cancel()
        delayed_task.cancel()

        await asyncio.gather(*worker_tasks, delayed_task, return_exceptions=True)

        if self._session is not None:
            await self._save_blocked_users()

        end_time = time.time()
        total_duration = end_time - self.start_time
        success_count = sum(1 for r in self.results if r)
        avg_speed = self.total_sent / total_duration if total_duration > 0 else 0

        stats = {
            "total_duration": total_duration,
            "total_sent": self.total_sent,
            "success_count": success_count,
            "failed_count": len(self.results) - success_count,
            "avg_speed": avg_speed,
            "total_messages": len(messages),
            "blocked_users": len(self.blocked_users),
            "blocked_user_ids": list(self.blocked_users),
        }

        logger.info(
            f"✅ Рассылка завершена: {success_count}/{len(messages)} успешно, "
            f"скорость: {avg_speed:.1f} сообщений/сек, время: {total_duration:.1f} сек"
        )

        return stats
