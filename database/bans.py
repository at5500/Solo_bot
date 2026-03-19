from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BlockedUser
from logger import logger


async def create_blocked_user(session: AsyncSession, tg_id: int):
    stmt = insert(BlockedUser).values(tg_id=tg_id).on_conflict_do_nothing(index_elements=[BlockedUser.tg_id])
    await session.execute(stmt)
    await session.commit()


async def save_blocked_user_ids(session: AsyncSession, tg_ids: list[int]) -> None:
    """Вставка списка tg_id в таблицу BlockedUser батчами по 500. Вызывать только из основного event loop."""
    if not tg_ids:
        return
    batch_size = 500
    total = 0
    for i in range(0, len(tg_ids), batch_size):
        batch = tg_ids[i : i + batch_size]
        values = [{"tg_id": tg_id} for tg_id in batch]
        stmt = insert(BlockedUser).values(values).on_conflict_do_nothing(index_elements=[BlockedUser.tg_id])
        await session.execute(stmt)
        await session.commit()
        total += len(batch)
    logger.info(f"📝 Добавлено {total} пользователей в blocked_users")
