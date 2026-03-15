"""Pydantic schemas for public user-facing /me endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class MeProfileResponse(BaseModel):
    """User profile visible to the account owner."""

    tg_id: int
    username: str | None = None
    first_name: str | None = None
    balance: float = 0.0
    trial: int = 0
    key_count: int = 0
    preferred_currency: str = "RUB"
    created_at: datetime | None = None


class MeKeyShort(BaseModel):
    """Short key summary for the keys list."""

    email: str
    client_id: str
    server_id: str | None = None
    alias: str | None = None
    expiry_time: int
    is_frozen: bool = False
    tariff_id: int | None = None


class MeKeyDetails(BaseModel):
    """Full key details including subscription link."""

    email: str | None = None
    client_id: str
    server_id: str | None = None
    alias: str | None = None
    created_at: int | None = None
    expiry_time: int | None = None
    is_frozen: bool = False
    tariff_id: int | None = None
    link: str | None = Field(None, description="VLESS subscription URL")
    expiry_date: str | None = None
    days_left: int | None = None
    hours_left: int | None = None
    expired: bool = False
    selected_device_limit: int | None = None
    selected_traffic_limit: int | None = None
    current_device_limit: int | None = None
    current_traffic_limit: int | None = None


class MeTariffResponse(BaseModel):
    """Active tariff available for purchase."""

    id: int
    name: str
    group_code: str | None = None
    duration_days: int
    price_rub: int
    traffic_limit: int | None = None
    device_limit: int | None = None
    subgroup_title: str | None = None
    vless: bool = False
    configurable: bool = False


class MeReferralStats(BaseModel):
    """Referral statistics for the current user."""

    total_referrals: int = 0
    active_referrals: int = 0
    total_bonus: float = 0.0
    referral_link: str | None = None


class MePaymentResponse(BaseModel):
    """Payment history entry."""

    id: int | None = None
    amount: float
    currency: str = "RUB"
    status: str
    payment_system: str
    created_at: datetime | None = None


class MePurchaseRequest(BaseModel):
    """Request to buy a new subscription."""

    tariff_id: int = Field(..., description="Tariff ID to purchase")
    provider_id: str | None = Field(None, description="Payment provider if balance insufficient")
    success_url: str | None = Field(None, description="Redirect URL after successful payment")
    failure_url: str | None = Field(None, description="Redirect URL after failed payment")


class MePurchaseResponse(BaseModel):
    """Result of a purchase attempt."""

    created: bool = False
    payment_required: bool = False
    payment_url: str | None = None
    payment_id: str | None = None
    missing_amount: float | None = None
    error: str | None = None
    email: str | None = None
    client_id: str | None = None


class MeRenewRequest(BaseModel):
    """Request to renew a subscription key."""

    tariff_id: int = Field(..., description="Tariff ID to renew with")
    provider_id: str | None = Field(None, description="Payment provider for top-up if balance insufficient")
    success_url: str | None = Field(None, description="Redirect URL after successful payment")
    failure_url: str | None = Field(None, description="Redirect URL after failed payment")


class MeRenewResponse(BaseModel):
    """Result of a renewal attempt."""

    renewed: bool = False
    payment_required: bool = False
    payment_url: str | None = None
    payment_id: str | None = None
    missing_amount: float | None = None
    error: str | None = None
    new_expiry_time: int | None = None


class MiniAppLoginRequest(BaseModel):
    """Telegram Mini App initData for authentication."""

    init_data: str = Field(..., description="Raw initData string from Telegram Mini App")
