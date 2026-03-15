"""Tests for Telegram Mini App initData HMAC validation.

Imports the validation function directly to avoid loading the full app.
"""

import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest


# --- Stub transitive dependencies so the module file can be loaded ----------

for _mod in [
    "config", "logger",
    "api", "api.depends", "api.v2", "api.v2.schemas", "api.v2.schemas.identities",
    "api.v2.schemas.me",
    "database", "database.identities",
]:
    if _mod not in sys.modules:
        _m = ModuleType(_mod)
        sys.modules[_mod] = _m

sys.modules["config"].API_TOKEN = "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"
sys.modules["logger"].logger = MagicMock()
sys.modules["api.depends"].get_session = MagicMock()
from pydantic import BaseModel as _BM

class _LoginResponse(_BM):
    identity_id: str = ""
    token: str = ""

sys.modules["api.v2.schemas.identities"].LoginResponse = _LoginResponse

from pydantic import BaseModel, Field

class _Stub(BaseModel):
    init_data: str = Field(...)

sys.modules["api.v2.schemas.me"].MiniAppLoginRequest = _Stub

# --- Load the module under test directly ------------------------------------

_AUTH_PATH = os.path.join(os.path.dirname(__file__), "..", "api", "v2", "routes", "miniapp_auth.py")
_spec = importlib.util.spec_from_file_location("miniapp_auth", os.path.abspath(_AUTH_PATH))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_verify = _mod._verify_miniapp_init_data

BOT_TOKEN = "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ123456789"


def _build_init_data(user_data: dict, bot_token: str, *, auth_date: int | None = None) -> str:
    if auth_date is None:
        auth_date = int(time.time())

    user_json = json.dumps(user_data, separators=(",", ":"))
    params = {
        "user": user_json,
        "auth_date": str(auth_date),
        "query_id": "test_query",
    }

    data_pairs = sorted(f"{k}={v}" for k, v in params.items())
    data_check_string = "\n".join(data_pairs)

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    parts = [f"{k}={quote(v)}" for k, v in params.items()]
    parts.append(f"hash={computed_hash}")
    return "&".join(parts)


class TestVerifyInitData:
    def test_valid_data(self):
        user = {"id": 123, "first_name": "Test"}
        init_data = _build_init_data(user, BOT_TOKEN)
        result = _verify(init_data, BOT_TOKEN)
        assert result is not None
        assert result["id"] == 123

    def test_expired_data(self):
        user = {"id": 123, "first_name": "Test"}
        init_data = _build_init_data(user, BOT_TOKEN, auth_date=int(time.time()) - 100_000)
        assert _verify(init_data, BOT_TOKEN) is None

    def test_wrong_bot_token(self):
        user = {"id": 123, "first_name": "Test"}
        init_data = _build_init_data(user, BOT_TOKEN)
        assert _verify(init_data, "9999999999:WRONGtokenXYZ") is None

    def test_missing_hash(self):
        assert _verify("user={}&auth_date=123", BOT_TOKEN) is None

    def test_missing_user(self):
        auth_date = str(int(time.time()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, f"auth_date={auth_date}".encode(), hashlib.sha256).hexdigest()
        assert _verify(f"auth_date={auth_date}&hash={h}", BOT_TOKEN) is None

    def test_empty_string(self):
        assert _verify("", BOT_TOKEN) is None

    def test_tampered_hash(self):
        """Replacing the hash with a bogus value should fail."""
        user = {"id": 123, "first_name": "Test"}
        init_data = _build_init_data(user, BOT_TOKEN)
        parts = init_data.split("&")
        parts = [p for p in parts if not p.startswith("hash=")]
        parts.append("hash=0000000000000000000000000000000000000000000000000000000000000000")
        tampered = "&".join(parts)
        assert _verify(tampered, BOT_TOKEN) is None

    def test_malformed_user_json(self):
        auth_date = str(int(time.time()))
        params = {"user": "not-json", "auth_date": auth_date}
        data_pairs = sorted(f"{k}={v}" for k, v in params.items())
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, "\n".join(data_pairs).encode(), hashlib.sha256).hexdigest()
        assert _verify(f"user=not-json&auth_date={auth_date}&hash={h}", BOT_TOKEN) is None
