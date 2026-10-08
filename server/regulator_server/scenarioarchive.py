"""Scenarios as portable archives: safe extraction in, reproducible bytes out.

A scenario is a directory (``scenario.yaml`` plus, usually, the
``savedsearches.conf`` it points at). Until now the only way one could reach an
instance was pasting a conf into the console, and the only way one could leave
was reading that conf back. This module gives both directions a single
artefact, so a scenario authored on one control plane can be moved to a
colleague's, to a customer's air-gapped instance, into a bucket or into git.

Extraction is the security-critical half, because an uploaded archive is
**untrusted input supplied over HTTP**. The stdlib extractors are deliberately
not used: ``tarfile.extractall`` was traversal-unsafe for most of its life (its
``filter=`` parameter fixes that, but member-by-member extraction lets us
enforce byte caps *while streaming*, which ``filter=`` does not), and
``zipfile.extractall`` neither caps decompressed output nor surfaces symlinks.
Every member is validated and copied by hand instead:

* **Path traversal** - any member whose path is absolute, carries a Windows
  drive/UNC prefix, or resolves outside the destination (``..`` segments) is
  refused. Paths are normalised (backslashes treated as separators, for a zip
  built on Windows) before the check, and the joined destination is re-checked
  with ``realpath`` as a second layer.
* **Symlinks and hardlinks are never created.** A link pointing out of the
  scenario would let an archive read or shadow arbitrary control-plane files,
  the master key included. A link member is refused outright rather than
  skipped, so an archive that tries it fails loudly instead of half-landing.
* **Device / fifo / other special members** are refused: nothing but plain
  files and directories is ever created.
* **Extraction bombs** - the member count, each member's uncompressed size and
  the total uncompressed size are capped. The byte caps bind on the bytes
  actually produced while streaming, never on the sizes the archive declares,
  which a malicious archive lies about.
* **Nothing left behind** - extraction happens in a throwaway staging
  directory; on any rejection it is removed, so a bad upload leaves no trace.

The format is detected from the CONTENT (magic bytes), not the filename, so a
``.tar.gz`` renamed ``.zip`` still extracts and a disguised non-archive is
refused with a clear message.

Export is the easy half. A fixed list of names is carried rather than a
directory walk, so a stray file an operator dropped beside the scenario cannot
ride along, and the archive is reproducible - sorted members, fixed
mtime/uid/gid/mode, zeroed gzip mtime - so exporting an unchanged scenario
twice yields identical bytes and a checksum is worth comparing across
instances.

Adapted from Stoker's ``server/packupload.py`` and ``server/packexport.py``
(see docs/VENDOR-FROM-STOKER.md).
"""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import re
import shutil
import stat
import tarfile
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("regulator.server.scenarioarchive")

# Magic bytes for content-based format detection. Zip: local-file header, or
# the empty/spanned end-of-central-directory records (a zip of zero members is
# still recognised, then rejected for holding no scenario). Gzip: the two-byte
# header of a .tar.gz/.tgz. A plain uncompressed tar has its magic at offset
# 257 ("ustar", POSIX or GNU flavour).
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"
_TAR_MAGIC_OFFSET = 257
_TAR_MAGICS = (b"ustar\x00", b"ustar ")

_COPY_CHUNK = 1 << 16

# Windows drive-letter prefix ("C:..."), meaningless on the server but present
# in archives built by some Windows tools; treated as an absolute path.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Top-level archive entries that are packaging junk rather than content: macOS
# tar/Finder add `__MACOSX/` and `.DS_Store`, and `._*` are AppleDouble
# resource forks. Skipped when deciding whether the archive wraps one directory.
_JUNK_NAMES = frozenset(("__MACOSX", ".DS_Store"))

# Scenario directory names are slugged from the archive's own directory name or
# the operator's: anything else becomes '-'. Cosmetic only, containment never
# relies on it.
_SLUG_BAD_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: The only names an archive contributes to a scenario, in export order. A
#: scenario is text and nothing else; an allowlist means a hostile archive
#: cannot drop an executable, a dotfile or a second scenario.yaml deeper in the
#: tree and have it survive.
SCENARIO_FILES = ("scenario.yaml", "savedsearches.conf", "README.md")

ARCHIVE_SUFFIX = ".tar.gz"

# A fixed timestamp for every exported member. Any constant works; this is the
# Unix epoch's first day, which is obviously synthetic rather than looking like
# a real authoring time.
_FIXED_MTIME = 0


class ScenarioArchiveError(Exception):
    """The archive was rejected; the message is operator-facing."""


@dataclasses.dataclass(frozen=True)
class ArchiveLimits:
    """Extraction-bomb caps.

    Much smaller than Stoker's pack equivalents on purpose: a pack carries
    sample data and can legitimately be hundreds of megabytes, whereas a
    scenario is a YAML file and a conf. A cap that reflects that is a cap that
    actually bounds the damage.
    """

    max_members: int = 2000
    max_member_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings) -> "ArchiveLimits":
        return cls(
            max_members=int(getattr(settings, "scenario_upload_max_members", 2000)),
            max_member_bytes=int(
                getattr(settings, "scenario_upload_max_member_bytes", 8 * 1024 * 1024)
            ),
            max_total_bytes=int(
                getattr(settings, "scenario_upload_max_total_bytes", 32 * 1024 * 1024)
            ),
        )


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #

def detect_format(data: bytes) -> Optional[str]:
    """``"zip"`` / ``"tar"`` from the archive's magic bytes, else ``None``.

    Content-based on purpose: the filename extension is typo and attacker
    territory. Gzip data is assumed to be a compressed tar (``tarfile`` with
    ``mode="r:*"`` verifies that when it opens).
    """
    if any(data.startswith(magic) for magic in _ZIP_MAGICS):
        return "zip"
    if data.startswith(_GZIP_MAGIC):
        return "tar"
    at = data[_TAR_MAGIC_OFFSET : _TAR_MAGIC_OFFSET + 6]
    if any(at.startswith(magic) for magic in _TAR_MAGICS):
        return "tar"
    return None


def _member_dest(dest_dir: str, name: str) -> str:
    """Resolve an archive member name to its destination path, or refuse.

    The single traversal chokepoint for both formats.
    """
    normalised = name.replace("\\", "/")
    if not normalised or normalised.startswith("/") or normalised.startswith("//"):
        raise ScenarioArchiveError(f"archive member {name!r} has an absolute path (refused)")
    if _WINDOWS_DRIVE_RE.match(normalised):
        raise ScenarioArchiveError(f"archive member {name!r} has a drive-letter path (refused)")
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if not parts:
        raise ScenarioArchiveError(f"archive member {name!r} has an empty path (refused)")
    if ".." in parts:
        raise ScenarioArchiveError(
            f"archive member {name!r} escapes the extraction directory (refused)"
        )
    dest = os.path.join(dest_dir, *parts)
    # Belt and braces: nothing above can produce an escaping path, but the
    # realpath check makes that a verified property rather than an assumption.
    root = os.path.realpath(dest_dir)
    real = os.path.realpath(dest)
    if real != root and not real.startswith(root + os.sep):
        raise ScenarioArchiveError(
            f"archive member {name!r} escapes the extraction directory (refused)"
        )
    return dest


def _copy_capped(src, dest: str, name: str, member_cap: int, total_so_far: int, total_cap: int) -> int:
    """Stream ``src`` to ``dest``, enforcing the byte caps on ACTUAL output.

    The caps bind on the bytes the stream produces, not on any size the archive
    declared: a lying header (the classic bomb trick) is caught the moment the
    real output crosses a cap, and the partial file goes with the caller's
    staging cleanup.
    """
    written = 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > member_cap:
                raise ScenarioArchiveError(
                    f"archive member {name!r} exceeds the {member_cap}-byte per-file "
                    "limit (refused)"
                )
            if total_so_far + written > total_cap:
                raise ScenarioArchiveError(
                    f"archive exceeds the {total_cap}-byte total uncompressed limit (refused)"
                )
            out.write(chunk)
    return total_so_far + written


def _extract_tar(data: bytes, dest_dir: str, limits: ArchiveLimits) -> None:
    """Extract a (possibly gzipped) tar member by member with every guard."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise ScenarioArchiveError(f"could not read the upload as a tar archive: {exc}")
    members = 0
    total = 0
    with archive:
        for member in archive:
            members += 1
            if members > limits.max_members:
                raise ScenarioArchiveError(
                    f"archive has more than {limits.max_members} members (refused)"
                )
            if member.issym():
                raise ScenarioArchiveError(
                    f"archive member {member.name!r} is a symlink "
                    "(refused: links are never extracted)"
                )
            if member.islnk():
                raise ScenarioArchiveError(
                    f"archive member {member.name!r} is a hardlink "
                    "(refused: links are never extracted)"
                )
            if member.isdev() or member.isfifo():
                raise ScenarioArchiveError(
                    f"archive member {member.name!r} is a device/fifo node (refused)"
                )
            if member.isdir():
                os.makedirs(_member_dest(dest_dir, member.name), exist_ok=True)
                continue
            if not member.isreg():
                raise ScenarioArchiveError(
                    f"archive member {member.name!r} has unsupported type "
                    f"{member.type!r} (refused)"
                )
            dest = _member_dest(dest_dir, member.name)
            src = archive.extractfile(member)
            if src is None:  # pragma: no cover - regular members always yield a stream
                raise ScenarioArchiveError(
                    f"archive member {member.name!r} could not be read (refused)"
                )
            with src:
                total = _copy_capped(
                    src, dest, member.name, limits.max_member_bytes, total, limits.max_total_bytes
                )


def _extract_zip(data: bytes, dest_dir: str, limits: ArchiveLimits) -> None:
    """Extract a zip member by member with every guard.

    A zip's central directory can lie about uncompressed sizes, so the byte
    caps are enforced on the decompressed stream, not on ``ZipInfo.file_size``.
    Unix mode bits ride ``external_attr >> 16``: Info-ZIP encodes a symlink
    there, and extracting one as a plain file would silently change the
    scenario while creating the link would be the exfiltration hazard.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScenarioArchiveError(f"could not read the upload as a zip archive: {exc}")
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            raise ScenarioArchiveError(
                f"archive has more than {limits.max_members} members (refused)"
            )
        total = 0
        for info in infos:
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ScenarioArchiveError(
                    f"archive member {info.filename!r} is a symlink "
                    "(refused: links are never extracted)"
                )
            # Only the FILE-TYPE bits matter: many tools store bare permission
            # bits, or nothing at all for a Windows-built zip, so an absent
            # type (0) is a regular file. Any explicit non-file, non-directory
            # type (device, fifo, socket) is refused.
            ftype = stat.S_IFMT(mode)
            if ftype not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ScenarioArchiveError(
                    f"archive member {info.filename!r} is not a regular file or "
                    "directory (refused)"
                )
            if info.is_dir():
                os.makedirs(_member_dest(dest_dir, info.filename), exist_ok=True)
                continue
            dest = _member_dest(dest_dir, info.filename)
            try:
                with archive.open(info) as src:
                    total = _copy_capped(
                        src,
                        dest,
                        info.filename,
                        limits.max_member_bytes,
                        total,
                        limits.max_total_bytes,
                    )
            except zipfile.BadZipFile as exc:
                # A corrupt or lying entry surfaced mid-stream is an
                # operator-facing rejection, not a 500.
                raise ScenarioArchiveError(f"archive member {info.filename!r} is corrupt: {exc}")


def find_scenario_root(extract_dir: str) -> str:
    """Locate the scenario root inside an extracted archive, or refuse.

    Someone usually tars up a *folder*, so the archive commonly holds one
    top-level directory that is the scenario (``myscenario/scenario.yaml``);
    equally it may be rooted at the scenario itself (``scenario.yaml`` at the
    archive top). Both shapes are accepted, and nothing else is: an archive of
    several scenarios is refused rather than guessed at, because picking one of
    them would silently discard the rest.
    """
    if os.path.isfile(os.path.join(extract_dir, "scenario.yaml")):
        return extract_dir
    dirs: List[str] = []
    for entry in sorted(os.listdir(extract_dir)):
        if entry in _JUNK_NAMES or entry.startswith("._"):
            continue
        full = os.path.join(extract_dir, entry)
        if os.path.isdir(full):
            dirs.append(full)
    if len(dirs) == 1 and os.path.isfile(os.path.join(dirs[0], "scenario.yaml")):
        return dirs[0]
    if len(dirs) > 1:
        raise ScenarioArchiveError(
            "this archive holds more than one directory: upload one scenario at a time, "
            "so that what lands is what you chose"
        )
    raise ScenarioArchiveError(
        "no scenario found in the archive: expected scenario.yaml at the archive root, "
        "or inside a single top-level directory"
    )


def slug(name: str, fallback: str = "scenario") -> str:
    """A safe directory name. Cosmetic: containment never depends on it."""
    cleaned = _SLUG_BAD_RE.sub("-", name or "").strip("-").lstrip(".")
    return cleaned or fallback


def extract_scenario(
    data: bytes,
    library_dir: Path,
    limits: Optional[ArchiveLimits] = None,
    name_hint: Optional[str] = None,
    overwrite: bool = False,
) -> Path:
    """Extract one scenario archive into ``library_dir``; returns its directory.

    The full pipeline: detect the format by content, extract safely into a
    throwaway ``.extract-*`` staging directory (every guard above), locate the
    scenario root, keep only the allowlisted files, then move it into place.
    Any failure removes the staging directory, so a rejected upload leaves
    nothing behind.

    ``overwrite`` is required to replace a scenario that already exists. The
    default refuses, because the alternative - quietly writing over an
    operator's edited scenario because a bucket happened to hold one with the
    same name - is the kind of data loss nobody looks for until much later.
    """
    limits = limits or ArchiveLimits()
    fmt = detect_format(data)
    if fmt is None:
        raise ScenarioArchiveError(
            "unrecognised archive: upload a .tar.gz/.tgz/.tar or .zip (the format is "
            "detected from the file content, not its name)"
        )
    library_dir.mkdir(parents=True, exist_ok=True)
    staging = library_dir / f".extract-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        if fmt == "zip":
            _extract_zip(data, str(staging), limits)
        else:
            _extract_tar(data, str(staging), limits)
        root = Path(find_scenario_root(str(staging)))
        name = slug(name_hint or root.name)
        final = library_dir / name
        if final.exists() and not overwrite:
            raise ScenarioArchiveError(
                f"a scenario named {name!r} already exists: delete it first, or upload "
                "with replace turned on"
            )
        # Keep only what a scenario is made of. Everything else the archive
        # carried is dropped here rather than moved into the library.
        staged = staging / f".keep-{uuid.uuid4().hex}"
        staged.mkdir()
        kept = 0
        for filename in SCENARIO_FILES:
            candidate = root / filename
            if candidate.is_file() and not candidate.is_symlink():
                shutil.copyfile(candidate, staged / filename)
                kept += 1
        if not (staged / "scenario.yaml").is_file():
            raise ScenarioArchiveError("the archive has no scenario.yaml")
        if final.exists():
            shutil.rmtree(final)
        # Same filesystem (both under library_dir), so this is an atomic
        # rename: the scenario appears complete or not at all.
        os.rename(staged, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    log.info("stored scenario at %s (%s archive, %d bytes, %d file(s))", final, fmt, len(data), kept)
    return final


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def archive_name(scenario_name: str) -> str:
    return slug(scenario_name) + ARCHIVE_SUFFIX


def export_scenario_bytes(directory: Path, scenario_name: Optional[str] = None) -> bytes:
    """A reproducible ``.tar.gz`` of one scenario directory.

    Rooted at a single top-level directory named after the scenario, which is
    one of the two shapes :func:`find_scenario_root` accepts, so what this
    writes is exactly what an upload reads.
    """
    name = slug(scenario_name or directory.name)
    entries: Dict[str, bytes] = {}
    for filename in SCENARIO_FILES:
        path = directory / filename
        if path.is_file() and not path.is_symlink():
            entries[f"{name}/{filename}"] = path.read_bytes()
    if f"{name}/scenario.yaml" not in entries:
        raise ScenarioArchiveError(f"{directory} has no scenario.yaml to export")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as tar:
        for member_name in sorted(entries):
            payload = entries[member_name]
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            info.mtime = _FIXED_MTIME
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))
    raw = buffer.getvalue()
    # The gzip HEADER carries its own mtime, which tarfile fills in from the
    # clock with no way to override, so the same scenario exported twice would
    # differ in bytes 4 to 7 and a checksum comparison would be worthless.
    # Zeroing them here makes reproducibility a property of this function
    # rather than of the interpreter version.
    if len(raw) > 8 and raw[:2] == _GZIP_MAGIC:
        raw = raw[:4] + b"\x00\x00\x00\x00" + raw[8:]
    return raw


__all__ = [
    "ARCHIVE_SUFFIX",
    "ArchiveLimits",
    "ScenarioArchiveError",
    "SCENARIO_FILES",
    "archive_name",
    "detect_format",
    "export_scenario_bytes",
    "extract_scenario",
    "find_scenario_root",
    "slug",
]
