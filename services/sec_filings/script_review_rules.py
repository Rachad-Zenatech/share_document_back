"""Social Media Script review: who may do what, as pure functions (no database, no events).

The same rules as the Social Media portal's script documents (its `DocAccess`), so a script reviewed here behaves like one
reviewed there:

    DRAFT / CHANGES_REQUESTED  the creator edits, invites reviewers and sends it
    IN_REVIEW                  collaborators with EDIT capacity edit / propose; reviewers and suggesting reviewers check in
                               (REVIEWER: APPROVE or CHANGES, SUGGESTER: DONE). When everyone has, the finalizer routes it to
                               LEVEL2_REVIEW (a manager decides) or approves it; any CHANGES vote sends it back instead.
    LEVEL2_REVIEW              a manager reviews / merges and decides: APPROVE or SEND_BACK
    APPROVED                   the creator or a manager may create the post

Roles: CREATOR, MANAGER, REVIEWER (content reviewer), SUGGESTER (suggesting reviewer), VIEWER. A collaborator with VIEW
capacity, and a VIEWER, can read and add notes only. Nobody decides their own script, except a super admin.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

STATUSES = ("DRAFT", "IN_REVIEW", "LEVEL2_REVIEW", "CHANGES_REQUESTED", "APPROVED")
EDITING = ("DRAFT", "CHANGES_REQUESTED")
IN_REVIEW_STATES = ("IN_REVIEW", "LEVEL2_REVIEW")
COLLABORATOR_ROLES = ("MANAGER", "REVIEWER", "SUGGESTER", "VIEWER")
CAPACITIES = ("EDIT", "VIEW")
VOTES = ("APPROVE", "CHANGES", "DONE")
TEMPLATE_ID = "social-media-script"
FORM_TYPE = "Social Media Script"


@dataclass
class ScriptAccess:
    role: str
    can_edit: bool
    can_note: bool
    can_submit: bool
    can_check_in: bool
    can_finalize: bool
    can_decide: bool
    can_create_post: bool
    can_delete: bool

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def is_script_document(template_type: Optional[str], form_type: Optional[str]) -> bool:
    return template_type == TEMPLATE_ID or form_type == FORM_TYPE


def is_required(collab: Dict[str, Any]) -> bool:
    """Someone whose check-in the review waits for: a reviewer or suggesting reviewer who can edit."""
    return collab["capacity"] == "EDIT" and collab["role"] in ("REVIEWER", "SUGGESTER")


def all_checked(status: str, collaborators: List[Dict[str, Any]]) -> bool:
    required = [c for c in collaborators if is_required(c)]
    decided = any(c.get("vote") in ("APPROVE", "CHANGES") for c in required)
    return status == "IN_REVIEW" and bool(required) and (decided or all(c.get("vote") for c in required))


def compute_access(status: str, *, is_creator: bool, is_super: bool, mine: Optional[Dict[str, Any]]) -> Optional[ScriptAccess]:
    """What this person may do with the script, or None when they may not see it."""
    manager = is_super or bool(mine and mine["role"] == "MANAGER")
    if not (is_creator or manager or mine):
        return None
    editor = bool(mine and is_required(mine))
    can_review = bool(mine and mine["role"] == "REVIEWER") or is_super
    own = is_creator and not is_super
    approver = editor and can_review and not own and status == "IN_REVIEW"
    deciding = manager and not own and status in IN_REVIEW_STATES
    can_edit = (
        is_super
        or (is_creator and status in EDITING)
        or (editor and status == "IN_REVIEW")
        or (manager and not own and status in IN_REVIEW_STATES)
    )
    in_play = status in IN_REVIEW_STATES
    can_note = is_super or can_edit or bool(mine and in_play) or (is_creator and status != "DRAFT") or (manager and status != "DRAFT")
    if is_creator:
        role = "CREATOR"
    elif manager:
        role = "MANAGER"
    elif editor and mine["role"] == "REVIEWER":
        role = "REVIEWER"
    elif editor:
        role = "SUGGESTER"
    else:
        role = "VIEWER"
    return ScriptAccess(
        role=role, can_edit=can_edit, can_note=can_note,
        can_submit=(is_creator or is_super) and status in EDITING,
        can_check_in=(editor or is_super) and status == "IN_REVIEW" and not own,
        can_finalize=approver or (manager and not own and status == "IN_REVIEW"),
        can_decide=deciding,
        can_create_post=status == "APPROVED" and (is_creator or manager),
        can_delete=is_super or ((is_creator or manager) and status == "DRAFT"),
    )


def check_vote(role: str, vote: str, note: Optional[str]) -> Optional[str]:
    """Why this check-in isn't allowed, or None when it is."""
    if vote not in VOTES:
        return "Choose approve, request changes or done"
    if role == "REVIEWER" or role == "MANAGER":
        if vote == "DONE":
            return "Choose approve or request changes"
        if vote == "CHANGES" and not (note or "").strip():
            return "Say what needs to change"
    elif vote != "DONE":
        return "A suggesting reviewer can only mark their suggestions as done"
    return None


def finalize_target(collaborators: List[Dict[str, Any]], route: Optional[str]) -> str:
    """Where a fully checked-in script goes: CHANGES_REQUESTED if anyone asked for changes, LEVEL2_REVIEW when asked or when
    nobody who can approve took part, else APPROVED."""
    votes = [c.get("vote") for c in collaborators if is_required(c)]
    if "CHANGES" in votes:
        return "CHANGES_REQUESTED"
    if route == "LEVEL2" or "APPROVE" not in votes:
        return "LEVEL2_REVIEW"
    return "APPROVED"
