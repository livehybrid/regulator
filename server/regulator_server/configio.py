"""Export and restore a Regulator instance's configuration as JSON.

The point is a disposable deployment: configure an instance, export the JSON,
tear the environment down including its volumes, and bring it back from the
file. So the export describes configuration an operator **authored** and
nothing a run **produced**.

What travels
------------
``targets``
    The Splunk instances under test, with their credentials as the ciphertext
    they are stored as.

``scenarios``
    The operator's own scenario library (``user_scenarios_dir``), each as its
    files inline. Unlike Stoker, which leaves packs out of the config and moves
    them through a pack source instead, a Regulator scenario is a handful of
    small text files (a ``scenario.yaml`` and usually a ``savedsearches.conf``),
    so carrying them makes the difference between a file that restores an
    instance and a file that restores half of one and then needs a bucket too.
    The built-in library is not exported: it ships in the image, so writing it
    into a restore would shadow the newer copy after an upgrade.

What deliberately does not travel
---------------------------------
Runs, worker leases, samples and audit events are history, not configuration.
**Baselines** look like configuration but are not: a baseline is a label
pointing at a run id, so on an instance with no runs there is nothing for it to
point at and restoring one would create a label that resolves to nothing. They
are left out rather than exported and silently skipped.

Nothing is addressed by id
--------------------------
A restored instance allocates its own primary keys, so every cross-reference
travels by natural key: a target is matched by name, a scenario by directory
name. That also makes the file diffable and hand-editable, which is what you
want from something living in git beside the chart that mounts it.

Secrets travel as ciphertext
----------------------------
A target's token and password are Fernet ciphertext under ``REG_MASTER_KEY``
and are exported exactly as stored. Restoring therefore needs the same master
key, which in Kubernetes is a Secret that survives precisely the volume
teardown this exists for. The alternative is writing live splunkd credentials
in clear into a file destined for a ConfigMap or a git repo, which is not a
trade worth making. The export records a fingerprint of the key so a restore
under the wrong one says so immediately, rather than leaving every target
quietly unable to authenticate. Pass ``include_secrets=False`` for a sanitised
copy to share or commit; restoring it recreates the configuration with the
credentials blank, ready to be filled in.

Import is an idempotent upsert, so re-applying the same file changes nothing
and a half-finished restore can simply be run again.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .adapters import user_scenarios_dir
from .config import get_settings
from .models import Target

log = logging.getLogger("regulator.server.configio")

CONFIG_VERSION = 1

#: Env var naming a JSON file (or holding the JSON itself) to restore at boot.
IMPORT_ENV = "REG_CONFIG_IMPORT"

# Fields copied verbatim per entity. Explicit lists rather than reflection: a
# new column should not silently start travelling between instances, and a
# secret must never join an export by accident.
TARGET_FIELDS = [
    "name",
    "mgmt_url",
    "web_url",
    "username",
    "verify_tls",
    "app",
    "owner",
    "api_version",
    "indexer_urls",
    "indexer_username",
]
TARGET_SECRETS = [
    "token_encrypted",
    "password_encrypted",
    "indexer_token_encrypted",
    "indexer_password_encrypted",
]

# A scenario directory is text. These are the only names carried, so a stray
# file an operator dropped in the directory cannot ride along, and nothing
# binary can bloat a document meant for a ConfigMap.
SCENARIO_FILES = ("scenario.yaml", "savedsearches.conf", "README.md")

#: A single scenario file larger than this is left out and reported. 1 MiB is
#: far above any real savedsearches.conf and far below a size that would make
#: the config document unusable as an env var.
MAX_SCENARIO_FILE_BYTES = 1024 * 1024


class ConfigError(Exception):
    """A config document that cannot be read or applied."""


def master_key_fingerprint(settings: Optional[Any] = None) -> Optional[str]:
    """8 hex characters identifying the master key, never the key itself.

    Enough to tell an operator "this export was made under a different key",
    and useless to anyone who obtains the file.
    """
    key = ""
    if settings is not None:
        key = getattr(settings, "master_key", "") or ""
    if not key:
        try:
            key = get_settings().master_key or ""
        except Exception:  # noqa: BLE001 - a fingerprint is never worth a 500
            key = ""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def _export_scenarios(report: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The operator's own scenario library, each scenario's files inline."""
    out: List[Dict[str, Any]] = []
    root = user_scenarios_dir()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not (child / "scenario.yaml").is_file():
            continue
        files: Dict[str, str] = {}
        for name in SCENARIO_FILES:
            path = child / name
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > MAX_SCENARIO_FILE_BYTES:
                if report is not None:
                    report.append(
                        f"scenario {child.name!r}: {name} is larger than "
                        f"{MAX_SCENARIO_FILE_BYTES // 1024} KiB and was left out"
                    )
                continue
            try:
                files[name] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                if report is not None:
                    report.append(f"scenario {child.name!r}: {name} is unreadable ({exc})")
        if files:
            out.append({"name": child.name, "files": files})
    return out


def export_config(
    session: Session,
    include_secrets: bool = True,
    include_scenarios: bool = True,
    settings: Optional[Any] = None,
) -> Dict[str, Any]:
    """The instance's configuration as a JSON-serialisable document."""
    warnings: List[str] = []
    doc: Dict[str, Any] = {
        "regulator_config_version": CONFIG_VERSION,
        "exported_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "includes_secrets": bool(include_secrets),
        "master_key_fingerprint": master_key_fingerprint(settings),
        "targets": [],
        "scenarios": [],
    }

    for target in session.query(Target).order_by(Target.name).all():
        row: Dict[str, Any] = {f: getattr(target, f) for f in TARGET_FIELDS}
        if include_secrets:
            row.update({f: getattr(target, f) for f in TARGET_SECRETS})
        doc["targets"].append(row)

    if include_scenarios:
        doc["scenarios"] = _export_scenarios(warnings)
    if warnings:
        doc["warnings"] = warnings
    return doc


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def load_document(source: str) -> Dict[str, Any]:
    """Parse a config document from a file path or from inline JSON.

    Both spellings are accepted because both are natural in a container: a
    mounted file for a ConfigMap or volume, inline JSON for a plain env var.
    """
    text = source
    # Anything opening with a JSON structural character is inline JSON, even if
    # it turns out to be the wrong shape: reporting an array as "not a readable
    # file" sends the operator looking for a mount that was never the problem.
    if source.lstrip()[:1] not in ("{", "["):
        if not os.path.isfile(source):
            raise ConfigError(
                f"config import source {source!r} is neither a readable file nor inline JSON"
            )
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"config import source {source!r} cannot be read: {exc}")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"config import is not valid JSON: {exc}")
    if not isinstance(doc, dict):
        raise ConfigError("config import must be a JSON object")
    version = doc.get("regulator_config_version")
    if version is not None:
        try:
            numeric = int(version)
        except (TypeError, ValueError):
            raise ConfigError(f"regulator_config_version is not a number: {version!r}")
        if numeric > CONFIG_VERSION:
            raise ConfigError(
                f"config was exported by a newer Regulator (version {numeric}, this "
                f"instance understands {CONFIG_VERSION})"
            )
    return doc


def _safe_scenario_dir(root: Path, name: str) -> Path:
    """Resolve a scenario name inside ``root``, refusing anything that escapes.

    A config document is operator-supplied and may have come from a bucket or a
    git repo, so a name is untrusted input: ``../../etc`` in it would otherwise
    write wherever the control plane can reach. Mirrors the containment check
    ``adapters.scenario_path`` already applies to names arriving from requests.
    """
    cleaned = (name or "").strip()
    if not cleaned or cleaned in (".", ".."):
        raise ConfigError("a scenario with no usable name")
    if "/" in cleaned or "\\" in cleaned or cleaned.startswith("."):
        raise ConfigError(f"scenario name {name!r} is not a plain directory name")
    candidate = (root / cleaned).resolve()
    if candidate != root.resolve() / cleaned:
        raise ConfigError(f"scenario name {name!r} does not stay inside the library")
    return candidate


def import_config(
    session: Session,
    doc: Dict[str, Any],
    settings: Optional[Any] = None,
) -> Dict[str, Any]:
    """Apply a config document. Idempotent: re-running changes nothing.

    Returns a report rather than raising on a partial apply. A restore that got
    most of the way is worth keeping, and the usual partial cases are benign:
    the operator will want to see which targets landed, not lose the lot
    because one scenario in the file was malformed.
    """
    report: Dict[str, Any] = {
        "targets": 0,
        "targets_updated": 0,
        "scenarios": 0,
        "scenarios_updated": 0,
        "skipped": [],
        "warnings": [],
    }

    want = doc.get("master_key_fingerprint")
    have = master_key_fingerprint(settings)
    if want and have and want != have:
        # Not fatal: the configuration still restores and is still useful, but
        # every encrypted credential in it is unreadable, so say so loudly once
        # rather than let it surface later as a pile of auth failures.
        report["warnings"].append(
            f"this config was exported under master key {want} but this instance uses "
            f"{have}: the stored tokens and passwords cannot be decrypted and must be "
            "re-entered"
        )
    if doc.get("includes_secrets") is False:
        report["warnings"].append(
            "this config was exported without secrets, so every target restores with "
            "no credential and needs one before it can be used"
        )

    for row in doc.get("targets") or []:
        if not isinstance(row, dict):
            report["skipped"].append("a target that is not an object")
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            report["skipped"].append("a target with no name")
            continue
        mgmt_url = str(row.get("mgmt_url") or "").strip()
        target = session.query(Target).filter(Target.name == name).one_or_none()
        if target is None:
            if not mgmt_url:
                report["skipped"].append(f"target {name!r}: no mgmt_url to create it with")
                continue
            target = Target(name=name, mgmt_url=mgmt_url)
            session.add(target)
            report["targets"] += 1
        else:
            report["targets_updated"] += 1
        for field_name in TARGET_FIELDS + TARGET_SECRETS:
            if field_name in row and field_name != "name":
                setattr(target, field_name, row[field_name])

    session.flush()

    root = user_scenarios_dir()
    for row in doc.get("scenarios") or []:
        if not isinstance(row, dict):
            report["skipped"].append("a scenario that is not an object")
            continue
        files = row.get("files")
        if not isinstance(files, dict) or "scenario.yaml" not in files:
            report["skipped"].append(
                f"scenario {row.get('name')!r}: no scenario.yaml in the document"
            )
            continue
        try:
            directory = _safe_scenario_dir(root, str(row.get("name") or ""))
        except ConfigError as exc:
            report["skipped"].append(str(exc))
            continue
        existed = directory.is_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for name, text in files.items():
                if name not in SCENARIO_FILES:
                    report["skipped"].append(
                        f"scenario {directory.name!r}: {name!r} is not a file a config "
                        "document may carry"
                    )
                    continue
                if not isinstance(text, str):
                    report["skipped"].append(
                        f"scenario {directory.name!r}: {name!r} is not text"
                    )
                    continue
                (directory / name).write_text(text, encoding="utf-8")
        except OSError as exc:
            report["skipped"].append(f"scenario {directory.name!r}: {exc}")
            continue
        if existed:
            report["scenarios_updated"] += 1
        else:
            report["scenarios"] += 1

    return report


def import_from_env(
    session: Session,
    settings: Optional[Any] = None,
    env: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Apply ``REG_CONFIG_IMPORT`` if it is set. Returns the report, or None.

    Never raises: a malformed config file must not stop the control plane from
    starting, or a typo in a ConfigMap takes the whole deployment down and
    leaves no way in to fix the typo.
    """
    env = env if env is not None else dict(os.environ)
    source = (env.get(IMPORT_ENV) or "").strip()
    if not source:
        return None
    try:
        doc = load_document(source)
        report = import_config(session, doc, settings=settings)
    except ConfigError as exc:
        log.error("config import failed: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - startup must survive anything here
        log.exception("config import failed unexpectedly: %s", exc)
        return {"error": str(exc)}
    log.info(
        "config import: %d target(s) and %d scenario(s) created, %d and %d updated%s",
        report["targets"],
        report["scenarios"],
        report["targets_updated"],
        report["scenarios_updated"],
        f"; {len(report['skipped'])} skipped" if report["skipped"] else "",
    )
    for line in report["warnings"] + report["skipped"]:
        log.warning("config import: %s", line)
    return report
