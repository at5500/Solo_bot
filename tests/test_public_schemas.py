"""Tests for public store Pydantic schemas."""

import importlib.util
import os

import pytest
from pydantic import ValidationError


_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "api", "v2", "schemas", "public.py")
_spec = importlib.util.spec_from_file_location("public_schemas", os.path.abspath(_SCHEMA_PATH))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

PublicTariffItem = _mod.PublicTariffItem
PublicPurchaseRequest = _mod.PublicPurchaseRequest
PublicPurchaseResponse = _mod.PublicPurchaseResponse
PublicStatusResponse = _mod.PublicStatusResponse


class TestPublicTariffItem:
    def test_creation(self):
        t = PublicTariffItem(id=1, name="30 days", duration_days=30, price_rub=299)
        assert t.price_rub == 299
        assert t.traffic_limit is None

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            PublicTariffItem(id=1, name="30 days")


class TestPublicPurchaseRequest:
    def test_valid(self):
        r = PublicPurchaseRequest(email="a@b.com", tariff_id=1, provider_id="YOOKASSA")
        assert r.email == "a@b.com"

    def test_missing_email(self):
        with pytest.raises(ValidationError):
            PublicPurchaseRequest(tariff_id=1, provider_id="YOOKASSA")

    def test_missing_provider(self):
        with pytest.raises(ValidationError):
            PublicPurchaseRequest(email="a@b.com", tariff_id=1)

    def test_short_email(self):
        with pytest.raises(ValidationError):
            PublicPurchaseRequest(email="ab", tariff_id=1, provider_id="X")


class TestPublicPurchaseResponse:
    def test_success(self):
        r = PublicPurchaseResponse(payment_url="https://pay", payment_id="pid-1")
        assert r.payment_url == "https://pay"
        assert r.error is None

    def test_error(self):
        r = PublicPurchaseResponse(error="Provider not found")
        assert r.payment_url is None


class TestPublicStatusResponse:
    def test_pending(self):
        r = PublicStatusResponse(status="pending")
        assert r.link is None

    def test_ready(self):
        r = PublicStatusResponse(status="ready", link="vless://config", email="a@b.com")
        assert r.link == "vless://config"

    def test_missing_status(self):
        with pytest.raises(ValidationError):
            PublicStatusResponse()
