from asyncio import run
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from superlily_core.models import Base
from superlily_core.settings import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", Settings.from_env().database_url)
target_metadata = Base.metadata


_SQLITE_ARCHIVE_TABLES = {
    "import_batches",
    "conversation_mappings",
    "legacy_messages",
    "source_message_identities",
}


def include_object(object_, name, type_, reflected, compare_to):
    """Keep the schema-only archive outside ORM autogenerate ownership.

    H1 creates archive objects with deliberate raw DDL because PostgreSQL
    partitioned tables and the versioned timeline view are not represented by
    the online ORM metadata.  PostgreSQL keeps them in a separate schema;
    SQLite uses the logical table names as its schema-less fallback.
    """

    if not reflected:
        return True
    table = object_ if type_ == "table" else getattr(object_, "table", None)
    table_schema = getattr(table, "schema", None)
    table_name = getattr(table, "name", None)
    if table_schema == "archive":
        return False
    if table_schema is None and table_name in _SQLITE_ARCHIVE_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run(run_migrations_online())
