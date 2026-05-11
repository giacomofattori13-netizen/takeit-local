import os

from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = "sqlite:///./takeit.db"

SQL_ECHO = os.getenv("SQL_ECHO", "").strip().lower() in {"1", "true", "yes", "on"}
engine = create_engine(DATABASE_URL, echo=SQL_ECHO)


@event.listens_for(engine, "connect")
def _set_sqlite_wal(dbapi_conn, _connection_record):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
