import hashlib

from typing import Any

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_key_by_client_id, get_key_by_email, get_keys
from database.models import Key
from handlers.payments.currency_rates import format_for_user


def key_owned_by_user(record: dict | None, user_id: int) -> bool:
    """Проверка, что ключ принадлежит пользователю (защита от пересылки callback)."""
    return record is not None and record.get("tg_id") == user_id


def build_key_ref(client_id: str | None, email: str | None = None) -> str:
    source = str(client_id or email or "")
    return hashlib.blake2s(source.encode(), digest_size=6).hexdigest()


def build_key_callback(prefix: str, client_id: str | None, email: str | None = None) -> str:
    return f"{prefix}|{build_key_ref(client_id, email)}"


async def resolve_key(session: AsyncSession, tg_id: int, key_ref: str | int | None) -> Key | None:
    if key_ref is None:
        return None

    key_ref_str = str(key_ref)

    key_obj = await get_key_by_email(session, key_ref_str, tg_id)
    if key_obj:
        return key_obj

    key_obj = await get_key_by_client_id(session, key_ref_str, tg_id)
    if key_obj:
        return key_obj

    for candidate in await get_keys(session, tg_id):
        if build_key_ref(candidate.client_id, candidate.email) == key_ref_str:
            return await get_key_by_client_id(session, candidate.client_id, tg_id)

    return None


async def add_tariff_button_generic(
    builder: InlineKeyboardBuilder,
    tariff: dict[str, Any],
    session: AsyncSession,
    tg_id: int,
    language_code: str | None,
    callback_prefix: str,
):
    """Добавляет кнопку тарифа с учётом конфигуратора."""
    is_configurable = bool(tariff.get("configurable"))
    if is_configurable:
        button_text = tariff["name"]
    else:
        price_rub = float(tariff.get("price_rub") or 0)
        price_text = await format_for_user(session, tg_id, price_rub, language_code)
        button_text = f"{tariff['name']} — {price_text}"

    builder.row(
        InlineKeyboardButton(
            text=button_text,
            callback_data=f"{callback_prefix}|{tariff['id']}",
        )
    )
