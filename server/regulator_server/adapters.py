"""Turning a stored Target into something the worker can use.

The control plane does not reimplement any of the worker. It imports it. A
Target row becomes a worker ``Config``, and everything below that (the splunkd
client, the report, the cache manager, the scheduler) is the same code the
standalone agent runs, so the UI and the command line cannot drift apart in
behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import importlib.util

from regulator_agent.config import Config, HecConfig, TargetConfig
from regulator_agent.scenario import Scenario, lint, load_scenario, scenario_digest

from .config import ServerConfig, get_settings
from .crypto import decrypt
from .models import Target

# Where the built-in scenario library lives when nothing overrides it. In the
# image this is /app/scenarios; from a checkout it is the repo's scenarios/.
_REPO_SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def scenarios_dir() -> Path:
    """The built-in library that ships in the image."""
    configured = get_settings().scenarios_dir
    if configured:
        return Path(configured)
    return _REPO_SCENARIOS


def user_scenarios_dir() -> Path:
    """Where scenarios created through the API live. Survives an image upgrade."""
    return Path(get_settings().user_scenarios_dir)


def _libraries() -> List[tuple[Path, str]]:
    return [(scenarios_dir(), "builtin"), (user_scenarios_dir(), "user")]


def browser_engine_available() -> bool:
    """Whether this process could run a browser scenario at all."""
    return importlib.util.find_spec("playwright") is not None


def list_scenarios() -> List[tuple[Scenario, str]]:
    """Every scenario in both libraries that parses, with which library it came from.

    One that does not parse is skipped rather than fatal: a broken scenario
    should not stop the operator running the other nine. A user scenario with
    the same name as a built-in one is hidden, because the run row stores only
    the name and the built-in library is resolved first.
    """
    found: List[tuple[Scenario, str]] = []
    seen: set[str] = set()
    for root, origin in _libraries():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not (child / "scenario.yaml").is_file() or child.name in seen:
                continue
            try:
                found.append((load_scenario(child), origin))
                seen.add(child.name)
            except Exception:  # noqa: BLE001 - a broken scenario is skipped, not fatal
                continue
    return found


def scenario_path(name: str) -> tuple[Path, str]:
    """Resolve a scenario name to a directory inside one of the two libraries.

    The containment check is not belt and braces on top of the schema
    validator, it is the actual boundary: a scenario name reaches here from a
    stored run row as well as from a request, and an absolute path or a
    traversal would otherwise load and execute a scenario from anywhere on the
    filesystem, turning anything that can drop a file on the box into arbitrary
    SPL against the target.
    """
    for root, origin in _libraries():
        root = root.resolve()
        candidate = (root / name).resolve()
        if root != candidate and root not in candidate.parents:
            raise FileNotFoundError(
                f"scenario {name!r} resolves outside the scenario library at {root}"
            )
        if (candidate / "scenario.yaml").is_file():
            return candidate, origin
    raise FileNotFoundError(f"no scenario named {name!r} in the scenario libraries")


def load_named_scenario(name: str) -> Scenario:
    """Load a scenario from the libraries, and only from the libraries."""
    directory, _ = scenario_path(name)
    return load_scenario(directory)


def scenario_summary(scenario: Scenario, origin: str = "builtin") -> Dict[str, object]:
    engines = sorted({step.engine for step in scenario.steps})
    runnable_here = True
    reason = ""
    if len(engines) > 1:
        runnable_here = False
        reason = "mixes the api and browser engines, which needs the worker fleet"
    elif "browser" in engines and not browser_engine_available():
        runnable_here = False
        reason = (
            "a browser scenario: this control plane image carries no browser, so run it "
            "from the browser worker image until the fleet lands"
        )
    saved = [step for step in scenario.steps if step.saved]
    return {
        "name": scenario.name,
        "description": scenario.description,
        "engine": scenario.engine,
        "origin": origin,
        "tags": list(scenario.tags),
        "personas": len(scenario.personas),
        "steps": len(scenario.steps),
        "saved_searches": len(saved),
        "load_model": scenario.load.model,
        "virtual_users": scenario.load.virtual_users,
        "duration_s": scenario.load.duration_s,
        "requires_packs": list(scenario.corpus.requires_packs),
        "sourcetypes": list(scenario.corpus.sourcetypes),
        "index": scenario.corpus.index,
        "seed": scenario.seed,
        "digest": scenario_digest(scenario),
        "runnable_here": runnable_here,
        "not_runnable_reason": reason,
        "lint": lint(scenario),
    }


def hec_config(settings: ServerConfig) -> Optional[HecConfig]:
    """The control plane's telemetry destination as the worker's HecConfig."""
    if settings.hec is None:
        return None
    return HecConfig(
        url=settings.hec.url,
        token=settings.hec.token,
        index=settings.hec.index,
        source=settings.hec.source,
        verify_tls=settings.hec.verify_tls,
        sourcetype_step=settings.hec.sourcetype_step,
        sourcetype_run=settings.hec.sourcetype_run,
        sourcetype_sample=settings.hec.sourcetype_sample,
        sourcetype_lifecycle=settings.hec.sourcetype_lifecycle,
        sourcetype_health=settings.hec.sourcetype_health,
        gzip=settings.hec.gzip,
        batch_bytes=settings.hec.batch_bytes,
        batch_ms=settings.hec.batch_ms,
    )


def target_config(target: Target) -> TargetConfig:
    """Decrypt a target's credentials just long enough to use them."""
    return TargetConfig(
        url=target.mgmt_url,
        web_url=target.web_url,
        token=decrypt(target.token_encrypted),
        username=target.username,
        password=decrypt(target.password_encrypted),
        verify_tls=target.verify_tls,
        app=target.app,
        owner=target.owner,
        api_version=target.api_version,
    )


def indexer_settings(target: Target) -> Dict[str, object]:
    """The indexer-side settings a target carries, decrypted for a worker Config."""
    urls = tuple(
        part.strip() for part in (target.indexer_urls or "").split(",") if part.strip()
    )
    return {
        "indexer_urls": urls,
        "indexer_token": decrypt(target.indexer_token_encrypted),
        "indexer_username": target.indexer_username,
        "indexer_password": decrypt(target.indexer_password_encrypted),
    }


def worker_config(
    target: Target,
    *,
    scenario_path: str,
    virtual_users: Optional[int] = None,
    duration_s: Optional[float] = None,
    arrival_rate_per_min: Optional[float] = None,
    pacing_s: Optional[float] = None,
    run_label: str = "cp",
    evict_cache: bool = False,
    evict_cache_indexes: Optional[List[str]] = None,
    hec: Optional[HecConfig] = None,
    seed: Optional[int] = None,
    cold_window_s: Optional[float] = None,
) -> Config:
    """Build the worker's own Config for an in-process run.

    Note ``standalone=True``. A fleet worker takes the same values from its
    claim (see ``fleet._claim_env``); this is the in-process path, where the
    scheduler runs inside the control plane. Fine for tens to low hundreds of
    virtual users, and honest about it, because the generator-drift guard
    invalidates a run where this process could not keep to its own schedule
    rather than reporting a number that describes the server.
    """
    return Config(
        standalone=True,
        scenario_path=scenario_path,
        target=target_config(target),
        hec=hec,
        virtual_users=virtual_users,
        duration_s=duration_s,
        arrival_rate_per_min=arrival_rate_per_min,
        pacing_s=pacing_s,
        seed=seed,
        run_id=run_label,
        slot=0,
        total_workers=1,
        max_in_flight=512,
        connect_timeout_s=10.0,
        read_timeout_s=300.0,
        http2=False,
        delete_jobs=True,
        poll_initial_ms=250,
        poll_max_ms=1000,
        cache_bust=True,
        evict_cache=evict_cache,
        evict_cache_indexes=tuple(evict_cache_indexes or ()),
        output_path=None,
        summary_path=None,
        log_level="INFO",
        metrics_port=0,
        builtin_scenarios_dir=str(scenarios_dir()),
        cold_window_s=cold_window_s,
        **indexer_settings(target),
    )
