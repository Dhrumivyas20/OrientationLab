"""Persistent session, sensor, and orientation records (MySQL or local SQLite)."""
from __future__ import annotations
import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class TrackingSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="Untitled run")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  default=lambda: datetime.now(timezone.utc))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    peak_error: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sensor_logs: Mapped[list["SensorLog"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    orientations: Mapped[list["OrientationRecord"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class SensorLog(Base):
    __tablename__ = "sensor_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    x_deg: Mapped[float] = mapped_column(Float, nullable=False)
    y_deg: Mapped[float] = mapped_column(Float, nullable=False)
    z_deg: Mapped[float] = mapped_column(Float, nullable=False)
    order_name: Mapped[str] = mapped_column(String(3), nullable=False, default="XYZ")
    quantize_decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  default=lambda: datetime.now(timezone.utc))
    session: Mapped[TrackingSession] = relationship(back_populates="sensor_logs")


class OrientationRecord(Base):
    __tablename__ = "orientations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    matrix: Mapped[list] = mapped_column(JSON, nullable=False)
    orthogonality_error: Mapped[float] = mapped_column(Float, nullable=False)
    determinant: Mapped[float] = mapped_column(Float, nullable=False)
    repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  default=lambda: datetime.now(timezone.utc))
    session: Mapped[TrackingSession] = relationship(back_populates="orientations")


def database_url() -> str:
    configured = os.getenv("DATABASE_URL")
    if configured:
        if configured.startswith("mysql://"):
            configured = "mysql+pymysql://" + configured[len("mysql://"):]
        if configured.startswith("mysql+pymysql://"):
            return configured
        return configured
    instance = Path(__file__).resolve().parents[1] / "instance"
    instance.mkdir(exist_ok=True)
    return "sqlite:///" + str(instance / "orientation_lab.sqlite3")


_url = database_url()
_connect_args = {"check_same_thread": False, "timeout": 15} if _url.startswith("sqlite") else {"ssl": {"check_hostname": True}}
engine = create_engine(_url, pool_pre_ping=True, connect_args=_connect_args)
if _url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
