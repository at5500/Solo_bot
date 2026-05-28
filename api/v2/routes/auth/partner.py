"""Apply a partner code to the authenticated identity (Mini App flow).

Called by the front-end after a successful ``loginTelegramWebapp`` when
``start_param`` is a ``partner_<code>`` deep link — i.e. the user
arrived via the smart-redirect from ``<site>/?partner=<code>``.

The endpoint is deliberately idempotent and silent on the «nothing to
do» path: a re-share of the same link to an already-referred user
should not change any state and should not flash an error in the UI.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.depends import get_session, verify_identity_token
from logger import logger
from utils.partner_apply import apply_partner_code


router = APIRouter()


class ApplyPartnerCodeRequest(BaseModel):
    """Body of ``POST /auth/apply-partner-code``."""

    code: str


class ApplyPartnerCodeResult(BaseModel):
    """Result of an apply attempt.

    ``applied`` is ``True`` only when a brand-new referral was recorded.
    Every other outcome (already-referred, malformed code, self-refer,
    unknown inviter) returns ``False`` — the front-end is expected to
    stay silent in that case.
    """

    ok: bool
    applied: bool


@router.post("/apply-partner-code", response_model=ApplyPartnerCodeResult)
async def apply_partner_code_endpoint(
    body: ApplyPartnerCodeRequest,
    session: AsyncSession = Depends(get_session),
    identity=Depends(verify_identity_token),
):
    """Records a referral for the authenticated identity if none yet.

    See :func:`utils.partner_apply.apply_partner_code` for the full
    silent-skip matrix. The commit happens here so the helper stays
    composable inside other transactions.
    """
    raw = (body.code or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Код обязателен")
    try:
        applied = await apply_partner_code(session, identity, raw)
    except Exception as exc:
        # Any DB / cache hiccup here shouldn't fail the caller's flow —
        # the partner code is a nice-to-have, not a hard prerequisite
        # for using the Mini App. Log and report ``applied=False``.
        logger.warning("[Auth] apply-partner-code failed: {}", exc)
        return ApplyPartnerCodeResult(ok=True, applied=False)
    if applied:
        await session.commit()
    return ApplyPartnerCodeResult(ok=True, applied=applied)
