import asyncio
import asyncpg
import os
import sys

# Load .env if present
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

db_url = os.getenv("DATABASE_URL")
print(f"Testing database connection...")
if not db_url:
    print("DATABASE_URL not set!")
    sys.exit(1)

host_part = db_url.split("@")[-1] if "@" in db_url else db_url
print(f"Connecting to host: {host_part}")

async def test_connection():
    try:
        conn = await asyncpg.connect(db_url, timeout=10)
        version = await conn.fetchval("SELECT version();")
        print(f"DATABASE CONNECTION SUCCESSFUL!")
        print(f"PostgreSQL Version: {version}")

        # Check existing SEC tables
        tables = await conn.fetch("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
              AND (table_name LIKE '%sec_%' OR table_name LIKE '%navigation_%' OR table_name LIKE '%permission_%')
            ORDER BY table_name;
        """)
        table_names = [t["table_name"] for t in tables]
        print(f"SEC & RBAC Tables found in database: {table_names}")

        # Check financial table templates
        if "sec_financial_table_templates" in table_names:
            count = await conn.fetchval("SELECT count(*) FROM sec_financial_table_templates;")
            print(f"sec_financial_table_templates row count: {count}")
            rows = await conn.fetch("SELECT id, name, badge FROM sec_financial_table_templates LIMIT 5;")
            for r in rows:
                print(f"  - [{r['badge']}] {r['name']} (ID: {r['id']})")
        else:
            print("Note: sec_financial_table_templates table does not exist yet in this DB schema.")

        await conn.close()
        print("Database connection closed cleanly.")
    except Exception as e:
        print(f"CONNECTION ERROR: {type(e).__name__}: {e}")
        sys.exit(1)

asyncio.run(test_connection())