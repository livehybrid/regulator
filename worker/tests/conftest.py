"""Shared fixtures.

The important thing in here is :class:`FakeEngine`. The scheduler is the part
of Regulator most worth testing and the part hardest to test against a real
target, because the properties that matter are about *timing under stress*: how
latency is attributed when work overruns its schedule, what happens when the
engine stalls, whether guard rails fire. A fake engine with a programmable
latency curve makes all of that deterministic and fast.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
import yaml

# Make `import regulator_agent` work when pytest runs from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from regulator_agent.engines.base import StepContext, TargetCapabilities  # noqa: E402
from regulator_agent.results import ERROR_SERVER, StepRecord  # noqa: E402


def run_async(coro: Any) -> Any:
    """Run a coroutine to completion.

    pytest-asyncio is not a dependency of this project, and adding one for the
    sake of a decorator is not worth it. Tests call this explicitly.
    """
    return asyncio.run(coro)


class FakeEngine:
    """An engine that does no I/O and lies about time in controllable ways.

    ``latency`` is either a fixed number of seconds or a callable taking the
    zero-based execution index and returning seconds, which is how a test
    simulates a target that stalls partway through a run.
    """

    name = "fake"

    def __init__(
        self,
        latency: float | Callable[[int], float] = 0.01,
        fail_every: int = 0,
        fail_all: bool = False,
        raise_on: Optional[int] = None,
        capabilities: Optional[TargetCapabilities] = None,
    ) -> None:
        self.latency = latency
        self.fail_every = fail_every
        self.fail_all = fail_all
        self.raise_on = raise_on
        self.capabilities = capabilities or TargetCapabilities(
            version="10.4.0", cpu_count=8, base_max_searches=6, max_searches_per_cpu=1
        )
        self.contexts: List[StepContext] = []
        self.executions = 0
        self.concurrent = 0
        self.peak_concurrent = 0
        self.started = False
        self.closed = False
        self.validate_problems: List[str] = []
        self.bound: Dict[str, Any] = {}

    async def start(self) -> None:
        self.started = True

    async def probe(self) -> TargetCapabilities:
        return self.capabilities

    async def validate(self, scenario: Any) -> List[str]:
        return list(self.validate_problems)

    async def resolve_parameters(self, resolver: Any) -> None:
        for name, spec in resolver.dynamic_parameters.items():
            values = self.bound.get(name, ["fake-value-1", "fake-value-2"])
            resolver.bind(name, values)

    async def execute(self, ctx: StepContext) -> StepRecord:
        index = self.executions
        self.executions += 1
        self.contexts.append(ctx)

        if self.raise_on is not None and index == self.raise_on:
            raise RuntimeError("engine exploded on purpose")

        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        began = asyncio.get_running_loop().time()
        try:
            delay = self.latency(index) if callable(self.latency) else float(self.latency)
            if delay > 0:
                await asyncio.sleep(delay)
        finally:
            self.concurrent -= 1

        record = ctx.blank_record()
        record.service_time_ms = (asyncio.get_running_loop().time() - began) * 1000.0
        record.sid = f"fake-{index}"
        record.dispatch_ms = 1.0
        record.run_duration_s = record.service_time_ms / 1000.0
        record.scan_count = 1000
        record.event_count = 100
        record.result_count = 10
        record.dispatch_state = "DONE"

        failed = self.fail_all or (self.fail_every and index % self.fail_every == 0)
        if failed:
            record.ok = False
            record.error_class = ERROR_SERVER
            record.error_detail = "fake failure"
            record.dispatch_state = "FAILED"
        return record

    async def close(self) -> None:
        self.closed = True


BASE_ENV = {
    "REG_STANDALONE": "1",
    "REG_SCENARIO": "/scenarios/smoke",
    "REG_TARGET_URL": "https://splunk.example:8089",
    "REG_TARGET_TOKEN": "s3cr3t-token-value",
}


@pytest.fixture
def env():
    def _env(**overrides: str) -> Dict[str, str]:
        merged = dict(BASE_ENV)
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    return _env


def tiny_scenario_dict() -> Dict[str, Any]:
    """A minimal scenario that lints with no blocking problems."""
    return {
        "name": "tiny",
        "engine": "api",
        "seed": 7,
        "description": "minimal",
        "corpus": {"index": "main"},
        "parameters": {"n": {"type": "int_range", "min": 1, "max": 1000}},
        "time_policy": {"mode": "rolling", "window": "1h", "jitter": "5m", "align": "1m"},
        "personas": [
            {
                "name": "user",
                "weight": 100,
                "think_time": {"dist": "fixed", "value_s": 0},
                "steps": [
                    {
                        "id": "one",
                        "type": "search",
                        "engine": "api",
                        "class": "dense",
                        "spl": "search index=main n={{n}} | stats count",
                    },
                    {
                        "id": "two",
                        "type": "search",
                        "engine": "api",
                        "class": "sparse",
                        "spl": "| tstats count where index=main",
                    },
                ],
            }
        ],
        "load": {"model": "closed", "virtual_users": 2, "duration": "1s"},
        "abort_if": {"error_rate_pct": 50, "p95_ms": 60000},
    }


@pytest.fixture
def scenario_dict():
    return tiny_scenario_dict


@pytest.fixture
def write_scenario(tmp_path):
    def _write(mapping: Dict[str, Any], name: str = "scn") -> Path:
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "scenario.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
        return directory

    return _write
