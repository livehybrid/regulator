"""A shared copy of the configuration, and the run history, in S3.

:mod:`.configio` made an instance's configuration a JSON document and
``REG_CONFIG_IMPORT`` restores one at boot, but only from a mounted file or
inline JSON, and run history did not travel at all, so a rebuilt instance lost
every baseline its CI gate compares against. This keeps both in the kind of
place the scenario library already lives, so a fresh or rebuilt instance comes
up configured and with its history, and an engineer starting a test does not
re-enter targets first.

``REG_CONFIG_SOURCE``
    ``s3://bucket/prefix`` or a directory. Under it, ``regulator-config.json``
    (targets, the operator's scenarios, baselines) and ``runs/<uid>.json``, one
    per finished run in the format the run page's Download already produces.
    Pulled at boot, after the scenario source, and on demand from the
    Configuration page.

``REG_CONFIG_SOURCE_WRITE=1``
    Also push: the configuration whenever it changes, and every run as it
    finishes. Off by default and a separate switch, as for the scenario source:
    several instances reading one copy is the common case, and one of them
    quietly overwriting it is hard to notice.

``REG_CONFIG_SOURCE_SSE`` / ``REG_CONFIG_SOURCE_SSE_KMS_KEY_ID``
    Server-side encryption headers for each upload, for a bucket policy that
    denies PutObject without them (``StringNotEquals`` on the header matches
    when it is absent, and S3 does not fill it in from the bucket's default
    encryption). Unset, the request is unchanged.

**A pull never removes anything.** The configuration is the existing upsert by
name; a run is imported only if no run here has its uid. Baselines are applied
after the runs are in, since a baseline is a pointer into them.

**Push is the whole document, secrets included as ciphertext**, so a restore
needs the same ``REG_MASTER_KEY``. A change is noticed by comparing the
document with the last one pulled or pushed every few seconds, rather than by
hooking each route that edits a target, scenario or baseline: a route added
later cannot forget to back up.

**A row the pull could not apply is carried, not dropped**: pushing this
instance's view verbatim would delete it from the shared copy on the first
unrelated edit. Rows a pull skipped are kept in every push until this instance
has them itself.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import posixpath
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import configio, runarchive
from .adapters import user_scenarios_dir
from .db import session_scope
from .models import Baseline, Run, Target, TERMINAL_STATES

log = logging.getLogger("regulator.server.configsource")

SOURCE_ENV = "REG_CONFIG_SOURCE"
WRITE_ENV = "REG_CONFIG_SOURCE_WRITE"
SSE_ENV = "REG_CONFIG_SOURCE_SSE"
SSE_KEY_ENV = "REG_CONFIG_SOURCE_SSE_KMS_KEY_ID"

CONFIG_OBJECT = "regulator-config.json"
RUNS_PREFIX = "runs/"

#: How often the publisher compares the configuration and looks for new runs.
PUSH_INTERVAL_S = 15.0
#: A run is archived this long after it ends: the last samples and the
#: cluster's own account are written just after the state turns terminal.
ARCHIVE_SETTLE_S = 30.0


class ConfigSourceError(Exception):
    """A config source that cannot be read or written."""


# --------------------------------------------------------------------------- #
# Stores. Nested keys, unlike the scenario source's flat set of archives.
# --------------------------------------------------------------------------- #

class Store:
    def get(self, key: str) -> Optional[bytes]:
        """The object's bytes, or None when there is no such object."""
        raise NotImplementedError

    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def list(self, prefix: str) -> List[str]:
        """Keys under ``prefix``, relative to the store, one level deep."""
        raise NotImplementedError


class DirectoryStore(Store):
    """A local directory: a mounted volume, an NFS share, and the tests."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _path(self, key: str) -> str:
        return os.path.join(self.path, *key.split("/"))

    def get(self, key: str) -> Optional[bytes]:
        try:
            with open(self._path(key), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Write then rename, so a reader never sees half a document.
        temporary = path + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def list(self, prefix: str) -> List[str]:
        directory = self._path(prefix.rstrip("/"))
        if not os.path.isdir(directory):
            return []
        return sorted(
            prefix + name for name in os.listdir(directory)
            if not name.endswith(".tmp") and os.path.isfile(os.path.join(directory, name))
        )


class S3Store(Store):
    """An ``s3://bucket/prefix``. Credentials come from boto3's own chain."""

    def __init__(self, bucket: str, prefix: str, client: Optional[Any] = None,
                 sse: Optional[Dict[str, str]] = None) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client
        self._sse = sse or {}

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415 - optional dependency, by design
            except ImportError:
                raise ConfigSourceError(
                    f"{SOURCE_ENV} names an s3:// source but boto3 is not installed in this image"
                )
            self._client = boto3.client("s3")
        return self._client

    def _key(self, name: str) -> str:
        return posixpath.join(self.prefix, name) if self.prefix else name

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except Exception as exc:  # noqa: BLE001 - botocore's error types are optional
            code = str(((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", ""))
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._key(key), Body=data,
            ContentType="application/json", **self._sse,
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def list(self, prefix: str) -> List[str]:
        names: List[str] = []
        full = self._key(prefix)
        token = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": full}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                relative = obj["Key"][len(full):]
                if relative and "/" not in relative:
                    names.append(prefix + relative)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return sorted(names)


def _sse_args(env: Dict[str, str]) -> Dict[str, str]:
    algorithm = (env.get(SSE_ENV) or "").strip()
    if not algorithm:
        return {}
    args = {"ServerSideEncryption": algorithm}
    key_id = (env.get(SSE_KEY_ENV) or "").strip()
    if key_id:
        # Omitted for AES256, which takes no key and rejects an empty one.
        args["SSEKMSKeyId"] = key_id
    return args


def open_store(source: str, env: Optional[Dict[str, str]] = None) -> Store:
    env = env if env is not None else dict(os.environ)
    source = (source or "").strip()
    if not source:
        raise ConfigSourceError("empty config source")
    parsed = urlparse(source)
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise ConfigSourceError(f"config source {source!r} has no bucket")
        return S3Store(parsed.netloc, parsed.path, sse=_sse_args(env))
    if parsed.scheme in ("", "file"):
        return DirectoryStore(parsed.path if parsed.scheme == "file" else source)
    raise ConfigSourceError(
        f"config source {source!r}: only s3:// and local directories are supported"
    )


def store_from_env(env: Optional[Dict[str, str]] = None) -> Optional[Store]:
    env = env if env is not None else dict(os.environ)
    source = (env.get(SOURCE_ENV) or "").strip()
    return open_store(source, env) if source else None


def writes_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    env = env if env is not None else dict(os.environ)
    return (env.get(WRITE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# What it last did
# --------------------------------------------------------------------------- #

# Each section of the document and the field that names a row in it.
_SECTIONS = (("targets", "name"), ("scenarios", "name"), ("baselines", "label"))

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "synced": None,   # digest of the document last pulled or pushed
    "carried": {},    # rows the last pull could not apply, by section and key
    "last_pull": None,
    "last_push": None,
}


def _reset_for_tests() -> None:
    with _lock:
        _state.update(synced=None, carried={}, last_pull=None, last_push=None)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def describe(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Where the copy lives and what it last did, for the console.

    ``location`` names a bucket and prefix and never a credential.
    """
    env = env if env is not None else dict(os.environ)
    source = (env.get(SOURCE_ENV) or "").strip()
    with _lock:
        out: Dict[str, Any] = {
            "configured": bool(source),
            "writable": False,
            "kind": None,
            "location": source or None,
            "object": CONFIG_OBJECT,
            "error": None,
            "last_pull": _state["last_pull"],
            "last_push": _state["last_push"],
            "carried": sum(len(rows) for rows in _state["carried"].values()),
        }
    if not source:
        return out
    try:
        store = open_store(source, env)
    except ConfigSourceError as exc:
        out["error"] = str(exc)
        return out
    out["kind"] = "s3" if isinstance(store, S3Store) else "directory"
    out["writable"] = writes_enabled(env)
    return out


def _digest(doc: Dict[str, Any]) -> str:
    """A hash of what the document says, not of when or in what order."""
    body = {k: v for k, v in doc.items() if k not in ("exported_at", "warnings")}
    for section, key in _SECTIONS:
        if isinstance(body.get(section), list):
            body[section] = sorted(
                body[section], key=lambda row: str(row.get(key) if isinstance(row, dict) else row)
            )
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _present(session: Session, section: str, name: str) -> bool:
    if section == "targets":
        return session.scalar(select(Target.id).where(Target.name == name)) is not None
    if section == "scenarios":
        return (user_scenarios_dir() / name / "scenario.yaml").is_file()
    return session.get(Baseline, name) is not None


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #

def pull_runs(session: Session, store: Store) -> Dict[str, Any]:
    """Import every archived run this instance does not already have."""
    have = set(session.scalars(select(Run.uid).where(Run.uid.isnot(None))).all())
    report: Dict[str, Any] = {"imported": 0, "present": 0, "failed": []}
    for key in store.list(RUNS_PREFIX):
        if not key.endswith(".json"):
            continue
        uid = key[len(RUNS_PREFIX):-len(".json")]
        if uid in have:
            report["present"] += 1
            continue
        try:
            data = store.get(key)
            if data is None:
                continue
            run = runarchive.import_document(session, json.loads(data.decode("utf-8")), uid=uid)
        except (runarchive.RunArchiveError, ValueError, UnicodeDecodeError) as exc:
            report["failed"].append(f"{key}: {exc}")
            continue
        if run is None:
            report["present"] += 1
        else:
            have.add(run.uid)
            report["imported"] += 1
    return report


def pull(session: Session, settings: Optional[Any] = None, store: Optional[Store] = None) -> Dict[str, Any]:
    """Apply the shared copy and its runs to this instance. The caller commits."""
    store = store if store is not None else store_from_env()
    if store is None:
        raise ConfigSourceError(f"{SOURCE_ENV} is not set")
    try:
        data = store.get(CONFIG_OBJECT)
    except Exception as exc:  # noqa: BLE001 - reported, never a 500
        with _lock:
            _state["last_pull"] = {"at": _now(), "ok": False, "error": str(exc)}
        raise ConfigSourceError(f"could not read {CONFIG_OBJECT}: {exc}")

    doc: Optional[Dict[str, Any]] = None
    if data is not None:
        try:
            doc = configio.load_document(data.decode("utf-8"))
        except (configio.ConfigError, UnicodeDecodeError) as exc:
            with _lock:
                _state["last_pull"] = {"at": _now(), "ok": False, "error": str(exc)}
            raise ConfigSourceError(f"{CONFIG_OBJECT} is not a usable config: {exc}")

    report: Dict[str, Any] = {
        "found": doc is not None, "targets": 0, "targets_updated": 0, "scenarios": 0,
        "scenarios_updated": 0, "baselines": 0, "baselines_updated": 0,
        "baselines_waiting": [], "skipped": [], "warnings": [],
    }
    if doc is not None:
        report.update(configio.import_config(session, doc, settings=settings))
        report["found"] = True
    try:
        report["runs"] = pull_runs(session, store)
    except Exception as exc:  # noqa: BLE001 - the configuration still applied
        report["runs"] = {"imported": 0, "present": 0, "failed": [f"listing runs: {exc}"]}
    if doc is not None:
        # Now the runs are in, the baselines that point at them can land.
        report["baselines_waiting"] = []
        configio.import_baselines(session, doc, report)

    carried: Dict[str, Dict[str, Any]] = {}
    for section, key in _SECTIONS:
        for row in (doc or {}).get(section) or []:
            name = str(row.get(key) or "").strip() if isinstance(row, dict) else ""
            if name and not _present(session, section, name):
                carried.setdefault(section, {})[name] = row
    with _lock:
        _state["carried"] = carried
        if doc is not None:
            _state["synced"] = _digest(doc)
        _state["last_pull"] = {
            "at": _now(), "ok": True, "found": doc is not None,
            "created": {
                "targets": report["targets"], "scenarios": report["scenarios"],
                "baselines": report["baselines"], "runs": report["runs"]["imported"],
            },
            "skipped": len(report["skipped"]) + len(report["runs"]["failed"]),
        }
    return report


def load_from_env(settings: Optional[Any] = None, env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Pull at boot when ``REG_CONFIG_SOURCE`` is set. Never raises.

    An unreachable bucket must degrade to "this instance starts unconfigured",
    not "this instance does not start".
    """
    env = env if env is not None else dict(os.environ)
    if not (env.get(SOURCE_ENV) or "").strip():
        return None
    try:
        with session_scope() as session:
            report = pull(session, settings, store=store_from_env(env))
    except Exception as exc:  # noqa: BLE001 - startup must survive anything here
        log.error("config source pull failed: %s", exc)
        with _lock:
            _state["last_pull"] = {"at": _now(), "ok": False, "error": str(exc)}
        return {"error": str(exc)}
    if not report["found"]:
        log.info("config source: no %s yet", CONFIG_OBJECT)
    log.info(
        "config source: %d target(s), %d scenario(s), %d baseline(s) and %d run(s) pulled",
        report["targets"], report["scenarios"], report["baselines"], report["runs"]["imported"],
    )
    for line in report["warnings"] + report["skipped"] + report["runs"]["failed"]:
        log.warning("config source: %s", line)
    if report["baselines_waiting"]:
        log.warning(
            "config source: baseline(s) %s name runs that are not archived in the source",
            ", ".join(report["baselines_waiting"]),
        )
    return report


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #

def document(session: Session, settings: Optional[Any] = None) -> Dict[str, Any]:
    """This instance's configuration plus any rows the last pull carried."""
    doc = configio.export_config(session, include_secrets=True, settings=settings)
    doc.pop("warnings", None)
    with _lock:
        carried = {section: dict(rows) for section, rows in _state["carried"].items()}
    for section, key in _SECTIONS:
        rows = carried.get(section) or {}
        if not rows:
            continue
        present = {str(row.get(key) or "") for row in doc[section]}
        kept = {}
        for name, row in rows.items():
            if name in present:
                continue  # this instance has it now; its own version wins.
            doc[section].append(row)
            kept[name] = row
        with _lock:
            if kept:
                _state["carried"][section] = kept
            else:
                _state["carried"].pop(section, None)
    return doc


def push_config(session: Session, settings: Optional[Any] = None, store: Optional[Store] = None) -> Dict[str, Any]:
    store = store if store is not None else store_from_env()
    if store is None:
        raise ConfigSourceError(f"{SOURCE_ENV} is not set")
    doc = document(session, settings)
    try:
        store.put(CONFIG_OBJECT, (json.dumps(doc, indent=2, default=str) + "\n").encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported on the page, not a 500
        with _lock:
            _state["last_push"] = {"at": _now(), "ok": False, "error": str(exc)}
        raise ConfigSourceError(f"could not write {CONFIG_OBJECT}: {exc}")
    counts = {section: len(doc[section]) for section, _key in _SECTIONS}
    with _lock:
        _state["synced"] = _digest(doc)
        previous = _state["last_push"] or {}
        _state["last_push"] = {"at": _now(), "ok": True, "counts": counts,
                               "runs": previous.get("runs", 0)}
    return counts


def archive_runs(session: Session, store: Store, force: bool = False) -> int:
    """Write every finished run not yet in the source. Returns how many."""
    settled = time.time() - (0 if force else ARCHIVE_SETTLE_S)
    pending = session.scalars(
        select(Run)
        .where(Run.state.in_(TERMINAL_STATES), Run.archived_at.is_(None))
        .order_by(Run.id)
    ).all()
    written = 0
    for run in pending:
        if run.ended_at is not None and run.ended_at > settled:
            continue
        if not run.uid:
            continue
        doc = runarchive.build_document(session, run, run.target, points=100_000)
        try:
            store.put(f"{RUNS_PREFIX}{run.uid}.json",
                      json.dumps(doc, default=str).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            with _lock:
                _state["last_push"] = {"at": _now(), "ok": False, "error": f"run {run.id}: {exc}"}
            raise ConfigSourceError(f"could not archive run {run.id}: {exc}")
        run.archived_at = time.time()
        written += 1
    if written:
        session.flush()
        with _lock:
            last = dict(_state["last_push"] or {"at": _now(), "ok": True})
            last["runs"] = int(last.get("runs") or 0) + written
            last["at"] = _now()
            _state["last_push"] = last
    return written


def push(session: Session, settings: Optional[Any] = None, store: Optional[Store] = None) -> Dict[str, Any]:
    """Archive finished runs and write the configuration, now."""
    store = store if store is not None else store_from_env()
    if store is None:
        raise ConfigSourceError(f"{SOURCE_ENV} is not set")
    runs = archive_runs(session, store, force=True)
    counts = push_config(session, settings, store)
    return {"pushed": True, "runs": runs, **counts}


def maybe_push(session: Session, settings: Optional[Any] = None,
               env: Optional[Dict[str, str]] = None) -> bool:
    """Archive settled runs, and push the configuration if it changed."""
    if not writes_enabled(env):
        return False
    store = store_from_env(env)
    if store is None:
        return False
    # Runs first, so a baseline in the document never names a run that is not
    # in the source yet.
    wrote = archive_runs(session, store) > 0
    doc = document(session, settings)
    with _lock:
        unchanged = _state["synced"] == _digest(doc)
    if not unchanged:
        push_config(session, settings, store)
        wrote = True
    return wrote


def forget_run(uid: Optional[str]) -> None:
    """Remove a deleted run's archive, so the next pull does not bring it back.

    Only where this instance writes the source; elsewhere a deleted run returns
    at the next boot, which the run page's delete says.
    """
    if not uid or not writes_enabled():
        return
    try:
        store = store_from_env()
        if store is not None:
            store.delete(f"{RUNS_PREFIX}{uid}.json")
    except Exception as exc:  # noqa: BLE001 - a delete here must still succeed
        log.warning("could not remove run %s from the config source: %s", uid, exc)


# --------------------------------------------------------------------------- #
# The publisher
# --------------------------------------------------------------------------- #

_publisher: Optional[threading.Thread] = None


def _publish_forever(settings: Optional[Any]) -> None:
    while True:
        time.sleep(PUSH_INTERVAL_S)
        try:
            with session_scope() as session:
                maybe_push(session, settings)
        except Exception as exc:  # noqa: BLE001 - a failed backup never stops the loop
            log.warning("config source push failed: %s", exc)


def start_publisher(settings: Optional[Any] = None) -> bool:
    """Start the background publisher once per process, if writes are on."""
    global _publisher
    if not writes_enabled() or store_from_env() is None:
        return False
    with _lock:
        if _publisher is not None and _publisher.is_alive():
            return False
        _publisher = threading.Thread(
            target=_publish_forever, args=(settings,), name="regulator-config-source", daemon=True
        )
        _publisher.start()
    return True


__all__ = [
    "CONFIG_OBJECT", "ConfigSourceError", "PUSH_INTERVAL_S", "RUNS_PREFIX", "SOURCE_ENV",
    "WRITE_ENV", "archive_runs", "describe", "document", "forget_run", "load_from_env",
    "maybe_push", "pull", "pull_runs", "push", "push_config", "start_publisher",
    "store_from_env", "writes_enabled",
]
