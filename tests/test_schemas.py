"""Tests for Pydantic schemas — pure validation, no app import needed.

Imports the schema file directly to avoid triggering api.v2.__init__
which pulls in the entire application.
"""

import importlib.util
import os
import sys
from datetime import datetime

import pytest
from pydantic import ValidationError


# Load the schema module directly without going through api.v2.__init__
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "api", "v2", "schemas", "me.py")
_spec = importlib.util.spec_from_file_location("me_schemas", os.path.abspath(_SCHEMA_PATH))
_me = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_me)

MePurchaseRequest = _me.MePurchaseRequest
MePurchaseResponse = _me.MePurchaseResponse
MeRenewRequest = _me.MeRenewRequest
MeRenewResponse = _me.MeRenewResponse
MeProfileResponse = _me.MeProfileResponse
MeKeyShort = _me.MeKeyShort
MeKeyDetails = _me.MeKeyDetails
MeTariffItem = _me.MeTariffItem
MeTariffGroup = _me.MeTariffGroup
MeTariffsResponse = _me.MeTariffsResponse
MeDiscountInfo = _me.MeDiscountInfo
MeReferralStats = _me.MeReferralStats
MePaymentResponse = _me.MePaymentResponse
MeTrialResponse = _me.MeTrialResponse
MiniAppLoginRequest = _me.MiniAppLoginRequest


class TestMeProfileResponse:
    def test_minimal(self):
        p = MeProfileResponse(tg_id=123)
        assert p.balance == 0.0
        assert p.trial == 0
        assert p.key_count == 0
        assert p.preferred_currency == "RUB"

    def test_full(self):
        p = MeProfileResponse(
            tg_id=123, username="user", first_name="Name",
            balance=100.5, trial=1, key_count=3,
            preferred_currency="USD", created_at=datetime(2026, 1, 1),
        )
        assert p.tg_id == 123
        assert p.balance == 100.5


class TestMeKeyShort:
    def test_required_fields(self):
        k = MeKeyShort(email="a@b", client_id="uuid", expiry_time=1000)
        assert k.is_frozen is False
        assert k.tariff_id is None

    def test_missing_email(self):
        with pytest.raises(ValidationError):
            MeKeyShort(client_id="uuid", expiry_time=1000)


class TestMeKeyDetails:
    def test_defaults(self):
        d = MeKeyDetails(client_id="uuid")
        assert d.expired is False
        assert d.link is None
        assert d.is_frozen is False

    def test_with_link(self):
        d = MeKeyDetails(client_id="uuid", link="vless://server")
        assert d.link == "vless://server"


class TestMeTariffItem:
    def test_creation(self):
        t = MeTariffItem(id=1, name="Basic", duration_days=30, price_rub=200)
        assert t.vless is False
        assert t.configurable is False
        assert t.group_code is None


class TestMeTariffsResponse:
    def test_empty(self):
        r = MeTariffsResponse()
        assert r.groups == {}
        assert r.discount is None

    def test_with_groups(self):
        t1 = MeTariffItem(id=1, name="30 days", duration_days=30, price_rub=200, group_code="standard")
        t2 = MeTariffItem(id=2, name="30 days -50%", duration_days=30, price_rub=100, group_code="discounts")
        r = MeTariffsResponse(
            groups={
                "standard": MeTariffGroup(tariffs=[t1]),
                "discounts": MeTariffGroup(tariffs=[t2]),
            },
            discount=MeDiscountInfo(
                type="hot_lead_step_2",
                tariff_group="discounts",
                expires_at=datetime(2026, 3, 18, 12, 0),
            ),
        )
        assert len(r.groups) == 2
        assert r.discount is not None
        assert r.discount.tariff_group == "discounts"
        assert len(r.groups["standard"].tariffs) == 1

    def test_no_discount(self):
        t1 = MeTariffItem(id=1, name="30 days", duration_days=30, price_rub=200)
        r = MeTariffsResponse(
            groups={"standard": MeTariffGroup(tariffs=[t1])},
            discount=None,
        )
        assert r.discount is None
        assert len(r.groups["standard"].tariffs) == 1


class TestMeDiscountInfo:
    def test_creation(self):
        d = MeDiscountInfo(
            type="hot_lead_step_3",
            tariff_group="discounts_max",
            expires_at=datetime(2026, 3, 18),
        )
        assert d.type == "hot_lead_step_3"
        assert d.tariff_group == "discounts_max"

    def test_missing_fields(self):
        with pytest.raises(ValidationError):
            MeDiscountInfo()


class TestMeReferralStats:
    def test_defaults(self):
        r = MeReferralStats()
        assert r.total_referrals == 0
        assert r.referral_link is None

    def test_with_link(self):
        r = MeReferralStats(total_referrals=5, referral_link="https://t.me/bot?start=ref_1")
        assert r.total_referrals == 5


class TestMePaymentResponse:
    def test_creation(self):
        p = MePaymentResponse(amount=200, status="success", payment_system="yookassa")
        assert p.currency == "RUB"
        assert p.id is None

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            MePaymentResponse(amount=200, status="success")


class TestMePurchaseRequest:
    def test_minimal(self):
        r = MePurchaseRequest(tariff_id=3)
        assert r.tariff_id == 3
        assert r.provider_id is None

    def test_with_provider(self):
        r = MePurchaseRequest(tariff_id=3, provider_id="ROBOKASSA")
        assert r.provider_id == "ROBOKASSA"

    def test_missing_tariff(self):
        with pytest.raises(ValidationError):
            MePurchaseRequest()


class TestMePurchaseResponse:
    def test_created(self):
        r = MePurchaseResponse(created=True, email="abc123", client_id="uuid-1")
        assert r.created is True
        assert r.payment_required is False

    def test_payment_required(self):
        r = MePurchaseResponse(
            created=False, payment_required=True,
            payment_url="https://pay", missing_amount=50.0,
        )
        assert r.payment_url == "https://pay"

    def test_error(self):
        r = MePurchaseResponse(created=False, error="No servers")
        assert r.error == "No servers"


class TestMeRenewRequest:
    def test_minimal(self):
        r = MeRenewRequest(tariff_id=5)
        assert r.tariff_id == 5
        assert r.provider_id is None

    def test_with_provider(self):
        r = MeRenewRequest(tariff_id=5, provider_id="YOOKASSA", success_url="https://ok")
        assert r.provider_id == "YOOKASSA"

    def test_missing_tariff(self):
        with pytest.raises(ValidationError):
            MeRenewRequest()


class TestMeRenewResponse:
    def test_renewed(self):
        r = MeRenewResponse(renewed=True, new_expiry_time=1700000000000)
        assert r.renewed is True
        assert r.payment_required is False

    def test_payment_required(self):
        r = MeRenewResponse(
            renewed=False, payment_required=True,
            payment_url="https://pay", missing_amount=100.0,
        )
        assert r.payment_url == "https://pay"
        assert r.missing_amount == 100.0


class TestMeKeyDetailsLiveFields:
    def test_with_panel_data(self):
        d = MeKeyDetails(
            client_id="uuid", traffic_used_gb=45.5, devices_connected=2,
            current_device_limit=10, current_traffic_limit=100,
        )
        assert d.traffic_used_gb == 45.5
        assert d.devices_connected == 2

    def test_without_panel_data(self):
        d = MeKeyDetails(client_id="uuid")
        assert d.traffic_used_gb is None
        assert d.devices_connected is None


class TestMeTrialResponse:
    def test_activated(self):
        r = MeTrialResponse(
            activated=True, email="abc123", client_id="uuid-1",
            link="vless://config", expiry_time=1700000000000,
        )
        assert r.activated is True
        assert r.link == "vless://config"

    def test_already_used(self):
        r = MeTrialResponse(activated=False, error="Trial already used")
        assert r.activated is False
        assert r.error == "Trial already used"

    def test_defaults(self):
        r = MeTrialResponse()
        assert r.activated is False
        assert r.email is None


class TestMiniAppLoginRequest:
    def test_valid(self):
        r = MiniAppLoginRequest(init_data="user=...&hash=abc")
        assert r.init_data == "user=...&hash=abc"

    def test_missing(self):
        with pytest.raises(ValidationError):
            MiniAppLoginRequest()
