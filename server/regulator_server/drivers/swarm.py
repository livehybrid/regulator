"""Docker Swarm through the Portainer API.

One replicated service per worker group, named ``regulator-run-<id>-<group>``
and labelled ``regulator.run=<id>``, attached to the control plane's own
overlay network so workers reach it by service name. The docker socket is
never mounted; everything goes over Portainer's proxy of the Engine API with
an API key, and the key never appears in a log line or an error.

Two details adapted from Stoker's driver because they were learned the hard
way there. The image tag is resolved to a registry digest before the service
is created, because swarm does not re-pull a floating tag a node already has
cached, so a freshly pushed worker would otherwise silently run stale code.
And "stop" is a scale to zero rather than a delete, so each task gets its
SIGTERM and its grace period to drain and post its final report.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

import httpx

from .base import DriverError, DriverRef, DriverStatus, NotFound, WorkerGroup

log = logging.getLogger("regulator.driver.swarm")

LABEL = "regulator.run"
_RUNNING = frozenset({"running"})


def service_name(run_id: int, group: str) -> str:
    return f"regulator-run-{run_id}-{group}"


def portainer_base_url(host: Optional[str]) -> str:
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if host.startswith(("http://", "https://")):
        return host
    return f"https://{host}:9443"


class SwarmDriver:
    kind = "swarm"

    def __init__(
        self,
        host: Optional[str],
        token: Optional[str],
        endpoint: int = 6,
        verify_tls: bool = False,
        network: str = "regulator_regulator_internal",
        constraints: Optional[List[str]] = None,
        timeout_s: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._host = portainer_base_url(host)
        self._token = token
        self._endpoint = int(endpoint)
        self._verify = verify_tls
        self._network = network
        self._constraints = list(constraints or [])
        self._timeout = timeout_s
        self._transport = transport

    # ---------------------------------------------------------------- http

    def _base(self) -> str:
        if not self._host:
            raise DriverError("the swarm driver has no Portainer host configured (PORTAINER_HOST)")
        return f"{self._host}/api/endpoints/{self._endpoint}/docker"

    def _client(self) -> httpx.Client:
        headers = {"X-API-Key": self._token} if self._token else {}
        kwargs: Dict[str, Any] = {"headers": headers, "verify": self._verify, "timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        ok: tuple = (200, 201),
    ) -> httpx.Response:
        url = self._base() + path
        try:
            with self._client() as client:
                response = client.request(method, url, params=params, json=body)
        except httpx.HTTPError as exc:
            raise DriverError(f"swarm {method} {path} failed: {exc}") from exc
        if response.status_code not in ok:
            detail = " ".join((response.text or "").split())[:300]
            message = f"swarm {method} {path} -> HTTP {response.status_code}" + (f": {detail}" if detail else "")
            if response.status_code == 404:
                raise NotFound(message)
            raise DriverError(message)
        return response

    # -------------------------------------------------------------- driver

    def create(self, group: WorkerGroup) -> DriverRef:
        if group.workers < 1:
            raise DriverError("workers must be at least 1")
        image = self._resolve_image(group.image)
        spec = self._service_spec(group, image)
        log.info("swarm: creating %s with %d replica(s) of %s", spec["Name"], group.workers, image)
        response = self._request("POST", "/services/create", body=spec)
        body = _json(response)
        service_id = body.get("ID") or body.get("Id") or ""
        if not service_id:
            raise DriverError(f"swarm create {spec['Name']} returned no service id")
        return DriverRef(
            kind=self.kind,
            id=str(service_id),
            group=group.group,
            raw={"run_id": group.run_id, "name": spec["Name"], "endpoint": self._endpoint},
        )

    def _resolve_image(self, image: str) -> str:
        if not image or "@sha256:" in image:
            return image
        try:
            response = self._request("GET", f"/distribution/{image}/json", ok=(200,))
            digest = (_json(response).get("Descriptor") or {}).get("digest")
        except DriverError as exc:
            log.warning("swarm: could not resolve %s to a digest (%s); a node may run a cached copy", image, exc)
            return image
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            return image
        repo = image.split("@", 1)[0]
        slash, colon = repo.rfind("/"), repo.rfind(":")
        if colon > slash:
            repo = repo[:colon]
        return f"{repo}@{digest}"

    def _service_spec(self, group: WorkerGroup, image: str) -> Dict[str, Any]:
        labels = {LABEL: str(group.run_id), "regulator.group": group.group, **(group.labels or {})}
        env_list = [f"{key}={value}" for key, value in sorted(group.env.items()) if value is not None]
        constraints = list(self._constraints) + [str(c) for c in (group.options.get("constraints") or [])]
        placement: Dict[str, Any] = {"Preferences": [{"Spread": {"SpreadDescriptor": "node.id"}}]}
        if constraints:
            placement["Constraints"] = constraints
        task: Dict[str, Any] = {
            "ContainerSpec": {
                "Image": image,
                "Env": env_list,
                "Labels": dict(labels),
                "StopGracePeriod": int(group.stop_grace_s) * 1_000_000_000,
            },
            # A worker that exits is finished, not failed: its final report has
            # been posted. Restarting it would claim a fresh lease and run the
            # slice again.
            "RestartPolicy": {"Condition": "none"},
            "Placement": placement,
        }
        if self._network:
            task["Networks"] = [{"Target": self._network}]
        resources = group.options.get("resources")
        if isinstance(resources, dict):
            rendered: Dict[str, Any] = {}
            for ours, theirs in (("limits", "Limits"), ("reservations", "Reservations")):
                block = resources.get(ours)
                if isinstance(block, dict):
                    entry: Dict[str, Any] = {}
                    if block.get("cpus"):
                        entry["NanoCPUs"] = int(float(block["cpus"]) * 1_000_000_000)
                    if block.get("memory_mb"):
                        entry["MemoryBytes"] = int(float(block["memory_mb"]) * 1024 * 1024)
                    if entry:
                        rendered[theirs] = entry
            if rendered:
                task["Resources"] = rendered
        return {
            "Name": service_name(group.run_id, group.group),
            "Labels": labels,
            "TaskTemplate": task,
            "Mode": {"Replicated": {"Replicas": int(group.workers)}},
        }

    def stop(self, ref: DriverRef, grace_s: int) -> None:
        self._update_replicas(ref, 0)
        log.info("swarm: scaled %s to zero (grace %ds)", ref.id, grace_s)

    def destroy(self, ref: DriverRef) -> None:
        response = self._request("DELETE", f"/services/{ref.id}", ok=(200, 201, 204, 404))
        log.info("swarm: %s %s", "already gone" if response.status_code == 404 else "removed", ref.id)

    def status(self, ref: DriverRef) -> DriverStatus:
        try:
            service = self._get_service(ref.id)
        except NotFound:
            return DriverStatus(desired=0, running=0)
        replicated = ((service.get("Spec") or {}).get("Mode") or {}).get("Replicated") or {}
        desired = int(replicated.get("Replicas") or 0)
        name = (ref.raw or {}).get("name") or service_name(int((ref.raw or {}).get("run_id", 0)), ref.group)
        response = self._request("GET", "/tasks", params={"filters": json.dumps({"service": [name]})})
        raw_tasks = _json(response)
        tasks = [_task_view(t) for t in raw_tasks] if isinstance(raw_tasks, list) else []
        running = sum(1 for t in tasks if t["state"] in _RUNNING)
        return DriverStatus(desired=desired, running=running, tasks=tasks)

    def logs(self, ref: DriverRef, tail: int) -> str:
        params = {"stdout": "true", "stderr": "true", "timestamps": "false", "tail": str(tail) if tail > 0 else "all"}
        try:
            response = self._request("GET", f"/services/{ref.id}/logs", params=params)
        except DriverError as exc:
            log.warning("swarm: logs for %s unavailable: %s", ref.id, exc)
            return ""
        return _decode_logs(response.content)

    def list_run_ids(self) -> Set[int]:
        response = self._request("GET", "/services", params={"filters": json.dumps({"label": [LABEL]})})
        body = _json(response)
        if not isinstance(body, list):
            raise DriverError("swarm /services returned a non-list body")
        found: Set[int] = set()
        for service in body:
            labels = ((service.get("Spec") or {}).get("Labels") or {}) if isinstance(service, dict) else {}
            raw = labels.get(LABEL)
            try:
                if raw is not None:
                    found.add(int(raw))
            except (TypeError, ValueError):
                continue
        return found

    def refs_for_run(self, run_id: int) -> List[DriverRef]:
        """Every service this driver holds for a run, for the stray sweep."""
        response = self._request("GET", "/services", params={"filters": json.dumps({"label": [f"{LABEL}={run_id}"]})})
        body = _json(response)
        refs: List[DriverRef] = []
        for service in body if isinstance(body, list) else []:
            spec = service.get("Spec") or {}
            refs.append(
                DriverRef(
                    kind=self.kind,
                    id=str(service.get("ID") or service.get("Id") or ""),
                    group=str((spec.get("Labels") or {}).get("regulator.group", "api")),
                    raw={"run_id": run_id, "name": spec.get("Name")},
                )
            )
        return refs

    # ------------------------------------------------------------- helpers

    def _get_service(self, service_id: str) -> Dict[str, Any]:
        body = _json(self._request("GET", f"/services/{service_id}", ok=(200,)))
        if not isinstance(body, dict):
            raise DriverError(f"swarm service {service_id} inspect returned a non-object")
        return body

    def _update_replicas(self, ref: DriverRef, replicas: int) -> None:
        service = self._get_service(ref.id)
        version = (service.get("Version") or {}).get("Index")
        if version is None:
            raise DriverError(f"swarm service {ref.id} has no version index")
        spec = service.get("Spec")
        if not isinstance(spec, dict):
            raise DriverError(f"swarm service {ref.id} has no spec")
        mode = spec.setdefault("Mode", {})
        mode.pop("Global", None)
        mode.setdefault("Replicated", {})["Replicas"] = int(replicas)
        self._request("POST", f"/services/{ref.id}/update", params={"version": int(version)}, body=spec)


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise DriverError("swarm response was not valid JSON") from exc


def _task_view(task: Any) -> Dict[str, Any]:
    if not isinstance(task, dict):
        return {"slot": None, "node": None, "state": None}
    status = task.get("Status") or {}
    state = status.get("State")
    return {
        "slot": task.get("Slot"),
        "node": task.get("NodeID"),
        "state": state.lower() if isinstance(state, str) else state,
        "message": status.get("Message"),
    }


def _decode_logs(raw: bytes) -> str:
    if not raw:
        return ""
    if raw[0] in (0, 1, 2) and len(raw) >= 8 and raw[1] == 0 and raw[2] == 0 and raw[3] == 0:
        out: List[bytes] = []
        index = 0
        while index + 8 <= len(raw):
            length = int.from_bytes(raw[index + 4:index + 8], "big")
            index += 8
            out.append(raw[index:index + length])
            index += length
        return b"".join(out).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")
