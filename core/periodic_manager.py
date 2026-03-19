import asyncio
import fcntl
import os

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker

from logger import logger


LoopRunner = Callable[[Bot, async_sessionmaker], Awaitable[None]]
CronRunner = Callable[[], Awaitable[None]]


@dataclass
class ManagedLoopTask:
    task_id: str
    runner: LoopRunner


@dataclass
class ManagedCronTask:
    task_id: str
    runner: CronRunner
    trigger: BaseTrigger


class PeriodicTaskManager:
    def __init__(self, timezone_name: str = "Europe/Moscow") -> None:
        self.timezone_name = timezone_name
        self._loop_tasks: dict[str, ManagedLoopTask] = {}
        self._cron_tasks: dict[str, ManagedCronTask] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._scheduler: AsyncIOScheduler | None = None
        self._started = False
        self._lock = asyncio.Lock()
        self._process_lock_file = None
        self._process_lock_path = "/tmp/solo_bot_periodic_manager.lock"

    def register_loop_task(self, task_id: str, runner: LoopRunner) -> None:
        self._loop_tasks[task_id] = ManagedLoopTask(task_id=task_id, runner=runner)

    def register_cron_task(self, task_id: str, runner: CronRunner, trigger: BaseTrigger) -> None:
        self._cron_tasks[task_id] = ManagedCronTask(task_id=task_id, runner=runner, trigger=trigger)

    def _acquire_process_lock(self) -> bool:
        if self._process_lock_file is not None:
            return True
        lock_file = open(self._process_lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            self._process_lock_file = lock_file
            return True
        except OSError:
            lock_file.close()
            return False

    def _release_process_lock(self) -> None:
        if self._process_lock_file is None:
            return
        try:
            fcntl.flock(self._process_lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._process_lock_file.close()
        except OSError:
            pass
        self._process_lock_file = None

    async def start(self, bot: Bot, sessionmaker: async_sessionmaker) -> None:
        async with self._lock:
            if self._started:
                return
            if not self._acquire_process_lock():
                logger.info("[PeriodicManager] Уже запущен в другом процессе, текущий запуск пропущен")
                return
            scheduler = AsyncIOScheduler(timezone=self.timezone_name)
            for cron_task in self._cron_tasks.values():
                scheduler.add_job(
                    cron_task.runner,
                    cron_task.trigger,
                    id=cron_task.task_id,
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
            scheduler.start()
            self._scheduler = scheduler
            for loop_task in self._loop_tasks.values():
                self._running_tasks[loop_task.task_id] = asyncio.create_task(loop_task.runner(bot, sessionmaker))
            self._started = True
            logger.info(
                "[PeriodicManager] Запущен: loop=%s cron=%s",
                len(self._loop_tasks),
                len(self._cron_tasks),
            )

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            if self._scheduler is not None:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:
                    pass
                self._scheduler = None
            tasks = list(self._running_tasks.values())
            self._running_tasks.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._started = False
            self._release_process_lock()
            logger.info("[PeriodicManager] Остановлен")


periodic_task_manager = PeriodicTaskManager()
