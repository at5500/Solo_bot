from fastapi import APIRouter

from api.v2.routes.auth import (
    email_verify,
    google,
    link,
    link_account,
    partner,
    password,
    photo,
    self_link,
    session,
    telegram,
    tg_name,
    yandex,
)


router = APIRouter(prefix="/auth", tags=["Auth"])
router.include_router(password.router)
router.include_router(telegram.router)
router.include_router(google.router)
router.include_router(yandex.router)
router.include_router(link.router)
router.include_router(link_account.router)
router.include_router(email_verify.router)
router.include_router(session.router)
router.include_router(photo.router)
router.include_router(self_link.router)
router.include_router(tg_name.router)
router.include_router(partner.router)

__all__ = ["router"]
