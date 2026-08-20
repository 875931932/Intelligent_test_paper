"""Synchronous SQLAlchemy session dependency for the HTTP API."""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


@lru_cache
def get_engine():
    # pool_pre_ping + pool_recycle：远程/共享 PostgreSQL 会掐断空闲连接，
    # 取连接前先探活并回收陈旧连接，避免 "server closed the connection" 500。
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        # SQLite 默认禁止跨线程使用连接，uvicorn 多线程下必须放开。
        connect_args["check_same_thread"] = False
    elif settings.database_url.startswith("postgresql"):
        # 远程 PG 的 search_path 可能包含无权限的 schema，强制用 public
        connect_args["options"] = "-c search_path=public"
    return create_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_timeout=30,
        connect_args=connect_args,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
