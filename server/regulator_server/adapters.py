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

from regulator_agent.config import Config, HecConfig, TargetConfig
from regulator_agent.scenario import Scenario, lint, load_scenario

from .config import get_settings
from .crypto import decrypt
from .models import Target

# Where the built-in scenario library lives when nothing overrides it. In the
# image this is /app/scenarios; from a checkout it is the repo's scenarios/.
_REPO_SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def scenarios_dir() -> Path:
    configured = get_settings().scenarios_dir
    if configured:
        return Path(configured)
    return _REPO_SCENARIOS


def list_scenarios() -> List[Scenario]:
    """Every scenario in the library that parses.

    One that does not parse is skipped rather than fatal: a broken scenario
    should not stop the operator running the other nine.
    """
    root = scenarios_dir()
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if not (child / "scenario.yaml").is_file():
            continue
        try:
            found.append(load_scenario(child))
        except Exception:  # noqa: BLE001 - a broken scenario is skipped, not fatal
            continue
    return found


def load_named_scenario(name: str) -> Scenario:
    """Load a scenario from the library, and only from the library.

    The containment check is not belt and braces on top of the schema
    validator, it is the actual boundary: a scenario name reaches here from a
    stored run row as well as from a request, and an absolute path or a
    traversal would otherwise load and execute a scenario from anywhere on the
    filesystem, turning anything that can drop a file on the box into arbitrary
    SPL against the target.
    """
    root = scenarios_dir().resolve()
    candidate = (root / name).resolve()
    if root != candidate and root not in candidate.parents:
        raise FileNotFoundError(
            f"scenario {name!r} resolves outside the scenario library at {root}"
        )
    if not (candidate / "scenario.yaml").is_file():
        raise FileNotFoundError(f"no scenario named {name!r} in {root}")
    return load_scenario(candidate)


def scenario_summary(scenario: Scenario) -> Dict[str, object]:
    return {
        "name": scenario.name,
        "description": scenario.description,
        "engine": scenario.engine,
        "tags": list(scenario.tags),
        "personas": len(scenario.personas),
        "steps": len(scenario.steps),
        "virtual_users": scenario.load.virtual_users,
        "duration_s": scenario.load.duration_s,
        "requires_packs": list(scenario.corpus.requires_packs),
        "index": scenario.corpus.index,
        "seed": scenario.seed,
        "lint": lint(scenario),
    }


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
) -> Config:
    """Build the worker's own Config for an in-process run.

    Note ``standalone=True``. The worker's managed mode talks to a control
    plane over the claim protocol, which is the fleet path and does not exist
    yet. Running the scheduler directly inside the control plane is the
    in-process fleet: fine for tens to low hundreds of virtual users, and
    honest about it, because the generator-drift guard invalidates a run where
    this process could not keep to its own schedule rather than reporting a
    number that describes the server.
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
        seed=None,
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
        log_level="INFO",
        metrics_port=0,
        builtin_scenarios_dir=str(scenarios_dir()),
    )
