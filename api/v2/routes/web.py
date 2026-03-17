import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_admin
from api.v2.schemas import WebPageResponse, WebPageUpdate, WebBlockResponse, WebTheme
from api.v2.schemas.web import WebUploadResponse
from database.models import WebPage, WebBlock, WebTheme as WebThemeModel

UPLOAD_DIR = Path("static/web_uploads")
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm"})
MAX_FILE_SIZE = 100 * 1024 * 1024

router = APIRouter(tags=["Web"])


class WebPagesListResponse(BaseModel):
    slugs: list[str]


KNOWN_PAGE_SLUGS = ["landing", "tariffs", "faq", "login", "dashboard"]


@router.get("/api/web/pages", response_model=WebPagesListResponse)
async def list_web_pages(
    session: AsyncSession = Depends(get_session),
):
    """Список slug всех страниц сайта (для экспорта/импорта). Включает известные страницы, даже если запись ещё не создана."""
    result = await session.execute(select(WebPage.slug).order_by(WebPage.slug))
    from_db = {row[0] for row in result.fetchall()}
    slugs = sorted(from_db | set(KNOWN_PAGE_SLUGS))
    return WebPagesListResponse(slugs=slugs)


async def get_or_create_page(session: AsyncSession, slug: str) -> WebPage:
    result = await session.execute(select(WebPage).where(WebPage.slug == slug))
    page = result.scalar_one_or_none()
    if page is None:
        page = WebPage(slug=slug, title=slug)
        session.add(page)
        await session.flush()
    return page


@router.get("/api/web/pages/{slug}", response_model=WebPageResponse)
async def get_web_page(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    await get_or_create_page(session, slug)

    blocks_result = await session.execute(
        select(WebBlock).where(WebBlock.page_slug == slug).order_by(WebBlock.order, WebBlock.id)
    )
    blocks = [WebBlockResponse.model_validate(b) for b in blocks_result.scalars().all()]

    theme_result = await session.execute(select(WebThemeModel).where(WebThemeModel.page_slug == slug))
    theme_row = theme_result.scalar_one_or_none()
    theme = WebTheme(tokens=theme_row.tokens) if theme_row else None

    return WebPageResponse(slug=slug, blocks=blocks, theme=theme)


@router.put("/api/web/pages/{slug}", response_model=WebPageResponse)
async def update_web_page(
    slug: str,
    body: WebPageUpdate,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_admin),
):
    await get_or_create_page(session, slug)

    await session.execute(delete(WebBlock).where(WebBlock.page_slug == slug))

    for block in body.blocks:
        session.add(
            WebBlock(
                page_slug=slug,
                order=block.order,
                type=block.type,
                data=block.data,
            )
        )

    theme_row = None
    if body.theme is not None:
        result = await session.execute(select(WebThemeModel).where(WebThemeModel.page_slug == slug))
        theme_row = result.scalar_one_or_none()
        if theme_row is None:
            theme_row = WebThemeModel(page_slug=slug, tokens=body.theme.tokens)
            session.add(theme_row)
        else:
            theme_row.tokens = body.theme.tokens

    await session.flush()

    blocks_result = await session.execute(
        select(WebBlock).where(WebBlock.page_slug == slug).order_by(WebBlock.order, WebBlock.id)
    )
    blocks = [WebBlockResponse.model_validate(b) for b in blocks_result.scalars().all()]

    if theme_row is None:
        theme_result = await session.execute(select(WebThemeModel).where(WebThemeModel.page_slug == slug))
        theme_row = theme_result.scalar_one_or_none()
    theme = WebTheme(tokens=theme_row.tokens) if theme_row else None

    return WebPageResponse(slug=slug, blocks=blocks, theme=theme)


@router.post("/api/web/upload", response_model=WebUploadResponse)
async def upload_media(
    file: UploadFile = File(...),
    identity=Depends(verify_identity_admin),
):
    """Upload image or video for landing blocks and return same-origin URL."""
    if not file.filename or "." not in file.filename:
        raise HTTPException(400, "Файл должен иметь расширение")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Разрешены только: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    size = 0
    for chunk in file.file:
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise HTTPException(400, f"Размер файла не более {MAX_FILE_SIZE // (1024*1024)} МБ")
    await file.seek(0)
    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / name
    with open(path, "wb") as f:
        while chunk := await file.read(64 * 1024):
            f.write(chunk)
    url = f"/api/web/uploads/{name}"
    return WebUploadResponse(url=url)

