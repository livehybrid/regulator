"""Credentials at rest, and session cookies.

Two keys, both derived from one master key with separate domains so that
compromising one does not hand over the other. Copied in spirit from Stoker,
which learned it the sensible way round: by deciding it before shipping.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import get_settings

SESSION_DOMAIN = "regulator-session-v1"


class DecryptionError(RuntimeError):
    """A stored credential could not be decrypted.

    Almost always means the master key changed, which is what happens when a
    deployment runs without ``REG_MASTER_KEY`` set and restarts. The message
    says so, because the alternative is an operator staring at a target that
    used to work.
    """


def _fernet() -> Fernet:
    key = get_settings().master_key
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "a stored credential could not be decrypted with the current master key. "
            "If REG_MASTER_KEY was not set, a throwaway key is generated on every "
            "restart and everything encrypted by the previous one is unrecoverable: "
            "set REG_MASTER_KEY and re-enter the credential"
        ) from exc


def _session_serializer() -> URLSafeTimedSerializer:
    # Domain-separated from the Fernet key so a session cookie signature and a
    # credential ciphertext never share key material.
    # The password is part of the key material on purpose: rotating the
    # admin password must invalidate every session issued under the old one,
    # otherwise a copied cookie outlives the credential it was minted with.
    settings = get_settings()
    secret = hashlib.blake2b(
        (SESSION_DOMAIN + settings.master_key + "|" + (settings.admin_password or "")).encode(),
        digest_size=32,
    ).hexdigest()
    return URLSafeTimedSerializer(secret, salt=SESSION_DOMAIN)


def sign_session(payload: str) -> str:
    return _session_serializer().dumps(payload)


def verify_session(token: str, max_age_s: int) -> Optional[str]:
    try:
        return _session_serializer().loads(token, max_age=max_age_s)
    except (BadSignature, Exception):  # noqa: BLE001 - any failure means "not signed in"
        return None
