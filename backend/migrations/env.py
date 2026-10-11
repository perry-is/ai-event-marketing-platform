from alembic import context
from sqlalchemy import create_engine, pool

from app.config import Settings
from app.models import Base

target_metadata = Base.metadata


def run_migrations():
    # Tests supply a disposable database URL through Alembic attributes, never an INI secret.
    url = context.config.attributes.get("database_url")
    if url is None:
        url = Settings().database_url.get_secret_value()
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, poolclass=pool.NullPool, connect_args={"connect_timeout": 3})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
