"""Kubernetes: one Indexed Job per worker group.

The Job named ``regulator-run-<id>-<group>`` carries ``regulator.run=<id>``,
``parallelism == completions == N``, ``completionMode: Indexed`` so each pod
learns its index and offers it as a slot hint, ``restartPolicy: Never`` so a
finished worker is finished, and a ``ttlSecondsAfterFinished`` stray-catcher.
The per-run token rides in an ephemeral Secret owned by the Job, projected
through ``secretKeyRef``, never inline in the pod spec. Pods carry no service
account token, pull fresh images, and land on the node group the settings
name.

The client library is imported lazily: a control plane that never launches a
Kubernetes fleet never loads it, and the request shapes are unit tested
against injected fakes with no cluster anywhere.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from .base import DriverError, DriverRef, DriverStatus, NotFound, WorkerGroup

log = logging.getLogger("regulator.driver.k8s")

LABEL = "regulator.run"
_RUNNING = frozenset({"Running"})
TOKEN_ENV = "REG_RUN_JWT"
# The UID each worker image actually runs as. runAsNonRoot alone is not enough:
# both images declare a NAME in their Dockerfile USER line (regulator, pwuser),
# and the kubelet refuses to start a container when it cannot prove the user is
# not root -- "image has non-numeric user, cannot verify user is non-root".
# Numbers, not names, are what make that check pass.
DEFAULT_WORKER_UID = 10011        # worker/Dockerfile: useradd --uid 10011
DEFAULT_BROWSER_WORKER_UID = 1000  # playwright base image's pwuser
TOKEN_KEY = "run-token"
# The logs endpoint returns at most this much per group: a worker writes its
# whole summary, histograms included, as one stdout line.
_LOG_BYTES_CAP = 2 * 1024 * 1024


_TOLERATION_KEYS = ("key", "operator", "value", "effect", "tolerationSeconds")


def _clean_tolerations(tolerations: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Normalise tolerations into pod-spec objects.

    Accepts what :func:`config._parse_tolerations` stores, ``(key, operator,
    value, effect)`` quads with ``""`` for absent parts, and also plain dicts
    so a run's ``options["tolerations"]`` can pass them straight through. Only
    whitelisted keys survive, so a malformed or hostile entry cannot smuggle
    extra pod-spec fields in. Anything unusable is dropped rather than raising:
    the boot-time parser is where a bad value should have been caught.
    """
    if not tolerations:
        return []
    cleaned: List[Dict[str, Any]] = []
    for entry in tolerations:
        if isinstance(entry, dict):
            item = {k: v for k, v in entry.items() if k in _TOLERATION_KEYS and v not in (None, "")}
            if item.get("key") or item.get("operator") == "Exists":
                cleaned.append(item)
            continue
        if isinstance(entry, (tuple, list)) and len(entry) == 4:
            key, operator, value, effect = (str(p or "") for p in entry)
            if not key:
                continue
            item = {"key": key, "operator": operator or "Exists"}
            if value:
                item["value"] = value
            if effect:
                item["effect"] = effect
            cleaned.append(item)
    return cleaned


def job_name(run_id: int, group: str) -> str:
    return f"regulator-run-{run_id}-{group}"


def secret_name(run_id: int, group: str) -> str:
    return f"regulator-run-{run_id}-{group}-token"


class K8sDriver:
    kind = "k8s"

    def __init__(
        self,
        namespace: str = "regulator",
        node_selector: Optional[Dict[str, str]] = None,
        tolerations: Optional[Sequence[Any]] = None,
        worker_uid: int = DEFAULT_WORKER_UID,
        browser_worker_uid: int = DEFAULT_BROWSER_WORKER_UID,
        batch_api: Any = None,
        core_api: Any = None,
        kubeconfig: Optional[str] = None,
        context: Optional[str] = None,
        in_cluster: Optional[bool] = None,
        service_url: Optional[str] = None,
    ) -> None:
        self._namespace = namespace or "regulator"
        self._node_selector = dict(node_selector or {})
        self._tolerations = _clean_tolerations(tolerations)
        self._worker_uid = int(worker_uid)
        self._browser_worker_uid = int(browser_worker_uid)
        self._batch = batch_api
        self._core = core_api
        self._kubeconfig = kubeconfig
        self._context = context
        self._in_cluster = in_cluster
        self.service_url = service_url or f"http://regulator.{self._namespace}.svc:8080"

    # --------------------------------------------------------------- clients

    def _load_config(self) -> None:
        from kubernetes import config as kube_config  # type: ignore

        if self._in_cluster:
            kube_config.load_incluster_config()
            return
        if self._in_cluster is False or self._kubeconfig:
            kube_config.load_kube_config(config_file=self._kubeconfig, context=self._context)
            return
        try:
            kube_config.load_kube_config(context=self._context)
        except Exception:  # noqa: BLE001 - no kubeconfig: try the pod's own account
            kube_config.load_incluster_config()

    def _batch_api(self) -> Any:
        if self._batch is None:
            from kubernetes import client  # type: ignore

            self._load_config()
            self._batch = client.BatchV1Api()
        return self._batch

    def _core_api(self) -> Any:
        if self._core is None:
            from kubernetes import client  # type: ignore

            self._load_config()
            self._core = client.CoreV1Api()
        return self._core

    @staticmethod
    def _call(function: Any, **kwargs: Any) -> Any:
        try:
            return function(**kwargs)
        except Exception as exc:  # noqa: BLE001 - the client raises its own ApiException
            status = getattr(exc, "status", None)
            if status == 404:
                raise NotFound(f"kubernetes: {getattr(function, '__name__', 'call')} -> 404") from exc
            raise DriverError(f"kubernetes: {getattr(function, '__name__', 'call')} failed: {exc}") from exc

    # ---------------------------------------------------------------- driver

    def create(self, group: WorkerGroup) -> DriverRef:
        if group.workers < 1:
            raise DriverError("workers must be at least 1")
        namespace = self._namespace
        name = job_name(group.run_id, group.group)
        token_secret = secret_name(group.run_id, group.group)

        env = dict(group.env)
        token = group.secrets.get(TOKEN_ENV) or env.pop(TOKEN_ENV, None)
        env.pop(TOKEN_ENV, None)
        env["REG_SLOT_BASE"] = str(int(group.options.get("slot_base") or 0))
        env["REG_WORKER_ENGINE"] = group.group
        if token:
            self._call(
                self._core_api().create_namespaced_secret,
                namespace=namespace,
                body={
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": token_secret, "labels": {LABEL: str(group.run_id)}},
                    "type": "Opaque",
                    "stringData": {TOKEN_KEY: token},
                },
            )
        job = self._job_manifest(group, env, token_secret if token else None)
        log.info("kubernetes: creating job %s in %s with %d pod(s) of %s", name, namespace, group.workers, group.image)
        try:
            created = self._call(self._batch_api().create_namespaced_job, namespace=namespace, body=job)
        except DriverError:
            if token:
                self._delete_secret_quietly(namespace, token_secret)
            raise
        uid = _uid_of(created)
        if token and uid:
            self._adopt_secret(namespace, token_secret, name, uid)
        return DriverRef(
            kind=self.kind,
            id=name,
            group=group.group,
            raw={"run_id": group.run_id, "namespace": namespace, "secret": token_secret if token else None},
        )

    def _job_manifest(self, group: WorkerGroup, env: Dict[str, str], token_secret: Optional[str]) -> Dict[str, Any]:
        labels = {LABEL: str(group.run_id), "regulator.group": group.group, "app.kubernetes.io/name": "regulator-worker", **(group.labels or {})}
        env_items: List[Dict[str, Any]] = [
            {"name": key, "value": str(value)} for key, value in sorted(env.items()) if value is not None
        ]
        if token_secret:
            env_items.append({"name": TOKEN_ENV, "valueFrom": {"secretKeyRef": {"name": token_secret, "key": TOKEN_KEY}}})
        # The pod's completion index is its slot hint.
        env_items.append({"name": "REG_HINT_SLOT", "valueFrom": {"fieldRef": {"fieldPath": "metadata.annotations['batch.kubernetes.io/job-completion-index']"}}})
        env_items.append({"name": "REG_HOLDER", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}})
        container: Dict[str, Any] = {
            "name": "worker",
            "image": group.image,
            "imagePullPolicy": "Always",
            "env": env_items,
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "resources": group.options.get("resources") or {
                "requests": {"cpu": "1", "memory": "1Gi" if group.group == "api" else "4Gi"},
                "limits": {"memory": "2Gi" if group.group == "api" else "6Gi"},
            },
        }
        volumes: List[Dict[str, Any]] = []
        if group.group == "browser":
            container["volumeMounts"] = [{"name": "dshm", "mountPath": "/dev/shm"}]
            volumes.append({"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}})
        run_as_user = int(
            group.options.get("run_as_user")
            or (self._browser_worker_uid if group.group == "browser" else self._worker_uid)
        )
        pod: Dict[str, Any] = {
            "metadata": {"labels": dict(labels)},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "terminationGracePeriodSeconds": int(group.stop_grace_s),
                "securityContext": {
                    "runAsNonRoot": True,
                    # Without an explicit numeric UID the kubelet cannot verify
                    # the image's named user is non-root and refuses to start
                    # the container (CreateContainerConfigError), so a
                    # Kubernetes fleet never gets a worker running at all.
                    "runAsUser": run_as_user,
                    "runAsGroup": run_as_user,
                    "fsGroup": run_as_user,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [container],
                "volumes": volumes,
            },
        }
        selector = dict(self._node_selector)
        selector.update(group.options.get("node_selector") or {})
        if selector:
            pod["spec"]["nodeSelector"] = selector
        # Placement and admission are two different questions. nodeSelector
        # matches node LABELS and says where the pod MAY go; a toleration
        # matches node TAINTS and says whether a reserved node will ACCEPT it.
        # They coincide only when an estate happens to label and taint with the
        # same key=value, which is exactly the assumption that used to leave
        # workers Pending forever against a node group labelled one way and
        # tainted another.
        #
        # An explicit toleration list (REG_K8S_TOLERATIONS, or a group's own
        # options) therefore wins outright. With none given we still derive one
        # NoSchedule toleration per selector pair, which is what deployments
        # configured before this existed rely on.
        tolerations = _clean_tolerations(group.options.get("tolerations")) or self._tolerations
        if not tolerations and selector:
            tolerations = [
                {"key": key, "operator": "Equal", "value": value, "effect": "NoSchedule"}
                for key, value in selector.items()
            ]
        if tolerations:
            pod["spec"]["tolerations"] = [dict(t) for t in tolerations]
        deadline = group.options.get("active_deadline_s")
        spec: Dict[str, Any] = {
            "completionMode": "Indexed",
            "parallelism": int(group.workers),
            "completions": int(group.workers),
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "template": pod,
        }
        if deadline:
            spec["activeDeadlineSeconds"] = int(deadline)
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name(group.run_id, group.group), "labels": labels},
            "spec": spec,
        }

    def _adopt_secret(self, namespace: str, secret: str, job: str, uid: str) -> None:
        try:
            self._call(
                self._core_api().patch_namespaced_secret,
                name=secret,
                namespace=namespace,
                body={"metadata": {"ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": job, "uid": uid, "blockOwnerDeletion": False}]}},
            )
        except DriverError as exc:
            log.warning("kubernetes: could not adopt secret %s under job %s: %s", secret, job, exc)

    def _delete_secret_quietly(self, namespace: str, secret: str) -> None:
        try:
            self._call(self._core_api().delete_namespaced_secret, name=secret, namespace=namespace)
        except DriverError:
            pass

    def stop(self, ref: DriverRef, grace_s: int) -> None:
        namespace = (ref.raw or {}).get("namespace") or self._namespace
        try:
            self._call(
                self._batch_api().delete_namespaced_job,
                name=ref.id, namespace=namespace, propagation_policy="Foreground", grace_period_seconds=int(grace_s),
            )
        except NotFound:
            return
        log.info("kubernetes: stopping job %s (grace %ds)", ref.id, grace_s)

    def destroy(self, ref: DriverRef) -> None:
        namespace = (ref.raw or {}).get("namespace") or self._namespace
        # The secret goes whether or not the job is still there: a stop has
        # usually deleted the job already, and the owner reference that would
        # have collected the secret is best effort.
        secret = (ref.raw or {}).get("secret")
        if secret:
            self._delete_secret_quietly(namespace, secret)
        try:
            self._call(self._batch_api().delete_namespaced_job, name=ref.id, namespace=namespace, propagation_policy="Foreground")
        except NotFound:
            return
        log.info("kubernetes: removed job %s", ref.id)

    def status(self, ref: DriverRef) -> DriverStatus:
        namespace = (ref.raw or {}).get("namespace") or self._namespace
        try:
            job = self._call(self._batch_api().read_namespaced_job, name=ref.id, namespace=namespace)
        except NotFound:
            return DriverStatus(desired=0, running=0)
        desired = int(_get(job, "spec", "parallelism") or 0)
        tasks: List[Dict[str, Any]] = []
        running: Optional[int] = None
        try:
            pods = self._call(
                self._core_api().list_namespaced_pod, namespace=namespace, label_selector=f"{LABEL}={(ref.raw or {}).get('run_id')}"
            )
            for pod in _items(pods):
                phase = _get(pod, "status", "phase")
                tasks.append({"pod": _get(pod, "metadata", "name"), "state": phase, "node": _get(pod, "spec", "node_name") or _get(pod, "spec", "nodeName")})
            running = sum(1 for t in tasks if t["state"] in _RUNNING)
        except DriverError as exc:
            log.warning("kubernetes: pod list for %s unavailable: %s", ref.id, exc)
        if running is None:
            running = int(_get(job, "status", "active") or 0)
        return DriverStatus(desired=desired, running=running, tasks=tasks)

    def logs(self, ref: DriverRef, tail: int) -> str:
        namespace = (ref.raw or {}).get("namespace") or self._namespace
        selector = f"{LABEL}={(ref.raw or {}).get('run_id')},regulator.group={ref.group}"
        try:
            pods = self._call(
                self._core_api().list_namespaced_pod, namespace=namespace, label_selector=selector, _request_timeout=20
            )
        except DriverError as exc:
            log.warning("kubernetes: logs for %s unavailable: %s", ref.id, exc)
            return ""
        chunks: List[str] = []
        budget = _LOG_BYTES_CAP
        for pod in _items(pods):
            name = _get(pod, "metadata", "name")
            if not name or budget <= 0:
                continue
            try:
                text = self._call(
                    self._core_api().read_namespaced_pod_log,
                    name=name,
                    namespace=namespace,
                    tail_lines=tail if tail > 0 else None,
                    limit_bytes=budget,
                    _request_timeout=20,
                )
            except DriverError:
                continue
            if text:
                budget -= len(text)
                chunks.append(f"--- {name}\n{text}")
        return "\n".join(chunks)

    def list_run_ids(self) -> Set[int]:
        jobs = self._call(self._batch_api().list_namespaced_job, namespace=self._namespace, label_selector=LABEL)
        found: Set[int] = set()
        for job in _items(jobs):
            raw = (_get(job, "metadata", "labels") or {}).get(LABEL)
            try:
                if raw is not None:
                    found.add(int(raw))
            except (TypeError, ValueError):
                continue
        return found


def _get(obj: Any, *path: str) -> Any:
    """Read a nested attribute or key from a client model or a plain dict."""
    current = obj
    for part in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def _items(listing: Any) -> List[Any]:
    items = _get(listing, "items")
    return list(items) if isinstance(items, (list, tuple)) else []


def _uid_of(created: Any) -> str:
    return str(_get(created, "metadata", "uid") or "")
