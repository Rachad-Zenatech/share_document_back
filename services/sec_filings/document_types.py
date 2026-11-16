"""The kinds (categories) of document, and what each one does when it changes.

    sec-filing  filings, reports and checklists: edited by the people invited to them, proposals merged by the owner. Open
                editors refresh when someone else saves; nothing is published for other applications.
    script      Social Media scripts: a review with roles and check-ins (script_review_rules), events for the Social Media
                portal on the event bus (script_review_events), and open editors refresh when anything about it changes.

A kind is told from the document's template type or form type. Add a kind here and give it its own controls; code that
differs by kind asks `config_of` instead of comparing names.
"""

from dataclasses import dataclass
from typing import Optional

SEC_FILING = "sec-filing"
SCRIPT = "script"


@dataclass(frozen=True)
class DocumentTypeConfig:
    key: str
    # Open editors are told at once (the portal's notification socket) when someone else changes the document.
    live_sync: bool
    # The document has a review (roles, invites, check-ins, routing, decisions) that decides who may change it.
    review: bool
    # Changes are published on the event bus for other applications.
    bus_events: bool


CONFIGS = {
    SEC_FILING: DocumentTypeConfig(SEC_FILING, live_sync=True, review=False, bus_events=False),
    SCRIPT: DocumentTypeConfig(SCRIPT, live_sync=True, review=True, bus_events=True),
}


def type_of(template_type: Optional[str], form_type: Optional[str]) -> str:
    from services.sec_filings.script_review_rules import is_script_document

    return SCRIPT if is_script_document(template_type, form_type) else SEC_FILING


def config_of(template_type: Optional[str], form_type: Optional[str]) -> DocumentTypeConfig:
    return CONFIGS[type_of(template_type, form_type)]
