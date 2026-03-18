from aiogram import Router

from . import admins, database, domain, file_upload, import_3xui, import_remnawave, maintenance

router = Router()

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
