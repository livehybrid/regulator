"""Scenario parsing and lint.

Lint is a hard gate before a run launches. The failure it exists to prevent is
expensive and quiet: discovering after a twenty minute run that one step
referenced an index that does not exist, so every number in the report is
subtly wrong and nobody notices.
"""

from __future__ import annotations

import copy

import pytest

from conftest import tiny_scenario_dict
from regulator_agent.scenario import (
    ScenarioError,
    is_advice,
    lint,
    load_scenario,
    parse_duration,
    parse_scenario,
    placeholders,
)


def blocking(problems):
    return [p for p in problems if not is_advice(p)]


def advisory(problems):
    return [p for p in problems if is_advice(p)]


# ---------------------------------------------------------------- durations


@pytest.mark.parametrize(
    "raw, expected",
    [("500ms", 0.5), ("30s", 30.0), ("15m", 900.0), ("24h", 86400.0),
     ("7d", 604800.0), ("1w", 604800.0), (45, 45.0), (2.5, 2.5), ("2 h", 7200.0)],
)
def test_durations_parse(raw, expected):
    assert parse_duration(raw, "test") == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["soon", "10 fortnights", "", "-", None])
def test_bad_durations_raise(raw):
    with pytest.raises(ScenarioError):
        parse_duration(raw, "test")


# ------------------------------------------------------------------ parsing


def test_a_valid_scenario_parses_and_lints_clean(write_scenario):
    scenario = load_scenario(write_scenario(tiny_scenario_dict()))
    assert scenario.name == "tiny"
    assert len(scenario.personas) == 1
    assert len(scenario.steps) == 2
    assert blocking(lint(scenario)) == []


def test_placeholders_are_found_in_order():
    assert placeholders("search x={{a}} y={{ b }} z={{a}}") == ["a", "b", "a"]


def test_load_scenario_accepts_a_directory_or_a_file(write_scenario):
    directory = write_scenario(tiny_scenario_dict())
    assert load_scenario(directory).name == "tiny"
    assert load_scenario(directory / "scenario.yaml").name == "tiny"


def test_a_missing_scenario_is_a_readable_error(tmp_path):
    with pytest.raises(ScenarioError) as excinfo:
        load_scenario(tmp_path / "nope")
    assert "no scenario at" in str(excinfo.value)


def test_malformed_yaml_is_a_readable_error(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        load_scenario(path)
    assert "not valid YAML" in str(excinfo.value)


def test_a_top_level_list_is_rejected(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ScenarioError) as excinfo:
        load_scenario(path)
    assert "mapping" in str(excinfo.value)


def test_a_scenario_needs_a_name_and_a_persona():
    with pytest.raises(ScenarioError):
        parse_scenario({"personas": []})
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "x", "personas": []})


def test_a_persona_needs_at_least_one_step():
    doc = tiny_scenario_dict()
    doc["personas"][0]["steps"] = []
    with pytest.raises(ScenarioError):
        parse_scenario(doc)


# --------------------------------------------------------------- lint rules


def lint_with(**mutations):
    """Apply a mutation to the tiny scenario and return its lint output."""
    doc = copy.deepcopy(tiny_scenario_dict())
    for path, value in mutations.items():
        node = doc
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[int(part)] if part.isdigit() else node[part]
        last = parts[-1]
        if last.isdigit():
            node[int(last)] = value
        else:
            node[last] = value
    return lint(parse_scenario(doc))


@pytest.mark.parametrize(
    "mutation, fragment",
    [
        ({"name": "Not A Name"}, "lowercase"),
        ({"seed": 0}, "seed"),
        ({"engine": "telepathy"}, "engine"),
    ],
)
def test_top_level_lint_rules(mutation, fragment):
    problems = blocking(lint_with(**mutation))
    assert any(fragment in p for p in problems), problems


def test_persona_weights_must_sum_above_zero():
    problems = blocking(lint_with(**{"personas.0.weight": 0}))
    assert any("weights must sum" in p for p in problems), problems


def test_duplicate_persona_names_are_rejected():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"].append(copy.deepcopy(doc["personas"][0]))
    problems = blocking(lint(parse_scenario(doc)))
    assert any("duplicate persona" in p for p in problems), problems


def test_duplicate_step_ids_within_a_persona_are_rejected():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"][1]["id"] = "one"
    problems = blocking(lint(parse_scenario(doc)))
    assert any("duplicate step id" in p for p in problems), problems


@pytest.mark.parametrize(
    "step_mutation, fragment",
    [
        ({"class": "spicy"}, "class must be"),
        ({"exec_mode": "telepathic"}, "exec_mode must be"),
        ({"spl": ""}, "needs spl"),
        ({"spl": "index=main | stats count"}, "should start with"),
        ({"engine": "browser"}, "must use the api engine"),
        ({"type": "dashboard", "dashboard": None}, "needs a dashboard name"),
        ({"spl": "search index=main x={{undeclared}}"}, "no parameter named"),
    ],
)
def test_step_lint_rules(step_mutation, fragment):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"][0].update(step_mutation)
    problems = blocking(lint(parse_scenario(doc)))
    assert any(fragment in p for p in problems), problems


def test_a_browser_step_in_an_api_scenario_is_reported():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"][0].update(
        {"type": "dashboard", "engine": "browser", "app": "search", "dashboard": "d", "spl": None}
    )
    problems = blocking(lint(parse_scenario(doc)))
    assert any("declare engine: mixed" in p for p in problems), problems


@pytest.mark.parametrize(
    "spec, fragment",
    [
        ({"type": "choice", "values": []}, "non-empty values"),
        ({"type": "int_range", "min": 10, "max": 1}, "min must not exceed max"),
        ({"type": "int_range"}, "needs min and max"),
        ({"type": "choice_from_search", "spl": "search x"}, "needs field"),
        ({"type": "choice_from_search", "field": "f"}, "needs spl"),
        ({"type": "telepathy"}, "unknown type"),
        ({}, "needs a type"),
    ],
)
def test_parameter_lint_rules(spec, fragment):
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["parameters"]["n"] = spec
    problems = blocking(lint(parse_scenario(doc)))
    assert any(fragment in p for p in problems), problems


def test_closed_model_needs_virtual_users():
    problems = blocking(lint_with(**{"load.virtual_users": 0}))
    assert any("virtual_users must be at least 1" in p for p in problems), problems


def test_open_model_needs_an_arrival_rate():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["load"] = {"model": "open", "arrival_rate_per_min": 0, "duration": "1s"}
    problems = blocking(lint(parse_scenario(doc)))
    assert any("arrival_rate_per_min must be positive" in p for p in problems), problems


def test_a_pinned_policy_needs_both_epochs():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["time_policy"] = {"mode": "pinned", "earliest_epoch": 1000}
    problems = blocking(lint(parse_scenario(doc)))
    assert any("earliest_epoch and latest_epoch" in p for p in problems), problems


# ---------------------------------------------------------------- advisories
#
# These four are legal choices that quietly cost accuracy. They must be
# reported loudly and must not block a run, because sometimes they are exactly
# what the operator meant.


def test_oneshot_exec_mode_is_advisory_not_blocking():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["personas"][0]["steps"][0]["exec_mode"] = "oneshot"
    problems = lint(parse_scenario(doc))
    assert any("no job artefact" in p for p in advisory(problems)), problems
    assert blocking(problems) == []


def test_a_pinned_time_policy_is_advisory():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["time_policy"] = {"mode": "pinned", "earliest_epoch": 1000, "latest_epoch": 2000}
    problems = lint(parse_scenario(doc))
    assert any("dispatch cache" in p for p in advisory(problems)), problems
    assert blocking(problems) == []


def test_zero_jitter_is_advisory():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["time_policy"]["jitter"] = 0
    problems = lint(parse_scenario(doc))
    assert any("page cache" in p for p in advisory(problems)), problems
    assert blocking(problems) == []


def test_a_missing_guard_rail_is_advisory():
    doc = copy.deepcopy(tiny_scenario_dict())
    doc["abort_if"] = {}
    problems = lint(parse_scenario(doc))
    assert any("no guard rail" in p for p in advisory(problems)), problems
    assert blocking(problems) == []


def test_is_advice_distinguishes_the_two_kinds():
    assert is_advice("advice: this is only advice")
    assert is_advice("persona x, step y: advice: nested advice")
    assert not is_advice("seed is 0 or unset")


# ------------------------------------------------------ the shipped library


from pathlib import Path as _Path

_LIBRARY = _Path(__file__).resolve().parents[2] / "scenarios"
_SHIPPED = sorted(child.name for child in _LIBRARY.iterdir() if (child / "scenario.yaml").is_file())


@pytest.mark.parametrize("name", _SHIPPED)
def test_every_shipped_scenario_lints_clean(name):
    """The library ships in the image, so a broken one is a broken release.

    Every directory, discovered rather than listed, so a new scenario cannot
    ship unlinted because nobody added it here.
    """
    scenario = load_scenario(_LIBRARY / name)
    problems = blocking(lint(scenario))
    assert problems == [], f"{name}: {problems}"
    if scenario.searches is not None:
        # A scenario built on a savedsearches.conf must actually select
        # something, otherwise it runs nothing and reports a valid, empty run.
        assert scenario.saved_selected, f"{name}: no saved searches were selected"
        assert all(step.spl for step in scenario.steps if step.type == "search")


def test_the_library_covers_every_stoker_pack():
    """One scenario per Stoker pack, so a fill has a matching benchmark."""
    packs = {
        "web-access", "apigw", "aws-cloudtrail", "aws-s3-access", "aws-elb-alb",
        "splunk-tutorial-web", "splunk-tutorial-secure", "splunk-tutorial-vendor-sales",
        "flatline", "attack-replay", "host-infra-metrics", "api-service-red-metrics",
        "k8s-workload-metrics", "database-metrics", "message-queue-metrics",
        "network-interface-metrics", "web-store-metrics",
    }
    shipped = {name[len("pack-"):] for name in _SHIPPED if name.startswith("pack-")}
    assert packs <= shipped, sorted(packs - shipped)
