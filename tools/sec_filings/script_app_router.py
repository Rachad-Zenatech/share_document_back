"""App-to-app routes for the Social Media portal's backend: create a script's shared document, read it back, invite and
send it, move it through the review on a person's behalf, and get the sign-in token for the iframe. Callers are
authenticated services (script_service_auth), never browsers; every call names the person it acts for by email."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from models.sec_filings.script_review_model import AppAct, AppCreateScript, AppDelete, AppSend, AppImportSubmission
from services.sec_filings import script_app_service as app
from services.sec_filings.script_review_service import ScriptReviewError
from services.sec_filings.script_service_auth import require_script_service

read_scope = Depends(require_script_service("script_documents:read"))
write_scope = Depends(require_script_service("script_documents:write"))

router = APIRouter(prefix="/service/script-documents")


async def _run(coro):
    try:
        return await coro
    except (app.AppServiceError, ScriptReviewError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("")
async def create_script(payload: AppCreateScript, _: Dict[str, Any] = write_scope):
    details = {str(k): str(v) for k, v in (payload.details or {}).items()}
    return await _run(app.create_script(payload.user_email, payload.title, details, [b.model_dump() for b in payload.rows],
                                        payload.external_ref, payload.number, payload.headers, payload.table_rows))


@router.post("/from-submission")
async def import_submission(payload: AppImportSubmission, _: Dict[str, Any] = write_scope):
    from services.sec_filings.script_submission_import import import_submission as adopt
    return await _run(adopt(payload))


@router.post("/delete")
async def delete_scripts(payload: AppDelete, _: Dict[str, Any] = write_scope):
    """Delete scripts (one, or all the pages of an attached file) as their creator. Returns what was deleted and what was skipped."""
    return await _run(app.delete_scripts(payload.actor_email, payload.document_ids))


@router.get("/{document_id}")
async def read_script(document_id: str, email: str, _: Dict[str, Any] = read_scope):
    """The script's details, beats and text, as `email` sees it."""
    return await _run(app.get_script(document_id, email))


@router.get("/{document_id}/review")
async def read_review(document_id: str, email: str, _: Dict[str, Any] = read_scope):
    return await _run(app.get_review(document_id, email))


@router.post("/{document_id}/send")
async def send(document_id: str, payload: AppSend, _: Dict[str, Any] = write_scope):
    return await _run(app.send_script(document_id, payload.actor_email, payload.invitees, payload.page_ids, payload.note))


@router.post("/{document_id}/actions")
async def act(document_id: str, payload: AppAct, _: Dict[str, Any] = write_scope):
    return await _run(app.act(document_id, payload.actor_email, payload.action, payload.vote, payload.route, payload.decision,
                              payload.note))


@router.post("/{document_id}/embed-token")
async def embed_token(document_id: str, email: str, _: Dict[str, Any] = write_scope):
    return await _run(app.embed_token(document_id, email))
