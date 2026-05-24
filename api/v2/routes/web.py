import hashlib
import re
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import _identity_from_cookie, get_session, verify_identity_admin
from database.site_revision import bump_site_revision
from api.v2.schemas import WebBlockResponse, WebPageResponse, WebPageUpdate, WebTheme
from api.v2.schemas.web import (
    WebPageSaveResponse,
    WebPageThemeResponse,
    WebPageThemeUpdate,
    WebPageVariantCreate,
    WebPageVariantSummary,
    WebPageVariantUpdate,
    WebPageVariantsResponse,
    WebUploadResponse,
)
from database.models import (
    WebBlock,
    WebCustomElementBuild,
    WebErrorReport,
    WebFlow,
    WebFlowEvent,
    WebPage,
    WebPageVariant,
    WebPageVariantBlock,
    WebTheme as WebThemeModel,
)
from logger import logger


UPLOAD_DIR = Path("static/web_uploads")
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm"})
MAX_FILE_SIZE = 100 * 1024 * 1024
_IMAGE_RESIZE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_IMAGE_MAX_SIDE = 2048
_IMAGE_JPEG_QUALITY = 85
_IMAGE_WEBP_QUALITY = 85

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

EXTENSION_CONTENT_TYPES: dict[str, frozenset[str]] = {
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".gif": frozenset({"image/gif"}),
    ".webp": frozenset({"image/webp"}),
    ".svg": frozenset({"image/svg+xml", "text/xml", "application/xml", "text/plain"}),
    ".mp4": frozenset({"video/mp4"}),
    ".webm": frozenset({"video/webm"}),
}


def _optimize_image_bytes(data: bytes, ext: str) -> bytes:
    """Уменьшает большие картинки до _IMAGE_MAX_SIDE и пережимает с разумным качеством."""
    try:
        from io import BytesIO

        from PIL import Image, ImageOps

        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            w, h = img.size
            if max(w, h) <= _IMAGE_MAX_SIDE and len(data) < 500_000:
                return data
            if max(w, h) > _IMAGE_MAX_SIDE:
                img.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            save_kwargs: dict = {}
            if ext in (".jpg", ".jpeg"):
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                save_kwargs = {"format": "JPEG", "quality": _IMAGE_JPEG_QUALITY, "optimize": True, "progressive": True}
            elif ext == ".webp":
                save_kwargs = {"format": "WEBP", "quality": _IMAGE_WEBP_QUALITY, "method": 6}
            elif ext == ".png":
                save_kwargs = {"format": "PNG", "optimize": True}
            else:
                return data
            img.save(buffer, **save_kwargs)
            optimized = buffer.getvalue()
            return optimized if len(optimized) < len(data) else data
    except Exception:
        return data


def _sanitize_svg(data: bytes) -> bytes:
    import re as _re

    text = data.decode("utf-8", errors="replace")
    text = _re.sub(r"<script[^>]*>.*?</script>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"\bon\w+\s*=\s*[\"'][^\"']*[\"']", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"\bon\w+\s*=\s*\S+", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"(?:href|xlink:href)\s*=\s*[\"']\s*javascript:[^\"']*[\"']", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"(?:href|xlink:href)\s*=\s*[\"']\s*data:\s*text/html[^\"']*[\"']", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"(?:href|xlink:href)\s*=\s*[\"']\s*vbscript:[^\"']*[\"']", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"<foreignObject[^>]*>.*?</foreignObject>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"<iframe[^>]*>.*?</iframe>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r"<embed[^>]*>", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"<object[^>]*>.*?</object>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
    return text.encode("utf-8")


router = APIRouter(tags=["Web"])


class WebPagesListResponse(BaseModel):
    slugs: list[str]


KNOWN_PAGE_SLUGS = [
    "landing",
    "tariffs",
    "faq",
    "login",
    "dashboard",
    "checkout",
    "gift-entry",
    "referral-entry",
    "partner-entry",
    "payment-success",
    "payment-failure",
    "dashboard-keys",
    "dashboard-profile",
    "dashboard-instructions",
    "dashboard-referrals",
]

DEFAULT_VARIANT_KEY = "default"
DEFAULT_VARIANT_NAME = "Основной"


def _normalize_variant_key(value: str | None) -> str:
    raw = (value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not normalized:
        return DEFAULT_VARIANT_KEY
    return normalized[:64].strip("-") or DEFAULT_VARIANT_KEY


def _normalize_variant_name(value: str | None, fallback: str) -> str:
    name = (value or "").strip()
    return name[:255] if name else fallback


def _variant_summary(row: WebPageVariant) -> WebPageVariantSummary:
    return WebPageVariantSummary(
        key=row.variant_key,
        name=row.name or row.variant_key,
        is_active=bool(row.is_active),
    )


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


async def _list_variants(session: AsyncSession, slug: str) -> list[WebPageVariant]:
    result = await session.execute(
        select(WebPageVariant)
        .where(WebPageVariant.page_slug == slug)
        .order_by(WebPageVariant.is_active.desc(), WebPageVariant.created_at, WebPageVariant.variant_key)
    )
    return list(result.scalars().all())


async def _get_theme_tokens_for_legacy_page(session: AsyncSession, slug: str) -> dict:
    theme_result = await session.execute(select(WebThemeModel).where(WebThemeModel.page_slug == slug))
    theme_row = theme_result.scalar_one_or_none()
    return dict(theme_row.tokens or {}) if theme_row else {}


async def _get_legacy_blocks(session: AsyncSession, slug: str) -> list[WebBlock]:
    blocks_result = await session.execute(
        select(WebBlock).where(WebBlock.page_slug == slug).order_by(WebBlock.order, WebBlock.id)
    )
    return list(blocks_result.scalars().all())


async def _ensure_page_variants(session: AsyncSession, slug: str) -> list[WebPageVariant]:
    await get_or_create_page(session, slug)
    variants = await _list_variants(session, slug)
    if variants:
        if not any(variant.is_active for variant in variants):
            variants[0].is_active = True
            await session.flush()
            variants = await _list_variants(session, slug)
        return variants

    legacy_blocks = await _get_legacy_blocks(session, slug)
    theme_tokens = await _get_theme_tokens_for_legacy_page(session, slug)
    variant = WebPageVariant(
        page_slug=slug,
        variant_key=DEFAULT_VARIANT_KEY,
        name=DEFAULT_VARIANT_NAME,
        is_active=True,
        theme_tokens=theme_tokens,
    )
    session.add(variant)
    await session.flush()
    for legacy_block in legacy_blocks:
        session.add(
            WebPageVariantBlock(
                variant_id=variant.id,
                order=legacy_block.order,
                type=legacy_block.type,
                data=legacy_block.data,
            )
        )
    await session.flush()
    return await _list_variants(session, slug)


async def _resolve_variant(
    session: AsyncSession,
    slug: str,
    variant_key: str | None,
) -> tuple[WebPageVariant, list[WebPageVariant]]:
    variants = await _ensure_page_variants(session, slug)
    desired_key = _normalize_variant_key(variant_key) if variant_key else ""
    current = None
    if desired_key:
        current = next((variant for variant in variants if variant.variant_key == desired_key), None)
        if current is None:
            raise HTTPException(404, "Вариант страницы не найден")
    else:
        current = next((variant for variant in variants if variant.is_active), variants[0])
    return current, variants


async def _get_variant_blocks(session: AsyncSession, variant_id: str) -> list[WebBlockResponse]:
    blocks_result = await session.execute(
        select(WebPageVariantBlock)
        .where(WebPageVariantBlock.variant_id == variant_id)
        .order_by(WebPageVariantBlock.order, WebPageVariantBlock.id)
    )
    return [WebBlockResponse.model_validate(block) for block in blocks_result.scalars().all()]


async def _build_page_response(
    session: AsyncSession,
    slug: str,
    current: WebPageVariant,
    variants: list[WebPageVariant] | None = None,
) -> WebPageResponse:
    current_variants = variants or await _list_variants(session, slug)
    active = next((variant for variant in current_variants if variant.is_active), current)
    blocks = await _get_variant_blocks(session, current.id)
    theme = WebTheme(tokens=dict(current.theme_tokens or {}))
    return WebPageResponse(
        slug=slug,
        blocks=blocks,
        theme=theme,
        variant_key=current.variant_key,
        active_variant_key=active.variant_key,
        variants=[_variant_summary(variant) for variant in current_variants],
    )


async def _set_active_variant(session: AsyncSession, slug: str, variant_key: str) -> list[WebPageVariant]:
    variants = await _list_variants(session, slug)
    matched = False
    for variant in variants:
        is_target = variant.variant_key == variant_key
        variant.is_active = is_target
        matched = matched or is_target
    if not matched:
        raise HTTPException(404, "Вариант страницы не найден")
    await session.flush()
    return await _list_variants(session, slug)


def _generate_variant_key(existing_keys: set[str], requested_key: str | None, requested_name: str | None) -> str:
    base = _normalize_variant_key(requested_key or requested_name)
    if not base:
        base = DEFAULT_VARIANT_KEY
    if base not in existing_keys:
        return base
    suffix = 2
    while True:
        candidate = f"{base}-{suffix}"
        if candidate not in existing_keys:
            return candidate[:64]
        suffix += 1


@router.get("/api/web/pages/{slug}", response_model=WebPageResponse)
async def get_web_page(
    slug: str,
    variant: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    if not slug or len(slug) > 64 or not _SLUG_RE.match(slug):
        raise HTTPException(400, "Некорректный slug страницы")
    current, variants = await _resolve_variant(session, slug, variant)
    return await _build_page_response(session, slug, current, variants)


@router.get("/api/web/pages/{slug}/theme", response_model=WebPageThemeResponse)
async def get_web_page_theme(
    slug: str,
    variant: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Возвращает только theme tokens страницы (без блоков/вариантов). Используется для fallback-темы cabinet-страниц."""
    if not slug or len(slug) > 64 or not _SLUG_RE.match(slug):
        raise HTTPException(400, "Некорректный slug страницы")
    current, _ = await _resolve_variant(session, slug, variant)
    return WebPageThemeResponse(
        slug=slug,
        variant_key=current.variant_key,
        tokens=dict(current.theme_tokens or {}),
    )


@router.put("/api/web/pages/{slug}/theme", response_model=WebPageThemeResponse)
async def update_web_page_theme(
    slug: str,
    body: WebPageThemeUpdate,
    variant: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_admin),
):
    """Обновляет только theme_tokens страницы (без трогания блоков). Используется для sync темы между страницами."""
    if not slug or len(slug) > 64 or not _SLUG_RE.match(slug):
        raise HTTPException(400, "Некорректный slug страницы")
    current, _ = await _resolve_variant(session, slug, variant)
    current.theme_tokens = body.tokens
    await session.flush()
    await bump_site_revision(session)
    return WebPageThemeResponse(
        slug=slug,
        variant_key=current.variant_key,
        tokens=dict(current.theme_tokens or {}),
    )


@router.put("/api/web/pages/{slug}")
async def update_web_page(
    slug: str,
    body: WebPageUpdate,
    variant: str | None = Query(default=None),
    minimal: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_admin),
):
    current, _ = await _resolve_variant(session, slug, variant)
    await session.execute(delete(WebPageVariantBlock).where(WebPageVariantBlock.variant_id == current.id))

    for block in body.blocks:
        session.add(
            WebPageVariantBlock(
                variant_id=current.id,
                order=block.order,
                type=block.type,
                data=block.data,
            )
        )

    if body.theme is not None:
        current.theme_tokens = body.theme.tokens

    await session.flush()
    await bump_site_revision(session)
    refreshed_variants = await _list_variants(session, slug)
    refreshed_current = next((item for item in refreshed_variants if item.id == current.id), current)
    if minimal:
        active = next((item for item in refreshed_variants if item.is_active), refreshed_current)
        return WebPageSaveResponse(
            slug=slug,
            variant_key=refreshed_current.variant_key,
            active_variant_key=active.variant_key,
            variants=[_variant_summary(item) for item in refreshed_variants],
        )
    return await _build_page_response(session, slug, refreshed_current, refreshed_variants)


@router.get("/api/web/pages/{slug}/variants", response_model=WebPageVariantsResponse)
async def get_web_page_variants(
    slug: str,
    variant: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    try:
        current, variants = await _resolve_variant(session, slug, variant)
    except HTTPException:
        raise
    except Exception as exc:
        from logger import logger
        logger.warning("[web] variants resolve failed for slug={}: {}", slug, exc)
        raise HTTPException(status_code=404, detail="Страница или вариант не найдены")
    active = next((item for item in variants if item.is_active), current)
    return WebPageVariantsResponse(
        slug=slug,
        active_variant_key=active.variant_key,
        current_variant_key=current.variant_key,
        variants=[_variant_summary(item) for item in variants],
    )


@router.post("/api/web/pages/{slug}/variants", response_model=WebPageVariantsResponse)
async def create_web_page_variant(
    slug: str,
    body: WebPageVariantCreate,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_admin),
):
    source_variant, variants = await _resolve_variant(session, slug, body.from_variant_key)
    existing_keys = {variant.variant_key for variant in variants}
    variant_key = _generate_variant_key(existing_keys, body.key, body.name)
    if variant_key in existing_keys:
        raise HTTPException(400, "Вариант с таким ключом уже существует")

    variant_name = _normalize_variant_name(body.name, f"Вариант {len(variants) + 1}")
    new_variant = WebPageVariant(
        page_slug=slug,
        variant_key=variant_key,
        name=variant_name,
        is_active=False,
        theme_tokens=dict(source_variant.theme_tokens or {}),
    )
    session.add(new_variant)
    await session.flush()

    source_blocks = await _get_variant_blocks(session, source_variant.id)
    for block in source_blocks:
        session.add(
            WebPageVariantBlock(
                variant_id=new_variant.id,
                order=block.order,
                type=block.type,
                data=block.data,
            )
        )
    await session.flush()
    await bump_site_revision(session)

    refreshed = await _list_variants(session, slug)
    return WebPageVariantsResponse(
        slug=slug,
        active_variant_key=next((item.variant_key for item in refreshed if item.is_active), DEFAULT_VARIANT_KEY),
        current_variant_key=new_variant.variant_key,
        variants=[_variant_summary(item) for item in refreshed],
    )


@router.patch("/api/web/pages/{slug}/variants/{variant_key}", response_model=WebPageVariantsResponse)
async def update_web_page_variant(
    slug: str,
    variant_key: str,
    body: WebPageVariantUpdate,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_admin),
):
    current, variants = await _resolve_variant(session, slug, variant_key)
    if body.name is not None:
        current.name = _normalize_variant_name(body.name, current.name or current.variant_key)
    if body.make_active is True:
        variants = await _set_active_variant(session, slug, current.variant_key)
        current = next((item for item in variants if item.variant_key == current.variant_key), current)
    else:
        await session.flush()
        variants = await _list_variants(session, slug)

    await bump_site_revision(session)
    active = next((item for item in variants if item.is_active), current)
    return WebPageVariantsResponse(
        slug=slug,
        active_variant_key=active.variant_key,
        current_variant_key=current.variant_key,
        variants=[_variant_summary(item) for item in variants],
    )


@router.delete("/api/web/pages/{slug}/variants/{variant_key}", response_model=WebPageVariantsResponse)
async def delete_web_page_variant(
    slug: str,
    variant_key: str,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_admin),
):
    current, variants = await _resolve_variant(session, slug, variant_key)
    if len(variants) <= 1:
        raise HTTPException(400, "Нельзя удалить единственный вариант страницы")

    replacement = next((item for item in variants if item.variant_key != current.variant_key), None)
    await session.execute(delete(WebPageVariant).where(WebPageVariant.id == current.id))
    await session.flush()

    if current.is_active and replacement is not None:
        replacement_variants = await _set_active_variant(session, slug, replacement.variant_key)
    else:
        replacement_variants = await _list_variants(session, slug)

    await bump_site_revision(session)
    current_variant_key = replacement.variant_key if replacement is not None else DEFAULT_VARIANT_KEY
    active_variant_key = next(
        (item.variant_key for item in replacement_variants if item.is_active),
        current_variant_key,
    )
    return WebPageVariantsResponse(
        slug=slug,
        active_variant_key=active_variant_key,
        current_variant_key=current_variant_key,
        variants=[_variant_summary(item) for item in replacement_variants],
    )


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
    if file.content_type:
        allowed_types = EXTENSION_CONTENT_TYPES.get(ext)
        if allowed_types and file.content_type.lower() not in allowed_types:
            raise HTTPException(
                400,
                f"Тип файла ({file.content_type}) не соответствует расширению ({ext})",
            )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    chunks: list[bytes] = []
    size = 0
    for chunk in file.file:
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise HTTPException(400, f"Размер файла не более {MAX_FILE_SIZE // (1024 * 1024)} МБ")
        chunks.append(chunk)
    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOAD_DIR / name
    file_data = b"".join(chunks)
    if ext == ".svg":
        file_data = _sanitize_svg(file_data)
    elif ext in _IMAGE_RESIZE_EXTENSIONS:
        from core.executor import run_cpu

        file_data = await run_cpu(_optimize_image_bytes, file_data, ext)
    with open(path, "wb") as f:
        f.write(file_data)
    url = f"/api/web/uploads/{name}"
    logger.info(
        "[WebUpload] admin={} file={} -> {} ({} bytes)",
        identity.id,
        file.filename,
        name,
        len(file_data),
    )
    return WebUploadResponse(url=url)


# ── Custom Element Builds ──


class CustomElementBuildCreate(BaseModel):
    label: str = ""
    slug: str = ""
    runtime: str = "react-component"
    source_kind: str = "inline-code"
    source_value: str = ""
    export_name: str = "default"
    props_schema_text: str = ""
    sample_props_text: str = ""
    events_text: str = ""
    notes: str = ""


class CustomElementBuildUpdate(BaseModel):
    status: str | None = None
    summary: str | None = None
    next_steps: list[str] | None = None
    artifact: dict | None = None
    upload_meta: dict | None = None
    worker_id: str | None = None


def _build_to_dict(b: WebCustomElementBuild) -> dict:
    return {
        "id": b.id,
        "label": b.label,
        "slug": b.slug,
        "runtime": b.runtime,
        "sourceKind": b.source_kind,
        "sourceValue": b.source_value,
        "exportName": b.export_name,
        "propsSchemaText": b.props_schema_text,
        "samplePropsText": b.sample_props_text,
        "eventsText": b.events_text,
        "notes": b.notes,
        "status": b.status,
        "summary": b.summary,
        "nextSteps": b.next_steps or [],
        "artifact": b.artifact,
        "upload": b.upload_meta,
        "workerId": b.worker_id,
        "workerClaimedAt": b.worker_claimed_at.isoformat() if b.worker_claimed_at else None,
        "completedAt": b.completed_at.isoformat() if b.completed_at else None,
        "createdAt": b.created_at.isoformat() if b.created_at else None,
        "updatedAt": b.updated_at.isoformat() if b.updated_at else None,
    }


@router.get("/custom-element-builds")
async def list_custom_element_builds(
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    result = await session.execute(select(WebCustomElementBuild).order_by(WebCustomElementBuild.created_at.desc()))
    builds = result.scalars().all()
    return [_build_to_dict(b) for b in builds]


@router.post("/custom-element-builds")
async def create_custom_element_build(
    body: CustomElementBuildCreate,
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    build = WebCustomElementBuild(
        id=str(uuid.uuid4()),
        label=body.label,
        slug=body.slug,
        runtime=body.runtime,
        source_kind=body.source_kind,
        source_value=body.source_value,
        export_name=body.export_name,
        props_schema_text=body.props_schema_text,
        sample_props_text=body.sample_props_text,
        events_text=body.events_text,
        notes=body.notes,
        status="queued",
    )
    session.add(build)
    return _build_to_dict(build)


@router.get("/custom-element-builds/{build_id}")
async def get_custom_element_build(
    build_id: str,
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    build = await session.get(WebCustomElementBuild, build_id)
    if not build:
        raise HTTPException(404, "Build not found")
    return _build_to_dict(build)


@router.patch("/custom-element-builds/{build_id}")
async def update_custom_element_build(
    build_id: str,
    body: CustomElementBuildUpdate,
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    build = await session.get(WebCustomElementBuild, build_id)
    if not build:
        raise HTTPException(404, "Build not found")
    if body.status is not None:
        build.status = body.status
    if body.summary is not None:
        build.summary = body.summary
    if body.next_steps is not None:
        build.next_steps = body.next_steps
    if body.artifact is not None:
        build.artifact = body.artifact
    if body.upload_meta is not None:
        build.upload_meta = body.upload_meta
    if body.worker_id is not None:
        build.worker_id = body.worker_id
    return _build_to_dict(build)


@router.delete("/custom-element-builds/{build_id}")
async def delete_custom_element_build(
    build_id: str,
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    build = await session.get(WebCustomElementBuild, build_id)
    if not build:
        raise HTTPException(404, "Build not found")
    await session.delete(build)
    return {"ok": True}


# ── Flow Analytics ──


_SENSITIVE_KEY_RE = re.compile(
    r"(token|password|secret|api[_-]?key|authorization|cookie|session|auth|credential|bearer|pass|passwd|access[_-]?token|refresh[_-]?token|phone|email|hash|private|pin)",
    re.IGNORECASE,
)
_REDACTED = "[redacted]"
_MAX_REDACT_DEPTH = 6


def _redact_sensitive(value, depth: int = 0):
    if depth >= _MAX_REDACT_DEPTH:
        return _REDACTED
    if isinstance(value, dict):
        return {
            k: (_REDACTED if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k) else _redact_sensitive(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(v, depth + 1) for v in value[:100]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


class FlowEventBatch(BaseModel):
    events: list[dict]


@router.post("/analytics/flow-events")
async def ingest_flow_events(
    body: FlowEventBatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    try:
        from api.v2.routes.auth._fallback_limiter import check_and_increment
        from core.redis_cache import cache_incr_checked

        ip = (request.client.host if request.client else "") or "unknown"
        count, redis_ok = await cache_incr_checked(f"analytics_rate:{ip}", 60)
        if not redis_ok:
            count = check_and_increment(f"analytics_rate:{ip}", 60, 60)
        if count > 60:
            raise HTTPException(status_code=429, detail="Too many events")
    except HTTPException:
        raise
    except Exception:
        pass
    server_identity = await _identity_from_cookie(session, request)
    server_authenticated = server_identity is not None
    created = 0
    for raw in body.events[:100]:
        flow_id = str(raw.get("flowId", ""))[:64]
        node_id = str(raw.get("nodeId", ""))[:64]
        event_type = str(raw.get("eventType", ""))[:32]
        if not flow_id or not node_id or not event_type:
            continue
        metadata = raw.get("collectedDataSnapshot")
        if isinstance(metadata, dict):
            metadata = _redact_sensitive(metadata)
        else:
            metadata = None
        ev = WebFlowEvent(
            id=str(uuid.uuid4()),
            flow_id=flow_id,
            node_id=node_id,
            node_type=str(raw.get("nodeType", ""))[:32],
            event_type=event_type,
            ab_variant=(str(raw.get("abVariant"))[:16] if raw.get("abVariant") else None),
            device=(str(raw.get("device"))[:16] if raw.get("device") else None),
            locale=(str(raw.get("locale"))[:8] if raw.get("locale") else None),
            authenticated=server_authenticated,
            event_metadata=metadata,
        )
        session.add(ev)
        created += 1
    return {"ingested": created}


@router.get("/analytics/flow-funnel/{flow_id}")
async def get_flow_funnel(
    flow_id: str,
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                WebFlowEvent.node_id,
                WebFlowEvent.node_type,
                WebFlowEvent.event_type,
                func.count().label("cnt"),
            )
            .where(WebFlowEvent.flow_id == flow_id)
            .where(WebFlowEvent.created_at >= since)
            .group_by(WebFlowEvent.node_id, WebFlowEvent.node_type, WebFlowEvent.event_type)
        )
    ).all()

    nodes: dict[str, dict] = {}
    for node_id, node_type, event_type, cnt in rows:
        if node_id not in nodes:
            nodes[node_id] = {"nodeId": node_id, "nodeType": node_type, "entered": 0, "exited": 0, "completed": 0}
        if event_type == "flow_step_entered":
            nodes[node_id]["entered"] = cnt
        elif event_type == "flow_step_exited":
            nodes[node_id]["exited"] = cnt
        elif event_type == "flow_completed":
            nodes[node_id]["completed"] = cnt

    flow = await session.get(WebFlow, flow_id)
    if flow and flow.nodes:
        node_order = {n["id"]: i for i, n in enumerate(flow.nodes) if isinstance(n, dict)}
    else:
        node_order = {}

    funnel = sorted(nodes.values(), key=lambda n: node_order.get(n["nodeId"], 999))

    for i, node in enumerate(funnel):
        prev_entered = funnel[i - 1]["entered"] if i > 0 else node["entered"]
        node["dropOff"] = round((1 - node["entered"] / prev_entered) * 100, 1) if prev_entered > 0 else 0

    return {"flowId": flow_id, "days": days, "funnel": funnel}


# ── Error aggregation (in-house Sentry) ──


def _error_signature(name: str, message: str, stack: str | None, url: str | None) -> str:
    """Группировочная подпись: name + первая stack-frame + pathname."""
    first_frame = ""
    if stack:
        for line in stack.split("\n"):
            s = line.strip()
            if s.startswith("at ") or "webpack-internal" in s or ".tsx:" in s or ".ts:" in s or ".js:" in s:
                first_frame = s[:200]
                break
    pathname = ""
    if url:
        try:
            from urllib.parse import urlparse

            pathname = urlparse(url).path[:100]
        except Exception:
            pass
    key = f"{name}|{first_frame}|{pathname}|{message[:120]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# Strings that look like sensitive payloads — applied to URL, message and
# stack before persistence. ``_URL_SECRET_RE`` covers query/form params
# carrying one-shot tokens, OTP codes, or passwords; ``_JSON_SECRET_RE``
# does the same for JSON-shaped payloads that sometimes land in
# ``Error.message`` when frontend code throws ``JSON.stringify(payload)``;
# ``_BOT_TOKEN_RE`` scrubs Telegram bot tokens accidentally embedded in
# stringified exceptions; ``_LINK_PAYLOAD_RE`` catches ``link_<token>``
# payloads from ``/start link_…`` deeplinks that aiogram echoes in error
# reprs.
_SECRET_KEY_GROUP = (
    r"link_token|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth"
)
_URL_SECRET_RE = re.compile(
    r"((?:" + _SECRET_KEY_GROUP + r"|token|code)=)[^&\s#]+",
    re.IGNORECASE,
)
_JSON_SECRET_RE = re.compile(
    r'("(?:' + _SECRET_KEY_GROUP + r'|token|code)"\s*:\s*")[^"]*(")',
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]+", re.IGNORECASE)
_LINK_PAYLOAD_RE = re.compile(r"\blink_[A-Za-z0-9]{6,}\b")


def _scrub_secret_strings(value: str | None) -> str | None:
    """Removes secret-looking substrings from free-form text.

    Designed to be safe to apply to URLs, exception messages, and stack
    traces before they land in the error-report table (which is visible
    via the admin endpoint and would otherwise hold live ``link_token``
    values for the remainder of their 30-minute TTL).

    Catches both URL/form-encoded (``key=value``) and JSON-encoded
    (``"key": "value"``) shapes, so a frontend ``throw new
    Error(JSON.stringify(payload))`` cannot smuggle a token through.

    Args:
        value: Raw user-/exception-provided string, or ``None``.

    Returns:
        Scrubbed string, or ``None`` if the input was ``None``.
    """
    if not value:
        return value
    out = _URL_SECRET_RE.sub(r"\1<redacted>", value)
    out = _JSON_SECRET_RE.sub(r"\1<redacted>\2", out)
    out = _BOT_TOKEN_RE.sub("bot<redacted>", out)
    out = _LINK_PAYLOAD_RE.sub("link_<redacted>", out)
    return out


def _sanitize_http_url(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = str(value).strip()
    if not trimmed:
        return None
    lowered = trimmed.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://") or lowered.startswith("/")):
        return None
    return _scrub_secret_strings(trimmed[:500])


class ErrorReportIngest(BaseModel):
    name: str = ""
    message: str
    stack: str | None = None
    url: str | None = None
    userAgent: str | None = None
    tag: str | None = None
    context: dict | None = None


@router.post("/error-reports")
async def ingest_error_report(
    body: ErrorReportIngest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    try:
        from api.v2.routes.auth._fallback_limiter import check_and_increment
        from core.redis_cache import cache_incr_checked

        ip = (request.client.host if request.client else "") or "unknown"
        count, redis_ok = await cache_incr_checked(f"error_report_rate:{ip}", 60)
        if not redis_ok:
            count = check_and_increment(f"error_report_rate:{ip}", 30, 60)
        if count > 30:
            raise HTTPException(status_code=429, detail="Too many error reports")
    except HTTPException:
        raise
    except Exception:
        pass

    server_identity = await _identity_from_cookie(session, request)
    server_identity_id = getattr(server_identity, "id", None) if server_identity else None

    safe_context = None
    if isinstance(body.context, dict):
        safe_context = _redact_sensitive(body.context)

    safe_url = _sanitize_http_url(body.url)
    # Message and stack frequently embed request payloads / URL params that
    # carry one-shot tokens or OTP codes — scrub before persistence and
    # before signature computation (so signatures dedupe across distinct
    # token values).
    safe_message = _scrub_secret_strings(body.message[:4000]) if body.message else ""
    safe_stack = _scrub_secret_strings(body.stack[:16000]) if body.stack else None

    signature = _error_signature(body.name, safe_message or "", safe_stack, safe_url)

    existing = (
        await session.execute(select(WebErrorReport).where(WebErrorReport.signature == signature))
    ).scalar_one_or_none()

    if existing:
        existing.count += 1
        existing.last_seen_at = datetime.now(timezone.utc)
        existing.resolved = False
        if safe_context is not None:
            existing.last_context = safe_context
        if server_identity_id:
            existing.last_identity_id = server_identity_id[:36]
        return {"ok": True, "id": existing.id, "count": existing.count, "deduplicated": True}

    try:
        from api.v2.routes.auth._fallback_limiter import check_and_increment as _sig_check
        from core.redis_cache import cache_incr_checked as _sig_cache

        ip = (request.client.host if request.client else "") or "unknown"
        unique_key = f"error_sig_unique:{ip}"
        count_uniq, redis_ok = await _sig_cache(unique_key, 3600)
        if not redis_ok:
            count_uniq = _sig_check(unique_key, 20, 3600)
        if count_uniq > 20:
            raise HTTPException(status_code=429, detail="Too many distinct errors")
    except HTTPException:
        raise
    except Exception:
        pass

    report = WebErrorReport(
        id=str(uuid.uuid4()),
        signature=signature,
        error_name=body.name[:255] if body.name else "",
        error_message=safe_message or "",
        stack=safe_stack,
        url=safe_url,
        user_agent=body.userAgent[:500] if body.userAgent else None,
        tag=body.tag[:64] if body.tag else None,
        last_identity_id=server_identity_id[:36] if server_identity_id else None,
        last_context=safe_context,
        count=1,
        resolved=False,
    )
    session.add(report)
    return {"ok": True, "id": report.id, "count": 1, "deduplicated": False}


@router.get("/error-reports")
async def list_error_reports(
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
    resolved: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    q = select(WebErrorReport).order_by(WebErrorReport.last_seen_at.desc())
    if resolved is not None:
        q = q.where(WebErrorReport.resolved == resolved)
    q = q.offset(offset).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": r.id,
            "signature": r.signature,
            "errorName": r.error_name,
            "errorMessage": r.error_message,
            "stack": r.stack,
            "url": r.url,
            "userAgent": r.user_agent,
            "tag": r.tag,
            "lastIdentityId": r.last_identity_id,
            "lastContext": r.last_context,
            "count": r.count,
            "resolved": r.resolved,
            "firstSeenAt": r.first_seen_at.isoformat() if r.first_seen_at else None,
            "lastSeenAt": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ]


class ErrorReportPatch(BaseModel):
    resolved: bool | None = None


@router.patch("/error-reports/{report_id}")
async def update_error_report(
    report_id: str,
    body: ErrorReportPatch,
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    report = await session.get(WebErrorReport, report_id)
    if not report:
        raise HTTPException(404, "Not found")
    if body.resolved is not None:
        report.resolved = body.resolved
    return {"ok": True, "resolved": report.resolved}


@router.delete("/error-reports/{report_id}")
async def delete_error_report(
    report_id: str,
    session: AsyncSession = Depends(get_session),
    _identity=Depends(verify_identity_admin),
):
    report = await session.get(WebErrorReport, report_id)
    if not report:
        raise HTTPException(404, "Not found")
    await session.delete(report)
    return {"ok": True}
