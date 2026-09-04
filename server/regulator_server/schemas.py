"""Request and response shapes.

The load-bearing property here is that **no response model has a field for a
credential**. Not "we remember not to fill it in": there is nowhere to put it.
A test asserts that no response body from any GET contains a known secret.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ORM = ConfigDict(from_attributes=True)

# A run label ends up inside the SPL comment appended to every dispatched
# search, and inside a quoted operand in the _audit correlation query. Three
# backticks close the comment; a double quote closes the operand. Unfiltered,
# it was an SPL injection reaching every search a run dispatched, running under
# the target's own stored credentials, in a tool that otherwise deliberately
# offers no way to dispatch arbitrary SPL. The GitHub Action makes it worse by
# sourcing the label from a branch name, and git permits backticks there.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# A scenario name is joined onto the library directory. Unfiltered, "../.." or
# an absolute path loaded and EXECUTED a scenario from anywhere on the
# filesystem, which turns anything that can drop a file on the box into
# arbitrary SPL against the target.
_SCENARIO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    mgmt_url: str
    web_url: Optional[str] = None
    # Write-only. A bearer token is strongly preferred: it avoids re-running
    # the login flow thousands of times during a run and cannot expire
    # mid-test the way a session key does at roughly an hour.
    token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_tls: bool = True
    app: str = "search"
    owner: str = "nobody"
    api_version: str = "v2"
    # SmartStore state lives on the indexers. On a distributed target these
    # say where they are and how to log in; empty means discover the search
    # peers and reuse the target's own credential on each.
    indexer_urls: List[str] = Field(default_factory=list)
    indexer_token: Optional[str] = None
    indexer_username: Optional[str] = None
    indexer_password: Optional[str] = None

    @field_validator("indexer_urls")
    @classmethod
    def _indexers_http_only(cls, value: List[str]) -> List[str]:
        cleaned = []
        for url in value:
            url = (url or "").strip()
            if not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(f"indexer URL {url!r} must start with http:// or https://")
            if urlsplit(url).username or urlsplit(url).password:
                raise ValueError("an indexer URL must not embed a credential")
            cleaned.append(url.rstrip("/"))
        return cleaned

    @field_validator("mgmt_url", "web_url")
    @classmethod
    def _http_only(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("must start with http:// or https://")
        # A credential in the URL would be echoed back: the base URL appears in
        # transport exception messages, which land in health_detail and in a
        # run's error, both of which are returned by the API and rendered in
        # the UI. Credentials belong in the token or password field, which is
        # write-only by construction.
        parts = urlsplit(value)
        if parts.username or parts.password:
            raise ValueError(
                "a URL must not embed a username or password: put the credential in the "
                "token, or the username and password fields, which are never returned"
            )
        return value.rstrip("/")

    @field_validator("api_version")
    @classmethod
    def _known_version(cls, value: str) -> str:
        if value not in ("v1", "v2"):
            raise ValueError("must be v1 or v2")
        return value

    def check_credentials(self) -> None:
        if not self.token and not (self.username and self.password):
            raise ValueError("supply a token, or both a username and a password")


class TargetOut(BaseModel):
    model_config = _ORM

    id: int
    name: str
    mgmt_url: str
    web_url: Optional[str]
    verify_tls: bool
    app: str
    owner: str
    api_version: str
    created_at: float
    health: str
    health_detail: Optional[str]
    indexer_urls: Optional[str] = None
    indexer_username: Optional[str] = None
    # Deliberately absent: token, username, password, and their indexer
    # counterparts. There is no field to accidentally populate.


class TargetTestResult(BaseModel):
    ok: bool
    detail: str
    version: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    cores: Optional[int] = None
    max_hist_searches: Optional[int] = None


class EvictRequest(BaseModel):
    indexes: List[str] = Field(default_factory=list)
    all_indexes: bool = False

    def check(self) -> None:
        if not self.indexes and not self.all_indexes:
            raise ValueError(
                "name at least one index, or set all_indexes. There is no undo beyond "
                "waiting for everything to re-download, and on a shared cluster most of "
                "that cache belongs to other people's dashboards"
            )


class ScenarioOut(BaseModel):
    name: str
    description: str
    engine: str
    origin: str = "builtin"
    tags: List[str] = Field(default_factory=list)
    personas: int
    steps: int
    saved_searches: int = 0
    load_model: str = "closed"
    virtual_users: int
    duration_s: Optional[float] = None
    requires_packs: List[str] = Field(default_factory=list)
    sourcetypes: List[str] = Field(default_factory=list)
    index: str
    seed: int
    digest: Optional[str] = None
    runnable_here: bool = True
    not_runnable_reason: str = ""
    lint: List[str] = Field(default_factory=list)


class ScenarioCreate(BaseModel):
    """A scenario built from a savedsearches.conf.

    The conf text is stored verbatim next to a generated scenario.yaml, so
    what runs is exactly what was pasted, in Splunk's own format.
    """

    name: str
    savedsearches: str = Field(min_length=1, max_length=4_000_000)
    description: str = ""
    app: Optional[str] = None
    # closed: virtual users sampling the searches by cron weight.
    # schedule: every scheduled search fires on its own cron.
    load_model: str = "closed"
    virtual_users: int = Field(default=10, ge=1)
    duration_s: float = Field(default=600.0, gt=0)
    only_enabled: bool = True
    only_scheduled: bool = False
    allow_side_effects: bool = False
    time_from_saved: str = "derived"
    index: str = "main"
    seed: int = Field(default=0, ge=0)
    think_median_s: float = Field(default=30.0, ge=0)
    schedule_start: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", value):
            raise ValueError(
                "a scenario name is lowercase letters, digits, dashes and underscores, up "
                "to 64 characters"
            )
        return value

    @field_validator("load_model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in ("closed", "schedule"):
            raise ValueError("load_model must be closed or schedule")
        return value

    @field_validator("time_from_saved")
    @classmethod
    def _known_time_mode(cls, value: str) -> str:
        if value not in ("derived", "as_saved"):
            raise ValueError("time_from_saved must be derived or as_saved")
        return value

    @field_validator("schedule_start")
    @classmethod
    def _hhmm(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if not re.match(r"^\d{1,2}:\d{2}$", value):
            raise ValueError("schedule_start must be HH:MM")
        return value


class SavedSearchPreview(BaseModel):
    name: str
    app: Optional[str]
    search: str
    cron: Optional[str]
    scheduled: bool
    disabled: bool
    earliest: str
    latest: str
    guessed_class: str
    side_effects: List[str] = Field(default_factory=list)
    firings_per_day: Optional[float] = None
    skipped_reason: Optional[str] = None


class RunCreate(BaseModel):
    target_id: int
    scenario: str
    label: Optional[str] = None
    virtual_users: Optional[int] = Field(default=None, ge=1, le=100_000)
    # Bounded, and finite: json accepts the non-standard Infinity literal
    # and gt=0 let it through, so a run could occupy a slot for ever.
    duration_s: Optional[float] = Field(default=None, gt=0, le=7 * 86400, allow_inf_nan=False)
    arrival_rate_per_min: Optional[float] = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    pacing_s: Optional[float] = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    seed: Optional[int] = Field(default=None, ge=1)
    evict_cache: bool = False
    evict_cache_indexes: List[str] = Field(default_factory=list)
    evict_all_indexes: bool = False
    # Evict again every N seconds during the run (same scope as above), so
    # one run measures the cold path repeatedly; executions within
    # cold_window_s of an eviction are its cold part (default: half of N).
    evict_every_s: Optional[float] = Field(default=None, ge=10, le=86400, allow_inf_nan=False)
    cold_window_s: Optional[float] = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    # Where the load is generated: inprocess (this control plane), swarm or
    # k8s (worker containers). Blank means the configured default.
    fleet: Optional[str] = None
    # An explicit worker count; blank means one per REG_VUS_PER_WORKER users.
    workers: Optional[int] = Field(default=None, ge=1, le=500)

    @field_validator("fleet")
    @classmethod
    def _known_fleet(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if value not in ("inprocess", "swarm", "k8s"):
            raise ValueError("fleet must be inprocess, swarm or k8s")
        return value

    @field_validator("scenario")
    @classmethod
    def _safe_scenario(cls, value: str) -> str:
        if not _SCENARIO_RE.match(value):
            raise ValueError(
                "a scenario name may contain letters, digits, dots, underscores and "
                "dashes only. It is joined onto the scenario library path, so anything "
                "else could load a scenario from outside it"
            )
        return value

    @field_validator("label")
    @classmethod
    def _safe_label(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not _LABEL_RE.match(value):
            raise ValueError(
                "a label may contain letters, digits, dots, colons, underscores and "
                "dashes only. It is embedded in the SPL comment appended to every "
                "search this run dispatches"
            )
        return value

    def check(self) -> None:
        if self.virtual_users and self.arrival_rate_per_min:
            raise ValueError(
                "set virtual_users (closed model) or arrival_rate_per_min (open model), "
                "not both"
            )
        for name in self.evict_cache_indexes:
            if not re.match(r"^[A-Za-z0-9_:.-]{1,128}$", name or ""):
                raise ValueError(f"index name {name!r} is not a valid Splunk index name")
        if self.evict_every_s:
            if not (self.evict_cache_indexes or self.evict_all_indexes):
                raise ValueError(
                    "evict_every_s needs a scope: name the indexes in evict_cache_indexes or set evict_all_indexes"
                )
            if self.duration_s and self.evict_every_s >= self.duration_s:
                raise ValueError("evict_every_s must be shorter than the run's duration")
            if self.cold_window_s and self.cold_window_s > self.evict_every_s:
                raise ValueError("cold_window_s cannot be longer than evict_every_s")
        elif self.cold_window_s:
            raise ValueError("cold_window_s only makes sense with evict_every_s")


class RunOut(BaseModel):
    model_config = _ORM

    id: int
    label: Optional[str]
    target_id: Optional[int]
    target_name: Optional[str] = None
    scenario: str
    state: str
    virtual_users: Optional[int]
    duration_s: Optional[float]
    seed: Optional[int] = None
    scenario_digest: Optional[str] = None
    fleet: str = "inprocess"
    workers: int = 1
    fleet_state: Optional[str] = None
    created_at: float
    started_at: Optional[float]
    ended_at: Optional[float]
    error: Optional[str]
    stats: Optional[Dict[str, Any]] = None
    summary: Optional[Dict[str, Any]] = None

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.ended_at or time.time()) - self.started_at


class LoginRequest(BaseModel):
    password: str


class AuthStatus(BaseModel):
    authenticated: bool
    # True when no admin password is configured at all, so the deployment is
    # wide open. The UI says so rather than pretending everything is fine.
    setup_needed: bool
