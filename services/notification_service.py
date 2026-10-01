from typing import List
import json
import asyncio
from postgresql_db.database import execute, fetch_all, fetch_one
from models.notification_model import NotificationCreate, NotificationResponse

class NotificationBroadcaster:
    def __init__(self):
        self.listeners = []

    def add_listener(self, queue: asyncio.Queue):
        self.listeners.append(queue)

    def remove_listener(self, queue: asyncio.Queue):
        if queue in self.listeners:
            self.listeners.remove(queue)

    def broadcast(self, notification: NotificationResponse):
        for queue in self.listeners:
            queue.put_nowait(notification)

broadcaster = NotificationBroadcaster()



async def create_notification(data: NotificationCreate) -> NotificationResponse:
    sql = """
        INSERT INTO notifications (user_id, type, title, message, link_url, entity_type, entity_id, sender_name, sender_avatar, attachments, is_read, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, false, now())
        RETURNING *
    """
    row = await execute(
        sql,
        data.user_id,
        data.type,
        data.title,
        data.message,
        data.link_url,
        data.entity_type,
        data.entity_id,
        data.sender_name,
        data.sender_avatar,
        json.dumps(data.attachments) if data.attachments is not None else None
    )
    if not row:
        raise Exception("Failed to create notification")

    # row from asyncpg Record, decode json for attachments
    row_dict = dict(row)
    if row_dict.get("attachments") and isinstance(row_dict["attachments"], str):
        row_dict["attachments"] = json.loads(row_dict["attachments"])

    res = NotificationResponse(**row_dict)
    broadcaster.broadcast(res)
    return res


def broadcast_workflow_sync(entity_type: str = "PurchaseRequest", entity_id: str = ""):
    """Broadcast an instant workflow update to all active SSE client streams."""
    from datetime import datetime
    sync_event = NotificationResponse(
        id=0,
        user_id="*",
        type="WORKFLOW_SYNC",
        title="Workflow Update",
        message="A request workflow status has changed.",
        link_url=f"/purchasing/requests/{entity_id}" if entity_id else "/purchasing/requests",
        entity_type=entity_type,
        entity_id=str(entity_id),
        sender_name="System",
        sender_avatar=None,
        attachments=None,
        is_read=True,
        read_at=None,
        created_at=datetime.utcnow()
    )
    broadcaster.broadcast(sync_event)


async def get_recent_notifications(user_id: str, limit: int = 20) -> List[NotificationResponse]:
    sql = """
        SELECT * FROM notifications
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
    """
    rows = await fetch_all(sql, user_id, limit)
    res = []
    for r in rows:
        d = dict(r)
        if d.get("attachments") and isinstance(d["attachments"], str):
            d["attachments"] = json.loads(d["attachments"])
        res.append(NotificationResponse(**d))
    return res


async def get_unread_count(user_id: str) -> int:
    sql = "SELECT COUNT(*) as count FROM notifications WHERE user_id = $1 AND is_read = false"
    row = await fetch_one(sql, user_id)
    return row["count"] if row else 0


async def mark_notification_as_read(notification_id: int, user_id: str) -> bool:
    sql = """
        UPDATE notifications
        SET is_read = true, read_at = now()
        WHERE id = $1 AND user_id = $2 AND is_read = false
        RETURNING id
    """
    row = await execute(sql, notification_id, user_id)
    return bool(row)


async def mark_all_notifications_as_read(user_id: str) -> bool:
    sql = """
        UPDATE notifications
        SET is_read = true, read_at = now()
        WHERE user_id = $1 AND is_read = false
        RETURNING id
    """
    rows = await fetch_all(sql, user_id)
    return len(rows) > 0


async def clear_read_notifications(user_id: str) -> bool:
    sql = "DELETE FROM notifications WHERE user_id = $1 AND is_read = true RETURNING id"
    rows = await fetch_all(sql, user_id)
    return len(rows) > 0




async def clear_all_notifications(user_id: str) -> bool:
    sql = "DELETE FROM notifications WHERE user_id = $1 RETURNING id"
    rows = await fetch_all(sql, user_id)
    return len(rows) > 0

