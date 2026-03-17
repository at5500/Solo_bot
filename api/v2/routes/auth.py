from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from audit import set_api_actor
from api.depends import get_session, verify_identity_token
from api.v2.schemas.identities import (
    IdentityResponse,
    LinkTelegramRequest,
    LoginByCodeRequest,
    LoginRequest,
    LoginResponse,
    LoginTelegramRequest,
    RegisterByEmailRequest,
    RegisterResponse,
    SendLoginCodeRequest,
)
from config import API_TOKEN_TTL_DAYS, API_TOKEN
from database import identities as idb
from utils.telegram_login import verify_telegram_login

router = APIRouter(prefix="/auth", tags=["Auth"])
TOKEN_TTL_HINT = "бессрочно" if API_TOKEN_TTL_DAYS is None else f"{API_TOKEN_TTL_DAYS} дн."
TELEGRAM_LOGIN_MAX_AGE = 86400  # 24 часа


@router.post("/register", response_model=RegisterResponse)
async def register_by_email(
    body: RegisterByEmailRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    (
        """Регистрация по почте и паролю: создаётся идентичность, выдаётся токен. Срок действия токена: """
        + TOKEN_TTL_HINT
        + "."
    )
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email обязателен")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Пароль минимум 8 символов")
    existing = await idb.get_identity_by_email(session, email)
    if existing:
        raise HTTPException(status_code=409, detail="Идентичность с таким email уже существует")
    identity, token = await idb.create_identity_with_token(session, email=email, password=body.password)
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return RegisterResponse(identity_id=identity.id, token=token)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Вход по email и паролю. Возвращает identity_id и новый токен. Срок действия токена: """ + TOKEN_TTL_HINT + "."
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email обязателен")
    result = await idb.login_by_email(session, email, body.password)
    if not result:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    identity, token = result
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return LoginResponse(identity_id=identity.id, token=token)


_LOGIN_CODES: dict[str, tuple[str, float]] = {}
_LOGIN_CODE_TTL = 600.0  # 10 min


def _clean_login_codes() -> None:
    import time
    now = time.time()
    for k in list(_LOGIN_CODES):
        if now - _LOGIN_CODES[k][1] > _LOGIN_CODE_TTL:
            del _LOGIN_CODES[k]


@router.post("/send-login-code")
async def send_login_code(
    body: SendLoginCodeRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Отправить код входа на email. Код хранится на сервере 10 мин (для демо — без реальной отправки письма)."""
    _clean_login_codes()
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email обязателен")
    identity = await idb.get_identity_by_email(session, email)
    if not identity:
        raise HTTPException(status_code=404, detail="Аккаунт с таким email не найден")
    import secrets
    import time
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    _LOGIN_CODES[email] = (code, time.time())
    return {"ok": True, "message": "Код отправлен на почту"}


@router.post("/login-by-code", response_model=LoginResponse)
async def login_by_code(
    body: LoginByCodeRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Вход по email и коду из письма."""
    _clean_login_codes()
    email = body.email.strip().lower()
    if not email or not body.code or not body.code.strip():
        raise HTTPException(status_code=400, detail="Email и код обязательны")
    stored = _LOGIN_CODES.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="Код не найден или истёк. Запросите новый.")
    code_value, _ = stored
    if body.code.strip() != code_value:
        raise HTTPException(status_code=401, detail="Неверный код")
    del _LOGIN_CODES[email]
    identity = await idb.get_identity_by_email(session, email)
    if not identity:
        raise HTTPException(status_code=401, detail="Аккаунт не найден")
    token = await idb.issue_token_for_identity(session, identity)
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return LoginResponse(identity_id=identity.id, token=token)


@router.post("/login-telegram", response_model=LoginResponse)
async def login_telegram(
    body: LoginTelegramRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    (
        """Вход через Telegram Login Widget (кнопка на сайте). По tg_id находим или создаём Identity, выдаём токен. Срок действия токена: """
        + TOKEN_TTL_HINT
        + "."
    )
    payload = body.model_dump(mode="json")
    if not verify_telegram_login(payload, API_TOKEN, max_age_seconds=TELEGRAM_LOGIN_MAX_AGE):
        raise HTTPException(status_code=401, detail="Неверная подпись или устаревшие данные от Telegram")
    identity = await idb.get_or_create_identity_for_tg(session, body.id)
    token = await idb.issue_token_for_identity(session, identity)
    set_api_actor(request, identity_id=identity.id, tg_id=identity.tg_id)
    return LoginResponse(identity_id=identity.id, token=token)


@router.post("/link-telegram", response_model=IdentityResponse)
async def link_telegram(
    body: LinkTelegramRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Привязывает Telegram к текущей идентичности. Требуется подпись от Telegram Login Widget (доказательство владения аккаунтом)."""
    payload = body.model_dump(mode="json")
    if not verify_telegram_login(payload, API_TOKEN, max_age_seconds=TELEGRAM_LOGIN_MAX_AGE):
        raise HTTPException(status_code=401, detail="Неверная подпись или устаревшие данные от Telegram")
    result = await idb.attach_telegram(session, identity.id, body.id)
    if not result:
        raise HTTPException(
            status_code=409,
            detail="Этот Telegram уже привязан к другой идентичности",
        )
    set_api_actor(request, identity_id=result.id, tg_id=result.tg_id)
    return IdentityResponse.model_validate(result)


@router.get("/me", response_model=IdentityResponse)
async def me(
    identity=Depends(verify_identity_token),
):
    """Текущая идентичность по заголовкам X-Identity-Id и X-Token."""
    return IdentityResponse.model_validate(identity)
