"""Pydantic models for the Social Media Script review workflow (see services/sec_filings/script_review_rules.py)."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Capacity = Literal["EDIT", "VIEW"]
CollaboratorRole = Literal["MANAGER", "REVIEWER", "SUGGESTER", "VIEWER"]
ScriptStatus = Literal["DRAFT", "IN_REVIEW", "LEVEL2_REVIEW", "CHANGES_REQUESTED", "APPROVED"]


class ScriptInvitee(BaseModel):
    user_id: str
    role: CollaboratorRole
    capacity: Capacity = "EDIT"


class ScriptSendPayload(BaseModel):
    """Invite reviewers (replacing the list) and send the script for review. `page_ids` is the script pages
    (one or several) being sent; empty means the whole document."""
    invitees: List[ScriptInvitee] = Field(default_factory=list)
    page_ids: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class ScriptInvitePayload(BaseModel):
    invitees: List[ScriptInvitee]


class ScriptCheckInPayload(BaseModel):
    vote: Literal["APPROVE", "CHANGES", "DONE"]
    note: Optional[str] = None


class ScriptFinalizePayload(BaseModel):
    route: Optional[Literal["LEVEL2", "CREATOR"]] = None
    note: Optional[str] = None


class ScriptDecisionPayload(BaseModel):
    decision: Literal["APPROVE", "SEND_BACK"]
    note: Optional[str] = None


class ScriptCreatePostPayload(BaseModel):
    note: Optional[str] = None


# --- the Social Media portal's backend (app-to-app), see tools/sec_filings/script_app_router.py. People are named by email.


class AppBeat(BaseModel):
    time: str = ""
    visual: str = ""
    voiceover: str = ""
    onscreen: str = ""


class AppCreateScript(BaseModel):
    """Make a script document for `user_email`. `details` keys: Storyline, Description, Duration, Post / batch, Hook score, Lane.
    `external_ref` is the portal's own id for the script, kept on the document (e.g. {"system": "social-media", "id": "..."})."""
    user_email: str
    title: str = "Untitled script"
    details: dict = Field(default_factory=dict)
    rows: List[AppBeat] = Field(default_factory=list)
    external_ref: Optional[dict] = None
    # The script's number in its document, shown in its title ("2. The movie star's patent").
    number: int = Field(default=1, ge=1, le=999)
    # Optional custom table columns/rows when the Social Media portal has a custom uploaded default template.
    headers: Optional[List[str]] = None
    table_rows: Optional[List[List[str]]] = None


class AppInvitee(BaseModel):
    email: str
    role: CollaboratorRole
    capacity: Capacity = "EDIT"
    # Left out, instead of failing the send, when this person isn't a user of the portal yet.
    optional: bool = False


class AppDelete(BaseModel):
    """Delete script documents on behalf of their creator (one call for one script or for a whole attached file)."""
    actor_email: str
    document_ids: List[str] = Field(default_factory=list, max_length=200)


class AppSend(BaseModel):
    actor_email: str
    invitees: List[AppInvitee] = Field(default_factory=list)
    page_ids: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class AppAct(BaseModel):
    actor_email: str
    action: Literal["check_in", "withdraw", "finalize", "decide", "create_post"]
    vote: Optional[Literal["APPROVE", "CHANGES", "DONE"]] = None
    route: Optional[Literal["LEVEL2", "CREATOR"]] = None
    decision: Optional[Literal["APPROVE", "SEND_BACK"]] = None
    note: Optional[str] = None


class AppImportSubmission(AppCreateScript):
    """Idempotently adopt an existing submitted post into the shared script workflow."""
    document_id: str = Field(pattern=r"^doc-social-submission-[0-9a-f]{32}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["IN_REVIEW", "CHANGES_REQUESTED", "APPROVED"] = "IN_REVIEW"
    round: int = Field(default=1, ge=1)
    invitees: List[AppInvitee] = Field(default_factory=list)
