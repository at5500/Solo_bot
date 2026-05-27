"""Endpoints driving the «self-link» auto-attach flow.

* ``GET /auth/me/self-link`` — returns the caller's stable Profile URL
  with an opaque ``?l=<code>`` token. The URL is the canonical entry
  point to the web app: the user puts it in browser favourites,
  forwards it, etc. Any visitor who hits the URL while authenticated
  (in the other context — web vs Mini App) auto-merges with the
  originating identity via :func:`consume_self_link`.

* ``POST /auth/consume-self-link`` — looks up the identity behind a
  code, decides whether to attach caller's missing channel (email or
  ``tg_id``) into that identity, and re-issues the auth cookie for the
  surviving identity. The caller must already be authenticated.

The merge intentionally narrows to «one identity has only email, the
other only tg_id». If both sides already have both channels the call
returns ``linked=false`` — we don't silently fuse two complete profiles.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, set_auth_cookie, verify_identity_token
from core.settings.web_config import get_site_url
from database import identities as idb
from utils.photo_cache import invalidate_photo_cache
from utils.self_link import (
    drop_self_link_code,
    ensure_self_link_code,
    resolve_self_link_code,
)
from utils.tg_name_cache import invalidate_tg_name_cache


router = APIRouter()


class SelfLinkUrlResponse(BaseModel):
    """Stable Profile URL for the caller.

    The URL is safe to share — the embedded ``?l=`` code is opaque and
    only useful when consumed against the same backend.
    """

    url: str
    code: str


class SelfLinkConsumeRequest(BaseModel):
    """Body of ``POST /auth/consume-self-link``."""

    code: str


class SelfLinkConsumeResult(BaseModel):
    """Outcome of a consume attempt — phrased so the frontend can use
    ``message`` directly in a user-facing dialog."""

    ok: bool
    linked: bool
    message: str


@router.get("/me/self-link", response_model=SelfLinkUrlResponse)
async def get_my_self_link(
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Returns the caller's stable Profile URL."""
    del session
    site = get_site_url()
    if not site:
        raise HTTPException(status_code=503, detail="URL веб-приложения не настроен")
    code = await ensure_self_link_code(str(identity.id))
    return SelfLinkUrlResponse(url=f"{site}/?l={code}", code=code)


@router.post("/consume-self-link", response_model=SelfLinkConsumeResult)
async def consume_self_link(
    body: SelfLinkConsumeRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Auto-merges the caller into the identity that owns ``code``.

    Decision matrix (``caller`` is the current auth-cookie identity,
    ``target`` is the owner of the self-link code):

    +----------------------+----------------------+------------------------------+
    | caller has...        | target has...        | action                       |
    +======================+======================+==============================+
    | email (no tg_id)     | tg_id (no email)     | attach caller email → target |
    | tg_id (no email)     | email (no tg_id)     | attach caller tg_id → target |
    | identical identity   | same                 | no-op (already same account) |
    | anything else        | anything else        | refuse — both are complete   |
    +----------------------+----------------------+------------------------------+

    On a successful attach the auth cookie is re-issued for the merged
    identity, since the caller's previous identity may have been deleted
    during the merge.

    Returns:
        :class:`SelfLinkConsumeResult` with a ready-to-show Russian
        ``message`` (one line, fits in an alert).
    """
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Код обязателен")

    target_id = await resolve_self_link_code(code)
    if not target_id:
        return SelfLinkConsumeResult(
            ok=True,
            linked=False,
            message=(
                "Эта ссылка уже не работает. "
                "Откройте профиль на нужном устройстве и скопируйте свежую."
            ),
        )

    if target_id == str(identity.id):
        # Caller opened their own self-link on the same identity — nothing
        # to do and nothing useful to say. Front-end skips the dialog on
        # an empty ``message``.
        return SelfLinkConsumeResult(ok=True, linked=False, message="")

    target = await idb.get_identity_by_id(session, target_id)
    if not target:
        # Stale code pointing at a deleted identity (e.g. it was the
        # losing side of a previous merge). Clean up and treat as expired.
        await drop_self_link_code(target_id)
        return SelfLinkConsumeResult(
            ok=True,
            linked=False,
            message=(
                "Эта ссылка уже не работает. "
                "Откройте профиль на нужном устройстве и скопируйте свежую."
            ),
        )

    caller_email = (identity.email or "").strip()
    caller_tg = identity.tg_id
    target_email = (target.email or "").strip()
    target_tg = target.tg_id

    merged = None
    if caller_email and not caller_tg and target_tg and not target_email:
        merged = await idb.attach_email(session, target_id, caller_email)
    elif caller_tg and not caller_email and target_email and not target_tg:
        merged = await idb.attach_telegram(session, target_id, int(caller_tg))
    else:
        # Both sides already complete (each has both email and tg_id) —
        # auto-merge isn't applicable. Stay silent: a user who opens a
        # self-link from an already-linked context doesn't need a popup
        # telling them nothing happened.
        return SelfLinkConsumeResult(ok=True, linked=False, message="")

    if merged is None:
        return SelfLinkConsumeResult(
            ok=True,
            linked=False,
            message=(
                "На обоих аккаунтах есть подписки — мы не стали ничего трогать, "
                "чтобы не потерять оплату. Напишите в поддержку, поможем."
            ),
        )

    await session.commit()

    # Refresh both per-identity caches: target may have just gained a
    # tg_id (positive resolution where there used to be none), or the
    # caller's previous cache entries are now defunct after the absorb.
    await invalidate_photo_cache(str(merged.id))
    await invalidate_tg_name_cache(str(merged.id))
    # Drop the self-link code that pointed at the caller — the caller
    # identity was absorbed and no longer exists.
    await drop_self_link_code(str(identity.id))

    # Re-issue auth cookie for the surviving identity so the caller's
    # session keeps working after the merge.
    new_token = await idb.issue_token_for_identity(session, merged, request=request)
    set_auth_cookie(response, new_token, request)

    return SelfLinkConsumeResult(
        ok=True,
        linked=True,
        message=(
            "Готово! Теперь телеграм-бот и личный кабинет — один аккаунт, "
            "и подписка работает везде."
        ),
    )
