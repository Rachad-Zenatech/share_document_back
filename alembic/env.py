import os
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# The project's runtime connects with asyncpg via DATABASE_URL (see
# postgresql_db/database.py); reuse the same env var here instead of the
# static sqlalchemy.url placeholder in alembic.ini.
_database_url = os.environ["DATABASE_URL"]
if _database_url.startswith("postgresql://"):
    _database_url = _database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
config.set_main_option("sqlalchemy.url", _database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# This project has no SQLAlchemy ORM (raw asyncpg + hand-written SQL), so
# postgresql_db/schema_metadata.py is a hand-maintained SQLAlchemy Core mirror
# of the tables kept solely so --autogenerate has something to diff against.
# When a table changes, update its Table() there first, then run
# `alembic revision --autogenerate`.
from postgresql_db import schema_metadata

target_metadata = schema_metadata.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def include_object(object, name, type_, reflected, compare_to):
    # Only track public schema objects; ignore Supabase internal schemas (auth, storage, vault, etc.)
    if type_ == "table" and object.schema and object.schema != "public":
        return False
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=False,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an asyncpg-backed async engine.

    Mirrors the ssl handling in postgresql_db/database.py so migrations can
    target the same Supabase/managed Postgres instances as the app.
    """
    ssl_required = os.getenv("DATABASE_SSL", "false").lower() in ("true", "require")
    connectable = create_async_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
        connect_args={"ssl": "require"} if ssl_required else {},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
