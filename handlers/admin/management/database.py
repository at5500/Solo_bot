import os
import re
import shutil
import subprocess
import sys
import traceback

from tempfile import NamedTemporaryFile

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import DB_NAME, DB_PASSWORD, DB_USER, PG_HOST, PG_IN_DOCKER, PG_PORT
from core.executor import run_io
from filters.admin import HasPermission
from filters.permissions import PERM_MANAGEMENT
from logger import logger
from utils.backup import _find_docker_postgres_container


_PG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_pg_identifier(value: str, label: str) -> str:
    if not _PG_IDENT_RE.match(value):
        raise ValueError(f"Недопустимый PostgreSQL-идентификатор ({label}): {value!r}")
    return value


from . import router
from .keyboard import AdminPanelCallback, build_back_to_db_menu, build_database_kb, build_export_db_sources_kb


DOCKER_POSTGRES_CONTAINER = "solobot-postgres"

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def sync_restore_database(
    tmp_path: str,
    db_name: str,
    db_user: str,
    db_password: str,
    pg_host: str,
    pg_port: str,
) -> tuple[bool, str]:
    """Восстановление БД из файла. Вызывать через run_io()."""
    is_custom_dump = False
    with open(tmp_path, "rb") as f:
        if f.read(5) == b"PGDMP":
            is_custom_dump = True

    use_docker = PG_IN_DOCKER
    docker_container = _find_docker_postgres_container() if use_docker else None

    if use_docker and not docker_container:
        return False, f"Контейнер PostgreSQL '{DOCKER_POSTGRES_CONTAINER}' не найден или не запущен"

    def _run_admin_psql(sql: str) -> None:
        if use_docker:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-e",
                    f"PGPASSWORD={db_password}",
                    docker_container,
                    "psql",
                    "-U",
                    db_user,
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "5432",
                    "-d",
                    "postgres",
                    "-c",
                    sql,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return

        if shutil.which("psql") is None:
            raise FileNotFoundError("psql не найден на хосте и контейнер PostgreSQL не обнаружен")

        env = os.environ.copy()
        env["PGPASSWORD"] = db_password
        subprocess.run(
            [
                "psql",
                "-U",
                db_user,
                "-h",
                pg_host,
                "-p",
                pg_port,
                "-d",
                "postgres",
                "-c",
                sql,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    try:
        safe_name = _safe_pg_identifier(db_name, "db_name")
        safe_user = _safe_pg_identifier(db_user, "db_user")
        _run_admin_psql(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{safe_name}' AND pid <> pg_backend_pid();"
        )
        _run_admin_psql(f"DROP DATABASE IF EXISTS {safe_name};")
        _run_admin_psql(f"CREATE DATABASE {safe_name} OWNER {safe_user};")
    except ValueError as e:
        return False, str(e)
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e))

    try:
        if use_docker:
            with open(tmp_path, "rb") as dump_file:
                if is_custom_dump:
                    result = subprocess.run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            "-e",
                            f"PGPASSWORD={db_password}",
                            docker_container,
                            "pg_restore",
                            f"--dbname={db_name}",
                            "-U",
                            db_user,
                            "-h",
                            "127.0.0.1",
                            "-p",
                            "5432",
                            "--no-owner",
                            "--exit-on-error",
                        ],
                        stdin=dump_file,
                        capture_output=True,
                    )
                else:
                    result = subprocess.run(
                        [
                            "docker",
                            "exec",
                            "-i",
                            "-e",
                            f"PGPASSWORD={db_password}",
                            docker_container,
                            "psql",
                            "-U",
                            db_user,
                            "-h",
                            "127.0.0.1",
                            "-p",
                            "5432",
                            "-d",
                            db_name,
                        ],
                        stdin=dump_file,
                        capture_output=True,
                    )
        else:
            env = os.environ.copy()
            env["PGPASSWORD"] = db_password
            if is_custom_dump:
                if shutil.which("pg_restore") is None:
                    return False, "pg_restore не найден на хосте и контейнер PostgreSQL не обнаружен"
                result = subprocess.run(
                    [
                        "pg_restore",
                        f"--dbname={db_name}",
                        "-U",
                        db_user,
                        "-h",
                        pg_host,
                        "-p",
                        pg_port,
                        "--no-owner",
                        "--exit-on-error",
                        tmp_path,
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                )
            else:
                if shutil.which("psql") is None:
                    return False, "psql не найден на хосте и контейнер PostgreSQL не обнаружен"
                result = subprocess.run(
                    ["psql", "-U", db_user, "-h", pg_host, "-p", pg_port, "-d", db_name, "-f", tmp_path],
                    capture_output=True,
                    text=True,
                    env=env,
                )
        stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        return result.returncode == 0, stderr or ""
    except Exception as e:
        return False, str(e)


class DatabaseState(StatesGroup):
    waiting_for_backup_file = State()


@router.callback_query(AdminPanelCallback.filter(F.action == "database"), HasPermission(PERM_MANAGEMENT))
async def handle_database_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        text="🗄 <b>Управление базой данных</b>",
        reply_markup=build_database_kb(),
    )


@router.callback_query(AdminPanelCallback.filter(F.action == "restore_db"), HasPermission(PERM_MANAGEMENT))
async def prompt_restore_db(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📂 Отправьте файл резервной копии (.sql), чтобы восстановить базу данных.\n"
        "⚠️ Все текущие данные будут перезаписаны.",
        reply_markup=build_back_to_db_menu(),
    )
    await state.set_state(DatabaseState.waiting_for_backup_file)


@router.message(DatabaseState.waiting_for_backup_file, HasPermission(PERM_MANAGEMENT))
async def restore_database(message: Message, state: FSMContext, bot: Bot):
    document = message.document

    if not document or not document.file_name.endswith(".sql"):
        await message.answer("❌ Пожалуйста, отправьте файл с расширением .sql.")
        return

    if document.file_size and document.file_size > TELEGRAM_DOWNLOAD_LIMIT:
        size_mb = document.file_size / (1024 * 1024)
        await message.answer(
            "❌ Файл слишком большой для восстановления через бота: "
            f"{size_mb:.1f} МБ при лимите Telegram 20 МБ.\n\n"
            "Telegram не отдаёт ботам файлы больше 20 МБ. Варианты:\n"
            "• выгрузить дамп в сжатом формате (custom/gzip) — он меньше;\n"
            "• восстановить базу на сервере напрямую через <code>psql</code>/<code>pg_restore</code>.",
        )
        return

    try:
        with NamedTemporaryFile(delete=False, suffix=".sql") as tmp_file:
            tmp_path = tmp_file.name

        await bot.download(document, destination=tmp_path)
        logger.info("[Restore] Файл получен: {}", tmp_path)

        success, err_msg = await run_io(
            sync_restore_database,
            tmp_path,
            DB_NAME,
            DB_USER,
            DB_PASSWORD,
            PG_HOST,
            PG_PORT,
        )

        if not success:
            logger.error("[Restore] Ошибка: {}", err_msg)
            await message.answer(
                f"❌ Ошибка при восстановлении базы данных:\n<pre>{err_msg}</pre>",
            )
            return

        logger.info("[Restore] База восстановлена")
        await message.answer(
            "✅ База данных восстановлена.",
            reply_markup=build_back_to_db_menu(),
        )
        logger.info("[Restore] Завершение для перезапуска")
        await state.clear()
        sys.exit(0)

    except Exception as e:
        if "file is too big" in str(e).lower():
            logger.error("[Restore] Файл превышает лимит Telegram 20 МБ")
            await message.answer(
                "❌ Telegram не отдаёт боту файлы больше 20 МБ. "
                "Выгрузите дамп в сжатом формате или восстановите базу на сервере напрямую.",
            )
            return
        logger.exception(f"[Restore] Непредвиденная ошибка: {e}")
        await message.answer(
            f"❌ Произошла ошибка:\n<pre>{traceback.format_exc()}</pre>",
        )
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@router.callback_query(AdminPanelCallback.filter(F.action == "export_db"), HasPermission(PERM_MANAGEMENT))
async def handle_export_db(callback: CallbackQuery):
    await callback.message.edit_text(
        "📤 Выберите панель, с которой требуется получить данные:\n\n"
        "<i>Подтянутся подписки с панели и будут сохранены в базу данных бота.</i>",
        reply_markup=build_export_db_sources_kb(),
    )


@router.callback_query(AdminPanelCallback.filter(F.action == "back_to_db_menu"), HasPermission(PERM_MANAGEMENT))
async def back_to_database_menu(callback: CallbackQuery):
    await callback.message.edit_text("📦 Управление базой данных:", reply_markup=build_database_kb())
