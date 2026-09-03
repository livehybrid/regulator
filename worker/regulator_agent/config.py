"""Environment configuration for the worker.

Every connection detail Regulator needs is settable by environment variable.
That is not just convenience: it is what makes an ephemeral deployment
disposable. Stoker's Kubernetes deployment has a standing annoyance where a
freshly built control plane knows about no targets and no packs, so every
nightly rebuild needs manual steps before it can do anything. Regulator is
built so a cold start with the right environment is immediately runnable.

Design rules, inherited from Stoker's config module because they earned their
place there:

* Parse once into a frozen dataclass. Nothing later in the process re-reads
  ``os.environ`` and gets a different answer.
* Secrets carry ``repr=False`` so an accidental log line or a pytest fixture
  dump cannot leak a token.
* A malformed value is a hard boot error, never a silent fallback to a default.
  A typo in a verify-TLS flag must not quietly widen the trust boundary.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional

# Managed mode needs a control plane, which lands in Phase 1. The env contract
# is already fixed here so the worker and the (future) control plane cannot
# drift apart while they are written weeks apart.
MANAGED_ENV_KEYS = ("REG_RUN_ID", "REG_CONTROL_URL", "REG_RUN_JWT")


@dataclass(frozen=True)
class ManagedBoot:
    """What a fleet member knows before it has claimed a slot.

    Only enough to reach the control plane and prove which run it belongs to.
    Everything else (the target, the scenario, its share of the load) arrives
    in the claim response, so the launcher never has to project a credential
    into a container's environment.
    """

    run_id: str
    control_url: str
    jwt: str = field(repr=False)
    hint_slot: Optional[int] = None
    holder: str = ""
    deadman_s: float = 300.0


def load_managed_boot(env: Optional[Mapping[str, str]] = None) -> Optional[ManagedBoot]:
    """The managed-mode bootstrap, or None when this is a standalone worker."""
    env = os.environ if env is None else env
    present = [key for key in MANAGED_ENV_KEYS if _get(env, key)]
    if not present:
        return None
    if len(present) != len(MANAGED_ENV_KEYS):
        missing = [key for key in MANAGED_ENV_KEYS if key not in present]
        raise ConfigError(
            f"managed mode needs all of {', '.join(MANAGED_ENV_KEYS)}; missing "
            f"{', '.join(missing)}"
        )
    hint_raw = _get(env, "REG_HINT_SLOT") or _get(env, "JOB_COMPLETION_INDEX")
    hint: Optional[int] = None
    if hint_raw is not None:
        try:
            hint = int(hint_raw)
        except ValueError as exc:
            raise ConfigError(f"REG_HINT_SLOT must be an integer, got {hint_raw!r}") from exc
    import socket

    return ManagedBoot(
        run_id=_require(env, "REG_RUN_ID", "the run this worker belongs to"),
        control_url=_url(env, "REG_CONTROL_URL", _require(env, "REG_CONTROL_URL", "the control plane")) or "",
        jwt=_require(env, "REG_RUN_JWT", "the per-run token the control plane minted"),
        hint_slot=hint,
        holder=_get(env, "REG_HOLDER") or socket.gethostname(),
        deadman_s=_number(env, "REG_DEADMAN_S", 300.0, minimum=10.0),
    )


class ConfigError(ValueError):
    """Raised for a malformed or missing environment value.

    Deliberately fatal. The alternative, defaulting past a bad value, is how a
    load test ends up running against the wrong cluster or with TLS
    verification silently off.
    """


def _get(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(env: Mapping[str, str], key: str, why: str) -> str:
    value = _get(env, key)
    if not value:
        raise ConfigError(f"{key} is required: {why}")
    return value


def _boolean(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"{key} must be one of 1/0/true/false/yes/no/on/off, got {raw!r}"
    )


def _integer(env: Mapping[str, str], key: str, default: int, minimum: Optional[int] = None) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _number(env: Mapping[str, str], key: str, default: float, minimum: Optional[float] = None) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _url(env: Mapping[str, str], key: str, value: Optional[str]) -> Optional[str]:
    """Reject a URL that is not http or https, and strip a trailing slash.

    A trailing slash matters: splunkd happily serves ``//services/...`` but the
    path then differs from what we record, and joining paths becomes a source
    of one-off bugs. Normalise here, once.
    """
    if value is None:
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ConfigError(f"{key} must start with http:// or https://, got {value!r}")
    return value.rstrip("/")


@dataclass(frozen=True)
class TargetConfig:
    """The Splunk instance under test."""

    # splunkd management interface, normally port 8089. This is where searches
    # are dispatched.
    url: str
    # Splunk Web, normally port 8000. Only the browser engine needs it, so it
    # stays optional in Phase 0.
    web_url: Optional[str] = None
    # A bearer token is strongly preferred over a username and password: it
    # avoids re-running the login flow (and paying its cost) thousands of times
    # during a run, and it never expires mid-test the way a session key does at
    # roughly one hour.
    token: Optional[str] = field(default=None, repr=False)
    username: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    verify_tls: bool = True
    # Namespace for dispatched searches. A search dispatched into the wrong app
    # sees different knowledge objects, so this is part of the test definition
    # rather than an incidental detail.
    app: str = "search"
    owner: str = "nobody"
    # v2 has been the current search endpoint for several releases and v1 is
    # documented as going away, but an old stack may still need v1.
    api_version: str = "v2"

    def __post_init__(self) -> None:
        if self.api_version not in ("v1", "v2"):
            raise ConfigError(
                f"REG_TARGET_API_VERSION must be v1 or v2, got {self.api_version!r}"
            )
        if not self.token and not (self.username and self.password):
            raise ConfigError(
                "a target needs REG_TARGET_TOKEN, or both REG_TARGET_USERNAME and "
                "REG_TARGET_PASSWORD"
            )

    @property
    def jobs_path(self) -> str:
        return "/services/search/v2/jobs" if self.api_version == "v2" else "/services/search/jobs"


@dataclass(frozen=True)
class HecConfig:
    """Where the worker ships its own results.

    Note the deliberate default of ``None``. Writing results into the cluster
    you are measuring adds load to it, so shipping telemetry is opt-in and the
    endpoint is separately configurable. When it is the same instance as the
    target, the run record is flagged self-instrumented.
    """

    url: str
    token: str = field(repr=False)
    index: Optional[str] = None
    source: str = "regulator"
    sourcetype_step: str = "regulator:step"
    sourcetype_run: str = "regulator:run"
    gzip: bool = True
    verify_tls: bool = True
    batch_bytes: int = 512 * 1024
    batch_ms: int = 200


@dataclass(frozen=True)
class Config:
    """The whole worker configuration, parsed once."""

    standalone: bool
    scenario_path: str
    target: TargetConfig
    hec: Optional[HecConfig]

    # Load shape. These override whatever the scenario declares, so one
    # scenario can be run at many sizes without editing it. That matters for a
    # CI ramp: same scenario, different virtual-user counts, comparable results.
    virtual_users: Optional[int]
    duration_s: Optional[float]
    arrival_rate_per_min: Optional[float]
    pacing_s: Optional[float]
    seed: Optional[int]

    # Identity. In standalone mode these are cosmetic labels that end up on
    # every step record, which is what lets you tell two local runs apart.
    run_id: str
    slot: int
    total_workers: int

    # Client behaviour.
    max_in_flight: int
    connect_timeout_s: float
    read_timeout_s: float
    http2: bool
    delete_jobs: bool
    poll_initial_ms: int
    poll_max_ms: int
    # Defeats Splunk's dispatch result cache by putting a per-iteration nonce in
    # a comment, which changes the search string without changing the work.
    # Configurable only so a test can prove the difference it makes.
    cache_bust: bool

    # SmartStore. Eviction is opt-in and never a default: throwing away a warm
    # cache makes the next run pay to re-download everything it touches, which
    # on a cloud object store costs real money and real time.
    evict_cache: bool
    evict_cache_indexes: tuple[str, ...]

    # Output.
    output_path: Optional[str]
    summary_path: Optional[str]
    log_level: str
    metrics_port: int
    builtin_scenarios_dir: Optional[str]

    # Run behaviour that used to be read straight from os.environ at the point
    # of use, which meant a typo surfaced after the load had been generated
    # rather than at boot.
    lint_strict: bool = True
    # How long in-flight searches may finish after the run ends before they
    # are abandoned. Abandoned work is recorded as a cancelled failure with
    # the time it had accrued, never silently dropped.
    drain_budget_s: float = 60.0
    # How long to wait for the target's own logging to catch up before asking
    # it about the run.
    sut_settle_s: float = 8.0
    # Discovered or configured indexer management URIs, for SmartStore cache
    # state on a distributed target. Empty means discover from the search
    # peers, with the target's own credential.
    indexer_urls: tuple[str, ...] = ()
    indexer_token: Optional[str] = field(default=None, repr=False)
    indexer_username: Optional[str] = None
    indexer_password: Optional[str] = field(default=None, repr=False)

    @property
    def self_instrumented(self) -> bool:
        """True when telemetry is being written to the cluster under test.

        Not an error, sometimes it is the only Splunk available, but the run
        record carries the flag so nobody later compares a self-instrumented
        run against a clean one without noticing.
        """
        if self.hec is None:
            return False
        target_host = self.target.url.split("://", 1)[-1].split(":")[0].split("/")[0]
        hec_host = self.hec.url.split("://", 1)[-1].split(":")[0].split("/")[0]
        return target_host == hec_host


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Parse the environment into a :class:`Config`.

    Accepts an explicit mapping so tests never mutate the real environment.
    """
    env = os.environ if env is None else env

    standalone = _boolean(env, "REG_STANDALONE", False)
    managed_present = [key for key in MANAGED_ENV_KEYS if _get(env, key)]
    if not standalone and managed_present:
        # A managed worker never reaches here: the entrypoint claims its slot
        # from the control plane first and builds this Config from the claim.
        raise ConfigError(
            f"{', '.join(managed_present)} is set: this worker is managed, so its "
            "configuration comes from the control plane's claim response, not from "
            "the environment. Run it through the entrypoint"
        )
    if not standalone:
        raise ConfigError(
            "set REG_STANDALONE=1 to run against the environment, or REG_RUN_ID, "
            "REG_CONTROL_URL and REG_RUN_JWT to run as a fleet member"
        )

    scenario_path = _require(
        env, "REG_SCENARIO", "the scenario directory or scenario.yaml to run"
    )

    target = TargetConfig(
        url=_url(env, "REG_TARGET_URL", _require(
            env, "REG_TARGET_URL", "the splunkd management URI of the system under test"
        )) or "",
        web_url=_url(env, "REG_TARGET_WEB_URL", _get(env, "REG_TARGET_WEB_URL")),
        token=_get(env, "REG_TARGET_TOKEN"),
        username=_get(env, "REG_TARGET_USERNAME"),
        password=_get(env, "REG_TARGET_PASSWORD"),
        verify_tls=_boolean(env, "REG_TARGET_VERIFY_TLS", True),
        app=_get(env, "REG_TARGET_APP", "search") or "search",
        owner=_get(env, "REG_TARGET_OWNER", "nobody") or "nobody",
        api_version=_get(env, "REG_TARGET_API_VERSION", "v2") or "v2",
    )

    hec: Optional[HecConfig] = None
    hec_url = _url(env, "REG_HEC_URL", _get(env, "REG_HEC_URL"))
    hec_token = _get(env, "REG_HEC_TOKEN")
    if hec_url or hec_token:
        # Half a configuration is worse than none: it looks like telemetry is
        # on when it silently is not.
        if not (hec_url and hec_token):
            raise ConfigError(
                "REG_HEC_URL and REG_HEC_TOKEN must be set together, or neither"
            )
        hec = HecConfig(
            url=hec_url,
            token=hec_token,
            index=_get(env, "REG_HEC_INDEX"),
            source=_get(env, "REG_HEC_SOURCE", "regulator") or "regulator",
            sourcetype_step=_get(env, "REG_HEC_SOURCETYPE_STEP", "regulator:step") or "regulator:step",
            sourcetype_run=_get(env, "REG_HEC_SOURCETYPE_RUN", "regulator:run") or "regulator:run",
            gzip=_boolean(env, "REG_HEC_GZIP", True),
            verify_tls=_boolean(env, "REG_HEC_VERIFY_TLS", True),
            batch_bytes=_integer(env, "REG_HEC_BATCH_BYTES", 512 * 1024, minimum=1024),
            batch_ms=_integer(env, "REG_HEC_BATCH_MS", 200, minimum=1),
        )

    virtual_users = _integer(env, "REG_VUS", 0, minimum=0) or None
    arrival_rate = _number(env, "REG_ARRIVAL_RATE_PER_MIN", 0.0, minimum=0.0) or None
    if virtual_users and arrival_rate:
        raise ConfigError(
            "set REG_VUS (closed model) or REG_ARRIVAL_RATE_PER_MIN (open model), not both"
        )

    seed_raw = _get(env, "REG_SEED")
    seed = _integer(env, "REG_SEED", 0) if seed_raw is not None else None

    poll_initial = _integer(env, "REG_POLL_INITIAL_MS", 250, minimum=10)
    poll_max = _integer(env, "REG_POLL_MAX_MS", 1000, minimum=10)
    if poll_max < poll_initial:
        raise ConfigError(
            f"REG_POLL_MAX_MS ({poll_max}) must be >= REG_POLL_INITIAL_MS ({poll_initial})"
        )

    log_level = (_get(env, "REG_LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(
            f"REG_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {log_level!r}"
        )

    return Config(
        standalone=standalone,
        scenario_path=scenario_path,
        target=target,
        hec=hec,
        virtual_users=virtual_users,
        duration_s=_number(env, "REG_DURATION_S", 0.0, minimum=0.0) or None,
        arrival_rate_per_min=arrival_rate,
        pacing_s=_number(env, "REG_PACING_S", 0.0, minimum=0.0) or None,
        seed=seed,
        # The default carries the start time so two default-configured runs
        # never emit byte-identical cache-busting markers for the same
        # (virtual user, iteration, step): with a pinned time policy that
        # made the second run's searches identical strings served straight
        # from the dispatch cache, which is the exact hole the marker exists
        # to close.
        run_id=_get(env, "REG_RUN_LABEL") or f"local-{int(time.time())}",
        slot=_integer(env, "REG_SLOT", 0, minimum=0),
        total_workers=_integer(env, "REG_TOTAL_WORKERS", 1, minimum=1),
        max_in_flight=_integer(env, "REG_MAX_IN_FLIGHT", 512, minimum=1),
        connect_timeout_s=_number(env, "REG_CONNECT_TIMEOUT_S", 10.0, minimum=0.1),
        read_timeout_s=_number(env, "REG_READ_TIMEOUT_S", 300.0, minimum=1.0),
        http2=_boolean(env, "REG_HTTP2", False),
        delete_jobs=_boolean(env, "REG_DELETE_JOBS", True),
        poll_initial_ms=poll_initial,
        poll_max_ms=poll_max,
        cache_bust=_boolean(env, "REG_CACHE_BUST", True),
        evict_cache=_boolean(env, "REG_EVICT_CACHE", False),
        evict_cache_indexes=tuple(
            part.strip()
            for part in (_get(env, "REG_EVICT_CACHE_INDEXES", "") or "").split(",")
            if part.strip()
        ),
        output_path=_get(env, "REG_OUTPUT"),
        summary_path=_get(env, "REG_SUMMARY_PATH"),
        log_level=log_level,
        metrics_port=_integer(env, "REG_METRICS_PORT", 0, minimum=0),
        builtin_scenarios_dir=_get(env, "REG_BUILTIN_SCENARIOS_DIR"),
        lint_strict=_boolean(env, "REG_LINT_STRICT", True),
        drain_budget_s=_number(env, "REG_DRAIN_BUDGET_S", 60.0, minimum=1.0),
        sut_settle_s=_number(env, "REG_SUT_SETTLE_S", 8.0, minimum=0.0),
        indexer_urls=tuple(
            _url(env, "REG_INDEXER_URLS", part.strip()) or ""
            for part in (_get(env, "REG_INDEXER_URLS", "") or "").split(",")
            if part.strip()
        ),
        indexer_token=_get(env, "REG_INDEXER_TOKEN"),
        indexer_username=_get(env, "REG_INDEXER_USERNAME"),
        indexer_password=_get(env, "REG_INDEXER_PASSWORD"),
    )
