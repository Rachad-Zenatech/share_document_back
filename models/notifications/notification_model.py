from typing import Optional
from pydantic import BaseModel
from datetime import datetime

class NotificationCreate(BaseModel):
    user_id: str
    type: str
    title: str
    message: str
    link_url: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    sender_name: Optional[str] = None
    sender_avatar: Optional[str] = None
    attachments: Optional[list] = None


class NotificationResponse(BaseModel):
    id: int
    user_id: str
    type: str
    title: str
    message: str
    link_url: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    sender_name: Optional[str] = None
    sender_avatar: Optional[str] = None
    attachments: Optional[list] = None

    is_read: bool = False
    created_at: datetime
    read_at: Optional[datetime] = None
