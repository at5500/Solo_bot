from sqlalchemy.ext.asyncio import AsyncSession

from logger import logger
from services.coupons import resolve_percent_coupon
from services.errors import ServiceError


async def resolve_percent_coupon_pricing(
    session: AsyncSession,
    billing_user_id: int,
    base_price_rub: int,
    coupon_code: str | None,
) -> tuple[int, int, int | None, str | None]:
    """Применяет процентный купон. Неверный/несуществующий купон игнорируется — возвращается базовая цена без скидки, чтобы оплата не блокировалась."""
    try:
        return await resolve_percent_coupon(
            session=session,
            billing_user_id=billing_user_id,
            base_price_rub=base_price_rub,
            coupon_code=coupon_code,
        )
    except ServiceError as e:
        logger.info("[Coupon] купон '{}' не применён ({}): {}", coupon_code, e.code, e.message)
        return int(base_price_rub), 0, None, None
