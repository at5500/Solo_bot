from datetime import datetime

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_servers, update_key_expiry
from database.models import Key, Server, Tariff
from filters.admin import IsAdminFilter
from middlewares.session import release_session_early
from handlers.keys.operations import renew_key_in_cluster
from logger import logger

from ..panel.keyboard import build_admin_back_kb
from .base import AdminClusterStates, router
from .keyboard import (
    AdminClusterCallback,
    AdminServerCallback,
    build_cluster_management_kb,
    build_manage_cluster_kb,
)


@router.callback_query(AdminClusterCallback.filter(F.action == "manage"), IsAdminFilter())
async def handle_clusters_manage(
    callback_query: types.CallbackQuery,
    callback_data: AdminClusterCallback,
    session: AsyncSession,
):
    cluster_name = callback_data.data

    result = await session.execute(
        select(Server.tariff_group)
        .where(
            Server.cluster_name == cluster_name,
            Server.tariff_group.isnot(None),
        )
        .limit(1)
    )
    row = result.first()
    tariff_group = row[0] if row else "—"

    result = await session.execute(select(Server.server_name).where(Server.cluster_name == cluster_name))
    server_names = [row[0] for row in result.all()]
    result = await session.execute(
        select(func.count(func.distinct(Key.tg_id))).where(
            (Key.server_id == cluster_name) | (Key.server_id.in_(server_names))
        )
    )
    user_count = result.scalar() or 0

    result = await session.execute(
        select(func.count()).where((Key.server_id == cluster_name) | (Key.server_id.in_(server_names)))
    )
    subscription_count = result.scalar() or 0

    text = (
        f"<b>🔧 Управление кластером <code>{cluster_name}</code></b>\n\n"
        f"📁 <b>Тарифная группа:</b> <code>{tariff_group}</code>\n"
        f"👥 <b>Пользователей на кластере:</b> <code>{user_count}</code>\n"
        f"🔑 <b>Всего подписок:</b> <code>{subscription_count}</code>"
    )

    await callback_query.message.edit_text(
        text=text,
        reply_markup=build_cluster_management_kb(cluster_name),
    )


@router.callback_query(F.data.startswith("cluster_servers|"), IsAdminFilter())
async def handle_cluster_servers(callback: CallbackQuery, session: AsyncSession):
    cluster_name = callback.data.split("|", 1)[1]
    servers = await get_servers(session=session, include_enabled=True)
    cluster_servers = servers.get(cluster_name, [])

    from handlers.utils import ALLOWED_GROUP_CODES

    allowed = set(ALLOWED_GROUP_CODES)
    lines = []
    for s in cluster_servers:
        tids = s.get("tariff_ids") or []
        subs = s.get("tariff_subgroups") or []
        if tids:
            subs_str = f"{len(tids)} тариф(ов)"
        elif subs:
            subs_str = ", ".join(sorted(subs))
        else:
            subs_str = "—"

        grps = s.get("special_groups") or []
        grps = [g for g in grps if g in allowed]
        grps_str = ", ".join(sorted(grps)) if grps else "—"

        lines.append(f"• {s.get('server_name', '?')} — {subs_str} | {grps_str}")

    details = "\n".join(lines) if lines else "нет серверов"

    await callback.message.edit_text(
        text=(
            f"<b>📡 Серверы в кластере {cluster_name}</b>\n<i>подгруппы | спецгруппы:</i>\n"
            f"<blockquote>{details}</blockquote>"
        ),
        reply_markup=build_manage_cluster_kb(cluster_servers, cluster_name),
    )


@router.callback_query(AdminClusterCallback.filter(F.action == "add_time"), IsAdminFilter())
async def handle_add_time(
    callback_query: CallbackQuery,
    callback_data: AdminClusterCallback,
    state: FSMContext,
):
    cluster_name = callback_data.data
    await state.set_state(AdminClusterStates.waiting_for_days_input)
    await state.update_data(cluster_name=cluster_name)

    await callback_query.message.edit_text(
        f"⏳ Введите количество дней, на которое хотите продлить все подписки в кластере <b>{cluster_name}</b>:",
        reply_markup=build_admin_back_kb("clusters"),
    )


@router.message(AdminClusterStates.waiting_for_days_input, IsAdminFilter())
async def handle_days_input(message: Message, state: FSMContext, session: AsyncSession):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError

        user_data = await state.get_data()
        cluster_name = user_data.get("cluster_name")
        add_ms = days * 86400 * 1000

        logger.info(f"[Cluster Extend] Добавляем {days} дней для кластера: {cluster_name}")

        server_stmt = select(Server.server_name).where(Server.cluster_name == cluster_name)
        server_rows = await session.execute(server_stmt)
        server_names = [row[0] for row in server_rows.all()]
        server_names.append(cluster_name)

        servers = await get_servers(session=session)
        cluster_servers = servers.get(cluster_name, [])

        if not cluster_servers:
            await message.answer("❌ Не найдены серверы в кластере.")
            await state.clear()
            return

        is_full_remnawave = all(str(s.get("panel_type", "")).lower() == "remnawave" for s in cluster_servers)

        if is_full_remnawave:
            uuid_rows = await session.execute(
                select(Key.client_id).where(
                    Key.server_id.in_(server_names),
                    Key.client_id.isnot(None),
                )
            )
            uuids = [row[0] for row in uuid_rows.all()]

            if not uuids:
                await message.answer("❌ Нет валидных подписок для продления.")
                await state.clear()
                return

            api_url = cluster_servers[0].get("api_url", "")
            if not api_url:
                await message.answer("❌ Не найден URL панели для кластера.")
                await state.clear()
                return

            from panels.remnawave import RemnawaveAPI

            remna = RemnawaveAPI(api_url)

            try:
                result_bulk = await remna.bulk_extend_expiration_date(uuids, days)
            finally:
                await remna.aclose()

            if result_bulk is None:
                await message.answer("❌ Ошибка при обращении к API панели.")
                await state.clear()
                return

            affected = result_bulk.get("affectedRows", 0)
            logger.info(f"[Cluster Extend] Bulk API: продлено {affected} подписок на {days} дней")

            await session.execute(
                update(Key)
                .where(Key.server_id.in_(server_names))
                .values(expiry_time=Key.expiry_time + add_ms)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

            await message.answer(
                f"✅ Время подписки продлено на <b>{days} дней</b> для <b>{affected}</b> пользователей в кластере <b>{cluster_name}</b>."
            )
        else:
            result = await session.execute(select(Key).where(Key.server_id.in_(server_names)))
            keys = result.scalars().all()

            if not keys:
                await message.answer("❌ Нет подписок в этом кластере или сервере.")
                await state.clear()
                return

            await release_session_early(session)
            for key in keys:
                new_expiry = key.expiry_time + add_ms

                traffic_limit = 0
                device_limit = 0
                key_subgroup = None
                if key.tariff_id:
                    tariff_result = await session.execute(
                        select(Tariff.traffic_limit, Tariff.device_limit, Tariff.subgroup_title).where(
                            Tariff.id == key.tariff_id,
                            Tariff.is_active.is_(True),
                        )
                    )
                    tariff = tariff_result.first()
                    if tariff:
                        traffic_limit = int(tariff[0]) if tariff[0] is not None else 0
                        device_limit = int(tariff[1]) if tariff[1] is not None else 0
                        key_subgroup = tariff[2]

                await renew_key_in_cluster(
                    cluster_name,
                    email=key.email,
                    client_id=key.client_id,
                    new_expiry_time=new_expiry,
                    total_gb=traffic_limit,
                    session=session,
                    hwid_device_limit=device_limit,
                    reset_traffic=False,
                    target_subgroup=key_subgroup,
                    old_subgroup=key_subgroup,
                    plan=key.tariff_id,
                )
                await update_key_expiry(session, key.client_id, new_expiry)

                logger.info(f"[Cluster Extend] {key.email} +{days}д → {datetime.utcfromtimestamp(new_expiry / 1000)}")

            await message.answer(
                f"✅ Время подписки продлено на <b>{days} дней</b> всем пользователям в кластере <b>{cluster_name}</b>."
            )

    except ValueError:
        await message.answer("❌ Введите корректное число дней.")
    except Exception as e:
        logger.error(f"[Cluster Extend] Ошибка при добавлении дней: {e}")
        await message.answer("❌ Произошла ошибка при продлении времени.")
    finally:
        await state.clear()


@router.callback_query(AdminClusterCallback.filter(F.action == "rename"), IsAdminFilter())
async def handle_rename_cluster(
    callback_query: CallbackQuery,
    callback_data: AdminClusterCallback,
    state: FSMContext,
):
    cluster_name = callback_data.data
    await state.update_data(old_cluster_name=cluster_name)

    text = (
        f"✏️ <b>Введите новое имя для кластера '{cluster_name}':</b>\n\n"
        "▸ Имя должно быть уникальным.\n"
        "▸ Имя не должно превышать 12 символов.\n\n"
        "📌 <i>Пример:</i> <code>new_cluster</code>"
    )

    await callback_query.message.edit_text(
        text=text,
        reply_markup=build_admin_back_kb("clusters"),
    )
    await state.set_state(AdminClusterStates.waiting_for_new_cluster_name)


@router.message(AdminClusterStates.waiting_for_new_cluster_name, IsAdminFilter())
async def handle_new_cluster_name_input(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer(
            text="❌ Имя кластера не может быть пустым! Попробуйте снова.",
            reply_markup=build_admin_back_kb("clusters"),
        )
        return

    new_cluster_name = message.text.strip()
    if len(new_cluster_name) > 12:
        await message.answer(
            text="❌ Имя кластера не должно превышать 12 символов! Попробуйте снова.",
            reply_markup=build_admin_back_kb("clusters"),
        )
        return

    user_data = await state.get_data()
    old_cluster_name = user_data.get("old_cluster_name")

    try:
        result = await session.execute(
            select(Server.cluster_name).where(Server.cluster_name == new_cluster_name).limit(1)
        )
        existing_cluster = result.scalar()

        if existing_cluster:
            await message.answer(
                text=f"❌ Кластер с именем '{new_cluster_name}' уже существует. Введите другое имя.",
                reply_markup=build_admin_back_kb("clusters"),
            )
            return

        keys_count_result = await session.execute(
            select(func.count()).select_from(Key).where(Key.server_id == old_cluster_name)
        )
        keys_count = keys_count_result.scalar()

        await session.execute(
            update(Server).where(Server.cluster_name == old_cluster_name).values(cluster_name=new_cluster_name)
        )

        if keys_count > 0:
            await session.execute(
                update(Key).where(Key.server_id == old_cluster_name).values(server_id=new_cluster_name)
            )

        await session.commit()

        await message.answer(
            text=f"✅ Название кластера успешно изменено с '{old_cluster_name}' на '{new_cluster_name}'!",
            reply_markup=build_admin_back_kb("clusters"),
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка при смене имени кластера {old_cluster_name} на {new_cluster_name}: {e}")
        await message.answer(
            text=f"❌ Произошла ошибка при смене имени кластера: {e}",
            reply_markup=build_admin_back_kb("clusters"),
        )
    finally:
        await state.clear()


@router.callback_query(AdminServerCallback.filter(F.action == "rename"), IsAdminFilter())
async def handle_rename_server(
    callback_query: CallbackQuery,
    callback_data: AdminServerCallback,
    state: FSMContext,
    session: AsyncSession,
):
    old_server_name = callback_data.data

    servers = await get_servers(session=session)
    cluster_name = None
    for c_name, server_list in servers.items():
        for server in server_list:
            if server["server_name"] == old_server_name:
                cluster_name = c_name
                break
        if cluster_name:
            break

    if not cluster_name:
        await callback_query.message.edit_text(
            text=f"❌ Не удалось найти кластер для сервера '{old_server_name}'.",
            reply_markup=build_admin_back_kb("clusters"),
        )
        return

    await state.update_data(old_server_name=old_server_name, cluster_name=cluster_name)

    text = (
        f"✏️ <b>Введите новое имя для сервера '{old_server_name}' в кластере '{cluster_name}':</b>\n\n"
        "▸ Имя должно быть уникальным в пределах кластера.\n"
        "▸ Имя не должно превышать 12 символов.\n\n"
        "📌 <i>Пример:</i> <code>new_server</code>"
    )

    await callback_query.message.edit_text(
        text=text,
        reply_markup=build_admin_back_kb("clusters"),
    )
    await state.set_state(AdminClusterStates.waiting_for_new_server_name)


@router.message(AdminClusterStates.waiting_for_new_server_name, IsAdminFilter())
async def handle_new_server_name_input(message: Message, state: FSMContext, session: AsyncSession):
    if not message.text:
        await message.answer(
            text="❌ Имя сервера не может быть пустым! Попробуйте снова.",
            reply_markup=build_admin_back_kb("clusters"),
        )
        return

    new_server_name = message.text.strip()
    if len(new_server_name) > 12:
        await message.answer(
            text="❌ Имя сервера не должно превышать 12 символов! Попробуйте снова.",
            reply_markup=build_admin_back_kb("clusters"),
        )
        return

    user_data = await state.get_data()
    old_server_name = user_data.get("old_server_name")
    cluster_name = user_data.get("cluster_name")

    try:
        result = await session.execute(
            select(Server)
            .where(
                Server.cluster_name == cluster_name,
                Server.server_name == new_server_name,
            )
            .limit(1)
        )
        existing_server = result.scalar()
        if existing_server:
            await message.answer(
                text=(
                    f"❌ Сервер с именем '{new_server_name}' уже существует в кластере '{cluster_name}'. "
                    f"Введите другое имя."
                ),
                reply_markup=build_admin_back_kb("clusters"),
            )
            return

        result = await session.execute(select(func.count()).select_from(Key).where(Key.server_id == old_server_name))
        keys_count = result.scalar()

        await session.execute(
            update(Server)
            .where(
                Server.cluster_name == cluster_name,
                Server.server_name == old_server_name,
            )
            .values(server_name=new_server_name)
        )

        if keys_count > 0:
            await session.execute(update(Key).where(Key.server_id == old_server_name).values(server_id=new_server_name))

        await session.commit()

        await message.answer(
            text=(
                f"✅ Название сервера успешно изменено с '{old_server_name}' на '{new_server_name}' "
                f"в кластере '{cluster_name}'!"
            ),
            reply_markup=build_admin_back_kb("clusters"),
        )
    except Exception as e:
        await session.rollback()
        logger.error(f"Ошибка при смене имени сервера {old_server_name} на {new_server_name}: {e}")
        await message.answer(
            text=f"❌ Произошла ошибка при смене имени сервера: {e}",
            reply_markup=build_admin_back_kb("clusters"),
        )
    finally:
        await state.clear()
