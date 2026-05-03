import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from alembic import context

# Import your models here so Alembic can read the metadata
from app.models import *
from app.database import DATABASE_URL

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = SQLModel.metadata

# Overwrite the sqlalchemy.url from alembic.ini with the one from the app.
# Escape `%` → `%%` because configparser performs string interpolation when
# the value is read back via get_main_option(). URL-encoded passwords (e.g.
# `%23` for `#`) would otherwise raise "invalid interpolation syntax". This
# is symmetric — get_main_option's interpolation pass collapses `%%` back to
# `%`, so the engine sees the original URL unchanged.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

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


def do_run_migrations(connection: Connection) -> None:
    # ``transaction_per_migration=True`` (R5 audit MEDIUM-A): some migrations
    # need to break out of the transactional-DDL guarantee to issue commands
    # like ``CREATE INDEX CONCURRENTLY`` (alembic 0067 issues an
    # ``op.execute("COMMIT")`` for exactly this reason). Without this flag,
    # the entire ``alembic upgrade head`` run is wrapped in one transaction
    # — so the commit in 0067 leaks into any subsequent migration (0068+),
    # which then runs in autocommit and silently regresses alembic's
    # atomicity guarantee. Per-migration transactions scope each migration's
    # transaction to itself; 0067's commit only affects 0067.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
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
