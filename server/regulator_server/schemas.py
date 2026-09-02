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
    # Deliberately absent: token, username, password. There is no field to
    # accidentally populate.


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
    tags: List[str] = Field(default_factory=list)
    personas: int
    steps: int
    virtual_users: int
    duration_s: Optional[float] = None
    requires_packs: List[str] = Field(default_factory=list)
    index: str
    seed: int
    lint: List[str] = Field(default_factory=list)


class RunCreate(BaseModel):
    target_id: int
    scenario: str
    label: Optional[str] = None
    virtual_users: Optional[int] = Field(default=None, ge=1)
    duration_s: Optional[float] = Field(default=None, gt=0)
    arrival_rate_per_min: Optional[float] = Field(default=None, gt=0)
    pacing_s: Optional[float] = Field(default=None, ge=0)
    evict_cache: bool = False
    evict_cache_indexes: List[str] = Field(default_factory=list)

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


class RunOut(BaseModel):
    model_config = _ORM

    id: int
    label: Optional[str]
    target_id: int
    target_name: Optional[str] = None
    scenario: str
    state: str
    virtual_users: Optional[int]
    duration_s: Optional[float]
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
