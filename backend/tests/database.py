"""Test-only disposable PostgreSQL creation. Never touches the application database."""

import os
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

LOCAL_TEST_URL = "postgresql+psycopg://emp_local:local-only-password@127.0.0.1:5433/emp_test"


def disposable_database():
    base = make_url(os.environ.get("TEST_DATABASE_URL", LOCAL_TEST_URL))
    if base.host not in {"localhost", "127.0.0.1", "::1"} or not base.database.startswith(
        "emp_test"
    ):
        raise RuntimeError("Test database must use local host and emp_test database prefix")
    base = base.set(query=dict(base.query) | {"connect_timeout": "3"})
    name = "emp_test_" + uuid.uuid4().hex
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.exec_driver_sql(f'CREATE DATABASE "{name}"')  # Generated identifier, never user input.
    url = base.set(database=name).render_as_string(hide_password=False)
    return url, admin, name


def drop_database(admin, name):
    assert name.startswith("emp_test_") and name[9:].isalnum()
    with admin.connect() as db:
        db.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.dispose()


def migrate(url, revision="head"):
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, revision)
    return cfg
