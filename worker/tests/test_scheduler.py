"""The scheduler: load shape, guard rails and honest latency.

The coordinated-omission tests below are the most valuable in the repository.
They are what proves the tool does not lie about its tail. A load generator
that waits for response N before issuing N+1 never issues the requests that
would have queued behind a stall, so the tail simply vanishes from the report
and a capacity test says the system is fine right up until it is not.

Every timing assertion here is on ordering or relative magnitude, never on an
absolute wall-clock number, so the suite does not go flaky on a loaded CI box.
"""

from __future__ import annotations

import copy

import pytest

from conftest import FakeEngine, run_async, tiny_scenario_dict
from regulator_agent.config import load_config
from regulator_agent.params import ParameterResolver
from regulator_agent.scenario import RampStage, parse_scenario
from regulator_agent.scheduler import Ramp, Scheduler


def build(scenario_doc, env_factory, **env_overrides):
    scenario = parse_scenario(scenario_doc)
    config = load_config(env_factory(**env_overrides))
    resolver = ParameterResolver(scenario)
    return scenario, config, resolver


def drive(scenario_doc, env_factory, engine, **env_overrides):
    scenario, config, resolver = build(scenario_doc, env_factory, **env_overrides)
    scheduler = Scheduler(
        scenario=scenario, config=config, engine=engine, resolver=resolver
    )
    summary = run_async(scheduler.run())
    return scheduler, summary


# -------------------------------------------------------------------- ramp


def test_ramp_interpolates_between_stages():
    ramp = Ramp([RampStage(to=10, over_s=10), RampStage(hold_s=5), RampStage(to=20, over_s=10)], 20)
    assert ramp.target_at(0) == pytest.approx(0, abs=0.01)
    assert ramp.target_at(5) == pytest.approx(5, abs=0.01)
    assert ramp.target_at(10) == pytest.approx(10, abs=0.01)
    assert ramp.target_at(12) == pytest.approx(10, abs=0.01)  # holding
    assert ramp.target_at(15) == pytest.approx(10, abs=0.01)  # hold just ended
    assert ramp.target_at(20) == pytest.approx(15, abs=0.01)  # halfway up the third leg
    assert ramp.target_at(25) == pytest.approx(20, abs=0.01)
    assert ramp.duration_s == 25


def test_the_final_target_persists_past_the_end_of_the_ramp():
    ramp = Ramp([RampStage(to=7, over_s=5)], 7)
    assert ramp.target_at(1000) == 7


def test_no_ramp_means_full_load_immediately():
    ramp = Ramp([], 12)
    assert ramp.target_at(0) == 12
    assert ramp.duration_s == 0


# ------------------------------------------------------------ closed model


def test_the_closed_model_holds_the_requested_concurrency(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 4, "duration": "1.2s"}
    engine = FakeEngine(latency=0.05)

    scheduler, summary = drive(doc, env, engine)

    assert summary.outcome == "completed"
    assert summary.valid is True
    assert engine.executions > 0
    # Four users, each doing one step at a time, so concurrency tops out near
    # four. Generous slack because task startup is not instantaneous.
    assert 1 <= engine.peak_concurrent <= 5
    assert summary.peak_virtual_users <= 4


def test_every_step_of_a_persona_runs_in_order(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 1, "duration": "0.8s"}
    engine = FakeEngine(latency=0.01)

    drive(doc, env, engine)

    ids = [c.step.id for c in engine.contexts]
    assert ids[:4] == ["one", "two", "one", "two"]
    iterations = [c.iteration for c in engine.contexts if c.step.id == "one"]
    assert iterations == sorted(iterations)
    assert iterations[0] == 0


def test_virtual_user_ids_are_disjoint_between_slots(env):
    """Two workers must never number their users into the same range.

    Otherwise a fleet's records collide and per-user analysis is nonsense.
    """
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 2, "duration": "0.5s"}

    engine_a = FakeEngine(latency=0.01)
    drive(doc, env, engine_a, REG_SLOT="0")
    engine_b = FakeEngine(latency=0.01)
    drive(doc, env, engine_b, REG_SLOT="2")

    ids_a = {c.vu_id for c in engine_a.contexts}
    ids_b = {c.vu_id for c in engine_b.contexts}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b)


# ------------------------------------------- coordinated omission, the point


def test_paced_iterations_report_growing_latency_when_the_engine_stalls(env):
    """The headline property.

    Pacing gives each iteration an intended start time. When execution takes
    far longer than the pacing interval, the schedule slips further behind on
    every iteration, and latency must grow to reflect the queue a real user
    population would have formed, even though the service time of each
    individual request is unchanged.
    """
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"] = [doc["personas"][0]["steps"][0]]
    doc["load"] = {
        "model": "closed",
        "virtual_users": 1,
        "pacing_s": 0.05,
        "duration": "1.5s",
    }
    records = []
    engine = FakeEngine(latency=0.15)  # three times the pacing interval

    scenario, config, resolver = build(doc, env)
    scheduler = Scheduler(
        scenario=scenario,
        config=config,
        engine=engine,
        resolver=resolver,
        emitters=[type("Sink", (), {"emit": lambda self, r: records.append(r)})()],
    )
    summary = run_async(scheduler.run())

    assert summary.co_corrected is True
    assert len(records) >= 4

    latencies = [r.latency_ms for r in records]
    service_times = [r.service_time_ms for r in records]

    # Service time is flat: each individual request is just as fast as before.
    assert max(service_times) - min(service_times) < 100
    # Latency is not: the schedule debt accumulates.
    assert latencies[-1] > latencies[0] * 2
    assert records[-1].late_by_ms > records[0].late_by_ms
    assert all(r.co_corrected for r in records)


def test_without_pacing_latency_is_service_time_and_says_so(env):
    """The honest fallback.

    A closed loop with no schedule cannot be corrected, so rather than pretend,
    the record says co_corrected is false and the two numbers agree. The
    operator is then told to read throughput rather than the tail.
    """
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"] = [doc["personas"][0]["steps"][0]]
    doc["load"] = {"model": "closed", "virtual_users": 1, "duration": "0.8s"}
    records = []
    engine = FakeEngine(latency=0.05)

    scenario, config, resolver = build(doc, env)
    scheduler = Scheduler(
        scenario=scenario,
        config=config,
        engine=engine,
        resolver=resolver,
        emitters=[type("Sink", (), {"emit": lambda self, r: records.append(r)})()],
    )
    summary = run_async(scheduler.run())

    assert summary.co_corrected is False
    assert records
    for record in records:
        assert record.co_corrected is False
        assert record.latency_ms == pytest.approx(record.service_time_ms, rel=0.01)


def test_only_the_first_step_of_an_iteration_carries_the_schedule_debt(env):
    """A user asks the second question after reading the first answer.

    So the later steps' intended start genuinely is the moment the previous one
    finished, and attributing one stall to every step behind it would
    double-count the delay.
    """
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {
        "model": "closed",
        "virtual_users": 1,
        "pacing_s": 0.02,
        "duration": "1.2s",
    }
    records = []
    engine = FakeEngine(latency=0.1)

    scenario, config, resolver = build(doc, env)
    scheduler = Scheduler(
        scenario=scenario,
        config=config,
        engine=engine,
        resolver=resolver,
        emitters=[type("Sink", (), {"emit": lambda self, r: records.append(r)})()],
    )
    run_async(scheduler.run())

    first_steps = [r for r in records if r.step_id == "one" and r.iteration > 0]
    later_steps = [r for r in records if r.step_id == "two"]
    assert first_steps and later_steps
    assert max(r.late_by_ms for r in first_steps) > max(r.late_by_ms for r in later_steps)


# ------------------------------------------------------------- guard rails


def test_a_failing_target_trips_the_error_rate_ceiling(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 4, "duration": "5s"}
    doc["abort_if"] = {"error_rate_pct": 20}
    engine = FakeEngine(latency=0.005, fail_all=True)

    _, summary = drive(doc, env, engine)

    assert summary.outcome == "aborted"
    assert "error rate" in (summary.abort_reason or "")
    # The guard must not fire on the very first failure during startup.
    assert engine.executions >= 20


def test_a_slow_target_trips_the_latency_ceiling(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 25, "duration": "5s"}
    doc["abort_if"] = {"p95_ms": 50}
    engine = FakeEngine(latency=0.2)

    _, summary = drive(doc, env, engine)

    assert summary.outcome == "aborted"
    assert "p95" in (summary.abort_reason or "")


def test_generator_drift_marks_the_run_invalid_rather_than_slow(env):
    """The distinction that matters most in the whole report.

    "The cluster is too slow" and "the load box was too small" need opposite
    responses, so they must never share an outcome.
    """
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"] = [doc["personas"][0]["steps"][0]]
    doc["load"] = {
        "model": "closed",
        "virtual_users": 1,
        "pacing_s": 0.01,
        "duration": "3s",
    }
    doc["abort_if"] = {"error_rate_pct": 90, "generator_drift_ms": 100}
    engine = FakeEngine(latency=0.25)

    _, summary = drive(doc, env, engine)

    assert summary.valid is False
    assert "behind its own schedule" in (summary.invalid_reason or "")
    assert "bottleneck" in (summary.invalid_reason or "")


def test_request_stop_ends_the_run_promptly(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 2, "duration": "30s"}
    scenario, config, resolver = build(doc, env)
    engine = FakeEngine(latency=0.02)
    scheduler = Scheduler(scenario=scenario, config=config, engine=engine, resolver=resolver)

    async def stop_soon():
        import asyncio

        await asyncio.sleep(0.4)
        scheduler.request_stop("test asked")

    async def both():
        import asyncio

        stopper = asyncio.create_task(stop_soon())
        summary = await scheduler.run()
        await stopper
        return summary

    summary = run_async(both())
    assert summary.outcome == "stopped"
    assert summary.duration_s < 10


def test_an_engine_that_raises_fails_the_run_without_hanging(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 2, "duration": "5s"}
    engine = FakeEngine(latency=0.01, raise_on=3)

    _, summary = drive(doc, env, engine)

    assert summary.outcome == "failed"
    assert summary.valid is False
    assert "exploded" in (summary.invalid_reason or "")


# --------------------------------------------------------------- open model


def test_the_open_model_issues_arrivals_on_schedule_despite_a_slow_engine(env):
    """Arrivals must not serialise behind slow work.

    This is the whole reason the open model exists: if the target stalls, the
    queue has to be visible in the numbers rather than absorbed by the
    generator.
    """
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"] = [doc["personas"][0]["steps"][0]]
    doc["load"] = {"model": "open", "arrival_rate_per_min": 600, "duration": "1.5s"}
    engine = FakeEngine(latency=0.5)  # far slower than the 100 ms arrival gap

    _, summary = drive(doc, env, engine)

    assert summary.load_model == "open"
    assert summary.co_corrected is True
    # Ten arrivals a second for about 1.5 seconds. A closed loop with a 500 ms
    # engine would have managed three at most.
    assert engine.executions >= 8
    assert engine.peak_concurrent >= 4


def test_exceeding_the_in_flight_ceiling_invalidates_the_run(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"] = [doc["personas"][0]["steps"][0]]
    doc["load"] = {"model": "open", "arrival_rate_per_min": 6000, "duration": "1s"}
    engine = FakeEngine(latency=2.0)

    _, summary = drive(doc, env, engine, REG_MAX_IN_FLIGHT="3")

    assert summary.valid is False
    assert "in-flight ceiling" in (summary.invalid_reason or "")
    assert summary.stats["arrivals_missed"] > 0


# ---------------------------------------------------- configuration effects


def test_an_environment_override_replaces_the_scenario_virtual_users(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {
        "model": "closed",
        "virtual_users": 2,
        "ramp": [{"to": 2, "over_s": 1}],
        "duration": "0.6s",
    }
    scenario, config, resolver = build(doc, env, REG_VUS="6")
    scheduler = Scheduler(scenario=scenario, config=config, engine=FakeEngine(), resolver=resolver)

    assert scheduler.load.virtual_users == 6
    # The scenario's ramp climbed to 2, which would cap an override of 6 at a
    # third of what was asked for. Dropping it is the honest behaviour.
    assert scheduler.load.ramp == []


def test_an_arrival_rate_override_switches_the_model_to_open(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    scenario, config, resolver = build(doc, env, REG_ARRIVAL_RATE_PER_MIN="120")
    scheduler = Scheduler(scenario=scenario, config=config, engine=FakeEngine(), resolver=resolver)
    assert scheduler.load.model == "open"
    assert scheduler.load.arrival_rate_per_min == 120


def test_a_run_with_no_duration_anywhere_is_a_readable_error(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 1}
    scenario, config, resolver = build(doc, env)
    with pytest.raises(ValueError) as excinfo:
        Scheduler(scenario=scenario, config=config, engine=FakeEngine(), resolver=resolver)
    assert "duration" in str(excinfo.value)


# ------------------------------------------------------------------ summary


def test_the_summary_carries_what_a_comparison_needs(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 2, "duration": "0.6s"}
    engine = FakeEngine(latency=0.01)

    _, summary = drive(doc, env, engine)
    payload = summary.to_dict()

    assert payload["scenario"] == "tiny"
    assert payload["scenario_seed"] == 7
    assert payload["agent_version"]
    assert payload["peak_virtual_users"] >= 1
    assert payload["target_url"] == "https://splunk.example:8089"
    assert "latency" in payload["stats"]
    assert payload["stats"]["executions"] == engine.executions
    assert payload["self_instrumented"] is False


def test_capabilities_are_folded_into_the_summary(env):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "closed", "virtual_users": 1, "duration": "0.4s"}
    engine = FakeEngine(latency=0.01)
    scenario, config, resolver = build(doc, env)
    scheduler = Scheduler(
        scenario=scenario,
        config=config,
        engine=engine,
        resolver=resolver,
        capabilities=engine.capabilities,
    )
    summary = run_async(scheduler.run())

    target = summary.stats["target"]
    assert target["version"] == "10.4.0"
    # 6 + (1 * 8): the ceiling the concurrency chart draws its line at.
    assert target["max_hist_searches"] == 14
