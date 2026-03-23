"""Pydantic schemas for anonymous public endpoints (/api/public/...)."""

from pydantic import BaseModel, Field


class PublicTariffItem(BaseModel):
    """Tariff available for purchase."""

    id: int
    name: str
    duration_days: int
    price_rub: int
    traffic_limit: int | None = None
    device_limit: int | None = None


class PublicPurchaseRequest(BaseModel):
    """Anonymous purchase request from landing page."""

    email: str = Field(..., min_length=3)
    tariff_id: int
    provider_id: str = Field(..., description="YOOKASSA, ROBOKASSA, etc.")
    success_url: str | None = None
    failure_url: str | None = None


class PublicPurchaseResponse(BaseModel):
    """Payment URL to redirect the buyer."""

    payment_url: str | None = None
    payment_id: str | None = None
    error: str | None = None


class PublicStatusResponse(BaseModel):
    """Payment/key status for polling."""

    status: str = Field(..., description="pending, paid, ready, failed")
    link: str | None = Field(None, description="VPN config link when ready")
    email: str | None = None
