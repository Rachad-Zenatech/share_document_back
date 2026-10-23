"""SEC filing routes: financial statement table templates."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from models.sec_filing_model import (
    AttachedSpreadsheetPayload,
    FinancialTableTemplate,
    FinancialTableTemplateCreate,
    FinancialTableTemplateList,
    FinancialTableTemplateUpdate,
    MobileSignatureCompleteRequest,
    MobileSignatureDispatchRequest,
    MobileSignatureEnvelopeResponse,
    SecDocumentWorkflowAssignPayload,
    SecWorkflow,
    SecWorkflowCreate,
    SecWorkflowDocumentsPayload,
    SecWorkflowList,
    SecWorkflowUpdate,
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
    delete_document_spreadsheet,
    get_document_spreadsheet,
    save_document_spreadsheet,
    update_spreadsheet_cell,
)
from services.sec_filing_template_service import (
    create_table_template,
    delete_table_template,
    get_table_template,
    list_table_templates,
    update_table_template,
)
from services.sec_filing_workflow_service import (
    assign_document_workflow,
    create_workflow,
    delete_workflow,
    get_document_workflow,
    get_workflow,
    list_workflows,
    set_workflow_documents,
    unassign_document_workflow,
    update_workflow,
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
# Reporting Workflows & Document Workflow Assignments
# -----------------------------------------------------------------------------


@router.get("/workflows", response_model=SecWorkflowList, response_model_exclude_none=True)
async def list_sec_workflows(
    user_id: UUID = Depends(require_permission("SEC_FILINGS_READ")),
):
    return await list_workflows()


@router.post("/workflows", response_model=SecWorkflow, status_code=201, response_model_exclude_none=True)
async def create_sec_workflow(
    payload: SecWorkflowCreate,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_CREATE")),
):
    data = payload.model_dump(by_alias=False)
    data["steps"] = [step.model_dump(by_alias=True) for step in payload.steps]
    return await create_workflow(data, user_id)


@router.get("/workflows/{workflow_id}", response_model=SecWorkflow, response_model_exclude_none=True)
async def get_sec_workflow(
    workflow_id: UUID,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_READ")),
):
    workflow = await get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.put("/workflows/{workflow_id}", response_model=SecWorkflow, response_model_exclude_none=True)
async def update_sec_workflow(
    workflow_id: UUID,
    payload: SecWorkflowUpdate,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_UPDATE")),
):
    data = payload.model_dump(by_alias=False, exclude_unset=True)
    if payload.steps is not None:
        data["steps"] = [step.model_dump(by_alias=True) for step in payload.steps]
    try:
        return await update_workflow(workflow_id, data, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/workflows/{workflow_id}")
async def delete_sec_workflow(
    workflow_id: UUID,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_DELETE")),
):
    try:
        return await delete_workflow(workflow_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/workflows/{workflow_id}/documents", response_model=SecWorkflow, response_model_exclude_none=True)
async def set_sec_workflow_documents(
    workflow_id: UUID,
    payload: SecWorkflowDocumentsPayload,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_UPDATE")),
):
    try:
        return await set_workflow_documents(workflow_id, payload.document_ids, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/documents/{document_id}/workflow")
async def get_sec_document_workflow(
    document_id: str,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_READ")),
):
    # `workflow` is null when none is assigned, so callers can tell that apart
    # from a lookup failure.
    workflow = await get_document_workflow(document_id)
    return {
        "documentId": document_id,
        "workflow": SecWorkflow.model_validate(workflow).model_dump(mode="json", by_alias=True) if workflow else None,
    }


@router.put("/documents/{document_id}/workflow")
async def assign_sec_document_workflow(
    document_id: str,
    payload: SecDocumentWorkflowAssignPayload,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_UPDATE")),
):
    try:
        workflow = await assign_document_workflow(document_id, payload.workflow_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "documentId": document_id,
        "workflow": SecWorkflow.model_validate(workflow).model_dump(mode="json", by_alias=True),
    }


@router.delete("/documents/{document_id}/workflow")
async def unassign_sec_document_workflow(
    document_id: str,
    user_id: UUID = Depends(require_permission("SEC_FILINGS_UPDATE")),
):
    deleted = await unassign_document_workflow(document_id)
    return {"documentId": document_id, "deleted": deleted}


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
    # `spreadsheet` is null when no workbook is linked, so callers can tell
    # "not linked" apart from an empty sheet.
    sheet = await get_document_spreadsheet(document_id)
    return {"documentId": document_id, "spreadsheet": sheet}


@router.delete("/documents/{document_id}/spreadsheet")
async def delete_attached_spreadsheet_endpoint(document_id: str):
    deleted = await delete_document_spreadsheet(document_id)
    return {"documentId": document_id, "deleted": deleted}


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
