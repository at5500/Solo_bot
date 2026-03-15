"""Telegram Mini App authentication endpoint."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, unquote

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session
from api.v2.schemas.identities import LoginResponse
from api.v2.schemas.me import MiniAppLoginRequest
from config import API_TOKEN
from database import identities as idb


router = APIRouter(prefix="/auth", tags=["Auth"])

MINIAPP_MAX_AGE = 86400  # 24h


def _verify_miniapp_init_data(init_data: str, bot_token: str, *, max_age: int = MINIAPP_MAX_AGE) -> dict | None:
    """Validate Telegram Mini App initData and return parsed user dict or None."""
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
    except Exception:
        return None

    received_hash = parsed.get("hash", [None])[0]
    if not received_hash:
        return None

    auth_date_str = parsed.get("auth_date", [None])[0]
    if not auth_date_str:
        return None
    try:
        auth_date = int(auth_date_str)
    except (TypeError, ValueError):
        return None
    if auth_date < time.time() - max_age:
        return None

    data_pairs = []
    for key, values in parsed.items():
        if key == "hash":
            continue
        data_pairs.append(f"{key}={values[0]}")
    data_pairs.sort()
    data_check_string = "\n".join(data_pairs)

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        return None

    user_raw = parsed.get("user", [None])[0]
    if not user_raw:
        return None
    try:
        return json.loads(unquote(user_raw))
    except (json.JSONDecodeError, TypeError):
        return None


@router.post("/login-miniapp", response_model=LoginResponse)
async def login_miniapp(
    body: MiniAppLoginRequest,
    session: AsyncSession = Depends(get_session),
):
    """Authenticate via Telegram Mini App initData. Returns identity_id and API token."""
    user_data = _verify_miniapp_init_data(body.init_data, API_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid or expired initData")

    tg_id = user_data.get("id")
    if not tg_id:
        raise HTTPException(status_code=401, detail="No user id in initData")

    identity = await idb.get_or_create_identity_for_tg(session, int(tg_id))
    token = await idb.issue_token_for_identity(session, identity)
    return LoginResponse(identity_id=identity.id, token=token)
