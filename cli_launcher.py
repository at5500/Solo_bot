import locale
import os
import re
import shutil
import subprocess
import sys

from time import sleep

import requests

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from config import BOT_SERVICE


def ensure_utf8_locale():
    try:
        current_locale = locale.getlocale()
        if current_locale and current_locale[1] == "UTF-8":
            return
    except Exception:
        pass

    console.print("[yellow]⏳ Проверка и установка локали UTF-8...[/yellow]")

    os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["LANG"] = "en_US.UTF-8"

    result = subprocess.run(["locale", "-a"], capture_output=True, text=True)
    if "en_US.utf8" not in result.stdout.lower():
        console.print("[blue]Добавляю локаль en_US.UTF-8 в систему...[/blue]")
        try:
            subprocess.run(["sudo", "locale-gen", "en_US.UTF-8"], check=True)
            subprocess.run(["sudo", "update-locale", "LANG=en_US.UTF-8"], check=True)
            console.print("[green]Локаль успешно установлена.[/green]")
        except Exception as e:
            console.print(f"[red]❌ Ошибка при установке локали: {e}[/red]")
    else:
        console.print("[green]Локаль UTF-8 уже доступна в системе.[/green]")


try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

console = Console()
ensure_utf8_locale()

BACK_DIR = os.path.expanduser("~/.solobot_backups")
TEMP_DIR = os.path.expanduser("~/.solobot_tmp")
PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
IS_ROOT_DIR = PROJECT_DIR == "/root"
GITHUB_REPO = "https://github.com/Vladless/Solo_bot"
SERVICE_NAME = BOT_SERVICE


def is_ascii_only(value: str) -> bool:
    """Проверка, что строка содержит только ASCII."""
    return all(ord(ch) < 128 for ch in value)


def _parse_tag_version(tag_name: str) -> tuple[int, ...]:
    """Извлекает кортеж (major, minor, patch, ...) из тега для сортировки. v.5.1 -> (5, 1), v4 -> (4, 0)."""
    s = tag_name.strip().lstrip("v.")
    parts = []
    for part in re.split(r"[.\s]+", s):
        try:
            parts.append(int(part))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def warn_english_only():
    """Предупреждение о необходимости английской раскладки."""
    console.print("[red]Обнаружен ввод с неанглийской раскладкой.[/red]")
    console.print("[yellow]Пожалуйста, переключите раскладку на ENG и введите снова.[/yellow]")


def safe_confirm(message: str, **kwargs) -> bool:
    """Безопасный Confirm.ask с защитой от русской раскладки."""
    while True:
        try:
            result = Confirm.ask(message, **kwargs)
            return result
        except UnicodeDecodeError:
            warn_english_only()


def safe_prompt(message: str, **kwargs) -> str:
    """Безопасный Prompt.ask с защитой от русской раскладки."""
    while True:
        try:
            value = Prompt.ask(message, **kwargs)
        except UnicodeDecodeError:
            warn_english_only()
            continue
        if isinstance(value, str) and not is_ascii_only(value):
            warn_english_only()
            continue
        return value


if IS_ROOT_DIR:
    console.print("[bold red]КРИТИЧЕСКАЯ ОШИБКА:[/bold red]")
    console.print("[red]Обнаружена установка бота прямо в корневой папке (/root).[/red]")
    console.print("[red]Это крайне опасно и может привести к потере данных![/red]")
    console.print("[red]Рекомендуется перенести бота в отдельную папку, например /root/solobot[/red]")
    console.print("[red]Обновление заблокировано в целях безопасности.[/red]")
    sys.exit(1)


def is_service_exists(service_name):
    result = subprocess.run(["systemctl", "list-unit-files", service_name], capture_output=True, text=True)
    return service_name in result.stdout


def print_logo():
    logo_lines = [
        "███████╗ ██████╗ ██╗      ██████╗ ██████╗  ██████╗ ████████╗",
        "██╔════╝██╔═══██╗██║     ██╔═══██╗██╔══██╗██╔═══██╗╚══██╔══╝",
        "███████╗██║   ██║██║     ██║   ██║██████╔╝██║   ██║   ██║   ",
        "╚════██║██║   ██║██║     ██║   ██║██╔══██╗██║   ██║   ██║   ",
        "███████║╚██████╔╝███████╗╚██████╔╝██████╔╝╚██████╔╝   ██║   ",
        "╚══════╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═════╝  ╚═════╝    ╚═╝   ",
    ]

    with Live(refresh_per_second=10) as live:
        display = []
        for line in logo_lines:
            display.append(f"[bold cyan]{line}[/bold cyan]")
            panel = Panel(Group(*display), border_style="cyan", padding=(0, 2), expand=False)
            live.update(panel)
            sleep(0.07)

    console.print(f"[bold green]Директория бота:[/bold green] [yellow]{PROJECT_DIR}[/yellow]\n")


def list_backups():
    if not os.path.isdir(BACK_DIR):
        return []
    pairs = []
    for name in os.listdir(BACK_DIR):
        path = os.path.join(BACK_DIR, name)
        if os.path.isdir(path):
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = 0
            pairs.append((mtime, path))
    pairs.sort(reverse=True)
    return [p for _, p in pairs]


def prune_old_backups():
    backups = list_backups()
    for path in backups[3:]:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            subprocess.run(["sudo", "rm", "-rf", path])


def backup_project():
    from datetime import datetime

    os.makedirs(BACK_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACK_DIR, f"backup-{ts}")
    console.print("[yellow]Создаётся резервная копия проекта...[/yellow]")
    with console.status("[bold cyan]Копирование файлов...[/bold cyan]"):
        subprocess.run(["cp", "-r", PROJECT_DIR, dst])
    console.print(f"[green]Бэкап сохранён в: {dst}[/green]")
    prune_old_backups()


def restore_from_backup():
    from datetime import datetime

    backups = list_backups()[:3]
    if not backups:
        console.print(f"[red]❌ Бэкапы не найдены: {BACK_DIR}[/red]")
        return

    console.print("\n[bold green]Доступные бэкапы:[/bold green]")
    shown = []
    for idx, path in enumerate(backups, 1):
        try:
            mtime = os.path.getmtime(path)
            dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            dt = "unknown"
        console.print(f"[cyan]{idx}.[/cyan] {os.path.basename(path)}  [dim]{dt}[/dim]")
        shown.append((idx, path))

    try:
        choice = safe_prompt(
            "[bold blue]Выберите номер бэкапа[/bold blue]",
            choices=[str(i) for i, _ in shown],
        )
    except Exception:
        return

    sel_path = shown[int(choice) - 1][1]

    console.print("[red]Внимание: текущие файлы проекта будут перезаписаны выбранным бэкапом.[/red]")
    if not safe_confirm("[yellow]Продолжить восстановление из бэкапа?[/yellow]"):
        return

    if is_service_exists(SERVICE_NAME):
        console.print("[blue]Останавливаю службу перед восстановлением...[/blue]")
        subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME])

    install_rsync_if_needed()

    console.print("[yellow]Копирую файлы из бэкапа в проект...[/yellow]")
    rc = subprocess.run(
        ["rsync", "-a", "--delete", f"{sel_path}/", f"{PROJECT_DIR}/"],
        check=False,
    ).returncode
    if rc != 0:
        console.print("[red]❌ Ошибка rsync при восстановлении[/red]")
        return

    install_dependencies()
    fix_permissions()
    restart_service()
    console.print("[green]✅ Восстановление из бэкапа завершено[/green]")


def auto_update_cli():
    console.print("[yellow]Проверка обновлений CLI...[/yellow]")
    try:
        url = "https://raw.githubusercontent.com/Vladless/Solo_bot/dev/cli_launcher.py"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            console.print("[red]Не удалось получить обновление CLI[/red]")
            return

        latest_text = response.text
        current_path = os.path.realpath(__file__)
        with open(current_path, encoding="utf-8") as f:
            current_text = f.read()

        if current_text != latest_text:
            console.print("[green]Доступна новая версия CLI. Обновляю...[/green]")
            with open(current_path, "w", encoding="utf-8") as f:
                f.write(latest_text)
            os.chmod(current_path, 0o644)
            console.print("[green]CLI обновлён. Перезапуск...[/green]")
            os.execv(sys.executable, [sys.executable, current_path])
        else:
            console.print("[green]CLI уже актуален[/green]")
    except Exception as e:
        console.print(f"[red]❌ Ошибка при автообновлении CLI: {e}[/red]")


def fix_permissions():
    console.print("[yellow]Восстанавливаю владельца и права доступа к проекту...[/yellow]")

    try:
        user = os.environ.get("SUDO_USER") or subprocess.check_output(["whoami"], text=True).strip()
        console.log(f"[cyan]Используем пользователь: {user}[/cyan]")

        for root, dirs, files in os.walk(PROJECT_DIR):
            for dir in dirs:
                if dir == "__pycache__":
                    pycache_path = os.path.join(root, dir)
                    subprocess.run(["sudo", "rm", "-rf", pycache_path], check=True)
            for file in files:
                if file.endswith(".pyc"):
                    pyc_path = os.path.join(root, file)
                    subprocess.run(["sudo", "rm", "-f", pyc_path], check=True)

        console.log("[blue]Изменение владельца на весь проект...[/blue]")
        subprocess.run(["sudo", "chown", "-R", f"{user}:{user}", PROJECT_DIR], check=True)

        console.log("[blue]Изменение прав доступа (u=rwX,go=rX)...[/blue]")
        subprocess.run(["sudo", "chmod", "-R", "u=rwX,go=rX", PROJECT_DIR], check=True)

        launcher_path = os.path.join(PROJECT_DIR, "cli_launcher.py")
        if os.path.exists(launcher_path):
            console.log("[blue]Установка флага +x для cli_launcher.py...[/blue]")
            subprocess.run(["chmod", "+x", launcher_path], check=True)

        console.print(f"[green]Все права восстановлены для пользователя [bold]{user}[/bold][/green]")

    except Exception as e:
        console.print(f"[red]❌ Ошибка при установке прав: {e}[/red]")


def install_rsync_if_needed():
    if subprocess.run(["which", "rsync"], capture_output=True).returncode != 0:
        console.print("[blue]Установка rsync...[/blue]")
        os.system("sudo apt update && sudo apt install -y rsync")


def clean_project_dir_safe(update_buttons=False, update_img=False, update_redis_cache=False):
    console.print("[yellow]Очистка проекта перед обновлением...[/yellow]")

    preserved_paths = set()

    preserved_paths.update([
        os.path.join(PROJECT_DIR, "config.py"),
        os.path.join(PROJECT_DIR, "handlers", "texts.py"),
        os.path.join(PROJECT_DIR, ".git"),
        os.path.join(PROJECT_DIR, "modules"),
    ])

    for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "modules")):
        for name in dirs + files:
            preserved_paths.add(os.path.join(root, name))

    if not update_buttons:
        preserved_paths.add(os.path.join(PROJECT_DIR, "handlers", "buttons.py"))

    if not update_img:
        preserved_paths.add(os.path.join(PROJECT_DIR, "img"))
        for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "img")):
            for name in dirs + files:
                preserved_paths.add(os.path.join(root, name))

    if not update_redis_cache:
        preserved_paths.add(os.path.join(PROJECT_DIR, "core", "redis_cache.py"))

    for root, dirs, files in os.walk(PROJECT_DIR, topdown=False):
        for file in files:
            path = os.path.join(root, file)
            if path in preserved_paths:
                continue
            try:
                os.remove(path)
            except PermissionError:
                subprocess.run(["sudo", "rm", "-f", path])
            except Exception as e:
                console.print(f"[red]Не удалось удалить файл: {path}: {e}[/red]")

        for dir in dirs:
            dir_path = os.path.join(root, dir)

            if os.path.abspath(dir_path) in [
                os.path.join(PROJECT_DIR, "handlers"),
                os.path.join(PROJECT_DIR, "img"),
                os.path.join(PROJECT_DIR, "modules"),
            ]:
                continue

            if os.path.abspath(dir_path).startswith(os.path.join(PROJECT_DIR, "modules") + os.sep):
                continue

            try:
                os.rmdir(dir_path)
            except Exception:
                subprocess.run(["sudo", "rm", "-rf", dir_path])


def install_git_if_needed():
    if subprocess.run(["which", "git"], capture_output=True).returncode != 0:
        console.print("[blue]Установка Git...[/blue]")
        os.system("sudo apt update && sudo apt install -y git")


def install_dependencies():
    console.print("[blue]Установка зависимостей...[/blue]")

    python312_path = shutil.which("python3.12")
    if not python312_path:
        console.print("[red]Не найден python3.12 в системе[/red]")
        console.print("[yellow]Установите Python 3.12: sudo apt install python3.12 python3.12-venv[/yellow]")
        sys.exit(1)

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task_id = progress.add_task(description="Создание виртуального окружения...", total=None)
        try:
            if os.path.exists("venv"):
                shutil.rmtree("venv")
                console.print("[yellow]Удалён старый venv[/yellow]")

            subprocess.run([python312_path, "-m", "venv", "venv"], check=True)

            progress.update(task_id, description="Установка зависимостей...")
            subprocess.run(
                [os.path.join("venv", "bin", "pip"), "install", "-r", "requirements.txt"],
                check=True,
                cwd=PROJECT_DIR,
            )

            progress.update(task_id, description="Установка завершена")

        except subprocess.CalledProcessError as e:
            progress.update(task_id, description="❌ Ошибка при установке")
            console.print(f"[red]❌ Ошибка: {e}[/red]")


def restart_service():
    if is_service_exists(SERVICE_NAME):
        console.print("[blue]🚀 Перезапуск службы...[/blue]")
        with console.status("[bold yellow]Перезапуск...[/bold yellow]"):
            subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME])
    else:
        console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")


def get_local_version():
    path = os.path.join(PROJECT_DIR, "bot.py")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = re.search(r'version\s*=\s*["\'](.+?)["\']', line)
            if match:
                return match.group(1)
    return None


def get_remote_version(branch="main"):
    try:
        url = f"https://raw.githubusercontent.com/Vladless/Solo_bot/{branch}/bot.py"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            for line in response.text.splitlines():
                match = re.search(r'version\s*=\s*["\'](.+?)["\']', line)
                if match:
                    return match.group(1)
    except Exception:
        return None
    return None


def update_from_beta():
    local_version = get_local_version()
    remote_version = get_remote_version(branch="dev")

    console.print(
        Panel(
            "[bold red]Обновление на DEV / BETA-ветку[/bold red]\n\n"
            "[white]"
            "• Dev-ветка может содержать изменения, которые ещё находятся в доработке.\n"
            "• Возможны ошибки и непредсказуемое поведение отдельных функций, особенно режима стран.\n\n"
            "• BETA-версии бота в первую очередь ориентированы на опытных пользователей, "
            "готовых протестировать новые возможности и осознанно работать с обновлённым функционалом.\n"
            "[/white]\n\n"
            "[yellow]Перед началом обновления CLI автоматически создаёт резервную копию проекта, "
            "что позволит при необходимости безопасно восстановиться из бэкапа.[/yellow]",
            border_style="red",
            title="[bold red]Нестабильная ветка разработки[/bold red]",
            padding=(1, 2),
        )
    )

    if local_version and remote_version:
        console.print(f"[cyan]Локальная версия: {local_version} | Последняя в dev: {remote_version}[/cyan]")
        if local_version == remote_version:
            if not safe_confirm("[yellow]Версия актуальна. Обновить всё равно?[/yellow]"):
                return

    if not safe_confirm(
        "[bold red]Продолжить обновление на dev-ветку с учётом возможных особенностей работы?[/bold red]"
    ):
        return

    console.print("[red]ВНИМАНИЕ! Папка бота будет перезаписана![/red]")
    if not safe_confirm("[red]Продолжить обновление?[/red]"):
        return

    update_buttons = safe_confirm("[yellow]Обновлять файл buttons.py?[/yellow]", default=False)
    update_img = safe_confirm("[yellow]Обновлять папку img?[/yellow]", default=False)
    update_redis_cache = safe_confirm("[yellow]Обновлять файл core/redis_cache.py?[/yellow]", default=False)

    backup_project()
    install_git_if_needed()
    install_rsync_if_needed()

    os.chdir(PROJECT_DIR)
    console.print("[cyan]Клонируем временный репозиторий...[/cyan]")
    subprocess.run(["rm", "-rf", TEMP_DIR])

    if os.system(f"git clone --depth=1000000 -b dev {GITHUB_REPO} {TEMP_DIR}") != 0:
        console.print("[red]❌ Ошибка при клонировании. Обновление отменено.[/red]")
        return

    subprocess.run(["sudo", "rm", "-rf", os.path.join(PROJECT_DIR, "venv")])
    clean_project_dir_safe(
        update_buttons=update_buttons,
        update_img=update_img,
        update_redis_cache=update_redis_cache,
    )

    exclude_options = ""
    if not update_img:
        exclude_options += "--exclude=img "
    if not update_buttons:
        exclude_options += "--exclude=handlers/buttons.py "
    if not update_redis_cache:
        exclude_options += "--exclude=core/redis_cache.py "
    exclude_options += "--exclude=modules "

    rsync_cmd = ["rsync", "-a"] + [x for x in exclude_options.split() if x] + [f"{TEMP_DIR}/", f"{PROJECT_DIR}/"]
    subprocess.run(rsync_cmd)

    modules_path = os.path.join(PROJECT_DIR, "modules")
    if not os.path.exists(modules_path):
        console.print("[yellow]Папка modules отсутствует — создаю вручную...[/yellow]")
        try:
            os.makedirs(modules_path, exist_ok=True)
            console.print("[green]Папка modules успешно создана.[/green]")
        except Exception as e:
            console.print(f"[red]❌ Не удалось создать папку modules: {e}[/red]")

    if os.path.exists(os.path.join(TEMP_DIR, ".git")):
        subprocess.run(["cp", "-r", os.path.join(TEMP_DIR, ".git"), PROJECT_DIR])

    subprocess.run(["rm", "-rf", TEMP_DIR])

    install_dependencies()
    fix_permissions()
    restart_service()
    console.print("[green]Обновление с ветки dev завершено.[/green]")


def _do_update_to_tag(tag_name: str, update_buttons: bool, update_img: bool, update_redis_cache: bool) -> None:
    """Общая логика обновления до указанного тега (релиз или произвольный тег)."""
    subprocess.run(["rm", "-rf", TEMP_DIR])
    subprocess.run(
        ["git", "clone", "--branch", tag_name, "--depth", "1", GITHUB_REPO, TEMP_DIR],
        check=True,
    )

    console.print("[red]Начинается перезапись файлов бота![/red]")
    subprocess.run(["sudo", "rm", "-rf", os.path.join(PROJECT_DIR, "venv")])
    clean_project_dir_safe(
        update_buttons=update_buttons,
        update_img=update_img,
        update_redis_cache=update_redis_cache,
    )

    exclude_options = ""
    if not update_img:
        exclude_options += "--exclude=img "
    if not update_buttons:
        exclude_options += "--exclude=handlers/buttons.py "
    if not update_redis_cache:
        exclude_options += "--exclude=core/redis_cache.py "
    exclude_options += "--exclude=modules "

    rsync_cmd = ["rsync", "-a"] + exclude_options.split() + [f"{TEMP_DIR}/", f"{PROJECT_DIR}/"]
    subprocess.run(rsync_cmd)

    modules_path = os.path.join(PROJECT_DIR, "modules")
    if not os.path.exists(modules_path):
        console.print("[yellow]Папка modules отсутствует — создаю вручную...[/yellow]")
        try:
            os.makedirs(modules_path, exist_ok=True)
            console.print("[green]Папка modules успешно создана.[/green]")
        except Exception as e:
            console.print(f"[red]❌ Не удалось создать папку modules: {e}[/red]")

    if os.path.exists(os.path.join(TEMP_DIR, ".git")):
        subprocess.run(["cp", "-r", os.path.join(TEMP_DIR, ".git"), PROJECT_DIR])

    subprocess.run(["rm", "-rf", TEMP_DIR])

    install_dependencies()
    fix_permissions()
    restart_service()
    console.print(f"[green]Обновление до {tag_name} завершено.[/green]")


def update_from_release():
    if not safe_confirm("[yellow]Подтвердите обновление Solobot до релиза или патча[/yellow]"):
        return

    console.print("[red]ВНИМАНИЕ! Папка бота будет полностью перезаписана![/red]")
    console.print("[red]  Исключения: папка img, файл handlers/buttons.py и файл core/redis_cache.py[/red]")
    if not safe_confirm("[red]Вы точно хотите продолжить?[/red]"):
        return

    update_buttons = safe_confirm("[yellow]Обновлять файл buttons.py?[/yellow]", default=False)
    update_img = safe_confirm("[yellow]Обновлять папку img?[/yellow]", default=False)
    update_redis_cache = safe_confirm("[yellow]Обновлять файл core/redis_cache.py?[/yellow]", default=False)

    backup_project()
    install_git_if_needed()
    install_rsync_if_needed()

    try:
        rel_resp = requests.get(
            "https://api.github.com/repos/Vladless/Solo_bot/releases",
            timeout=10,
        )
        releases = rel_resp.json() if rel_resp.status_code == 200 else []
        release_tag_names = {r["tag_name"] for r in releases}

        tags_resp = requests.get(
            "https://api.github.com/repos/Vladless/Solo_bot/tags",
            params={"per_page": 50},
            timeout=10,
        )
        if tags_resp.status_code != 200:
            raise ValueError("Не удалось получить список тегов")
        tags_data = tags_resp.json()
        all_tag_names = [t["name"] for t in tags_data]

        tag_names = [name for name in all_tag_names if _parse_tag_version(name)[0] >= 4]
        tag_names.sort(key=_parse_tag_version)

        if not tag_names:
            raise ValueError("Нет доступных тегов (ожидаются версии начиная с 4)")

        console.print("\n[bold green]Релизы и патчи:[/bold green]")
        for idx, name in enumerate(tag_names, 1):
            label = " [dim](релиз)[/dim]" if name in release_tag_names else " [dim](патч)[/dim]"
            console.print(f"[cyan]{idx}.[/cyan] {name}{label}")

        choices = [str(i) for i in range(1, len(tag_names) + 1)]
        selected = safe_prompt(
            "[bold blue]Выберите номер версии[/bold blue]",
            choices=choices,
        )
        tag_name = tag_names[int(selected) - 1]

        if not safe_confirm(f"[yellow]Установить {tag_name}?[/yellow]"):
            return

        console.print(f"[cyan]Клонируем {tag_name} во временную папку...[/cyan]")
        _do_update_to_tag(tag_name, update_buttons, update_img, update_redis_cache)

    except Exception as e:
        console.print(f"[red]❌ Ошибка при обновлении: {e}[/red]")


def show_update_menu():
    if IS_ROOT_DIR:
        console.print("[red]Обновление невозможно: бот находится в /root[/red]")
        console.print("[yellow]Перенесите бота в отдельную папку и повторите попытку[/yellow]")
        return

    table = Table(title="Выберите способ обновления", title_style="bold green")
    table.add_column("№", justify="center", style="cyan", no_wrap=True)
    table.add_column("Источник", style="white")
    table.add_row("1", "Обновить до BETA")
    table.add_row("2", "Обновить до релиза (релизы и патчи)")
    table.add_row("3", "Назад в меню")

    console.print(table)
    choice = safe_prompt("[bold blue]Введите номер[/bold blue]", choices=["1", "2", "3"])

    if choice == "1":
        update_from_beta()
    elif choice == "2":
        update_from_release()


def show_menu():
    table = Table(title="Solobot CLI v0.4.0", title_style="bold magenta", header_style="bold blue")
    table.add_column("№", justify="center", style="cyan", no_wrap=True)
    table.add_column("Операция", style="white")
    table.add_row("1", "Запустить бота (systemd)")
    table.add_row("2", "Запустить напрямую: venv/bin/python main.py")
    table.add_row("3", "Перезапустить бота (systemd)")
    table.add_row("4", "Остановить бота (systemd)")
    table.add_row("5", "Показать логи (80 строк)")
    table.add_row("6", "Показать статус")
    table.add_row("7", "Обновить Solobot")
    table.add_row("8", "Восстановить из бэкапа")
    table.add_row("9", "Выход")
    console.print(table)


def main():
    os.chdir(PROJECT_DIR)
    auto_update_cli()
    print_logo()
    try:
        while True:
            show_menu()
            choice = safe_prompt(
                "[bold blue]👉 Введите номер действия[/bold blue]",
                choices=[str(i) for i in range(1, 10)],
                show_choices=False,
            )
            if choice == "1":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run(["sudo", "systemctl", "start", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "2":
                if safe_confirm("[green]Вы действительно хотите запустить main.py вручную?[/green]"):
                    subprocess.run(["venv/bin/python", "main.py"])
            elif choice == "3":
                if is_service_exists(SERVICE_NAME):
                    if safe_confirm("[yellow]Вы действительно хотите перезапустить бота?[/yellow]"):
                        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "4":
                if is_service_exists(SERVICE_NAME):
                    if safe_confirm("[red]Вы уверены, что хотите остановить бота?[/red]"):
                        subprocess.run(["sudo", "systemctl", "stop", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "5":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run([
                        "sudo",
                        "journalctl",
                        "-u",
                        SERVICE_NAME,
                        "-n",
                        "80",
                        "--no-pager",
                    ])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "6":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run(["sudo", "systemctl", "status", SERVICE_NAME])
                else:
                    console.print(f"[red]❌ Служба {SERVICE_NAME} не найдена.[/red]")
            elif choice == "7":
                show_update_menu()
            elif choice == "8":
                restore_from_backup()
            elif choice == "9":
                console.print("[bold cyan]Выход из CLI. Удачного дня![/bold cyan]")
                break
    except KeyboardInterrupt:
        console.print("\n[bold red]⏹ Прерывание. Выход из CLI.[/bold red]")


if __name__ == "__main__":
    main()
