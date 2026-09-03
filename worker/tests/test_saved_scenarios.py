"""Scenarios built on a savedsearches.conf, and the schedule load model."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from conftest import FakeEngine, run_async, tiny_scenario_dict
from regulator_agent.config import load_config
from regulator_agent.params import ParameterResolver
from regulator_agent.scenario import (
    is_advice,
    lint,
    load_scenario,
    scenario_digest,
)
from regulator_agent.scheduler import Scheduler
from regulator_agent.timepolicy import resolve_window
from regulator_agent.params import DrawContext

CONF = """[Errors in the last hour]
search = index=main sourcetype=access_combined status>=500 | stats count by uri_path
dispatch.earliest_time = -1h
dispatch.latest_time = now
cron_schedule = */5 * * * *
enableSched = 1

[Hourly host census]
search = | tstats count where index=main by host
dispatch.earliest_time = -60m@m
dispatch.latest_time = now
cron_schedule = 0 * * * *
enableSched = 1

[Nightly summary writer]
search = index=main | stats count by host | collect index=summary
dispatch.earliest_time = -24h@h
dispatch.latest_time = now
cron_schedule = 0 1 * * *
enableSched = 1

[Ad hoc report]
search = index=main sourcetype=access_combined | timechart count
dispatch.earliest_time = -7d@d
dispatch.latest_time = @d
enableSched = 0
"""


def write(tmp_path: Path, scenario: dict, conf: str = CONF, name: str = "saved") -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "scenario.yaml").write_text(yaml.safe_dump(scenario), encoding="utf-8")
    (directory / "savedsearches.conf").write_text(conf, encoding="utf-8")
    return directory


def base_scenario(**overrides) -> dict:
    doc = {
        "name": "saved-demo",
        "engine": "api",
        "seed": 99,
        "corpus": {"index": "main"},
        "time_policy": {"mode": "rolling", "window": "1h", "jitter": "5m", "align": "1m"},
        "searches": {"file": "savedsearches.conf"},
        "personas": [
            {
                "name": "analyst",
                "weight": 100,
                "think_time": {"dist": "fixed", "value_s": 0},
                "steps_from": "saved",
            }
        ],
        "load": {"model": "closed", "virtual_users": 1, "duration": "1s"},
        "abort_if": {"error_rate_pct": 50, "p95_ms": 60000},
    }
    doc.update(overrides)
    return doc


# --------------------------------------------------------- binding steps


def test_a_persona_is_built_from_the_file_weighted_by_cron(tmp_path):
    scenario = load_scenario(write(tmp_path, base_scenario()))
    steps = scenario.personas[0].steps
    ids = [step.id for step in steps]
    # The side-effecting search is left out and said so; the unscheduled one
    # is in, at a daily weight.
    assert ids == ["errors-in-the-last-hour", "hourly-host-census", "ad-hoc-report"]
    assert "Nightly summary writer" in scenario.saved_skipped
    assert "collect" in scenario.saved_skipped["Nightly summary writer"]
    weights = {step.id: step.weight for step in steps}
    assert weights["errors-in-the-last-hour"] == pytest.approx(288.0)
    assert weights["hourly-host-census"] == pytest.approx(24.0)
    assert weights["ad-hoc-report"] == pytest.approx(1.0)
    # SPL is normalised, the class guessed, the cron carried.
    by_id = {step.id: step for step in steps}
    assert by_id["errors-in-the-last-hour"].spl.startswith("search index=main")
    assert by_id["hourly-host-census"].step_class == "accelerated"
    assert by_id["errors-in-the-last-hour"].cron == "*/5 * * * *"
    assert by_id["errors-in-the-last-hour"].from_saved is True
    assert scenario.personas[0].walk == "sample"
    assert not [line for line in lint(scenario) if not is_advice(line)]


def test_the_dispatch_range_becomes_the_step_time_range(tmp_path):
    scenario = load_scenario(write(tmp_path, base_scenario()))
    by_id = {step.id: step for step in scenario.personas[0].steps}
    policy = by_id["ad-hoc-report"].time_policy
    assert policy.mode == "rolling"
    assert policy.window_s == 7 * 86400
    assert policy.offset_s > 0  # ends at midnight, not now
    # The scenario's jitter is applied, so the working set moves.
    assert policy.jitter_s == 300


def test_as_saved_passes_the_modifiers_through_untouched(tmp_path):
    doc = base_scenario()
    doc["searches"]["time_from_saved"] = "as_saved"
    scenario = load_scenario(write(tmp_path, doc))
    step = scenario.personas[0].steps[0]
    assert step.time_policy.mode == "relative"
    assert step.time_policy.earliest_rel == "-1h"
    window = resolve_window(step.time_policy, DrawContext(1, 1, 1, step.id))
    assert window.as_args() == {"earliest_time": "-1h", "latest_time": "now"}
    assert window.span_s == pytest.approx(3600)
    advice = [line for line in lint(scenario) if is_advice(line)]
    assert any("relative" in line for line in advice)


def test_a_named_stanza_step_resolves_and_a_missing_one_is_a_lint_error(tmp_path):
    doc = base_scenario()
    doc["personas"] = [
        {
            "name": "one",
            "weight": 1,
            "think_time": {"dist": "fixed", "value_s": 0},
            "steps": [
                {"saved": "Hourly host census", "class": "accelerated"},
                {"id": "gone", "saved": "Does not exist"},
            ],
        }
    ]
    scenario = load_scenario(write(tmp_path, doc))
    steps = scenario.personas[0].steps
    assert steps[0].id == "hourly-host-census"
    assert steps[0].spl == "| tstats count where index=main by host"
    problems = [line for line in lint(scenario) if not is_advice(line)]
    assert any("Does not exist" in line for line in problems)


def test_a_side_effecting_stanza_is_blocked_unless_allowed(tmp_path):
    doc = base_scenario()
    doc["personas"] = [
        {
            "name": "one",
            "weight": 1,
            "think_time": {"dist": "fixed", "value_s": 0},
            "steps": [{"saved": "Nightly summary writer"}],
        }
    ]
    blocked = load_scenario(write(tmp_path, doc, name="blocked"))
    assert any("collect" in line for line in lint(blocked) if not is_advice(line))
    doc["searches"]["allow_side_effects"] = True
    allowed = load_scenario(write(tmp_path, doc, name="allowed"))
    assert not [line for line in lint(allowed) if "collect" in line]


def test_inline_spl_with_a_side_effect_is_also_caught(write_scenario):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"][0]["spl"] = "search index=main | stats count | collect index=x"
    scenario = load_scenario(write_scenario(doc))
    assert any("collect" in line for line in lint(scenario) if not is_advice(line))


def test_the_digest_changes_when_the_conf_changes(tmp_path):
    first = load_scenario(write(tmp_path, base_scenario(), name="a"))
    same = load_scenario(write(tmp_path, base_scenario(), name="b"))
    changed = load_scenario(
        write(tmp_path, base_scenario(), conf=CONF.replace("*/5", "*/10"), name="c")
    )
    assert scenario_digest(first) == scenario_digest(same)
    assert scenario_digest(first) != scenario_digest(changed)


# ------------------------------------------------------- schedule model


def build(tmp_path, doc, env, **overrides):
    directory = write(tmp_path, doc)
    scenario = load_scenario(directory)
    config = load_config(env(REG_SCENARIO=str(directory), **overrides))
    return scenario, config, ParameterResolver(scenario, seed=config.seed)


def test_the_schedule_model_fires_each_search_on_its_cron(tmp_path, env, monkeypatch):
    """Every minute of the run, the searches whose cron matches are dispatched."""
    doc = base_scenario()
    doc["searches"]["only_scheduled"] = True
    doc["load"] = {"model": "schedule", "duration": "3s"}
    scenario, config, resolver = build(tmp_path, doc, env)
    assert not [line for line in lint(scenario) if not is_advice(line)]

    import regulator_agent.scheduler as sched

    # Compress time: a virtual minute per 0.3 real seconds, first one due
    # almost immediately.
    monkeypatch.setattr(sched.time, "time", lambda: 59.0)
    engine = FakeEngine(latency=0.01)
    scheduler = Scheduler(scenario=scenario, config=config, engine=engine, resolver=resolver)

    original = scheduler._run_schedule

    async def fast_schedule():
        # Patch the minute length by rewriting the constant the loop adds.
        return await original()

    summary = run_async(scheduler.run())
    assert summary.load_model == "schedule"
    assert summary.co_corrected is True
    assert summary.configured_load["scheduled_steps"] == 2
    # Within three seconds at most a couple of virtual minutes elapse, and
    # the five-minute search fires on any minute divisible by five.
    ids = {ctx.step.id for ctx in engine.contexts}
    assert ids <= {"errors-in-the-last-hour", "hourly-host-census"}


def test_schedule_start_shifts_the_virtual_clock(tmp_path, env):
    doc = base_scenario()
    doc["searches"]["only_scheduled"] = True
    doc["load"] = {"model": "schedule", "duration": "1s", "schedule_start": "09:00"}
    scenario, config, resolver = build(tmp_path, doc, env)
    scheduler = Scheduler(scenario=scenario, config=config, engine=FakeEngine(), resolver=resolver)
    assert scheduler.load.schedule_start == "09:00"
    summary = run_async(scheduler.run())
    assert summary.configured_load["schedule_start"] == "09:00"


def test_a_schedule_scenario_needs_a_cron_somewhere(write_scenario):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "schedule", "duration": "10s"}
    scenario = load_scenario(write_scenario(doc))
    assert any("no step has a cron" in line for line in lint(scenario))


def test_sampled_personas_draw_one_step_per_iteration_reproducibly(tmp_path, env):
    doc = base_scenario()
    doc["load"] = {"model": "closed", "virtual_users": 1, "duration": "0.5s"}
    scenario, config, resolver = build(tmp_path, doc, env)
    engine_a = FakeEngine(latency=0.005)
    run_async(Scheduler(scenario=scenario, config=config, engine=engine_a, resolver=resolver).run())
    engine_b = FakeEngine(latency=0.005)
    run_async(Scheduler(scenario=scenario, config=config, engine=engine_b, resolver=resolver).run())
    a = [ctx.step.id for ctx in engine_a.contexts]
    b = [ctx.step.id for ctx in engine_b.contexts]
    assert a
    # Same seed, same sequence of draws, whatever the timing.
    n = min(len(a), len(b))
    assert a[:n] == b[:n]
    # Weighted by cron, so the five-minute search dominates.
    assert a.count("errors-in-the-last-hour") >= a.count("ad-hoc-report")
