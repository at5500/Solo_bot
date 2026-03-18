import asyncio

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

    def register_loop_task(self, task_id: str, runner: LoopRunner) -> None:
        self._loop_tasks[task_id] = ManagedLoopTask(task_id=task_id, runner=runner)

    def register_cron_task(self, task_id: str, runner: CronRunner, trigger: BaseTrigger) -> None:
        self._cron_tasks[task_id] = ManagedCronTask(task_id=task_id, runner=runner, trigger=trigger)

    async def start(self, bot: Bot, sessionmaker: async_sessionmaker) -> None:
        async with self._lock:
            if self._started:
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
            logger.info("[PeriodicManager] Остановлен")


periodic_task_manager = PeriodicTaskManager()
