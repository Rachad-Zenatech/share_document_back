"""Pydantic models for SEC filing financial statement table templates.

Shapes mirror the frontend types in `src/types/secFiling.ts`; keep the two in
step when either side changes.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SecTableRowType = Literal[
    "header",
    "category_header",
    "section_title",
    "data",
    "subtotal",
    "total",
    "blank",
]

SecCellAlignment = Literal["left", "center", "right"]


class SecTableRowPayload(BaseModel):
    """One row of a financial statement table.

    Stored in JSONB using the camelCase aliases the frontend uses, so the block
    the API returns can be dropped straight into a `financial_table` block.
    """

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    id: str
    type: SecTableRowType
    cells: list[str]
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    double_underline: bool | None = Field(default=None, alias="doubleUnderline")
    shading: str | None = None
    indent: int | None = Field(default=None, ge=0, le=3)
    align: SecCellAlignment | None = None
    cell_alignments: list[SecCellAlignment | None] | None = Field(
        default=None, alias="cellAlignments"
    )


class SecTablePeriodHeaderPayload(BaseModel):
    """Centered multi-line comparative reporting-period label for one column."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    column_index: int = Field(alias="columnIndex", ge=0)
    lines: list[str] = Field(min_length=1)


class SecFinancialTableBlockPayload(BaseModel):
    """The structure copied into a `financial_table` block when applied."""

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    title: str | None = None
    headers: list[str]
    header_shading: str | None = Field(default=None, alias="headerShading")
    period_headers: list[SecTablePeriodHeaderPayload] | None = Field(
        default=None, alias="periodHeaders"
    )
    column_alignments: list[SecCellAlignment] = Field(alias="columnAlignments")
    column_widths: list[str] | None = Field(default=None, alias="columnWidths")
    rows: list[SecTableRowPayload]
    footnotes: list[str] | None = None

    @field_validator("headers")
    @classmethod
    def _at_least_two_columns(cls, value: list[str]) -> list[str]:
        # A single-column financial table is always a parsing accident, and the
        # editor's compaction step refuses to produce one.
        if len(value) < 2:
            raise ValueError("a financial table needs at least two columns")
        return value

    @field_validator("rows")
    @classmethod
    def _rows_match_column_count(
        cls, rows: list[SecTableRowPayload], info
    ) -> list[SecTableRowPayload]:
        headers = info.data.get("headers")
        if not headers:
            return rows
        expected = len(headers)
        for index, row in enumerate(rows):
            if len(row.cells) != expected:
                raise ValueError(
                    f"row {index + 1} has {len(row.cells)} cells but the table has {expected} columns"
                )
        return rows

    @field_validator("period_headers")
    @classmethod
    def _period_headers_match_columns(
        cls, period_headers: list[SecTablePeriodHeaderPayload] | None, info
    ) -> list[SecTablePeriodHeaderPayload] | None:
        if period_headers is None:
            return None
        headers = info.data.get("headers")
        if headers:
            invalid = [header.column_index + 1 for header in period_headers if header.column_index >= len(headers)]
            if invalid:
                raise ValueError(
                    f"period header column(s) {', '.join(map(str, invalid))} exceed the table column count"
                )
        if len({header.column_index for header in period_headers}) != len(period_headers):
            raise ValueError("only one period header is allowed per table column")
        return period_headers


class FinancialTableTemplateBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    badge: str | None = Field(default=None, max_length=40)
    icon: str = Field(default="table", max_length=60)
    color: str | None = None
    sort_order: int = Field(default=0, alias="sortOrder")
    block: SecFinancialTableBlockPayload


class FinancialTableTemplateCreate(FinancialTableTemplateBase):
    """A template saved from a table the user has already built."""

    template_key: str | None = Field(
        default=None,
        alias="templateKey",
        max_length=120,
        description="Stable key. Derived from the name when omitted.",
    )


class FinancialTableTemplateUpdate(BaseModel):
    """Partial update; only the supplied fields are written."""

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    badge: str | None = Field(default=None, max_length=40)
    icon: str | None = Field(default=None, max_length=60)
    color: str | None = None
    sort_order: int | None = Field(default=None, alias="sortOrder")
    block: SecFinancialTableBlockPayload | None = None


class FinancialTableTemplate(BaseModel):
    """A template as returned to the frontend."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: UUID
    template_key: str = Field(serialization_alias="templateKey")
    name: str
    description: str | None = None
    badge: str | None = None
    icon: str
    color: str | None = None
    sort_order: int = Field(serialization_alias="sortOrder")
    is_builtin: bool = Field(serialization_alias="isBuiltin")
    block: SecFinancialTableBlockPayload
    created_at: datetime = Field(serialization_alias="createdAt")
    updated_at: datetime = Field(serialization_alias="updatedAt")
    updated_by: UUID | None = Field(default=None, serialization_alias="updatedBy")


class FinancialTableTemplateList(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    templates: list[FinancialTableTemplate]
    total: int


class FinancialTableTemplateSeedResult(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    inserted: int
    updated: int
    skipped: int


class MobileSignatureDispatchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    envelope_id: str = Field(alias="envelopeId")
    document_title: str = Field(alias="documentTitle")
    document_id: str | None = Field(default=None, alias="documentId")
    block_id: str | None = Field(default=None, alias="blockId")
    officer_id: str | None = Field(default=None, alias="officerId")
    signer_name: str = Field(alias="signerName")
    signer_title: str | None = Field(default=None, alias="signerTitle")
    phone_number: str | None = Field(default=None, alias="phoneNumber")
    provider: str = Field(default="docusign")
    delivery_method: str = Field(default="SMS", alias="deliveryMethod")


class MobileSignatureCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    signer_name: str | None = Field(default=None, alias="signerName")
    signature_text: str = Field(alias="signatureText")
    signature_image_url: str | None = Field(default=None, alias="signatureImageUrl")
    provider: str = Field(default="docusign")
    signed_via: str | None = Field(default=None, alias="signedVia")
    ip_address: str | None = Field(default=None, alias="ipAddress")


class MobileSignatureEnvelopeResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    envelope_id: str = Field(serialization_alias="envelopeId")
    document_title: str = Field(serialization_alias="documentTitle")
    document_id: str | None = Field(default=None, serialization_alias="documentId")
    block_id: str | None = Field(default=None, serialization_alias="blockId")
    officer_id: str | None = Field(default=None, serialization_alias="officerId")
    signer_name: str = Field(serialization_alias="signerName")
    signer_title: str | None = Field(default=None, serialization_alias="signerTitle")
    phone_number: str | None = Field(default=None, serialization_alias="phoneNumber")
    provider: str
    delivery_method: str = Field(serialization_alias="deliveryMethod")
    status: str
    signature_text: str | None = Field(default=None, serialization_alias="signatureText")
    signature_image_url: str | None = Field(default=None, serialization_alias="signatureImageUrl")
    signed_via: str | None = Field(default=None, serialization_alias="signedVia")
    signed_at: datetime | None = Field(default=None, serialization_alias="signedAt")
    audit_trail_id: str | None = Field(default=None, serialization_alias="auditTrailId")


class AttachedSpreadsheetColumnPayload(BaseModel):
    key: str
    title: str
    width: int | None = None
    type: str | None = None


class AttachedSpreadsheetPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str | None = None
    document_id: str | None = Field(default=None, alias="documentId")
    name: str = "Spreadsheet"
    sheet_name: str = Field(default="Sheet1", alias="sheetName")
    total_rows: int = Field(default=20, alias="totalRows")
    total_columns: int = Field(default=10, alias="totalColumns")
    columns: list[AttachedSpreadsheetColumnPayload] = []
    cells: dict[str, Any] = {}
    tabs: list[dict[str, Any]] = []
    active_tab_id: str | None = Field(default=None, alias="activeTabId")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class UpdateSpreadsheetCellPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cell_ref: str = Field(alias="cellRef")
    value: Any
