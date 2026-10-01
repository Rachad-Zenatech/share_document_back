from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
import asyncio
from typing import List, Dict
from models.notification_model import NotificationResponse
from services.notification_service import get_recent_notifications, get_unread_count, mark_notification_as_read, mark_all_notifications_as_read, clear_read_notifications, clear_all_notifications, broadcaster

from services.auth_service import get_current_user_id_dependency
from uuid import UUID

notification_router = APIRouter()

def get_current_user_id(user_id: UUID = Depends(get_current_user_id_dependency)) -> str:
    return str(user_id)


@notification_router.get("/notifications", response_model=List[NotificationResponse])
async def api_get_notifications(user_id: str = Depends(get_current_user_id)):
    return await get_recent_notifications(user_id)

@notification_router.get("/notifications/unread-count")
async def api_get_unread_count(user_id: str = Depends(get_current_user_id)) -> Dict[str, int]:
    count = await get_unread_count(user_id)
    return {"count": count}

@notification_router.patch("/notifications/{notification_id}/read")
async def api_mark_read(notification_id: int, user_id: str = Depends(get_current_user_id)):
    success = await mark_notification_as_read(notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found or already read")
    return {"success": True}
@notification_router.patch("/notifications/read-all")
async def api_mark_all_read(user_id: str = Depends(get_current_user_id)):
    await mark_all_notifications_as_read(user_id)
    return {"success": True}

@notification_router.delete("/notifications/read")
async def api_clear_read(user_id: str = Depends(get_current_user_id)):
    await clear_read_notifications(user_id)
    return {"success": True}


@notification_router.delete("/notifications/all")
async def api_clear_all(user_id: str = Depends(get_current_user_id)):
    await clear_all_notifications(user_id)
    return {"success": True}

from fastapi import Request

@notification_router.get("/notifications/stream")
async def api_notification_stream(request: Request, user_id: str = Depends(get_current_user_id)):
    async def event_generator():
        q = asyncio.Queue()
        broadcaster.add_listener(q)
        try:
            yield "retry: 5000\n\n: connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    if msg.user_id == "*" or str(msg.user_id).lower() == str(user_id).lower():
                        yield f"data: {msg.model_dump_json()}\n\n"
                except asyncio.TimeoutError:
                    # Lightweight keep-alive comment/ping to prevent CloudFront/proxy timeouts without querying the database
                    yield ": ping\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception:
            pass
        finally:
            broadcaster.remove_listener(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )

