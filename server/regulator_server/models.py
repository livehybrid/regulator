"""Storage.

Deliberately small. A target, a run, and nothing else yet. The temptation with
a control plane is to model the whole eventual system on day one; Stoker's
schema grew to a dozen tables and every one of them earned its place by being
needed, not by being anticipated.

SQLite by default, which is enough for a single-operator control plane that
runs scenarios in-process. Postgres arrives with the worker fleet, when several
processes need to agree about the same run.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Target(Base):
    """A Splunk instance under test.

    Credentials are Fernet-encrypted here and decrypted only transiently, when
    a request actually needs to talk to the instance. No response schema ever
    exposes them, and there is a test asserting that.
    """

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    mgmt_url: Mapped[str] = mapped_column(String(512))
    web_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    password_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    app: Mapped[str] = mapped_column(String(128), default="search")
    owner: Mapped[str] = mapped_column(String(128), default="nobody")
    api_version: Mapped[str] = mapped_column(String(8), default="v2")

    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    health_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_report_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    runs: Mapped[list["Run"]] = relationship(back_populates="target")


class Run(Base):
    """One execution of a scenario against a target.

    ``stats_json`` is the live aggregate, refreshed while the run is in flight
    so the UI has something to poll. ``summary_json`` is the final word and is
    written once, at the end. Keeping them apart means a crashed run still has
    whatever progress it made, and a finished run cannot be confused with one
    that is still going.
    """

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"))
    scenario: Mapped[str] = mapped_column(String(128))

    state: Mapped[str] = mapped_column(String(16), default="pending")
    virtual_users: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    arrival_rate_per_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pacing_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evict_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    evict_cache_indexes: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ended_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    stats_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    summary_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    target: Mapped[Target] = relationship(back_populates="runs")


class Baseline(Base):
    """A named run that later runs are judged against.

    A label rather than a run id, because a pipeline says "compare against
    main-green" and should not have to know which run that is this week.
    Re-labelling is how a new baseline is promoted, so the label is the primary
    key and pointing it at a different run is a single update.
    """

    __tablename__ = "baselines"

    label: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    scenario: Mapped[str] = mapped_column(String(128))
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


TERMINAL_STATES = ("completed", "stopped", "aborted", "failed")


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
