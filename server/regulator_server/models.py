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
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
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

    runs: Mapped[list["Run"]] = relationship(back_populates="target", passive_deletes=True)
    # Indexer management URIs for SmartStore state on a distributed target,
    # comma separated. Empty means discover the search peers and reuse this
    # target's credential on each of them.
    indexer_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    indexer_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    indexer_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    indexer_password_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


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
    # Nullable, and SET NULL rather than CASCADE: a run is a measurement that
    # outlives the target it was taken against. Deleting a target keeps its
    # history and only loses the name, which the summary carries anyway as
    # target_url.
    target_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("targets.id", ondelete="SET NULL"), nullable=True
    )
    scenario: Mapped[str] = mapped_column(String(128))

    state: Mapped[str] = mapped_column(String(16), default="pending")
    virtual_users: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    arrival_rate_per_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pacing_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evict_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    evict_cache_indexes: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Periodic eviction during the run (seconds between evictions, same scope
    # as evict_cache_indexes) and how long after each one counts as cold.
    evict_every_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cold_window_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ended_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    stats_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    summary_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    target: Mapped[Optional[Target]] = relationship(back_populates="runs")
    # The seed the run actually used (an override may differ from the
    # scenario's), and a digest of the scenario files, so two runs can be
    # proven to have been the same test.
    seed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scenario_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Where the load is generated. ``inprocess`` runs in this process; the
    # other fleets launch worker containers that claim slots and report back.
    fleet: Mapped[str] = mapped_column(String(16), default="inprocess")
    # The worker count. Unset means "size the fleet from the users per
    # worker"; the planner writes the total back once it has decided.
    workers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The driver's handle(s) on what it created, so a stray fleet can be found
    # and destroyed after a restart. A list: a mixed scenario is two groups.
    driver_refs_json: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    # The shared start instant, set when every worker is ready.
    t0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # provisioning -> releasing -> running -> draining, for a fleet run.
    fleet_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    leases: Mapped[list["WorkerLease"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class WorkerLease(Base):
    """One slot of a fleet run, and who holds it.

    Identity is the lease id, not the container: a worker that restarts claims
    a fresh lease and the old one is fenced, so a stale process that comes
    back from the dead cannot fold its numbers into a slot somebody else now
    holds. Adapted from Stoker, where this was learned in production.
    """

    __tablename__ = "worker_leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    slot: Mapped[int] = mapped_column(Integer)
    # Which engine this slot runs, and which worker group (image) it belongs to.
    engine: Mapped[str] = mapped_column(String(16), default="api")
    lease_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    holder: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # free, claimed, ready, running, done, lost
    state: Mapped[str] = mapped_column(String(16), default="free")
    # This slot's share of the load: virtual users, arrival rate, and the
    # scenario it runs (a mixed scenario is split by engine).
    share_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    last_heartbeat_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    claimed_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    # The latest live aggregate the worker reported, and its final summary.
    stats_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    summary_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    log_tail_json: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)

    run: Mapped[Run] = relationship(back_populates="leases")


class RunSample(Base):
    """One point of a run's time series, every few seconds.

    The aggregate row has no slot; a fleet run also keeps one row per slot
    from the workers' heartbeats. ``interval_json`` is what happened since the
    previous sample (counts and percentiles over that window alone), and
    ``cum_json`` the cumulative figures at that instant, so a chart can show
    both the honest moment and the number the gate reads.
    """

    __tablename__ = "run_samples"
    __table_args__ = (Index("ix_run_samples_run_at", "run_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    at: Mapped[float] = mapped_column(Float)
    elapsed_s: Mapped[float] = mapped_column(Float, default=0.0)
    executions: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    in_flight: Mapped[int] = mapped_column(Integer, default=0)
    searches_queued: Mapped[int] = mapped_column(Integer, default=0)
    throughput_per_s: Mapped[float] = mapped_column(Float, default=0.0)
    interval_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    cum_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "elapsed_s": self.elapsed_s,
            "slot": self.slot,
            "executions": self.executions,
            "errors": self.errors,
            "in_flight": self.in_flight,
            "searches_queued": self.searches_queued,
            "throughput_per_s": self.throughput_per_s,
            "interval": self.interval_json or {},
            "cum": self.cum_json or {},
        }


class TargetSample(Base):
    """A SmartStore cache reading on a target, whenever one was taken.

    Written around every run (before and after), by the Cache, Evict and
    Report actions, and by a periodic eviction; the target page draws the
    cache's history from these.
    """

    __tablename__ = "target_samples"
    __table_args__ = (Index("ix_target_samples_target_at", "target_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"))
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    at: Mapped[float] = mapped_column(Float)
    # before, after, evict, epoch, read
    kind: Mapped[str] = mapped_column(String(16), default="read")
    local_buckets: Mapped[int] = mapped_column(Integer, default=0)
    total_buckets: Mapped[int] = mapped_column(Integer, default=0)
    local_pct: Mapped[float] = mapped_column(Float, default=0.0)
    fill_pct: Mapped[float] = mapped_column(Float, default=0.0)
    local_bytes: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "run_id": self.run_id,
            "kind": self.kind,
            "local_buckets": self.local_buckets,
            "total_buckets": self.total_buckets,
            "local_pct": self.local_pct,
            "fill_pct": self.fill_pct,
            "local_bytes": self.local_bytes,
            "detail": self.detail_json or {},
        }


LEASE_FREE = "free"
LEASE_CLAIMED = "claimed"
LEASE_READY = "ready"
LEASE_RUNNING = "running"
LEASE_DONE = "done"
LEASE_LOST = "lost"
LIVE_LEASE_STATES = (LEASE_CLAIMED, LEASE_READY, LEASE_RUNNING)


class AuditEvent(Base):
    """Who did what to which target.

    Eviction has no undo and a run can saturate a production cluster, so the
    question "who pressed that at 14:20" must be answerable after the log has
    rotated. One row per state-changing action, with the caller's address and
    how they authenticated.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[float] = mapped_column(Float, default=time.time)
    actor: Mapped[str] = mapped_column(String(64))
    client: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


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
