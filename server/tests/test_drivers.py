"""The Swarm and Kubernetes drivers, against recorded transports and fake clients."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "worker"))
sys.path.insert(0, str(REPO / "server"))

from regulator_server.drivers.base import DriverError, DriverRef, NotFound, WorkerGroup  # noqa: E402
from regulator_server.drivers.k8s import K8sDriver, job_name, secret_name  # noqa: E402
from regulator_server.drivers.swarm import SwarmDriver, service_name  # noqa: E402


def group(**overrides) -> WorkerGroup:
    values = dict(
        run_id=42,
        group="api",
        image="ghcr.io/livehybrid/regulator-worker:latest",
        workers=3,
        env={"REG_RUN_ID": "42", "REG_CONTROL_URL": "http://regulator:8080", "REG_RUN_JWT": "tok.en", "REG_LOG_LEVEL": "INFO"},
        options={"slot_base": 0, "active_deadline_s": 900},
    )
    values.update(overrides)
    return WorkerGroup(**values)


# -------------------------------------------------------------------- swarm


class Portainer:
    """A recording Portainer that answers the handful of calls the driver makes."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.services: Dict[str, Dict[str, Any]] = {}
        self.digest_ok = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "body": body, "params": dict(request.url.params)})
        assert request.headers.get("X-API-Key") == "secret-key"
        if path.endswith("/distribution/ghcr.io/livehybrid/regulator-worker:latest/json"):
            if not self.digest_ok:
                return httpx.Response(500, json={"message": "registry unreachable"})
            return httpx.Response(200, json={"Descriptor": {"digest": "sha256:" + "ab" * 32}})
        if path.endswith("/services/create"):
            service_id = f"svc{len(self.services) + 1}"
            self.services[service_id] = {"ID": service_id, "Version": {"Index": 5}, "Spec": body}
            return httpx.Response(201, json={"ID": service_id})
        if "/services/" in path and path.endswith("/update"):
            service_id = path.split("/services/")[1].split("/")[0]
            self.services[service_id]["Spec"] = body
            self.services[service_id]["Version"]["Index"] += 1
            return httpx.Response(200, json={})
        if "/services/" in path and path.endswith("/logs"):
            return httpx.Response(200, content=b"\x01\x00\x00\x00\x00\x00\x00\x05hello")
        if "/services/" in path and request.method == "GET":
            service_id = path.rsplit("/", 1)[1]
            if service_id not in self.services:
                return httpx.Response(404, json={"message": "no such service"})
            return httpx.Response(200, json=self.services[service_id])
        if "/services/" in path and request.method == "DELETE":
            service_id = path.rsplit("/", 1)[1]
            if self.services.pop(service_id, None) is None:
                return httpx.Response(404, json={"message": "no such service"})
            return httpx.Response(200, json={})
        if path.endswith("/services") and request.method == "GET":
            return httpx.Response(200, json=list(self.services.values()))
        if path.endswith("/tasks"):
            return httpx.Response(200, json=[
                {"Slot": 1, "NodeID": "n1", "Status": {"State": "running"}},
                {"Slot": 2, "NodeID": "n2", "Status": {"State": "running"}},
                {"Slot": 3, "NodeID": "n1", "Status": {"State": "shutdown"}},
            ])
        return httpx.Response(404, json={"message": f"unhandled {path}"})


@pytest.fixture
def portainer():
    fake = Portainer()
    driver = SwarmDriver(
        host="192.0.2.10", token="secret-key", endpoint=6, network="regulator_regulator_internal",
        constraints=["node.hostname != macdev"], transport=httpx.MockTransport(fake.handler),
    )
    return fake, driver


def test_swarm_creates_a_digest_pinned_service_on_the_stack_network(portainer):
    fake, driver = portainer
    ref = driver.create(group())
    assert ref.kind == "swarm" and ref.group == "api"
    created = [r for r in fake.requests if r["path"].endswith("/services/create")][0]["body"]
    assert created["Name"] == service_name(42, "api") == "regulator-run-42-api"
    assert created["Labels"]["regulator.run"] == "42"
    task = created["TaskTemplate"]
    assert task["ContainerSpec"]["Image"].startswith("ghcr.io/livehybrid/regulator-worker@sha256:")
    assert "REG_RUN_JWT=tok.en" in task["ContainerSpec"]["Env"]
    assert task["Networks"] == [{"Target": "regulator_regulator_internal"}]
    assert task["Placement"]["Constraints"] == ["node.hostname != macdev"]
    assert task["RestartPolicy"] == {"Condition": "none"}
    assert task["ContainerSpec"]["StopGracePeriod"] == 90 * 1_000_000_000
    assert created["Mode"] == {"Replicated": {"Replicas": 3}}
    # Nothing secret in any request path or the error surfaces.
    assert all("secret-key" not in r["path"] for r in fake.requests)


def test_swarm_falls_back_to_the_tag_when_the_registry_is_unreachable(portainer):
    fake, driver = portainer
    fake.digest_ok = False
    driver.create(group())
    created = [r for r in fake.requests if r["path"].endswith("/services/create")][0]["body"]
    assert created["TaskTemplate"]["ContainerSpec"]["Image"] == "ghcr.io/livehybrid/regulator-worker:latest"


def test_swarm_stop_scales_to_zero_and_destroy_is_idempotent(portainer):
    fake, driver = portainer
    ref = driver.create(group())
    driver.stop(ref, grace_s=30)
    updated = [r for r in fake.requests if r["path"].endswith("/update")][0]
    assert updated["body"]["Mode"]["Replicated"]["Replicas"] == 0
    assert updated["params"]["version"] == "5"
    status = driver.status(ref)
    assert status.running == 2  # the shutdown task does not count
    assert driver.logs(ref, tail=10) == "hello"
    driver.destroy(ref)
    driver.destroy(ref)  # already gone: fine
    assert driver.status(ref).desired == 0


def test_swarm_lists_the_runs_it_owns(portainer):
    fake, driver = portainer
    driver.create(group(run_id=42))
    driver.create(group(run_id=43, group="browser"))
    assert driver.list_run_ids() == {42, 43}


def test_swarm_without_portainer_is_a_clear_error():
    driver = SwarmDriver(host=None, token=None)
    with pytest.raises(DriverError) as excinfo:
        driver.create(group())
    assert "PORTAINER_HOST" in str(excinfo.value)


# ---------------------------------------------------------------------- k8s


class FakeApi:
    """Records calls; raises like the kubernetes client on a missing job."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.secrets: Dict[str, Dict[str, Any]] = {}

    class ApiException(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"({status})")
            self.status = status

    def _record(self, call_name: str, **kwargs: Any) -> None:
        self.calls.append({"call": call_name, **kwargs})

    def create_namespaced_secret(self, namespace: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self._record("create_secret", namespace=namespace, body=body)
        self.secrets[body["metadata"]["name"]] = body
        return body

    def patch_namespaced_secret(self, name: str, namespace: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self._record("patch_secret", name=name, body=body)
        return body

    def delete_namespaced_secret(self, name: str, namespace: str) -> None:
        self._record("delete_secret", name=name)
        self.secrets.pop(name, None)

    def create_namespaced_job(self, namespace: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self._record("create_job", namespace=namespace, body=body)
        self.jobs[body["metadata"]["name"]] = body
        return {"metadata": {"uid": "uid-1", "name": body["metadata"]["name"]}}

    def read_namespaced_job(self, name: str, namespace: str) -> Dict[str, Any]:
        if name not in self.jobs:
            raise self.ApiException(404)
        return {"spec": {"parallelism": self.jobs[name]["spec"]["parallelism"]}, "status": {"active": 1}}

    def delete_namespaced_job(self, name: str, namespace: str, **kwargs: Any) -> None:
        self._record("delete_job", name=name, **kwargs)
        if name not in self.jobs:
            raise self.ApiException(404)
        del self.jobs[name]

    def list_namespaced_pod(self, namespace: str, label_selector: str) -> Dict[str, Any]:
        return {"items": [
            {"metadata": {"name": "pod-0"}, "status": {"phase": "Running"}, "spec": {"nodeName": "ip-1"}},
            {"metadata": {"name": "pod-1"}, "status": {"phase": "Succeeded"}, "spec": {"nodeName": "ip-2"}},
        ]}

    def read_namespaced_pod_log(self, name: str, namespace: str, tail_lines: Any = None) -> str:
        return f"log of {name}"

    def list_namespaced_job(self, namespace: str, label_selector: str) -> Dict[str, Any]:
        return {"items": [{"metadata": {"labels": {"regulator.run": name.split("-")[2]}}} for name in self.jobs]}


@pytest.fixture
def k8s():
    api = FakeApi()
    driver = K8sDriver(namespace="regulator", node_selector={"workload": "regulator"}, batch_api=api, core_api=api)
    return api, driver


def test_k8s_creates_an_indexed_job_with_the_token_in_a_secret(k8s):
    api, driver = k8s
    ref = driver.create(group(workers=4))
    assert ref.id == job_name(42, "api") == "regulator-run-42-api"
    secret = api.secrets[secret_name(42, "api")]
    assert secret["stringData"] == {"run-token": "tok.en"}
    job = api.jobs[ref.id]
    spec = job["spec"]
    assert spec["completionMode"] == "Indexed"
    assert spec["parallelism"] == 4 and spec["completions"] == 4
    assert spec["activeDeadlineSeconds"] == 900
    pod = spec["template"]["spec"]
    assert pod["nodeSelector"] == {"workload": "regulator"}
    assert pod["tolerations"][0]["key"] == "workload"
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    assert container["imagePullPolicy"] == "Always"
    env = {item["name"]: item for item in container["env"]}
    assert env["REG_RUN_JWT"]["valueFrom"]["secretKeyRef"]["name"] == secret_name(42, "api")
    assert "value" not in env["REG_RUN_JWT"]
    assert env["REG_CONTROL_URL"]["value"] == "http://regulator:8080"
    assert "REG_HINT_SLOT" in env and "REG_HOLDER" in env
    # The secret was adopted by the job so it is garbage collected with it.
    adopted = [c for c in api.calls if c["call"] == "patch_secret"][0]
    assert adopted["body"]["metadata"]["ownerReferences"][0]["uid"] == "uid-1"


def test_k8s_browser_group_gets_shared_memory(k8s):
    api, driver = k8s
    ref = driver.create(group(group="browser", image="browser-img"))
    pod = api.jobs[ref.id]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["name"] == "dshm"
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/dev/shm"


def test_k8s_status_stop_and_destroy(k8s):
    api, driver = k8s
    ref = driver.create(group(workers=2))
    status = driver.status(ref)
    assert status.desired == 2
    assert status.running == 1
    assert "log of pod-0" in driver.logs(ref, tail=5)
    assert driver.list_run_ids() == {42}
    driver.stop(ref, grace_s=30)
    stopped = [c for c in api.calls if c["call"] == "delete_job"][0]
    assert stopped["propagation_policy"] == "Foreground" and stopped["grace_period_seconds"] == 30
    # Already gone: destroy and a second stop are no-ops rather than errors.
    driver.destroy(ref)
    driver.stop(ref, grace_s=30)
    assert driver.status(ref).desired == 0


def test_k8s_cleans_up_the_secret_when_the_job_cannot_be_created(k8s):
    api, driver = k8s

    def broken(namespace: str, body: Dict[str, Any]) -> Dict[str, Any]:
        raise FakeApi.ApiException(500)

    api.create_namespaced_job = broken  # type: ignore[assignment]
    with pytest.raises(DriverError):
        driver.create(group())
    assert secret_name(42, "api") not in api.secrets
