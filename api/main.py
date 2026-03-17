import asyncio
import os
from time import perf_counter

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

from audit import ensure_api_context, log_api_access, record_api_access_event_background
from config import API_LOGGING, API_VERSION, API_CORS_ORIGINS
from database import async_session_maker
from logger import logger

if API_VERSION == 1:
    from api.v1 import router as api_router, VERSION as API_DOC_VERSION
else:
    from api.v2 import router as api_router, VERSION as API_DOC_VERSION

app = FastAPI(
    title=f"SoloBot API (Alpha) — API v{API_DOC_VERSION}",
    version=API_DOC_VERSION,
    description=f"Версия API: **v{API_DOC_VERSION}**.",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=API_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_access_log_middleware(request: Request, call_next):
    context = ensure_api_context(request)
    if not API_LOGGING:
        response = await call_next(request)
        response.headers["X-Request-Id"] = context.request_id
        return response

    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = int((perf_counter() - started) * 1000)
        log_api_access(
            request,
            status_code=500,
            duration_ms=duration_ms,
            result="fail",
            reason=type(exc).__name__,
        )
        asyncio.create_task(
            record_api_access_event_background(
                async_session_maker,
                request,
                result="fail",
                reason=type(exc).__name__,
                status_code=500,
            )
        )
        raise

    duration_ms = int((perf_counter() - started) * 1000)
    response.headers["X-Request-Id"] = context.request_id
    result = "success" if response.status_code < 400 else "fail"
    log_api_access(
        request,
        status_code=response.status_code,
        duration_ms=duration_ms,
        result=result,
    )
    asyncio.create_task(
        record_api_access_event_background(
            async_session_maker,
            request,
            result=result,
            reason=None if response.status_code < 400 else str(response.status_code),
            status_code=response.status_code,
        )
    )
    return response


app.include_router(api_router)

_web_uploads_dir = "static/web_uploads"
os.makedirs(_web_uploads_dir, exist_ok=True)
app.mount("/api/web/uploads", StaticFiles(directory=_web_uploads_dir), name="web_uploads")
