from collections import Counter
from datetime import datetime, timedelta
from html import escape

import pytz

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from audit import get_audit_funnel, get_audit_funnel_from_redis, get_audit_stats, get_audit_stats_from_redis
from bot import bot
from config import ADMIN_ID
from database import (
    count_active_keys,
    count_active_paid_keys,
    count_active_trial_keys,
    count_hot_leads,
    count_total_keys,
    count_total_referrals,
    count_total_users,
    count_users_registered_between,
    count_users_registered_since,
    count_users_updated_today,
    get_tariff_distribution,
    get_tariff_names_groups_subgroups_durations,
    sum_payments_between,
    sum_payments_since,
    sum_total_payments,
)
from filters.admin import IsAdminFilter
from hooks.hooks import run_hooks
from logger import logger
from utils.csv_export import (
    export_hot_leads_csv,
    export_keys_csv,
    export_payments_csv,
    export_users_csv,
)

from ..panel.keyboard import AdminPanelCallback, build_admin_back_kb
from .keyboard import build_audit_refresh_kb, build_stats_kb


router = Router()


@router.callback_query(AdminPanelCallback.filter(F.action == "stats"), IsAdminFilter())
async def handle_stats(callback_query: CallbackQuery, session: AsyncSession):
    try:
        moscow_tz = pytz.timezone("Europe/Moscow")
        now = datetime.now(moscow_tz)
        today = now.date()

        today_start = moscow_tz.localize(datetime.combine(today, datetime.min.time()))
        today_start_utc = today_start.astimezone(pytz.UTC).replace(tzinfo=None)

        yesterday_date = today - timedelta(days=1)
        yesterday_start = moscow_tz.localize(datetime.combine(yesterday_date, datetime.min.time()))
        yesterday_end = moscow_tz.localize(datetime.combine(today, datetime.min.time()))
        yesterday_start_utc = yesterday_start.astimezone(pytz.UTC).replace(tzinfo=None)
        yesterday_end_utc = yesterday_end.astimezone(pytz.UTC).replace(tzinfo=None)

        week_start_date = today - timedelta(days=today.weekday())
        week_start = moscow_tz.localize(datetime.combine(week_start_date, datetime.min.time()))
        week_start_utc = week_start.astimezone(pytz.UTC).replace(tzinfo=None)

        month_start_date = today.replace(day=1)
        month_start = moscow_tz.localize(datetime.combine(month_start_date, datetime.min.time()))
        month_start_utc = month_start.astimezone(pytz.UTC).replace(tzinfo=None)

        last_month_start_date = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        this_month_start_date = today.replace(day=1)
        last_month_start = moscow_tz.localize(datetime.combine(last_month_start_date, datetime.min.time()))
        last_month_end = moscow_tz.localize(datetime.combine(this_month_start_date, datetime.min.time()))
        last_month_start_utc = last_month_start.astimezone(pytz.UTC).replace(tzinfo=None)
        last_month_end_utc = last_month_end.astimezone(pytz.UTC).replace(tzinfo=None)

        total_users = await count_total_users(session)
        users_updated_today = await count_users_updated_today(session, today_start_utc)
        registrations_today = await count_users_registered_since(session, today_start_utc)
        registrations_yesterday = await count_users_registered_between(
            session, yesterday_start_utc, yesterday_end_utc
        )
        registrations_week = await count_users_registered_since(session, week_start_utc)
        registrations_month = await count_users_registered_since(session, month_start_utc)
        registrations_last_month = await count_users_registered_between(
            session, last_month_start_utc, last_month_end_utc
        )
        total_keys = await count_total_keys(session)
        active_keys = await count_active_keys(session)
        active_paid_keys = await count_active_paid_keys(session)
        active_trial_keys = await count_active_trial_keys(session)
        tariff_counts, no_tariff_keys = await get_tariff_distribution(session, include_unbound=True)

        expired_keys = total_keys - active_keys
        tariff_ids = [tid for tid, _ in tariff_counts]
        tariff_names, tariff_groups, tariff_subgroups, tariff_durations = (
            await get_tariff_names_groups_subgroups_durations(session, tariff_ids)
        )

        grouped_tariffs = {}
        for tid, count in tariff_counts:
            group = tariff_groups.get(tid, "unknown")
            subgroup = tariff_subgroups.get(tid)
            if group not in grouped_tariffs:
                grouped_tariffs[group] = {}
            if subgroup not in grouped_tariffs[group]:
                grouped_tariffs[group][subgroup] = []
            grouped_tariffs[group][subgroup].append((tid, count))

        tariff_stats_text = ""
        duration_buckets = Counter()
        now_ts = int(now.timestamp() * 1000)

        for key in no_tariff_keys:
            duration_days = round((key["expiry_time"] - now_ts) / (1000 * 60 * 60 * 24))
            if 25 <= duration_days <= 35:
                bucket = "Без тарифа: 1 мес"
            elif 80 <= duration_days <= 100:
                bucket = "Без тарифа: 3 мес"
            elif 170 <= duration_days <= 200:
                bucket = "Без тарифа: 6 мес"
            elif 350 <= duration_days <= 380:
                bucket = "Без тарифа: 12 мес"
            else:
                bucket = "Без тарифа: прочее"
            duration_buckets[bucket] += 1

        bucket_order = {
            "Без тарифа: 1 мес": 1,
            "Без тарифа: 3 мес": 2,
            "Без тарифа: 6 мес": 3,
            "Без тарифа: 12 мес": 4,
            "Без тарифа: прочее": 5,
        }
        sorted_buckets = sorted(duration_buckets.items(), key=lambda x: bucket_order.get(x[0], 999))

        for name, count in sorted_buckets:
            tariff_stats_text += f"├ {name}: <b>{count}</b>\n"

        for _group_idx, (group, subgroups_dict) in enumerate(grouped_tariffs.items()):
            group_total = 0
            for tariffs_list in subgroups_dict.values():
                group_total += sum(count for _, count in tariffs_list)

            tariff_stats_text += f"Тариф <b>{group}</b> (<b>{group_total}</b>)\n"
            sorted_subgroups = sorted(subgroups_dict.items(), key=lambda x: (x[0] is None, x[0] or ""))
            for subgroup_idx, (subgroup, tariffs) in enumerate(sorted_subgroups):
                sorted_tariffs = sorted(tariffs, key=lambda x: tariff_durations.get(x[0], 0))
                subgroup_total = sum(count for _, count in sorted_tariffs)
                is_last_subgroup = subgroup_idx == len(sorted_subgroups) - 1

                if subgroup:
                    prefix = "└─" if is_last_subgroup else "├─"
                    tariff_stats_text += f" {prefix} Подгруппа: <b>{subgroup}</b> (<b>{subgroup_total}</b>)\n"

                for tariff_idx, (tid, count) in enumerate(sorted_tariffs):
                    name = tariff_names.get(tid, f"ID {tid}")
                    is_last_tariff = tariff_idx == len(sorted_tariffs) - 1

                    if subgroup:
                        if is_last_tariff and is_last_subgroup:
                            prefix = "    └─"
                        else:
                            prefix = "    ├─"
                    else:
                        if is_last_tariff and is_last_subgroup:
                            prefix = " └─"
                        else:
                            prefix = " ├─"
                    tariff_stats_text += f"{prefix} {name}: <b>{count}</b>\n"

        tariff_stats_text = (
            "└ По тарифам и срокам:\n" + tariff_stats_text if tariff_stats_text else "└ Нет данных по тарифам\n"
        )

        total_referrals = await count_total_referrals(session)

        total_payments_today = await sum_payments_since(session, today_start.replace(tzinfo=None))
        total_payments_yesterday = await sum_payments_between(
            session, yesterday_start.replace(tzinfo=None), yesterday_end.replace(tzinfo=None)
        )
        total_payments_week = await sum_payments_since(session, week_start.replace(tzinfo=None))
        total_payments_month = await sum_payments_since(session, month_start.replace(tzinfo=None))
        total_payments_last_month = await sum_payments_between(
            session, last_month_start.replace(tzinfo=None), last_month_end.replace(tzinfo=None)
        )
        total_payments_all_time = await sum_total_payments(session)
        hot_leads_count = await count_hot_leads(session)

        update_time = now.strftime("%d.%m.%y %H:%M:%S")

        stats_message = (
            f"📊 <b>Статистика проекта</b>\n\n"
            f"👤 <b>Пользователи:</b>\n"
            f"<blockquote>"
            f"├ 🗓️ За день: <b>{registrations_today}</b>\n"
            f"├ 🗓️ Вчера: <b>{registrations_yesterday}</b>\n"
            f"├ 📆 За неделю: <b>{registrations_week}</b>\n"
            f"├ 🗓️ За месяц: <b>{registrations_month}</b>\n"
            f"├ 📅 За прошлый месяц: <b>{registrations_last_month}</b>\n"
            f"└ 🌐 Всего: <b>{total_users}</b>\n"
            f"</blockquote>\n"
            f"💡 <b>Активность:</b>\n"
            f"└ 👥 Сегодня были активны: <b>{users_updated_today}</b>\n\n"
            f"🤝 <b>Реферальная система:</b>\n"
            f"└ 👥 Всего привлечено: <b>{total_referrals}</b>\n\n"
            f"🔐 <b>Подписки:</b>\n"
            f"<blockquote>"
            f"├ 📦 Всего сгенерировано: <b>{total_keys}</b>\n"
            f"├ ✅ Активных: <b>{active_keys}</b>\n"
            f"│  ├ 💰 Платных: <b>{active_paid_keys}</b>\n"
            f"│  └ 🧪 Триальных: <b>{active_trial_keys}</b>\n"
            f"├ ❌ Просроченных: <b>{expired_keys}</b>\n"
            f"{tariff_stats_text}"
            f"</blockquote>\n"
            f"💰 <b>Финансы:</b>\n"
            f"<blockquote>"
            f"├ 📅 За день: <b>{total_payments_today} ₽</b>\n"
            f"├ 📆 Вчера: <b>{total_payments_yesterday} ₽</b>\n"
            f"├ 📆 За неделю: <b>{total_payments_week} ₽</b>\n"
            f"├ 📆 За месяц: <b>{total_payments_month} ₽</b>\n"
            f"├ 📆 Прошлый месяц: <b>{total_payments_last_month} ₽</b>\n"
            f"└ 🏦 Всего: <b>{total_payments_all_time} ₽</b>\n"
            f"</blockquote>\n"
            f"🔥 <b>Горячие лиды: {hot_leads_count}</b>\n"
            f"⏱️ <i>Последнее обновление:</i> <code>{update_time}</code>"
        )

        extra_blocks = await run_hooks("admin_stats", session=session, now=now)
        if extra_blocks:
            stats_message += "\n\n" + "\n\n".join([str(b) for b in extra_blocks if b])

        new_kb = build_stats_kb()
        current_text = callback_query.message.html_text or callback_query.message.text or ""
        cur_kb = callback_query.message.reply_markup
        cur_kb_json = cur_kb.model_dump_json() if cur_kb else None
        new_kb_json = new_kb.model_dump_json() if new_kb else None

        if current_text == stats_message and cur_kb_json == new_kb_json:
            try:
                await callback_query.answer()
            except Exception:
                pass
        else:
            await callback_query.message.edit_text(text=stats_message, reply_markup=new_kb)

    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.error(f"Error in user_stats_menu: {e}")
    except Exception as e:
        logger.error(f"Error in user_stats_menu: {e}")
        await callback_query.answer("Произошла ошибка при получении статистики", show_alert=True)


@router.callback_query(AdminPanelCallback.filter(F.action == "stats_audit"), IsAdminFilter())
async def handle_stats_audit(callback_query: CallbackQuery, session: AsyncSession):
    """Статистика аудита за вчера (МСК): объём по шагам, % ошибок, воронка."""
    kb = build_admin_back_kb("stats")
    try:
        moscow_tz = pytz.timezone("Europe/Moscow")
        now = datetime.now(moscow_tz)
        yesterday_date = (now.date() - timedelta(days=1))
        start = moscow_tz.localize(datetime.combine(yesterday_date, datetime.min.time()))
        end = start + timedelta(days=1)
        start_utc = start.astimezone(pytz.UTC)
        end_utc = end.astimezone(pytz.UTC)

        stats = await get_audit_stats(session, date_from=start_utc, date_to=end_utc)
        funnel = await get_audit_funnel(session, date_from=start_utc, date_to=end_utc)

        summary = stats["summary"]
        by_path = stats["by_path"]

        lines = [
            f"📊 <b>Аудит за {yesterday_date.strftime('%d.%m.%Y')}</b> (МСК)",
            "",
            f"📎 Событий: <b>{summary['total_events']}</b> │ Уникальных пользователей: <b>{summary['unique_users']}</b>",
            "",
            "<b>По шагам (топ по объёму):</b>",
        ]
        for row in by_path[:8]:
            fail_mark = "⚠️" if row["fail_rate_pct"] > 10 else "✅"
            lines.append(
                f"{fail_mark} {row['label']}: {row['total']} (ок: {row['success']}, ошибок: {row['fail']}, {row['fail_rate_pct']}% ошибок)"
            )
        lines.append("")
        by_step_totals = {row["step"]: row["total"] for row in by_path}
        funnel_counts = {s["step"]: s["count"] for s in funnel}
        pay_start = by_step_totals.get("pay_start", 0)
        pay_ok = max(by_step_totals.get("pay", 0), funnel_counts.get("pay", 0))
        key_created = by_step_totals.get("key_create", 0)
        connect_ok = max(by_step_totals.get("connect", 0), funnel_counts.get("connect", 0))
        pct_pay = round(100.0 * pay_ok / pay_start, 1) if pay_start else 0
        pct_connect = round(100.0 * connect_ok / key_created, 1) if key_created else 0
        lines.append("<b>Оплата:</b> начало {0}, успешная {1}, % успешных от созданных: {2}%".format(pay_start, pay_ok, pct_pay))
        lines.append("<b>Подписка:</b> оформлена {0}, подключена {1}, % успешных подключений: {2}%".format(key_created, connect_ok, pct_connect))
        lines.append("")
        lines.append("<b>Воронка (уник. пользователей по шагам):</b>")
        for step in funnel:
            conv = f" → {step['conversion_from_prev_pct']}%" if step["conversion_from_prev_pct"] is not None else ""
            lines.append(f"  • {step['label']}: {step['count']} польз.{conv}")

        text = "\n".join(lines)
        await callback_query.message.edit_text(
            text,
            reply_markup=kb,
        )
        await callback_query.answer()
    except Exception as e:
        logger.exception("Ошибка при получении статистики аудита: %s", e)
        await callback_query.answer("Ошибка при загрузке статистики аудита", show_alert=True)
        await callback_query.message.edit_text(
            f"❗ Ошибка: {e}",
            reply_markup=kb,
        )


async def _build_audit_report(session: AsyncSession) -> tuple[str | None, str | None]:
    """Собирает текст отчёта аудита. Возвращает (text, error): при успехе error=None; при отключённом Redis text=None, error=None."""
    try:
        stats = await get_audit_stats_from_redis(max_events=5000)
        funnel = await get_audit_funnel_from_redis(max_events=5000)
        if stats is None or funnel is None:
            return (None, None)
        summary = stats["summary"]
        by_path = stats["by_path"]
        if summary["total_events"] == 0:
            moscow_tz = pytz.timezone("Europe/Moscow")
            now = datetime.now(moscow_tz)
            end_utc = now.astimezone(pytz.UTC)
            start_utc = end_utc - timedelta(hours=24)
            stats = await get_audit_stats(session, date_from=start_utc, date_to=end_utc)
            funnel = await get_audit_funnel(session, date_from=start_utc, date_to=end_utc)
            summary = stats["summary"]
            by_path = stats["by_path"]
            header = "📊 <b>Аудит из БД</b> (буфер Redis пуст; последние 24 ч)"
        else:
            header = "📊 <b>Аудит из Redis</b> (буфер, последние события)"
        lines = [
            header,
            "",
            f"📎 Событий: <b>{summary['total_events']}</b> │ Уникальных пользователей: <b>{summary['unique_users']}</b>",
            "",
            "<b>По шагам (топ по объёму):</b>",
        ]
        for row in by_path[:8]:
            fail_mark = "⚠️" if row["fail_rate_pct"] > 10 else "✅"
            lines.append(
                f"{fail_mark} {row['label']}: {row['total']} (ок: {row['success']}, ошибок: {row['fail']}, {row['fail_rate_pct']}% ошибок)"
            )
        lines.append("")
        by_step_totals = {row["step"]: row["total"] for row in by_path}
        funnel_counts = {s["step"]: s["count"] for s in funnel}
        pay_start = by_step_totals.get("pay_start", 0)
        pay_ok = max(by_step_totals.get("pay", 0), funnel_counts.get("pay", 0))
        key_created = by_step_totals.get("key_create", 0)
        connect_ok = max(by_step_totals.get("connect", 0), funnel_counts.get("connect", 0))
        pct_pay = round(100.0 * pay_ok / pay_start, 1) if pay_start else 0
        pct_connect = round(100.0 * connect_ok / key_created, 1) if key_created else 0
        lines.append("<b>Оплата:</b> начало {0}, успешная {1}, % успешных от созданных: {2}%".format(pay_start, pay_ok, pct_pay))
        lines.append("<b>Подписка:</b> оформлена {0}, подключена {1}, % успешных подключений: {2}%".format(key_created, connect_ok, pct_connect))
        lines.append("")
        lines.append("<b>Воронка (уник. пользователей по шагам):</b>")
        for step in funnel:
            conv = f" → {step['conversion_from_prev_pct']}%" if step["conversion_from_prev_pct"] is not None else ""
            lines.append(f"  • {step['label']}: {step['count']} польз.{conv}")
        return ("\n".join(lines), None)
    except Exception as e:
        logger.exception("Ошибка при получении аудита: %s", e)
        return (None, str(e))


@router.message(F.text.in_(["Аудит", "аудит"]), IsAdminFilter())
async def handle_audit_command(message: Message, session: AsyncSession):
    """По команде «Аудит» — отправить статистику аудита из буфера Redis; при пустом буфере — из БД за 24 ч."""
    text, err = await _build_audit_report(session)
    if err:
        await message.answer(f"❗ Ошибка: {escape(err)}")
        return
    if text is None:
        await message.answer(
            "📊 Буфер аудита в Redis выключен (AUDIT_REDIS_BUFFER_ENABLED=False). "
            "Используйте кнопку «Аудит (воронки)» в Статистике для отчёта из БД.",
        )
        return
    await message.answer(text, reply_markup=build_audit_refresh_kb())


@router.callback_query(AdminPanelCallback.filter(F.action == "audit_refresh"), IsAdminFilter())
async def handle_audit_refresh(callback_query: CallbackQuery, session: AsyncSession):
    """Обновить отчёт аудита по нажатию кнопки «Обновить» под сообщением."""
    await callback_query.answer()
    text, err = await _build_audit_report(session)
    if err:
        await callback_query.message.edit_text(f"❗ Ошибка: {escape(err)}")
        return
    if text is None:
        await callback_query.message.edit_text(
            "📊 Буфер аудита в Redis выключен (AUDIT_REDIS_BUFFER_ENABLED=False). "
            "Используйте кнопку «Аудит (воронки)» в Статистике для отчёта из БД.",
        )
        return
    try:
        await callback_query.message.edit_text(text, reply_markup=build_audit_refresh_kb())
    except TelegramBadRequest:
        pass


@router.callback_query(AdminPanelCallback.filter(F.action == "stats_export_users_csv"), IsAdminFilter())
async def handle_export_users_csv(callback_query: CallbackQuery, session: AsyncSession):
    kb = build_admin_back_kb("stats")
    try:
        export = await export_users_csv(session)
        await callback_query.message.answer_document(document=export, caption="📅 Экспорт пользователей в CSV")
    except Exception as e:
        logger.error(f"Ошибка при экспорте пользователей: {e}")
        await callback_query.message.edit_text(text=f"❗ Ошибка: {e}", reply_markup=kb)


@router.callback_query(AdminPanelCallback.filter(F.action == "stats_export_payments_csv"), IsAdminFilter())
async def handle_export_payments_csv(callback_query: CallbackQuery, session: AsyncSession):
    kb = build_admin_back_kb("stats")
    try:
        export = await export_payments_csv(session)
        await callback_query.message.answer_document(document=export, caption="📅 Экспорт платежей в CSV")
    except Exception as e:
        logger.error(f"Ошибка при экспорте платежей: {e}")
        await callback_query.message.edit_text(text=f"❗ Ошибка: {e}", reply_markup=kb)


@router.callback_query(AdminPanelCallback.filter(F.action == "stats_export_hot_leads_csv"), IsAdminFilter())
async def handle_export_hot_leads_csv(callback_query: CallbackQuery, session: AsyncSession):
    kb = build_admin_back_kb("stats")
    try:
        export = await export_hot_leads_csv(session)
        await callback_query.message.answer_document(document=export, caption="📅 Экспорт горящих лидов")
    except Exception as e:
        logger.error(f"Ошибка при экспорте горящих лидов: {e}")
        await callback_query.message.edit_text(text=f"❗ Ошибка: {e}", reply_markup=kb)


@router.callback_query(AdminPanelCallback.filter(F.action == "stats_export_keys_csv"), IsAdminFilter())
async def handle_export_keys_csv(callback_query: CallbackQuery, session: AsyncSession):
    kb = build_admin_back_kb("stats")
    try:
        export = await export_keys_csv(session)
        await callback_query.message.answer_document(document=export, caption="📅 Экспорт подписок в CSV")
    except Exception as e:
        logger.error(f"Ошибка при экспорте подписок: {e}")
        await callback_query.message.edit_text(text=f"❗ Ошибка: {e}", reply_markup=kb)


async def send_daily_stats_report(session: AsyncSession):
    try:
        moscow_tz = pytz.timezone("Europe/Moscow")
        now_moscow = datetime.now(moscow_tz)
        update_time = now_moscow.strftime("%d.%m.%y %H:%M")

        report_date = now_moscow.date() - timedelta(days=1)

        start = moscow_tz.localize(datetime.combine(report_date, datetime.min.time()))
        end = moscow_tz.localize(datetime.combine(report_date + timedelta(days=1), datetime.min.time()))

        start_utc = start.astimezone(pytz.UTC).replace(tzinfo=None)
        end_utc = end.astimezone(pytz.UTC).replace(tzinfo=None)

        registrations_today = await count_users_registered_between(session, start_utc, end_utc)
        payments_today = await sum_payments_between(session, start.replace(tzinfo=None), end.replace(tzinfo=None))
        active_keys = await count_active_keys(session)

        text = (
            f"🗓️ <b>Сводка за {report_date.strftime('%d.%m.%Y')} с 00:00 до 23:59 МСК</b>\n\n"
            f"👤 Новых пользователей: <b>{registrations_today}</b>\n"
            f"💰 Оплачено: <b>{payments_today} ₽</b>\n"
            f"🔐 Активных подписок: <b>{active_keys}</b>\n\n"
            f"⏱️ <i>Отчёт сгенерирован: {update_time} МСК</i>"
        )

        for admin_id in ADMIN_ID:
            await bot.send_message(admin_id, text)

    except Exception as e:
        logger.error(f"[Stats] Ошибка при отправке статистики: {e}")


@router.message(F.text == "Сводка", IsAdminFilter())
async def test_stats_command(message: Message, session: AsyncSession):
    await send_daily_stats_report(session)
