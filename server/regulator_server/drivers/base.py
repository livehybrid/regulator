"""The execution driver contract: start N workers somewhere, and stop them.

A driver knows nothing about searches. It materialises a group of worker
containers for a run, reports whether they are up, tails their logs and
removes them. Identity stays with the control plane (the lease), so a driver
is never trusted as a store of who holds what: it is queried, not believed.

Adapted from Stoker's contract, where it has driven real fleets on Docker
Swarm through Portainer and on Kubernetes through Indexed Jobs. Five methods,
all synchronous; the control plane calls them from its own thread.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Protocol, Set, runtime_checkable


@dataclasses.dataclass
class WorkerGroup:
    """Everything a driver needs to launch one group of identical workers.

    A run has one group per engine: API workers and browser workers use
    different images and different resource profiles. ``env`` is the managed
    bootstrap (run id, control-plane URL, per-run token, hint slot handled by
    the driver); the driver adds placement, labels and restart policy.
    """

    run_id: int
    group: str  # "api" or "browser"
    image: str
    workers: int
    env: Dict[str, str]
    labels: Dict[str, str] = dataclasses.field(default_factory=dict)
    stop_grace_s: int = 90
    # Values that must not appear in a container's environment. A driver that
    # can project a secret (a swarm secret, a Kubernetes Secret) mounts these;
    # the in-memory driver merges them into the environment because a local
    # subprocess has no secret store.
    secrets: Dict[str, str] = dataclasses.field(default_factory=dict)
    # Driver-specific knobs: swarm placement constraints, k8s node selector,
    # resource requests.
    options: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class DriverRef:
    """An opaque handle to a created group, stored on the run as JSON."""

    kind: str
    id: str
    group: str = "api"
    raw: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "group": self.group, "raw": self.raw}

    @classmethod
    def from_json(cls, doc: Optional[Dict[str, Any]]) -> Optional["DriverRef"]:
        if not doc:
            return None
        return cls(
            kind=str(doc.get("kind", "")),
            id=str(doc.get("id", "")),
            group=str(doc.get("group", "api")),
            raw=dict(doc.get("raw") or {}),
        )


@dataclasses.dataclass
class DriverStatus:
    """The driver's own view: how many it wants, how many are up."""

    desired: int
    running: int
    tasks: List[Dict[str, Any]] = dataclasses.field(default_factory=list)


class DriverError(Exception):
    """A driver operation failed."""


class NotFound(DriverError):
    """The workload is genuinely absent (a 404), as distinct from a hiccup."""


@runtime_checkable
class ExecutionDriver(Protocol):
    kind: str

    def create(self, group: WorkerGroup) -> DriverRef:
        """Launch the group. Returns the handle."""

    def stop(self, ref: DriverRef, grace_s: int) -> None:
        """Ask the group to drain: SIGTERM with ``grace_s`` before the kill."""

    def destroy(self, ref: DriverRef) -> None:
        """Remove the group. Idempotent."""

    def status(self, ref: DriverRef) -> DriverStatus:
        """Desired and running counts, best effort."""

    def logs(self, ref: DriverRef, tail: int) -> str:
        """Recent log lines from the group, best effort."""

    def list_run_ids(self) -> Set[int]:
        """Run ids of every group this driver owns, for the stray sweep at boot.

        Optional: a driver that cannot enumerate raises NotImplementedError and
        the sweep skips it rather than mistaking "cannot list" for "nothing".
        """
