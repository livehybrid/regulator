"""A shared scenario library in S3 (or a mounted directory).

The problem this solves is a deployment that nobody should have to hand-feed.
A fresh control plane comes up with the scenarios that ship in its image and
nothing else; everything an operator wrote lives in a volume, and getting it
onto a second instance meant copying files, which means shell access to both.
Point ``REG_SCENARIO_SOURCE`` at ``s3://bucket/prefix`` (or at a directory) and
the instance pulls that library at boot instead, in the same ``.tar.gz`` format
the console's Download and Upload already speak.

Two switches, not one. Reading is the common case: several instances share one
curated library and none of them should be able to change it. ``REG_SCENARIO_
SOURCE_WRITE=1`` additionally pushes scenarios created here back to the prefix,
and is separate precisely because one instance silently overwriting the shared
library would be hard to notice.

Importing is **create if absent**. A scenario already in the local library is
left alone, so an operator's edit survives a restart and the sync never
clobbers local work; the console's explicit Push is how a local edit goes the
other way. One unreadable object is reported against its own archive rather
than aborting the whole sync, because a single bad file in a bucket must not
stop an instance starting.

S3 credentials come from the usual boto3 chain (instance role, environment,
profile), so nothing about them belongs in Regulator's own configuration.
boto3 is imported lazily: an image that never points at S3 never loads it.

Adapted from Stoker's ``server/packsource.py`` (see docs/VENDOR-FROM-STOKER.md).
"""

from __future__ import annotations

import logging
import os
import posixpath
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .adapters import user_scenarios_dir
from .config import get_settings
from .scenarioarchive import (
    ARCHIVE_SUFFIX,
    ArchiveLimits,
    ScenarioArchiveError,
    archive_name,
    export_scenario_bytes,
    extract_scenario,
)

log = logging.getLogger("regulator.server.scenariosource")

SOURCE_ENV = "REG_SCENARIO_SOURCE"
WRITE_ENV = "REG_SCENARIO_SOURCE_WRITE"


class ScenarioSourceError(Exception):
    """A scenario source that cannot be read or written."""


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #

class Store:
    """Somewhere a flat set of ``<name>.tar.gz`` archives lives."""

    def list_archives(self) -> List[str]:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError


class DirectoryStore(Store):
    """A local directory: a mounted volume, an NFS share, and the tests."""

    def __init__(self, path: str) -> None:
        self.path = path

    def list_archives(self) -> List[str]:
        if not os.path.isdir(self.path):
            return []
        return sorted(
            name for name in os.listdir(self.path) if name.endswith(ARCHIVE_SUFFIX)
        )

    def get(self, key: str) -> bytes:
        with open(os.path.join(self.path, key), "rb") as handle:
            return handle.read()

    def put(self, key: str, data: bytes) -> None:
        os.makedirs(self.path, exist_ok=True)
        # Write then rename, so a reader never sees a half-written archive.
        temporary = os.path.join(self.path, key + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, os.path.join(self.path, key))


class S3Store(Store):
    """An ``s3://bucket/prefix``."""

    def __init__(self, bucket: str, prefix: str, client: Optional[Any] = None) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415 - optional dependency, by design
            except ImportError:
                raise ScenarioSourceError(
                    f"{SOURCE_ENV} names an s3:// source but boto3 is not installed in "
                    "this image"
                )
            self._client = boto3.client("s3")
        return self._client

    def _key(self, name: str) -> str:
        return posixpath.join(self.prefix, name) if self.prefix else name

    def list_archives(self) -> List[str]:
        names: List[str] = []
        token = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": self.bucket}
            if self.prefix:
                kwargs["Prefix"] = self.prefix + "/"
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if not key.endswith(ARCHIVE_SUFFIX):
                    continue
                # Only this prefix's own level: a nested "archive/" of old
                # versions should not be imported as live scenarios.
                relative = key[len(self.prefix) + 1 :] if self.prefix else key
                if "/" in relative:
                    continue
                names.append(relative)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return sorted(names)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._key(key), Body=data, ContentType="application/gzip"
        )


def open_store(source: str) -> Store:
    """A :class:`Store` for ``s3://bucket/prefix`` or a directory path."""
    source = (source or "").strip()
    if not source:
        raise ScenarioSourceError("empty scenario source")
    parsed = urlparse(source)
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise ScenarioSourceError(f"scenario source {source!r} has no bucket")
        return S3Store(parsed.netloc, parsed.path)
    if parsed.scheme in ("", "file"):
        return DirectoryStore(parsed.path if parsed.scheme == "file" else source)
    raise ScenarioSourceError(
        f"scenario source {source!r}: only s3:// and local directories are supported"
    )


def store_from_env(env: Optional[Dict[str, str]] = None) -> Optional[Store]:
    env = env if env is not None else dict(os.environ)
    source = (env.get(SOURCE_ENV) or "").strip()
    return open_store(source) if source else None


def writes_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    env = env if env is not None else dict(os.environ)
    return (env.get(WRITE_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def describe(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """What the source is, for the console to show.

    Without this the whole feature is invisible: scenarios would be pulled at
    boot and pushed on save because two environment variables happened to be
    set, which is no use to an operator asking "did that reach the bucket?".

    ``location`` carries no credentials by construction - an ``s3://`` url
    names a bucket and a prefix, and boto3 takes the credentials from its own
    chain. A source that cannot even be parsed reports ``error`` rather than
    raising, so a typo in a ConfigMap shows up on the page instead of 500ing it.
    """
    env = env if env is not None else dict(os.environ)
    source = (env.get(SOURCE_ENV) or "").strip()
    out: Dict[str, Any] = {
        "configured": bool(source),
        "writable": False,
        "kind": None,
        "location": source or None,
        "error": None,
    }
    if not source:
        return out
    try:
        store = open_store(source)
    except ScenarioSourceError as exc:
        out["error"] = str(exc)
        return out
    out["kind"] = "s3" if isinstance(store, S3Store) else "directory"
    out["writable"] = writes_enabled(env)
    return out


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #

def sync_from_store(
    store: Store,
    library: Optional[Path] = None,
    limits: Optional[ArchiveLimits] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Import every archive in ``store`` that the local library lacks.

    Returns a report. Never raises for one bad object: a single unreadable or
    malformed archive is recorded against its own name and the rest still
    import, because an instance that will not start over one corrupt file in a
    shared bucket is worse than an instance missing one scenario.
    """
    library = library or user_scenarios_dir()
    if limits is None:
        # Honour the deployment's own caps rather than the module defaults, so
        # an operator who raised them for uploads has raised them here too.
        try:
            limits = ArchiveLimits.from_settings(get_settings())
        except Exception:  # noqa: BLE001 - settings are optional for a unit test
            limits = None
    report: Dict[str, Any] = {"imported": [], "skipped": [], "failed": {}}
    try:
        keys = store.list_archives()
    except ScenarioSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreachable bucket is reportable
        raise ScenarioSourceError(f"could not list the scenario source: {exc}")

    for key in keys:
        name = key[: -len(ARCHIVE_SUFFIX)]
        if not overwrite and (library / name / "scenario.yaml").is_file():
            report["skipped"].append(name)
            continue
        try:
            data = store.get(key)
            directory = extract_scenario(
                data, library, limits=limits, name_hint=name, overwrite=True
            )
        except (ScenarioArchiveError, OSError) as exc:
            report["failed"][key] = str(exc)
            log.warning("scenario source: %s could not be imported: %s", key, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad object is not fatal
            report["failed"][key] = str(exc)
            log.warning("scenario source: %s could not be imported: %s", key, exc)
            continue
        report["imported"].append(directory.name)
        log.info("imported scenario %r from the scenario source", directory.name)
    return report


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #

def publish_scenario(
    name: str,
    directory: Path,
    store: Optional[Store] = None,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Push one scenario to the configured source. Returns the key, or None.

    A no-op unless both a source and ``REG_SCENARIO_SOURCE_WRITE`` are set.
    Never raises: failing to mirror a scenario must not fail the save that
    produced it, or an operator loses work because a bucket policy changed.
    """
    if store is None:
        if not writes_enabled(env):
            return None
        store = store_from_env(env)
    if store is None:
        return None
    try:
        key = archive_name(name)
        store.put(key, export_scenario_bytes(directory, name))
    except Exception as exc:  # noqa: BLE001 - mirroring is best effort
        log.warning("could not publish scenario %r to the scenario source: %s", name, exc)
        return None
    log.info("published scenario %r to the scenario source as %s", name, key)
    return key


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #

def load_from_env(env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Pull the shared library at boot. Never raises, for the ConfigMap-typo reason."""
    env = env if env is not None else dict(os.environ)
    try:
        store = store_from_env(env)
    except ScenarioSourceError as exc:
        log.error("scenario source unusable: %s", exc)
        return {"error": str(exc)}
    if store is None:
        return None
    try:
        report = sync_from_store(store)
    except ScenarioSourceError as exc:
        log.error("scenario source unusable: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - startup must survive anything here
        log.exception("scenario source sync failed unexpectedly: %s", exc)
        return {"error": str(exc)}
    log.info(
        "scenario source: %d imported, %d already present%s",
        len(report["imported"]),
        len(report["skipped"]),
        f", {len(report['failed'])} failed" if report["failed"] else "",
    )
    return report


__all__ = [
    "DirectoryStore",
    "S3Store",
    "SOURCE_ENV",
    "ScenarioSourceError",
    "Store",
    "WRITE_ENV",
    "describe",
    "load_from_env",
    "open_store",
    "publish_scenario",
    "store_from_env",
    "sync_from_store",
    "writes_enabled",
]
