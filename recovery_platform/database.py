"""
recovery_platform/database.py
==============================
SQLModel engine, table initialisation, and FastAPI-compatible session
dependency for the AI Revenue Recovery Platform.

Usage in FastAPI routes
-----------------------
    from fastapi import Depends
    from sqlmodel import Session
    from recovery_platform.database import get_session

    @router.get("/transactions")
    def list_transactions(session: Session = Depends(get_session)):
        ...

Usage in scripts / tests
-------------------------
    from recovery_platform.database import init_db, engine
    init_db()   # creates all tables
"""

from __future__ import annotations

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from recovery_platform.config import get_settings

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# ``check_same_thread=False`` is required for SQLite when the engine is shared
# across threads (e.g. inside a FastAPI app with async handlers).  It is a
# no-op for Postgres and other backends.
# ---------------------------------------------------------------------------

def _build_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(url, echo=False, connect_args=connect_args)


engine = _build_engine()


# ---------------------------------------------------------------------------
# Table initialisation
# ---------------------------------------------------------------------------


def init_db(custom_engine=None) -> None:
    """
    Create all SQLModel tables in the target database.

    Parameters
    ----------
    custom_engine:
        Optional SQLAlchemy engine.  Useful in tests that use an in-memory
        SQLite database instead of the one from settings.
    """
    # Import all models so SQLModel.metadata is populated before create_all.
    import recovery_platform.models  # noqa: F401  (side-effect import)

    target = custom_engine or engine
    SQLModel.metadata.create_all(target)


# ---------------------------------------------------------------------------
# FastAPI session dependency
# ---------------------------------------------------------------------------


def get_session() -> Generator[Session, None, None]:
    """
    Yield a SQLModel ``Session`` scoped to a single request.

    Inject with ``Depends(get_session)`` in FastAPI route handlers.
    The session is automatically committed on success and rolled back on
    exception, then closed in the ``finally`` block.
    """
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
