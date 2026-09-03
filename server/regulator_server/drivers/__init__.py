"""Driver selection: which backend launches a run's workers.

``inprocess`` is not a driver here: the control plane runs that scenario in
its own thread and needs nothing launched. ``swarm`` and ``k8s`` are built
from the fleet settings and cached per process; ``fake`` is for tests.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from ..config import FleetSettings, get_settings
from .base import DriverError, DriverRef, DriverStatus, ExecutionDriver, NotFound, WorkerGroup

log = logging.getLogger("regulator.drivers")

_CACHE: Dict[str, ExecutionDriver] = {}
_LOCK = threading.Lock()

FLEET_KINDS = ("inprocess", "swarm", "k8s")


def get_driver(kind: str, settings: Optional[FleetSettings] = None) -> ExecutionDriver:
    with _LOCK:
        cached = _CACHE.get(kind)
        if cached is not None:
            return cached
    driver = _build(kind, settings or get_settings().fleet)
    with _LOCK:
        _CACHE.setdefault(kind, driver)
        return _CACHE[kind]


def _build(kind: str, settings: FleetSettings) -> ExecutionDriver:
    if kind == "swarm":
        from .swarm import SwarmDriver

        if not settings.swarm_available:
            raise DriverError("the swarm fleet needs PORTAINER_HOST and PORTAINER_TOKEN")
        return SwarmDriver(
            host=settings.portainer_host,
            token=settings.portainer_token,
            endpoint=settings.portainer_endpoint,
            verify_tls=settings.portainer_verify_tls,
            network=settings.swarm_network,
            constraints=list(settings.swarm_constraints),
        )
    if kind == "k8s":
        from .k8s import K8sDriver

        return K8sDriver(
            namespace=settings.k8s_namespace,
            node_selector=settings.k8s_node_selector,
            kubeconfig=settings.kubeconfig,
            context=settings.kube_context,
            in_cluster=settings.k8s_in_cluster,
        )
    if kind == "fake":
        from .fake import FakeDriver

        return FakeDriver()
    raise DriverError(f"unknown fleet {kind!r}, expected one of swarm, k8s, fake")


def register_driver(kind: str, driver: ExecutionDriver) -> None:
    """Test seam: bind a kind to an instance."""
    with _LOCK:
        _CACHE[kind] = driver


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "DriverError",
    "DriverRef",
    "DriverStatus",
    "ExecutionDriver",
    "FLEET_KINDS",
    "NotFound",
    "WorkerGroup",
    "clear_cache",
    "get_driver",
    "register_driver",
]
