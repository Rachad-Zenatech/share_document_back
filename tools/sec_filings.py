"""SEC filing routes: financial statement table templates."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from models.sec_filing_model import (
    AttachedSpreadsheetPayload,
    FinancialTableTemplate,
    FinancialTableTemplateCreate,
    FinancialTableTemplateList,
    FinancialTableTemplateSeedResult,
    FinancialTableTemplateUpdate,
    MobileSignatureCompleteRequest,
    MobileSignatureDispatchRequest,
    MobileSignatureEnvelopeResponse,
    UpdateSpreadsheetCellPayload,
)
from services.auth_service import require_permission
from services.sec_filing_signature_service import (
    cleanup_expired_envelopes,
    complete_envelope,
    delete_envelope,
    dispatch_envelope,
    get_envelope,
)
from services.sec_filing_spreadsheet_service import (
    get_document_spreadsheet,
    save_document_spreadsheet,
    update_spreadsheet_cell,
)
from services.sec_filing_template_service import (
    create_table_template,
    delete_table_template,
    get_table_template,
    list_table_templates,
    seed_builtin_table_templates,
    update_table_template,
)

router = APIRouter(prefix="/sec-filings")



@router.get(
    "/table-templates",
    response_model=FinancialTableTemplateList,
    response_model_exclude_none=True,
)
async def list_financial_table_templates(
    user_id: UUID = Depends(require_permission("SEC_FILINGS_READ")),
):
    return await list_table_templates()


@router.post(
    "/table-templates",
    response_model=FinancialTableTemplate,
    status_code=201,
    response_model_exclude_none=True,
)
async def create_financial_table_template(
    payload: FinancialTableTemplateCreate,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_CREATE")),
):
    try:
        data = payload.model_dump(by_alias=False, exclude_none=False)
        # The block is stored with the frontend's camelCase keys; the surrounding
        # columns are read by field name.
        data["block"] = payload.block.model_dump(by_alias=True, exclude_none=True)
        return await create_table_template(data, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/table-templates/seed", response_model=FinancialTableTemplateSeedResult)
async def seed_financial_table_templates(
    overwrite: bool = Query(
        default=False,
        description="Overwrite built-in templates that already exist, discarding local edits to them.",
    ),
    user_id: UUID = Depends(require_permission("SEC_FILINGS_CREATE")),
):
    return await seed_builtin_table_templates(overwrite=overwrite)


@router.get(
    "/table-templates/{template_id}",
    response_model=FinancialTableTemplate,
    response_model_exclude_none=True,
)
async def get_financial_table_template(
    template_id: str,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_READ")),
):
    template = await get_table_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.put(
    "/table-templates/{template_id}",
    response_model=FinancialTableTemplate,
    response_model_exclude_none=True,
)
async def update_financial_table_template(
    template_id: str,
    payload: FinancialTableTemplateUpdate,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_UPDATE")),
):
    try:
        data = payload.model_dump(by_alias=False, exclude_unset=True)
        if payload.block is not None:
            data["block"] = payload.block.model_dump(by_alias=True, exclude_none=True)
        return await update_table_template(template_id, data, user_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if "not found" in detail else 422, detail=detail) from exc


@router.delete("/table-templates/{template_id}")
async def delete_financial_table_template(
    template_id: str,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_DELETE")),
):
    try:
        return await delete_table_template(template_id, user_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(status_code=404 if "not found" in detail else 409, detail=detail) from exc


# -----------------------------------------------------------------------------
# Mobile E-Signature Envelopes (DocuSign / Dropbox Sign Cross-Device Sync)
# -----------------------------------------------------------------------------


@router.post(
    "/mobile-signatures/dispatch",
    response_model=MobileSignatureEnvelopeResponse,
    response_model_exclude_none=True,
)
async def dispatch_mobile_signature_envelope(
    payload: MobileSignatureDispatchRequest,
):
    """Registers an electronic signature envelope for mobile phone SMS/QR dispatch."""
    await cleanup_expired_envelopes()
    data = payload.model_dump(by_alias=False, exclude_none=False)
    return await dispatch_envelope(data)


@router.get(
    "/mobile-signatures/{envelope_id}",
    response_model=MobileSignatureEnvelopeResponse,
    response_model_exclude_none=True,
)
async def get_mobile_signature_status(
    envelope_id: str,
):
    """Polls or retrieves the signing status of a mobile signature envelope."""
    env = await get_envelope(envelope_id)
    if not env:
        raise HTTPException(status_code=404, detail="Signature envelope not found")
    return env


@router.post(
    "/mobile-signatures/{envelope_id}/complete",
    response_model=MobileSignatureEnvelopeResponse,
    response_model_exclude_none=True,
)
async def complete_mobile_signature_envelope(
    envelope_id: str,
    payload: MobileSignatureCompleteRequest,
):
    """Finalizes and records a signature completed from a phone / mobile canvas."""
    data = payload.model_dump(by_alias=False, exclude_none=False)
    env = await complete_envelope(envelope_id, data)
    if not env:
        raise HTTPException(status_code=404, detail="Failed to complete signature envelope")
    return env


@router.delete(
    "/mobile-signatures/{envelope_id}",
    response_model=dict,
)
async def delete_mobile_signature_envelope(
    envelope_id: str,
):
    """Deletes and removes signature envelope immediately so no signature data is retained."""
    deleted = await delete_envelope(envelope_id)
    return {"success": True, "deleted": deleted}


# -----------------------------------------------------------------------------
# Dynamic Attached Spreadsheets & Variable Sync
# -----------------------------------------------------------------------------

@router.get("/documents/{document_id}/spreadsheet")
async def get_attached_spreadsheet_endpoint(document_id: str):
    sheet = await get_document_spreadsheet(document_id)
    if not sheet:
        return {"documentId": document_id, "name": "Spreadsheet", "sheetName": "Sheet1", "totalRows": 20, "totalColumns": 10, "columns": [], "cells": {}}
    return sheet


@router.put("/documents/{document_id}/spreadsheet")
async def save_attached_spreadsheet_endpoint(
    document_id: str,
    payload: AttachedSpreadsheetPayload,
):
    data = payload.model_dump(by_alias=False, exclude_none=True)
    saved = await save_document_spreadsheet(document_id, data)
    return saved


@router.patch("/documents/{document_id}/spreadsheet/cells")
async def update_spreadsheet_cell_endpoint(
    document_id: str,
    payload: UpdateSpreadsheetCellPayload,
):
    updated = await update_spreadsheet_cell(document_id, payload.cell_ref, payload.value)
    return updated
