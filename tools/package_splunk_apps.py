#!/usr/bin/env python3
"""Package this repository's Splunk apps as installable .tgz files.

    python3 tools/package_splunk_apps.py --version 0.2.0 --out dist

Each app becomes ``<id>-<version>.tgz`` with the app id as its top-level
folder, which is what Splunk Web's "Install app from file", ``splunk install
app`` and Splunk Cloud's private-app upload expect. The version is stamped
into the packaged ``app.conf`` (``[launcher] version`` and ``[id]``), never the
source tree, so a release tag is the one place a version is decided.

Left out of every package: ``local/`` and ``metadata/local.meta`` (a search
head's own edits, never shipped), caches and dotfiles. Files are 0644 and
folders 0755, owned by uid 0, with a fixed mtime, so the same commit and
version always give the same bytes.
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The apps a release ships, as source folders relative to the repository.
APP_DIRS = (
    "splunk-app/regulator_sut",
    "splunk-app/regulator_telemetry",
)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SKIP_DIRS = {"local", "__pycache__"}
SKIP_FILES = {"local.meta"}
# 2026-01-01T00:00:00Z: any fixed instant will do; SOURCE_DATE_EPOCH wins.
DEFAULT_MTIME = 1767225600


class PackageError(Exception):
    pass


def app_id(app_dir):
    # type: (Path) -> str
    """The id from ``[package] id``, else the folder name."""
    conf = app_dir / "default" / "app.conf"
    if not conf.is_file():
        raise PackageError("%s has no default/app.conf" % app_dir)
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(conf.read_text(encoding="utf-8"))
    if parser.has_option("package", "id"):
        return parser.get("package", "id").strip()
    return app_dir.name


def stamp_app_conf(text, app, version):
    # type: (str, str, str) -> str
    """``app.conf`` with ``[launcher] version`` and ``[id] name/version`` set,
    keeping every other line (and its comments) as written."""
    out = []
    section = None
    seen = {"launcher": False, "id": False}
    wrote = {"launcher": set(), "id": set()}
    want = {"launcher": {"version": version}, "id": {"name": app, "version": version}}

    def flush(name):
        for key, value in want.get(name, {}).items():
            if key not in wrote[name]:
                out.append("%s = %s" % (key, value))

    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            if section in want:
                flush(section)
            section = header.group(1).strip()
            if section in seen:
                seen[section] = True
            out.append(line)
            continue
        key = re.match(r"^\s*([A-Za-z_]+)\s*=", line)
        if section in want and key and key.group(1) in want[section]:
            name = key.group(1)
            out.append("%s = %s" % (name, want[section][name]))
            wrote[section].add(name)
            continue
        out.append(line)
    if section in want:
        flush(section)
    for name in ("launcher", "id"):
        if not seen[name]:
            out.append("")
            out.append("[%s]" % name)
            for key, value in want[name].items():
                out.append("%s = %s" % (key, value))
    return "\n".join(out) + "\n"


def _members(app_dir):
    # type: (Path) -> list
    keep = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel_root = Path(root).relative_to(app_dir)
        keep.append(rel_root)
        for name in sorted(files):
            if name in SKIP_FILES or name.startswith(".") or name.endswith(".pyc"):
                continue
            keep.append(rel_root / name)
    return keep


def package(app_dir, version, out_dir, mtime=None):
    # type: (Path, str, Path, int) -> Path
    if not VERSION.match(version):
        raise PackageError("version %r is not MAJOR.MINOR.PATCH" % version)
    app_dir = Path(app_dir)
    app = app_id(app_dir)
    if mtime is None:
        mtime = int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_MTIME))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ("%s-%s.tgz" % (app, version))

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in _members(app_dir):
            src = app_dir / rel
            arcname = app if str(rel) == "." else "%s/%s" % (app, rel.as_posix())
            info = tarfile.TarInfo(arcname)
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if src.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
                continue
            data = src.read_bytes()
            if rel.as_posix() == "default/app.conf":
                data = stamp_app_conf(data.decode("utf-8"), app, version).encode("utf-8")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    with open(target, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as gz:
            gz.write(raw.getvalue())
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True,
                        help="MAJOR.MINOR.PATCH; a leading v (a tag name) is dropped")
    parser.add_argument("--out", default="dist", help="output folder (default dist)")
    parser.add_argument("apps", nargs="*", help="app folders (default: every app this repo ships)")
    args = parser.parse_args(argv)
    version = args.version[1:] if args.version.startswith("v") else args.version
    apps = [Path(a) for a in args.apps] or [REPO / d for d in APP_DIRS]
    sums = []
    try:
        for app_dir in apps:
            path = package(app_dir, version, Path(args.out))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            sums.append("%s  %s" % (digest, path.name))
            print("packaged %s" % path)
    except PackageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    (Path(args.out) / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
