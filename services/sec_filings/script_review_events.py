"""Events of the Social Media Script review, for the portal's event bus (MQTT and RabbitMQ).

The Social Media portal's backend listens to these to stay in step: it never copies the script, it learns what
happened and fetches the state it needs. Each event is published on both buses, best effort (a bus being down never
fails the request):

    MQTT         share-document/script/<event>      RabbitMQ routing key   script.<event>

    template.created   a script document was made from the Social Media Script template
    reviewer.invited   people were invited as reviewers / suggesters / managers / viewers
    review.submitted   the creator sent the script for review (a new round)
    review.checked_in a reviewer checked in
    review.finalized   everyone checked in and the script was routed (LEVEL2_REVIEW / CHANGES_REQUESTED / APPROVED)
    review.decided     a manager approved or sent it back
    content.saved      the script itself was saved (at most every few seconds per script)
    change.proposed    a change proposal on the script was saved
    merge.completed    accepted changes were merged into the script (a new version)
    post.requested     the creator or a manager asked for a post to be created from the approved script
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

EVENTS = (
    "template.created", "reviewer.invited", "review.submitted", "review.checked_in", "review.finalized",
    "review.decided", "content.saved", "change.proposed", "merge.completed", "post.requested",
)


def publish(event: str, document_id: str, actor_id: Optional[Any] = None, **data: Any) -> Dict[str, Any]:
    """Publish one event; returns the payload."""
    logger.info("[ScriptEvent] %s on %s", event, document_id)
    payload = {
        "eventType": f"share-document.script.{event}", "event": event, "documentId": document_id,
        "actorId": str(actor_id) if actor_id else None, "occurredAt": datetime.now(timezone.utc).isoformat(), **data,
    }
    try:
        from services.notifications.mqtt_presence import publish_share_document_event
        publish_share_document_event(f"share-document/script/{event}", payload)
    except Exception as e:  # a bus that is down must not fail the review
        logger.info("MQTT publish of script.%s skipped: %s", event, e)
    try:
        from services.notifications.rabbitmq_service import rabbitmq_manager
        asyncio.get_running_loop().create_task(rabbitmq_manager.publish_share_document_event(f"script.{event}", payload))
    except Exception as e:
        logger.info("RabbitMQ publish of script.%s skipped: %s", event, e)
    try:  # open editors in this portal refresh at once
        from services.notifications.notification_service import broadcast_workflow_sync
        broadcast_workflow_sync("SecFiling", document_id)
    except Exception:
        pass
    return payload
