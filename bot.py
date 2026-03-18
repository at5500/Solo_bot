from importlib import import_module

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import API_TOKEN, REDIS_URL
from database import async_session_maker
from filters.private import IsPrivateFilter
from handlers.notifications.task_manager import (
    ensure_periodic_task_manager_started,
    ensure_periodic_task_manager_stopped,
)
from utils.button_icons import apply_button_icons_patch, set_button_icon_config
from utils.custom_emojis import initialize_custom_emojis
from utils.errors import setup_error_handlers
from utils.modules_loader import load_modules_from_folder, modules_hub

apply_button_icons_patch()

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

RedisStorage = import_module("aiogram.fsm.storage.redis").RedisStorage
redis_from_url = import_module("redis.asyncio").from_url
redis = redis_from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
storage = RedisStorage(redis=redis)

dp = Dispatcher(bot=bot, storage=storage)

dp.include_router(modules_hub)

load_modules_from_folder()

from handlers.buttons import BUTTON_ICON_CONFIG

set_button_icon_config(BUTTON_ICON_CONFIG)

dp.message.filter(IsPrivateFilter())
dp.callback_query.filter(IsPrivateFilter())

async def _on_dispatcher_startup(*_args, **_kwargs):
    await ensure_periodic_task_manager_started(bot, async_session_maker)


async def _on_dispatcher_shutdown(*_args, **_kwargs):
    await ensure_periodic_task_manager_stopped()


dp.startup.register(_on_dispatcher_startup)
dp.shutdown.register(_on_dispatcher_shutdown)

setup_error_handlers(dp)
initialize_custom_emojis()
