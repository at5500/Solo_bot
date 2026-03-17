from typing import Any

from pydantic import BaseModel, Field


class WebBlockBase(BaseModel):
    type: str = Field(..., max_length=64)
    order: int
    data: dict[str, Any]


class WebBlockResponse(WebBlockBase):
    id: str

    class Config:
        from_attributes = True


class WebTheme(BaseModel):
    tokens: dict[str, Any]


class WebPageResponse(BaseModel):
    slug: str
    blocks: list[WebBlockResponse]
    theme: WebTheme | None = None


class WebPageUpdate(BaseModel):
    blocks: list[WebBlockBase]
    theme: WebTheme | None = None


class WebUploadResponse(BaseModel):
    url: str

