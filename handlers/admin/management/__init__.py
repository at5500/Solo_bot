from aiogram import Router

router = Router()

from . import admins, database, domain, file_upload, import_3xui, import_remnawave, maintenance

__all__ = (
    "router",
    "admins",
    "database",
    "domain",
    "file_upload",
    "import_3xui",
    "import_remnawave",
    "maintenance",
)
