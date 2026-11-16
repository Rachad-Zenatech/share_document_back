"""Social Media Script review routes: who may do what on a script document, inviting and sending it, checking in,
routing, the manager's decision and creating the post (see services/sec_filings/script_review_rules.py)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from models.sec_filings.script_review_model import (
    ScriptCheckInPayload,
    ScriptCreatePostPayload,
    ScriptDecisionPayload,
    ScriptFinalizePayload,
    ScriptInvitePayload,
    ScriptSendPayload,
)
from services.sec_filings import script_review_flow as flow
from services.sec_filings import script_review_service as review
from services.shared.auth.auth_service import get_current_user_id_dependency

router = APIRouter(prefix="/documents/{document_id}/script-review")


async def _run(coro):
    try:
        return await coro
    except review.ScriptReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("")
async def get_script_review(document_id: str, user_id: UUID = Depends(get_current_user_id_dependency)):
    """The review's state and what the caller may do (`access`: can_edit, can_note, can_submit, can_check_in,
    can_finalize, can_decide, can_create_post, can_delete)."""
    return await _run(review.get_state(user_id, document_id))


@router.get("/history")
async def get_script_review_history(document_id: str, user_id: UUID = Depends(get_current_user_id_dependency)):
    return {"events": await _run(review.history(user_id, document_id))}


@router.put("/invitees")
async def set_script_invitees(document_id: str, payload: ScriptInvitePayload, user_id: UUID = Depends(get_current_user_id_dependency)):
    """Creator only: who reviews (content reviewer, suggesting reviewer, manager or viewer) and with EDIT or VIEW capacity."""
    return await _run(review.invite(user_id, document_id, payload.invitees))


@router.post("/send")
async def send_script_for_review(document_id: str, payload: ScriptSendPayload, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await _run(review.send_for_review(user_id, document_id, payload.invitees, payload.page_ids, payload.note))


@router.put("/check-in")
async def check_in_script(document_id: str, payload: ScriptCheckInPayload, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await _run(flow.check_in(user_id, document_id, payload.vote, payload.note))


@router.delete("/check-in")
async def withdraw_script_check_in(document_id: str, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await _run(flow.withdraw_check_in(user_id, document_id))


@router.post("/finalize")
async def finalize_script(document_id: str, payload: ScriptFinalizePayload, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await _run(flow.finalize(user_id, document_id, payload.route, payload.note))


@router.post("/decision")
async def decide_script(document_id: str, payload: ScriptDecisionPayload, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await _run(flow.decide(user_id, document_id, payload.decision, payload.note))


@router.post("/create-post")
async def create_post_from_script(document_id: str, payload: ScriptCreatePostPayload, user_id: UUID = Depends(get_current_user_id_dependency)):
    return await _run(flow.create_post(user_id, document_id, payload.note))
