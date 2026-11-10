"""Alembic environment for OnePage.

The URL is never configured twice: this module imports the app's own engine
(`app.database.engine`), which is built from `settings.abs_db_path` and carries
the SQLite pragmas (WAL, busy_timeout, ...). So `alembic upgrade head` from a
shell and the upgrade the app runs at boot always talk to the same file.

`render_as_batch=True` everywhere: SQLite cannot ALTER a column in place, and
batch mode makes Alembic fall back to the copy-and-rename recipe when a future
revision needs it.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Repo root on sys.path, so `import app.*` works when Alembic is launched by
# its own console script (which does not put the cwd first).
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Base, engine  # noqa: E402
from app import models  # noqa: E402,F401  — import populates Base.metadata

config = context.config

# Only the CLI has an ini file; the app passes a Config built in code and must
# not have uvicorn's logging reconfigured underneath it.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout (`alembic upgrade head --sql`) without a DB."""
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # `connection` in attributes: the caller already holds one (not used today,
    # but it keeps this env.py usable from a test that wants its own engine).
    external = config.attributes.get("connection")
    if external is not None:
        context.configure(connection=external, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=False)
        with context.begin_transaction():
            context.run_migrations()
        return

    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=False)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
