from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, Field


# Minimal RFC-shaped email check: one ``@``, at least one ``.`` in the
# domain part, no whitespace. Strict enough to bounce obvious garbage
# (``notanemail``, ``a@``, ``@b.com``, ``a@b``) at the Pydantic layer
# with 422 — see audit F-016. Not full RFC 5322 (that would require
# email-validator); pyproject doesn't pull it in, and the existing
# normalisation downstream relies on the same shape anyway.
_EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
EmailStr = Annotated[str, Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)]
OptionalEmailStr = Optional[
    Annotated[str, Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)]
]


class IdentityCreate(BaseModel):
    email: OptionalEmailStr = Field(None, description="Почта для привязки")
    tg_id: int | None = Field(None, description="Telegram ID для привязки")


class IdentityResponse(BaseModel):
    id: str
    email: str | None
    tg_id: int | None
    is_admin: bool = False
    email_verified: bool = False
    password_set: bool = False
    onboarding_completed: bool = False
    onboarding_stage: str | None = None
    created_at: datetime | None
    updated_at: datetime | None

    class Config:
        from_attributes = True


class RegisterByEmailRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, description="Пароль (минимум 8 символов)")
    referral_code: str | None = Field(None, min_length=1)
    turnstile_token: str | None = Field(default=None, description="Cloudflare Turnstile CAPTCHA token")


class RegisterResponse(BaseModel):
    identity_id: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(...)


class SetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8, description="Новый пароль (минимум 8 символов)")
    password_confirm: str = Field(..., min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(...)
    password: str = Field(..., min_length=8, description="Новый пароль (минимум 8 символов)")
    password_confirm: str = Field(..., min_length=8)


class LoginResponse(BaseModel):
    identity_id: str
    identity: IdentityResponse


class SendLoginCodeRequest(BaseModel):
    email: EmailStr
    allow_register: bool = Field(
        default=True,
        description="Если true и email новый — создать идентичность и отправить код (passwordless flow)",
    )
    turnstile_token: str | None = Field(default=None, description="Cloudflare Turnstile CAPTCHA token")
    partner_code: str | None = Field(
        default=None,
        description=(
            "Optional partner code from a ``?partner=<code>`` web invite. "
            "Applied only when the email is new (a fresh identity is "
            "created in this call); otherwise silently ignored."
        ),
    )


class LoginByCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=1)
    link_token: str | None = Field(
        default=None,
        description=(
            "Optional one-shot token from /auth/link-tokens/web. When present "
            "and valid, the freshly logged-in email is attached to the "
            "originating Telegram identity instead of standing alone."
        ),
    )


class ConfirmPasswordResetRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    password_confirm: str = Field(..., min_length=8)


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
    email: EmailStr


class LinkEmailSendCodeRequest(BaseModel):
    email: EmailStr


class LinkEmailConfirmRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., min_length=1, max_length=16)


class IdentityAttachTelegram(BaseModel):
    tg_id: int = Field(...)


class IdentitySessionItem(BaseModel):
    id: str
    device_label: str | None = None
    ip: str | None = None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime | None = None
    is_current: bool = False


class IdentitySessionsResponse(BaseModel):
    sessions: list[IdentitySessionItem]
