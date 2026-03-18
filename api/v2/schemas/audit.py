from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEventResponse(BaseModel):
    id: int | None = None
    event_type: str
    channel: str
    actor_identity_id: str | None
    actor_tg_id: int | None
    path_or_handler: str
    entity_type: str | None
    entity_id: str | None
    result: str
    reason: str | None
    metadata: dict[str, Any] | None
    request_id: str | None
    created_at: datetime | None


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    limit: int
    offset: int


class AuditPathStat(BaseModel):
    step: str
    label: str
    total: int
    success: int
    fail: int
    unique_users: int
    fail_rate_pct: float


class AuditFunnelStep(BaseModel):
    step: str
    label: str
    count: int
    conversion_from_prev_pct: float | None


class AuditStatsSummary(BaseModel):
    date_from: str
    date_to: str
    total_events: int
    unique_users: int


class AuditStatsResponse(BaseModel):
    summary: AuditStatsSummary
    by_path: list[AuditPathStat]
    funnel: list[AuditFunnelStep]
