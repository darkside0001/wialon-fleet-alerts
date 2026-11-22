"""Database schema (SQLAlchemy 2). PostgreSQL in production, SQLite for tests and local runs.

Times are stored as unix seconds (UTC) so SQLite and PostgreSQL behave the same.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Wialon item id
    name: Mapped[str] = mapped_column(String(200))
    last_message_at: Mapped[int | None] = mapped_column(BigInteger)
    lat: Mapped[float | None]
    lon: Mapped[float | None]
    speed: Mapped[float | None]
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_unit_rule_status", "unit_id", "rule_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    unit_id: Mapped[int] = mapped_column(BigInteger, index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)  # open | acknowledged | resolved
    oneshot: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    opened_at: Mapped[int] = mapped_column(BigInteger)
    resolved_at: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_at: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_by: Mapped[str | None] = mapped_column(String(100))


class Notification(Base):
    """Transactional outbox: rows are written with the alert, sent by the dispatcher."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"))
    notifier: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(16))  # open | resolve
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    sent_at: Mapped[int | None] = mapped_column(BigInteger)


class WorkerStatus(Base):
    __tablename__ = "worker_status"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[int] = mapped_column(BigInteger)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


def make_sessionmaker(url: str) -> sessionmaker:
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
