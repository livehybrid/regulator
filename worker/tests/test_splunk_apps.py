"""The Splunk apps in splunk-app/ ship what the scenarios and READMEs say.

Every dashboard in both apps was missing for months: the repository's
``data/`` ignore rule matched each app's ``default/data/ui/views``, so the
files existed only on the machine that wrote them. The browser scenario named
dashboards that were never shipped and the telemetry README described one that
was not there. These tests read the checkout, so an ignore rule that hides a
view again fails here rather than on a customer's search head.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from regulator_agent.scenario import load_scenario

REPO = Path(__file__).resolve().parents[2]
APPS = REPO / "splunk-app"
SCENARIOS = REPO / "scenarios"

VIEWS = sorted(APPS.glob("*/default/data/ui/views/*.xml"))
TOKEN = re.compile(r"\$([A-Za-z0-9_.]+)\$")
# Tokens Splunk supplies itself inside a drilldown, never from an input.
BUILT_IN_PREFIXES = ("row.", "click.", "form.", "env:", "job.", "result.")


def _view(app: str, name: str) -> Path:
    return APPS / app / "default" / "data" / "ui" / "views" / f"{name}.xml"


def _inputs(root: ET.Element) -> dict:
    """token -> input type, for every input on the form."""
    return {i.get("token"): i.get("type") for i in root.iter("input") if i.get("token")}


def test_every_app_ships_its_views():
    assert {p.parent.parent.parent.parent.parent.name for p in VIEWS} >= {"regulator_sut", "regulator_telemetry"}


@pytest.mark.parametrize("path", VIEWS, ids=lambda p: f"{p.parts[-6]}/{p.stem}")
def test_a_view_parses_and_every_token_is_defined(path):
    root = ET.parse(path).getroot()
    assert root.tag in ("form", "dashboard")
    inputs = _inputs(root)
    defined = set(inputs)
    for token, kind in inputs.items():
        if kind == "time":
            defined |= {f"{token}.earliest", f"{token}.latest"}
    for query in root.iter("query"):
        for token in TOKEN.findall(query.text or ""):
            assert token in defined or token.startswith(BUILT_IN_PREFIXES), (
                f"{path.name}: ${token}$ is used but no input defines it"
            )
    for search in root.iter("search"):
        for key in ("earliest", "latest"):
            node = search.find(key)
            if node is not None and node.text and node.text.startswith("$"):
                assert node.text.strip("$") in defined, f"{path.name}: {node.text} is undefined"


@pytest.mark.parametrize("app", sorted(p.name for p in APPS.iterdir() if (p / "default" / "app.conf").is_file()))
def test_every_nav_entry_is_a_view_that_exists(app):
    nav = APPS / app / "default" / "data" / "ui" / "nav" / "default.xml"
    assert nav.is_file(), f"{app} has no nav"
    for view in ET.parse(nav).getroot().iter("view"):
        name = view.get("name")
        assert name == "search" or _view(app, name).is_file(), f"{app} nav names a missing view {name!r}"


def _dashboard_steps():
    for path in sorted(SCENARIOS.glob("*/scenario.yaml")):
        scenario = load_scenario(path.parent)
        for step in scenario.steps:
            if step.type == "dashboard" and (APPS / (step.app or "")).is_dir():
                yield pytest.param(scenario, step, id=f"{scenario.name}/{step.id}")


@pytest.mark.parametrize("scenario, step", list(_dashboard_steps()))
def test_every_dashboard_a_scenario_opens_is_shipped_with_the_tokens_it_pins(scenario, step):
    path = _view(step.app, step.dashboard)
    assert path.is_file(), f"{scenario.name} opens {step.app}/{step.dashboard}, which is not in splunk-app"
    inputs = _inputs(ET.parse(path).getroot())
    if step.time_token:
        assert inputs.get(step.time_token) == "time", (
            f"{step.dashboard} has no time input named {step.time_token!r}, so the pinned window is ignored"
        )
    # Every scenario parameter travels as form.<name>; the dashboard must read it.
    for name in scenario.parameters:
        assert name in inputs, f"{step.dashboard} has no {name!r} input for the scenario's parameter"


def test_at_least_one_scenario_opens_a_shipped_dashboard():
    assert list(_dashboard_steps()), "the dashboard-triage scenario should open the regulator_sut dashboards"
