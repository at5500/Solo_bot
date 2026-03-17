from fastapi import APIRouter

from api.v2.routes import (
    root_router,
    auth,
    users,
    keys,
    coupons,
    servers,
    tariffs,
    gifts,
    referrals,
    misc,
    partners,
    modules,
    management,
    settings,
    payment_links,
    identities,
    web,
)

router = APIRouter()

router.include_router(root_router)
router.include_router(auth.router, prefix="/api")
router.include_router(users.router, prefix="/api/users", tags=["Users"])
router.include_router(keys.router, prefix="/api/keys", tags=["Keys"])
router.include_router(coupons.router, prefix="/api/coupons", tags=["Coupons"])
router.include_router(servers.router, prefix="/api/servers", tags=["Servers"])
router.include_router(tariffs.public_router, prefix="/api/tariffs", tags=["Tariffs"])
router.include_router(tariffs.router, prefix="/api/tariffs", tags=["Tariffs"])
router.include_router(gifts.router, prefix="/api/gifts", tags=["Gifts"])
router.include_router(referrals.router, prefix="/api/referrals", tags=["Referrals"])
router.include_router(partners.router, prefix="/api/partners", tags=["Partners"])
router.include_router(payment_links.router, prefix="/api/payment-links", tags=["PaymentLinks"])
router.include_router(identities.router, prefix="/api/identities", tags=["Identities"])
router.include_router(misc.router, prefix="/api")
router.include_router(modules.router, prefix="/api")
router.include_router(management.router, prefix="/api/management", tags=["Management"])
router.include_router(settings.router, prefix="/api/settings", tags=["Settings"])
router.include_router(web.router, prefix="", tags=["Web"])