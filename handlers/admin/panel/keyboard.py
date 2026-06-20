from typing import Any, Iterable

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from filters.permissions import (
    PERM_ADMINS,
    PERM_ADS,
    PERM_BROADCASTING,
    PERM_CLUSTERS,
    PERM_COUPONS,
    PERM_EMOJI,
    PERM_GIFTS,
    PERM_KEYS,
    PERM_MANAGEMENT,
    PERM_MODULES,
    PERM_SETTINGS,
    PERM_STATS,
    PERM_TARIFFS,
    PERM_USERS,
)
from handlers.buttons import BACK, MAIN_MENU
from hooks.hook_buttons import insert_hook_buttons
from hooks.hooks import run_hooks


class AdminPanelCallback(CallbackData, prefix="admin_panel"):
    action: str
    page: int

    def __init__(self, /, **data: Any) -> None:
        if "page" not in data or data["page"] is None:
            data["page"] = 1
        super().__init__(**data)


async def build_panel_kb(
    admin_role: str,
    permissions: Iterable[str] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    is_super = admin_role == "superadmin"
    perm_set = frozenset(permissions or ())

    def can(perm: str) -> bool:
        return is_super or perm in perm_set

    row: list[InlineKeyboardButton] = []
    if can(PERM_USERS):
        row.append(
            InlineKeyboardButton(
                text="👤 Поиск пользователя",
                callback_data=AdminPanelCallback(action="search_user").pack(),
            )
        )
    if can(PERM_KEYS):
        row.append(
            InlineKeyboardButton(
                text="🔑 Поиск подписок",
                callback_data=AdminPanelCallback(action="search_key").pack(),
            )
        )
    if row:
        builder.row(*row)

    if can(PERM_KEYS):
        builder.row(
            InlineKeyboardButton(
                text="📦 Массовые действия",
                callback_data=AdminPanelCallback(action="bulk").pack(),
            )
        )

    if can(PERM_CLUSTERS):
        builder.row(
            InlineKeyboardButton(
                text="🖥️ Управление серверами",
                callback_data=AdminPanelCallback(action="clusters").pack(),
            )
        )
    if can(PERM_TARIFFS):
        builder.row(
            InlineKeyboardButton(
                text="💸Управление тарифами",
                callback_data=AdminPanelCallback(action="tariffs").pack(),
            )
        )
    if can(PERM_MANAGEMENT) or can(PERM_ADMINS):
        builder.row(
            InlineKeyboardButton(
                text="🤖 Управление ботом",
                callback_data=AdminPanelCallback(action="management").pack(),
            )
        )

    row = []
    if can(PERM_BROADCASTING):
        row.append(
            InlineKeyboardButton(
                text="📢 Рассылка",
                callback_data=AdminPanelCallback(action="sender").pack(),
            )
        )
    if can(PERM_COUPONS):
        row.append(
            InlineKeyboardButton(
                text="🎟️ Купоны",
                callback_data=AdminPanelCallback(action="coupons").pack(),
            )
        )
    if row:
        builder.row(*row)

    row = []
    if can(PERM_GIFTS):
        row.append(
            InlineKeyboardButton(
                text="🎁 Подарки",
                callback_data=AdminPanelCallback(action="gifts").pack(),
            )
        )
    if can(PERM_MODULES):
        row.append(
            InlineKeyboardButton(
                text="🧩 Мои модули",
                callback_data=AdminPanelCallback(action="modules").pack(),
            )
        )
    if row:
        builder.row(*row)

    row = []
    if can(PERM_STATS):
        row.append(
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data=AdminPanelCallback(action="stats").pack(),
            )
        )
    if can(PERM_ADS):
        row.append(
            InlineKeyboardButton(
                text="📈 Аналитика",
                callback_data=AdminPanelCallback(action="ads").pack(),
            )
        )
    if row:
        builder.row(*row)

    if can(PERM_EMOJI):
        builder.row(
            InlineKeyboardButton(
                text="😀 Эмоджи",
                callback_data=AdminPanelCallback(action="emoji").pack(),
            )
        )

    module_buttons = await run_hooks("admin_panel", admin_role=admin_role)
    builder = insert_hook_buttons(builder, module_buttons)

    if can(PERM_SETTINGS):
        builder.row(
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data=AdminPanelCallback(action="settings").pack(),
            )
        )

    builder.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))

    return builder.as_markup()


def build_restart_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Да, перезагрузить",
        callback_data=AdminPanelCallback(action="restart_confirm").pack(),
    )
    builder.row(build_admin_back_btn())
    builder.adjust(1)
    return builder.as_markup()


def build_admin_back_kb(action: str = "admin") -> InlineKeyboardMarkup:
    return build_admin_singleton_kb(BACK, action)


def build_admin_singleton_kb(text: str, action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(build_admin_btn(text, action))
    return builder.as_markup()


def build_admin_back_btn(action: str = "admin") -> InlineKeyboardButton:
    return build_admin_btn(BACK, action)


def build_admin_btn(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=AdminPanelCallback(action=action).pack(),
    )
