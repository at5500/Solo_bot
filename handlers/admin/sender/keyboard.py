from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..panel.keyboard import build_admin_back_btn


class AdminSenderCallback(CallbackData, prefix="admin_sender"):
    type: str
    data: str | None = None


class ScheduledBroadcastCallback(CallbackData, prefix="sb"):
    action: str
    broadcast_id: str = "0"
    page: int = 0


def build_sender_kb(include_scheduled: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="👥 Все пользователи",
            callback_data=AdminSenderCallback(type="all").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ С подпиской",
            callback_data=AdminSenderCallback(type="subscribed").pack(),
        ),
        InlineKeyboardButton(
            text="❌ Без подписки",
            callback_data=AdminSenderCallback(type="unsubscribed").pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📍 Не использовавшие триал",
            callback_data=AdminSenderCallback(type="untrial").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🧪 Триал",
            callback_data=AdminSenderCallback(type="trial").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔥 Горячие лиды",
            callback_data=AdminSenderCallback(type="hotleads").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📢 Кластер",
            callback_data=AdminSenderCallback(type="cluster-select").pack(),
        )
    )
    if include_scheduled:
        builder.row(
            InlineKeyboardButton(
                text="🗓 Запланированные",
                callback_data=ScheduledBroadcastCallback(action="list").pack(),
            )
        )
    builder.row(build_admin_back_btn())

    return builder.as_markup()


def build_clusters_kb(clusters: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for cluster in clusters:
        name = cluster["cluster_name"]
        builder.button(
            text=f"🌐 {name}",
            callback_data=AdminSenderCallback(type="cluster", data=name).pack(),
        )

    builder.adjust(2)
    builder.row(build_admin_back_btn())

    return builder.as_markup()


def build_broadcast_preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Отправить", callback_data="send_broadcast"),
                InlineKeyboardButton(text="🗓 Запланировать", callback_data="schedule_broadcast"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast"),
            ],
        ]
    )


def build_scheduled_broadcasts_list_kb(items: list, page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items:
        builder.row(
            InlineKeyboardButton(
                text=f"🗓 {item.id[:8]} | {item.status}",
                callback_data=ScheduledBroadcastCallback(action="view", broadcast_id=item.id, page=page).pack(),
            )
        )
    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=ScheduledBroadcastCallback(action="list", page=page - 1).pack(),
            )
        )
    if len(items) >= 5:
        nav_row.append(
            InlineKeyboardButton(
                text="▶️ Далее",
                callback_data=ScheduledBroadcastCallback(action="list", page=page + 1).pack(),
            )
        )
    if nav_row:
        builder.row(*nav_row)
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=ScheduledBroadcastCallback(action="list", page=page).pack(),
        )
    )
    builder.row(build_admin_back_btn())
    return builder.as_markup()


def build_scheduled_broadcast_detail_kb(item, page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if item.status in {"scheduled", "failed"}:
        builder.row(
            InlineKeyboardButton(
                text="✏️ Сообщение",
                callback_data=ScheduledBroadcastCallback(action="edit_message", broadcast_id=item.id, page=page).pack(),
            ),
            InlineKeyboardButton(
                text="🕒 Время",
                callback_data=ScheduledBroadcastCallback(action="edit_time", broadcast_id=item.id, page=page).pack(),
            ),
        )
        builder.row(
            InlineKeyboardButton(
                text="👥 Аудитория",
                callback_data=ScheduledBroadcastCallback(action="edit_audience", broadcast_id=item.id, page=page).pack(),
            ),
            InlineKeyboardButton(
                text="⚡ Отправить сейчас",
                callback_data=ScheduledBroadcastCallback(action="send_now", broadcast_id=item.id, page=page).pack(),
            ),
        )
        builder.row(
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=ScheduledBroadcastCallback(action="cancel", broadcast_id=item.id, page=page).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=ScheduledBroadcastCallback(action="view", broadcast_id=item.id, page=page).pack(),
        ),
        InlineKeyboardButton(
            text="📋 К списку",
            callback_data=ScheduledBroadcastCallback(action="list", page=page).pack(),
        ),
    )
    return builder.as_markup()
