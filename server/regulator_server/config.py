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
class HecSettings:
    """Where runs launched from the control plane ship their telemetry.

    The same contract as the worker's REG_HEC_* variables, because it becomes
    one: a run launched from the web interface is configured exactly as a
    standalone run would be, so the two produce identical records.
    """

    url: str
    token: str = field(repr=False)
    index: Optional[str] = None
    verify_tls: bool = True
    source: str = "regulator"


@dataclass(frozen=True)
class SeedTarget:
    """A target registered from the environment at boot.

    What makes an ephemeral deployment disposable: a nightly-rebuilt or
    CI-spawned control plane comes up with a target already registered and can
    run immediately, with no web step and no stored state.
    """

    name: str
    mgmt_url: str
    web_url: Optional[str]
    token: Optional[str] = field(default=None, repr=False)
    username: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    verify_tls: bool = True
    app: str = "search"
    owner: str = "nobody"


@dataclass(frozen=True)
class ServerConfig:
    database_url: str
    master_key: str = field(repr=False)
    master_key_generated: bool
    admin_password: Optional[str] = field(repr=False)
    session_ttl_s: int
    scenarios_dir: Optional[str]
    # Where scenarios created through the API (imported saved searches) are
    # written. Separate from the built-in library so an image upgrade never
    # overwrites an operator's own scenarios.
    user_scenarios_dir: str
    # In-process execution ceiling. The control plane runs scenarios itself for
    # now, which means the server is the load generator: past a few hundred
    # virtual users it becomes the bottleneck and the run is invalidated by the
    # generator-drift guard rather than producing a wrong answer quietly. The
    # Swarm and Kubernetes fleets lift this.
    max_virtual_users: int
    max_concurrent_runs: int
    # The longest run the control plane will accept, so a mistyped or
    # malicious duration cannot occupy a slot for ever.
    max_run_duration_s: float = 4 * 3600.0
    # Bearer tokens for scripts and CI. Compared in constant time. A token is
    # as powerful as the password, so treat it as one.
    api_tokens: tuple[str, ...] = field(default=(), repr=False)
    # Running with no password on a network interface is refused unless the
    # operator says so explicitly: this thing can evict a production cache.
    allow_unauthenticated: bool = False
    hec: Optional[HecSettings] = None
    seed_target: Optional[SeedTarget] = None

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password) or bool(self.api_tokens)


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
            # An empty mounted secret is the common Kubernetes mistake, and
            # silently generating a throwaway key in its place means every
            # credential stored afterwards is unreadable at the next restart.
            raise ServerConfigError(
                f"REG_MASTER_KEY_FILE points at {key_file}, which is empty. Generate a key with: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
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

    database_url = _get(env, "REG_DATABASE_URL", DEFAULT_DATABASE_URL) or DEFAULT_DATABASE_URL

    tokens = tuple(
        part.strip()
        for part in (_get(env, "REG_API_TOKENS", "") or _get(env, "REG_API_TOKEN", "") or "").split(",")
        if part.strip()
    )
    for token in tokens:
        if len(token) < 16:
            raise ServerConfigError(
                "each REG_API_TOKENS entry must be at least 16 characters: it is as "
                "powerful as the admin password"
            )

    hec: Optional[HecSettings] = None
    hec_url = _get(env, "REG_HEC_URL")
    hec_token = _get(env, "REG_HEC_TOKEN")
    if hec_url or hec_token:
        if not (hec_url and hec_token):
            raise ServerConfigError("REG_HEC_URL and REG_HEC_TOKEN must be set together, or neither")
        if not (hec_url.startswith("http://") or hec_url.startswith("https://")):
            raise ServerConfigError(f"REG_HEC_URL must start with http:// or https://, got {hec_url!r}")
        hec = HecSettings(
            url=hec_url.rstrip("/"),
            token=hec_token,
            index=_get(env, "REG_HEC_INDEX"),
            verify_tls=_boolean(env, "REG_HEC_VERIFY_TLS", True),
            source=_get(env, "REG_HEC_SOURCE", "regulator") or "regulator",
        )

    seed: Optional[SeedTarget] = None
    seed_url = _get(env, "REG_SEED_TARGET_URL")
    if seed_url:
        if not (seed_url.startswith("http://") or seed_url.startswith("https://")):
            raise ServerConfigError(
                f"REG_SEED_TARGET_URL must start with http:// or https://, got {seed_url!r}"
            )
        seed_token = _get(env, "REG_SEED_TARGET_TOKEN")
        seed_user = _get(env, "REG_SEED_TARGET_USERNAME")
        seed_pass = _get(env, "REG_SEED_TARGET_PASSWORD")
        if not seed_token and not (seed_user and seed_pass):
            raise ServerConfigError(
                "REG_SEED_TARGET_URL needs REG_SEED_TARGET_TOKEN, or both "
                "REG_SEED_TARGET_USERNAME and REG_SEED_TARGET_PASSWORD"
            )
        seed = SeedTarget(
            name=_get(env, "REG_SEED_TARGET_NAME", "env-seeded") or "env-seeded",
            mgmt_url=seed_url.rstrip("/"),
            web_url=(_get(env, "REG_SEED_TARGET_WEB_URL") or None),
            token=seed_token,
            username=seed_user,
            password=seed_pass,
            verify_tls=_boolean(env, "REG_SEED_TARGET_VERIFY_TLS", True),
            app=_get(env, "REG_SEED_TARGET_APP", "search") or "search",
            owner=_get(env, "REG_SEED_TARGET_OWNER", "nobody") or "nobody",
        )

    default_user_dir = _get(env, "REG_USER_SCENARIOS_DIR")
    if not default_user_dir:
        # Next to the database when it is a file, so one volume carries both.
        if database_url.startswith("sqlite:///"):
            db_path = Path(database_url[len("sqlite:///"):])
            default_user_dir = str((db_path.parent if db_path.parent != Path("") else Path(".")) / "scenarios")
        else:
            default_user_dir = "./data/scenarios"

    return ServerConfig(
        database_url=database_url,
        master_key=master_key,
        master_key_generated=generated,
        admin_password=_get(env, "REG_ADMIN_PASSWORD"),
        session_ttl_s=_integer(env, "REG_SESSION_TTL_S", 43200, minimum=60),
        scenarios_dir=_get(env, "REG_BUILTIN_SCENARIOS_DIR"),
        user_scenarios_dir=default_user_dir,
        max_virtual_users=_integer(env, "REG_MAX_VIRTUAL_USERS", 500, minimum=1),
        max_concurrent_runs=_integer(env, "REG_MAX_CONCURRENT_RUNS", 2, minimum=1),
        max_run_duration_s=float(_integer(env, "REG_MAX_RUN_DURATION_S", 4 * 3600, minimum=1)),
        api_tokens=tokens,
        allow_unauthenticated=_boolean(env, "REG_ALLOW_UNAUTHENTICATED", False),
        hec=hec,
        seed_target=seed,
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
