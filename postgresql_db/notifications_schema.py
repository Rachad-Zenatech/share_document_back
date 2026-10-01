import asyncio
from postgresql_db.database import get_pool

DDL = """
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    type VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    link_url VARCHAR(500),
    entity_type VARCHAR(100),
    entity_id VARCHAR(100),
    sender_name VARCHAR(255),
    sender_avatar VARCHAR(500),
    attachments JSONB,
    is_read BOOLEAN DEFAULT FALSE,
    read_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read);
"""

async def ensure_notifications_schema() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DDL)

if __name__ == "__main__":
    from dotenv import load_dotenv
    from postgresql_db.database import create_pool, close_pool

    load_dotenv()

    async def main():
        await create_pool()
        await ensure_notifications_schema()
        await close_pool()
        print("Notifications schema ensured successfully.")

    asyncio.run(main())
