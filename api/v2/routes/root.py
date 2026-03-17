from fastapi import APIRouter

from config import PROJECT_NAME, USERNAME_BOT

router = APIRouter(tags=["Root"])


@router.get("/api", include_in_schema=False)
async def root():
    return {"message": "SoloBot API v2", "docs": "/api/docs"}


@router.get("/api/version", include_in_schema=True)
async def version():
    return {"version": 2, "api": "v2"}


@router.get("/api/telegram-widget-bot", include_in_schema=True)
async def telegram_widget_bot():
    """Имя бота и имя проекта для веб-клиента."""
    return {
        "bot_username": USERNAME_BOT.replace("@", ""),
        "project_name": (PROJECT_NAME or "Solo").strip() if isinstance(PROJECT_NAME, str) else "Solo",
    }
