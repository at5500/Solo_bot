from datetime import datetime

from pydantic import BaseModel, Field


class IdentityCreate(BaseModel):
    email: str | None = Field(None, description="Почта для привязки")
    tg_id: int | None = Field(None, description="Telegram ID для привязки")


class IdentityResponse(BaseModel):
    id: str
    email: str | None
    tg_id: int | None
    is_admin: bool = False
    created_at: datetime | None
    updated_at: datetime | None

    class Config:
        from_attributes = True


class RegisterByEmailRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8, description="Пароль (минимум 8 символов)")


class RegisterResponse(BaseModel):
    identity_id: str
    token: str


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(...)


class LoginResponse(BaseModel):
    identity_id: str
    token: str


class SendLoginCodeRequest(BaseModel):
    email: str = Field(..., min_length=1)


class LoginByCodeRequest(BaseModel):
    email: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)


class LoginTelegramRequest(BaseModel):
    """Данные от Telegram Login Widget (кнопка «Войти через Telegram»)."""

    id: int = Field(..., description="Telegram user id (tg_id)")
    first_name: str = Field("")
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int = Field(..., description="Unix timestamp от Telegram")
    hash: str = Field(..., description="HMAC подпись для проверки на бэкенде")


class LinkTelegramRequest(BaseModel):
    """Данные от Telegram Login Widget — обязательны для доказательства владения аккаунтом при привязке."""

    id: int = Field(..., description="Telegram user id (tg_id)")
    first_name: str = Field("")
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int = Field(..., description="Unix timestamp от Telegram")
    hash: str = Field(..., description="HMAC подпись для проверки на бэкенде")


class IdentityAttachEmail(BaseModel):
    email: str = Field(..., min_length=1)


class IdentityAttachTelegram(BaseModel):
    tg_id: int = Field(...)
