"""Control-plane configuration.

Same rules as the worker's config, for the same reasons: parsed once into a
frozen dataclass, secrets carry ``repr=False``, and a malformed value is a hard
boot error rather than a silent default.

One setting deserves its own explanation. ``REG_MASTER_KEY`` encrypts every
target credential at rest. If it is unset the server generates a throwaway key
and says so loudly, which is fine for a laptop and ruinous in a deployment: the
key changes on every restart, so every credential already in the database
becomes undecryptable. Set it, back it up, and mount it from a file rather than
an environment variable wherever you can.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from cryptography.fernet import Fernet

DEFAULT_DATABASE_URL = "sqlite:///./regulator.db"
DEFAULT_PORT = 8080


class ServerConfigError(ValueError):
    """A malformed or missing server setting. Always fatal."""


def _get(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _boolean(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ServerConfigError(f"{key} must be a boolean, got {raw!r}")


def _integer(env: Mapping[str, str], key: str, default: int, minimum: Optional[int] = None) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ServerConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ServerConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class ServerConfig:
    database_url: str
    master_key: str = field(repr=False)
    master_key_generated: bool
    admin_password: Optional[str] = field(repr=False)
    session_ttl_s: int
    port: int
    scenarios_dir: Optional[str]
    # In-process execution ceiling. The control plane runs scenarios itself for
    # now, which means the server is the load generator: past a few hundred
    # virtual users it becomes the bottleneck and the run is invalidated by the
    # generator-drift guard rather than producing a wrong answer quietly. The
    # Swarm and Kubernetes fleets lift this.
    max_virtual_users: int
    max_concurrent_runs: int

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password)


def load_server_config(env: Optional[Mapping[str, str]] = None) -> ServerConfig:
    env = os.environ if env is None else env

    key_file = _get(env, "REG_MASTER_KEY_FILE")
    master_key = _get(env, "REG_MASTER_KEY")
    generated = False
    if key_file:
        path = Path(key_file)
        if not path.is_file():
            raise ServerConfigError(f"REG_MASTER_KEY_FILE points at {key_file}, which does not exist")
        master_key = path.read_text(encoding="utf-8").strip()
    if not master_key:
        master_key = Fernet.generate_key().decode()
        generated = True
    try:
        Fernet(master_key.encode() if isinstance(master_key, str) else master_key)
    except Exception as exc:  # noqa: BLE001 - any malformed key is fatal
        raise ServerConfigError(
            "the master key is not a valid Fernet key. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        ) from exc

    return ServerConfig(
        database_url=_get(env, "REG_DATABASE_URL", DEFAULT_DATABASE_URL) or DEFAULT_DATABASE_URL,
        master_key=master_key,
        master_key_generated=generated,
        admin_password=_get(env, "REG_ADMIN_PASSWORD"),
        session_ttl_s=_integer(env, "REG_SESSION_TTL_S", 43200, minimum=60),
        port=_integer(env, "REG_PORT", DEFAULT_PORT, minimum=1),
        scenarios_dir=_get(env, "REG_BUILTIN_SCENARIOS_DIR"),
        max_virtual_users=_integer(env, "REG_MAX_VIRTUAL_USERS", 500, minimum=1),
        max_concurrent_runs=_integer(env, "REG_MAX_CONCURRENT_RUNS", 2, minimum=1),
    )


_settings: Optional[ServerConfig] = None


def get_settings() -> ServerConfig:
    global _settings
    if _settings is None:
        _settings = load_server_config()
    return _settings


def set_settings(config: Optional[ServerConfig]) -> None:
    """Test seam. Never called in production."""
    global _settings
    _settings = config


def new_master_key() -> str:
    return Fernet.generate_key().decode()


def new_session_secret() -> str:
    return secrets.token_urlsafe(32)
