import locale
import json
import os
import re
import shutil
import subprocess
import sys

from contextlib import contextmanager
from datetime import datetime
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:
    requests = None

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
except ImportError:
    def _strip_markup(value):
        if not isinstance(value, str):
            return str(value)
        return re.sub(r"\[[^\]]+\]", "", value)


    class Group:
        def __init__(self, *items):
            self.items = items

        def __str__(self):
            return "\n".join(_strip_markup(item) for item in self.items)


    class Panel:
        def __init__(self, renderable, **kwargs):
            self.renderable = renderable

        def __str__(self):
            return _strip_markup(self.renderable)


    class Table:
        def __init__(self, title=None, **kwargs):
            self.title = title
            self.rows = []

        def add_column(self, *args, **kwargs):
            return None

        def add_row(self, *row):
            self.rows.append(row)

        def __str__(self):
            lines = []
            if self.title:
                lines.append(_strip_markup(self.title))
            lines.extend(" | ".join(_strip_markup(cell) for cell in row) for row in self.rows)
            return "\n".join(lines)


    class Live:
        def __init__(self, **kwargs):
            self.last_renderable = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, renderable):
            self.last_renderable = renderable
            print(_strip_markup(str(renderable)))


    class SpinnerColumn:
        pass


    class TextColumn:
        def __init__(self, *args, **kwargs):
            pass


    class Progress:
        def __init__(self, *args, **kwargs):
            self.last_description = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add_task(self, description, total=None):
            self.last_description = description
            print(_strip_markup(description))
            return 1

        def update(self, task_id, description=None):
            if description and description != self.last_description:
                self.last_description = description
                print(_strip_markup(description))


    class Prompt:
        @staticmethod
        def ask(message, choices=None, default=None, show_choices=True, **kwargs):
            suffix = ""
            if choices and show_choices:
                suffix = f" ({'/'.join(choices)})"
            if default is not None:
                suffix = f"{suffix} [{default}]"
            value = input(f"{_strip_markup(message)}{suffix}: ").strip()
            if not value and default is not None:
                value = str(default)
            if choices and value not in choices:
                raise ValueError(f"Ожидается одно из значений: {', '.join(choices)}")
            return value


    class Confirm:
        @staticmethod
        def ask(message, default=False, **kwargs):
            prompt = "Y/n" if default else "y/N"
            value = input(f"{_strip_markup(message)} [{prompt}]: ").strip().lower()
            if not value:
                return default
            return value in {"y", "yes", "1", "true"}


    class Console:
        def print(self, *args, **kwargs):
            print(*(_strip_markup(str(arg)) for arg in args))

        def log(self, *args, **kwargs):
            self.print(*args)

        @contextmanager
        def status(self, message):
            self.print(message)
            yield


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
DEFAULT_SERVICE_NAME = "bot.service"
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "bin", "python")


class HttpResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def http_get(url: str, *, params=None, timeout: int = 10) -> HttpResponse:
    if requests is not None:
        response = requests.get(url, params=params, timeout=timeout)
        return HttpResponse(response.status_code, response.text)

    final_url = url
    if params:
        final_url = f"{url}?{urlencode(params)}"
    request = Request(final_url, headers={"User-Agent": "SoloBot-CLI"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read().decode("utf-8"))
    except HTTPError as error:
        return HttpResponse(error.code, error.read().decode("utf-8", errors="replace"))
    except URLError:
        return HttpResponse(599, "")


def detect_service_name() -> str:
    config_path = os.path.join(PROJECT_DIR, "config.py")
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as config_file:
                config_text = config_file.read()
            match = re.search(r"BOT_SERVICE\s*=\s*['\"]([^'\"]+)['\"]", config_text)
            if match:
                return match.group(1)
        except Exception:
            pass
    return DEFAULT_SERVICE_NAME


def refresh_service_name() -> str:
    global SERVICE_NAME, SYSTEMD_SERVICE_PATH
    SERVICE_NAME = detect_service_name()
    SYSTEMD_SERVICE_PATH = os.path.join("/etc/systemd/system", SERVICE_NAME)
    return SERVICE_NAME


SERVICE_NAME = refresh_service_name()


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
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
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


def get_runtime_user() -> str:
    return os.environ.get("SUDO_USER") or subprocess.check_output(["whoami"], text=True).strip()


def has_project_code() -> bool:
    required_paths = ("requirements.txt", "main.py")
    return all(os.path.exists(os.path.join(PROJECT_DIR, path)) for path in required_paths)


def has_local_config() -> bool:
    return os.path.exists(os.path.join(PROJECT_DIR, "config.py"))


def bootstrap_project_files(branch: str = "main") -> bool:
    refresh_service_name()
    if has_project_code():
        return True

    console.print("[yellow]Полный проект рядом не найден. Подтягиваю файлы бота...[/yellow]")
    install_core_packages_if_needed()
    install_rsync_if_needed()

    subprocess.run(["rm", "-rf", TEMP_DIR], check=False)
    clone_result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, GITHUB_REPO, TEMP_DIR],
        check=False,
    )
    if clone_result.returncode != 0:
        console.print("[red]❌ Не удалось скачать проект из GitHub.[/red]")
        return False

    rsync_cmd = ["rsync", "-a", f"{TEMP_DIR}/", f"{PROJECT_DIR}/"]
    if has_local_config():
        rsync_cmd.insert(2, "--exclude=config.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "handlers", "texts.py")):
        rsync_cmd.insert(2, "--exclude=handlers/texts.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "handlers", "buttons.py")):
        rsync_cmd.insert(2, "--exclude=handlers/buttons.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "core", "redis_cache.py")):
        rsync_cmd.insert(2, "--exclude=core/redis_cache.py")
    if os.path.exists(os.path.join(PROJECT_DIR, "img")):
        rsync_cmd.insert(2, "--exclude=img")
    if os.path.exists(os.path.join(PROJECT_DIR, "modules")):
        rsync_cmd.insert(2, "--exclude=modules")
    rsync_cmd.insert(2, "--exclude=.git")

    sync_result = subprocess.run(rsync_cmd, check=False)
    subprocess.run(["rm", "-rf", TEMP_DIR], check=False)
    if sync_result.returncode != 0:
        console.print("[red]❌ Не удалось распаковать файлы проекта.[/red]")
        return False

    refresh_service_name()
    console.print("[green]Файлы проекта подготовлены.[/green]")
    return True


def install_core_packages_if_needed():
    missing_packages = []

    if shutil.which("git") is None:
        missing_packages.append("git")
    if shutil.which("rsync") is None:
        missing_packages.append("rsync")

    python312_path = shutil.which("python3.12")
    if python312_path is None:
        missing_packages.extend(["python3.12", "python3.12-venv"])
    else:
        venv_check = subprocess.run(
            [python312_path, "-m", "venv", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if venv_check.returncode != 0:
            missing_packages.append("python3.12-venv")

    if not missing_packages:
        return

    unique_packages = list(dict.fromkeys(missing_packages))
    console.print(f"[yellow]Устанавливаю системные пакеты: {', '.join(unique_packages)}[/yellow]")
    subprocess.run(["sudo", "apt", "update"], check=True)
    subprocess.run(["sudo", "apt", "install", "-y", *unique_packages], check=True)


def build_systemd_service() -> str:
    run_user = get_runtime_user()
    return (
        "[Unit]\n"
        "Description=SoloBot Telegram bot\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"User={run_user}\n"
        f"WorkingDirectory={PROJECT_DIR}\n"
        f"ExecStart={VENV_PYTHON} {os.path.join(PROJECT_DIR, 'main.py')}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        'Environment="PYTHONUNBUFFERED=1"\n\n'
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def ensure_systemd_service() -> bool:
    refresh_service_name()
    console.print(f"[yellow]Проверяю systemd-службу {SERVICE_NAME}...[/yellow]")
    service_text = build_systemd_service()
    service_exists = os.path.exists(SYSTEMD_SERVICE_PATH)

    if service_exists:
        try:
            with open(SYSTEMD_SERVICE_PATH, encoding="utf-8") as service_file:
                if service_file.read() == service_text:
                    console.print(f"[green]Служба {SERVICE_NAME} уже настроена.[/green]")
                    return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["sudo", "tee", SYSTEMD_SERVICE_PATH],
            input=service_text,
            text=True,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        console.print(f"[green]Служба {SERVICE_NAME} настроена.[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ Не удалось настроить службу {SERVICE_NAME}: {e}[/red]")
        return False


def initialize_database() -> bool:
    if not os.path.exists(VENV_PYTHON):
        console.print("[yellow]Инициализация базы пропущена: виртуальное окружение ещё не создано.[/yellow]")
        return False
    console.print("[yellow]Инициализация базы данных...[/yellow]")
    try:
        subprocess.run(
            [
                VENV_PYTHON,
                "-c",
                "import asyncio; from database.init_db import init_db; asyncio.run(init_db())",
            ],
            cwd=PROJECT_DIR,
            check=True,
        )
        console.print("[green]База данных успешно инициализирована.[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ Не удалось инициализировать базу данных: {e}[/red]")
        return False


def enable_and_start_service(start_now: bool = True) -> None:
    refresh_service_name()
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    subprocess.run(["sudo", "systemctl", "enable", SERVICE_NAME], check=True)
    if start_now:
        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], check=True)
        console.print(f"[green]Служба {SERVICE_NAME} включена и запущена.[/green]")
    else:
        console.print(
            f"[yellow]Служба {SERVICE_NAME} включена, но не запущена. Проверьте config.py и доступность базы данных.[/yellow]"
        )


def is_runtime_ready() -> bool:
    refresh_service_name()
    if not has_project_code():
        return False
    return os.path.exists(VENV_PYTHON) and is_service_exists(SERVICE_NAME)


def install_bot():
    console.print(
        Panel(
            "[white]CLI подготовит окружение, установит зависимости, создаст systemd-службу "
            "и попробует инициализировать базу данных. Если проекта ещё нет рядом, CLI сначала скачает его автоматически.[/white]",
            border_style="green",
            title="[bold green]Автоматическая установка SoloBot[/bold green]",
            padding=(1, 2),
        )
    )

    if not safe_confirm("[bold green]Запустить автоматическую установку?[/bold green]", default=True):
        return

    try:
        branch = "main"
        if not has_project_code():
            use_beta = safe_confirm("[yellow]Скачать beta/dev ветку вместо стабильной?[/yellow]", default=False)
            branch = "dev" if use_beta else "main"
        if not bootstrap_project_files(branch=branch):
            return
        refresh_service_name()
        install_core_packages_if_needed()
        install_dependencies()
        db_ready = initialize_database()
        if not ensure_systemd_service():
            return
        fix_permissions()
        enable_and_start_service(start_now=db_ready)
        console.print("[green]✅ Установка SoloBot завершена.[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ Ошибка во время установки: {e}[/red]")


def prompt_install_if_needed():
    if is_runtime_ready():
        return

    missing_parts = []
    if not has_project_code():
        missing_parts.append("файлы проекта")
    if has_project_code() and not os.path.exists(VENV_PYTHON):
        missing_parts.append("виртуальное окружение")
    refresh_service_name()
    if has_project_code() and not is_service_exists(SERVICE_NAME):
        missing_parts.append(f"служба {SERVICE_NAME}")

    console.print(f"[yellow]Обнаружена неполная установка: {', '.join(missing_parts)}.[/yellow]")
    if safe_confirm("[green]Выполнить автоматическую установку сейчас?[/green]", default=True):
        install_bot()


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

    local_version = get_local_version() or "unknown"
    last_update = get_last_update_date() or "unknown"
    console.print(f"[bold green]Директория бота:[/bold green] [yellow]{PROJECT_DIR}[/yellow]")
    console.print(f"[bold green]Установленная версия:[/bold green] [yellow]{local_version}[/yellow]")
    console.print(f"[bold green]Последнее обновление:[/bold green] [yellow]{last_update}[/yellow]\n")


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
        response = http_get(url, timeout=10)
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
    install_core_packages_if_needed()


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
    install_core_packages_if_needed()


def install_dependencies():
    console.print("[blue]Установка зависимостей...[/blue]")
    install_core_packages_if_needed()

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
    if ensure_systemd_service():
        console.print("[blue]🚀 Перезапуск службы...[/blue]")
        with console.status("[bold yellow]Перезапуск...[/bold yellow]"):
            subprocess.run(["sudo", "systemctl", "enable", SERVICE_NAME], check=False)
            subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME])


def get_local_version():
    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_DIR, "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            check=False,
        )
        version = result.stdout.strip()
        if result.returncode == 0 and version:
            return version
    except Exception:
        pass

    path = os.path.join(PROJECT_DIR, "bot.py")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = re.search(r'version\s*=\s*["\'](.+?)["\']', line)
            if match:
                return match.group(1)
    return None


def get_last_update_date():
    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_DIR, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M:%S"],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return value
    except Exception:
        pass

    excluded_dirs = {".git", "venv", ".venv", "__pycache__", "build", "dist"}
    latest_mtime = 0.0
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        for file_name in files:
            path = os.path.join(root, file_name)
            try:
                latest_mtime = max(latest_mtime, os.path.getmtime(path))
            except Exception:
                continue
    if latest_mtime <= 0:
        return None
    return datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S")


def get_remote_version(branch="main"):
    try:
        url = f"https://raw.githubusercontent.com/Vladless/Solo_bot/{branch}/bot.py"
        response = http_get(url, timeout=10)
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
        rel_resp = http_get(
            "https://api.github.com/repos/Vladless/Solo_bot/releases",
            timeout=10,
        )
        releases = rel_resp.json() if rel_resp.status_code == 200 else []
        release_tag_names = {r["tag_name"] for r in releases}

        tags_resp = http_get(
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
    table = Table(title="Solobot CLI v0.5.0", title_style="bold magenta", header_style="bold blue")
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
    table.add_row("9", "Установить / переустановить бота")
    table.add_row("10", "Выход")
    console.print(table)


def main():
    os.chdir(PROJECT_DIR)
   # auto_update_cli()
    print_logo()
    prompt_install_if_needed()
    try:
        while True:
            refresh_service_name()
            show_menu()
            choice = safe_prompt(
                "[bold blue]👉 Введите номер действия[/bold blue]",
                choices=[str(i) for i in range(1, 11)],
                show_choices=False,
            )
            if choice == "1":
                if is_service_exists(SERVICE_NAME):
                    subprocess.run(["sudo", "systemctl", "start", SERVICE_NAME])
                else:
                    console.print(f"[yellow]Служба {SERVICE_NAME} не найдена.[/yellow]")
                    if safe_confirm("[green]Установить бота и создать службу сейчас?[/green]", default=True):
                        install_bot()
            elif choice == "2":
                if not os.path.exists(VENV_PYTHON):
                    console.print("[yellow]Виртуальное окружение ещё не создано.[/yellow]")
                    if safe_confirm("[green]Подготовить окружение через автоматическую установку?[/green]", default=True):
                        install_bot()
                    continue
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
                install_bot()
            elif choice == "10":
                console.print("[bold cyan]Выход из CLI. Удачного дня![/bold cyan]")
                break
    except KeyboardInterrupt:
        console.print("\n[bold red]⏹ Прерывание. Выход из CLI.[/bold red]")


if __name__ == "__main__":
    main()
