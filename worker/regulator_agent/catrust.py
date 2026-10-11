"""The CA bundle that verifies a customer's splunkd certificate.

TLS verification used to be a boolean, which gave an operator at a customer
site two options and no good one. A Splunk cluster behind a private or
internal CA fails verification against the public trust store, so the only
setting that worked was ``verify_tls = false`` - and that ships a splunkd
admin credential over a connection nobody checked, to a production search
head. The certificate was fine; there was just nowhere to put it.

``REG_CA_BUNDLE`` is that place: a path to a PEM file, or a directory of them,
mounted into the container by whatever provisions the deployment. It is used
wherever verification is ON, so it composes with the per-target ``verify_tls``
switch rather than replacing it:

===================  ================  ==============================
``verify_tls``       ``CA_BUNDLE``     what httpx is given
===================  ================  ==============================
true                 unset             ``True`` (public trust store)
true                 set               an ``SSLContext`` for the bundle
false                either            ``False`` (no verification)
===================  ================  ==============================

httpx 0.28 deprecated the bare path its ``verify=`` used to accept, so a
bundle becomes an ``ssl.SSLContext`` here rather than being passed through.
One context is built per client, not per request.

**A fleet needs the bytes, not the path.** A worker is its own container, so a
path that resolves in the control plane usually resolves to nothing in a
worker. The control plane therefore reads its bundle once and projects the PEM
text as ``REG_CA_BUNDLE_PEM``; this module writes that to a file of its own and
uses it. A CA certificate is public material, so it travels in an environment
variable without being a secret the way a token is, and the deployment only has
to put the file in one place.

Known gap, stated rather than hidden: the **browser** engine drives Chromium
through Playwright, which does not read this bundle. A browser scenario against
a private CA still needs ``verify_tls: false`` on the target (which is what
sets Playwright's ``ignore_https_errors``). The API engine, which is what
search-load scenarios use, is fully covered.
"""

from __future__ import annotations

import logging
import os
import ssl
import tempfile
from typing import Mapping, Optional, Union

log = logging.getLogger("regulator.catrust")

#: Path to a PEM file, or a directory of them, used wherever TLS is verified.
CA_BUNDLE_ENV = "REG_CA_BUNDLE"

#: The same material as inline PEM text. What the control plane projects to a
#: worker, and what wins if both reach the same process.
CA_BUNDLE_PEM_ENV = "REG_CA_BUNDLE_PEM"

_PEM_MARKER = "-----BEGIN CERTIFICATE-----"


class CaBundleError(ValueError):
    """A configured CA bundle that cannot be used."""


def resolve_bundle(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """A usable bundle path from either spelling, or None.

    The inline PEM wins when both are present, because that is the one the
    control plane deliberately sent for this run, while the path may be a
    leftover from the image.

    Raises :class:`CaBundleError` when something is configured but unusable. A
    deployment that was handed a certificate and quietly verified against the
    public trust store instead is the precise failure this exists to prevent,
    and it would surface only as a handshake error naming neither the file nor
    the reason.
    """
    env = env if env is not None else os.environ

    pem = env.get(CA_BUNDLE_PEM_ENV) or ""
    if pem.strip():
        if _PEM_MARKER not in pem:
            raise CaBundleError(
                f"{CA_BUNDLE_PEM_ENV} is set but holds no PEM certificate "
                f"(no {_PEM_MARKER})"
            )
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", prefix="regulator-ca-", delete=False
        )
        try:
            handle.write(pem if pem.endswith("\n") else pem + "\n")
        finally:
            handle.close()
        # Left on disk deliberately: the file has to outlive this call for the
        # life of the process, and the container is discarded after the run,
        # which is a shorter life than any cleanup we could schedule.
        return handle.name

    path = (env.get(CA_BUNDLE_ENV) or "").strip()
    if not path:
        return None
    if not os.path.exists(path):
        raise CaBundleError(
            f"{CA_BUNDLE_ENV} points at {path!r}, which does not exist in this "
            f"container. Mount the PEM there, or let the control plane project it "
            f"as {CA_BUNDLE_PEM_ENV}."
        )
    if os.path.isdir(path):
        return path
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        raise CaBundleError(f"{CA_BUNDLE_ENV} points at {path!r}, which cannot be read: {exc}")
    if _PEM_MARKER.encode() not in head:
        raise CaBundleError(
            f"{CA_BUNDLE_ENV} points at {path!r}, which does not look like PEM (no "
            f"BEGIN CERTIFICATE in the first 4 KiB). Convert a DER certificate with: "
            f"openssl x509 -inform der -in <file> -out bundle.pem"
        )
    return path


def read_pem(path: Optional[str]) -> Optional[str]:
    """A bundle's PEM text, for projecting to a worker. None when unavailable.

    Never raises: a run must not fail to launch because the bundle became
    unreadable after boot. A worker that receives no PEM verifies against the
    public trust store, which is what it would have done with no bundle at all.
    """
    if not path:
        return None
    if os.path.isdir(path):
        # An OpenSSL hashed directory has no single file to send. Projecting
        # nothing is honest, and the message says what to do instead.
        log.warning(
            "%s is a directory, which cannot be projected to a fleet worker. Point it "
            "at one concatenated PEM file so workers can verify too.",
            CA_BUNDLE_ENV,
        )
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("could not read the CA bundle at %s to send to workers: %s", path, exc)
        return None


def verify_arg(verify_tls: bool, ca_bundle: Optional[str] = None) -> Union[bool, ssl.SSLContext]:
    """What to hand httpx's ``verify=``.

    Falls back to ordinary verification if the context cannot be built rather
    than raising: the path is validated loudly where it is configured, and
    failing a request here would report a certificate problem as a connection
    problem.
    """
    if not verify_tls:
        return False
    if not ca_bundle:
        return True
    try:
        if os.path.isdir(ca_bundle):
            return ssl.create_default_context(capath=ca_bundle)
        return ssl.create_default_context(cafile=ca_bundle)
    except (OSError, ssl.SSLError) as exc:
        log.warning(
            "could not load the CA bundle at %s (%s); verifying against the public "
            "trust store instead",
            ca_bundle,
            exc,
        )
        return True


__all__ = [
    "CA_BUNDLE_ENV",
    "CA_BUNDLE_PEM_ENV",
    "CaBundleError",
    "read_pem",
    "resolve_bundle",
    "verify_arg",
]
