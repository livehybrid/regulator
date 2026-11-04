"""Editing, duplicating and retagging scenarios, and which scenarios fit a target."""

from __future__ import annotations

import yaml

from test_scenarios_api import CONF, client, make_target, splunkd  # noqa: F401 - fixtures


def _create(client, name="editable"):
    response = client.post("/api/scenarios", json={"name": name, "savedsearches": CONF})
    assert response.status_code == 201, response.text
    return response.json()


def test_rows_are_listed_and_edits_keep_other_stanza_keys(client, tmp_path):
    _create(client)
    detail = client.get("/api/scenarios/editable").json()
    rows = detail["saved_searches_rows"]
    assert [r["name"] for r in rows] == ["Errors in the last hour", "Summary writer"]

    # Rename the first, drop the second, add a new one.
    edited = [
        {**rows[0], "original_name": rows[0]["name"], "name": "Errors (renamed)", "cron": "*/10 * * * *"},
        {
            "name": "Firewall denies",
            "search": "index=main sourcetype=cisco:asa action=blocked | stats count by src",
            "earliest": "-15m",
            "latest": "now",
            "cron": "*/15 * * * *",
            "scheduled": True,
        },
    ]
    response = client.put("/api/scenarios/editable", json={"searches": edited})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved_searches"] == 2
    # Sourcetypes follow the searches unless they were set by hand.
    assert body["sourcetypes"] == ["access_combined", "cisco:asa"]

    conf = client.get("/api/scenarios/editable/savedsearches.conf").text
    assert "[Errors (renamed)]" in conf
    assert "[Summary writer]" not in conf
    assert "cron_schedule = */10 * * * *" in conf
    # A key the table does not show survives the rename.
    assert "enableSched = 1" in conf
    # No staging directory is left behind.
    assert [p.name for p in (tmp_path / "user-scenarios").iterdir() if p.name.startswith(".")] == []


def test_an_edit_that_leaves_nothing_runnable_is_refused_and_changes_nothing(client):
    _create(client)
    before = client.get("/api/scenarios/editable/savedsearches.conf").text
    response = client.put(
        "/api/scenarios/editable",
        json={"searches": [{"name": "Writer", "search": "index=main | collect index=summary", "scheduled": True}]},
    )
    assert response.status_code == 422
    assert client.get("/api/scenarios/editable/savedsearches.conf").text == before


def test_raw_files_can_be_edited_but_not_renamed(client):
    _create(client)
    files = client.get("/api/scenarios/editable").json()["files"]
    document = yaml.safe_load(files["scenario.yaml"])
    document["description"] = "edited by hand"
    response = client.put(
        "/api/scenarios/editable", json={"files": {"scenario.yaml": yaml.safe_dump(document)}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "edited by hand"

    document["name"] = "something-else"
    response = client.put(
        "/api/scenarios/editable", json={"files": {"scenario.yaml": yaml.safe_dump(document)}}
    )
    assert response.status_code == 422
    response = client.put("/api/scenarios/editable", json={"files": {"../escape.txt": "x"}})
    assert response.status_code == 422


def test_builtin_scenarios_are_read_only_but_can_be_duplicated(client):
    assert client.put("/api/scenarios/smoke", json={"description": "x"}).status_code == 403
    response = client.post("/api/scenarios/search-classes/duplicate", json={"name": "my-classes"})
    assert response.status_code == 201, response.text
    assert response.json()["origin"] == "user"
    text = client.get("/api/scenarios/my-classes").json()["files"]["scenario.yaml"]
    # The copy keeps the original's comments: only the name line changed.
    assert "One search per class" in text
    assert "name: my-classes" in text
    assert client.post("/api/scenarios/search-classes/duplicate", json={"name": "smoke"}).status_code == 409
    assert client.post("/api/scenarios/search-classes/duplicate", json={"name": "Bad Name"}).status_code == 422


def test_tags_are_set_on_builtin_and_user_scenarios(client, tmp_path):
    response = client.put("/api/scenarios/smoke/tags", json={"tags": ["quick", "harness", "quick"]})
    assert response.status_code == 200, response.text
    assert response.json()["tags"] == ["quick", "harness"]
    listed = {s["name"]: s for s in client.get("/api/scenarios").json()}
    assert listed["smoke"]["tags"] == ["quick", "harness"]
    assert (tmp_path / "user-scenarios" / ".scenario-tags.json").is_file()

    _create(client)
    response = client.put("/api/scenarios/editable/tags", json={"tags": ["team-a"]})
    assert response.status_code == 200, response.text
    text = client.get("/api/scenarios/editable").json()["files"]["scenario.yaml"]
    assert yaml.safe_load(text)["tags"] == ["team-a"]
    # The generated header comment is still there: the tag line was swapped in place.
    assert text.startswith("# Generated by Regulator")

    assert client.put("/api/scenarios/smoke/tags", json={"tags": ["<script>"]}).status_code == 422


def test_fit_is_untested_until_a_report_then_follows_the_census(client, splunkd):
    target = make_target(client, splunkd)
    fit = client.get("/api/scenario-fit").json()
    assert fit["targets"] == [{"id": target["id"], "name": target["name"], "reported_at": None, "fit": None}]

    assert client.post(f"/api/targets/{target['id']}/report").status_code == 200
    fit = client.get("/api/scenario-fit").json()["targets"][0]
    assert fit["reported_at"]
    assert fit["fit"]["smoke"]["fit"] == "unknown"
    # A scenario created after the report still gets a fit.
    _create(client)
    fit = client.get("/api/scenario-fit").json()["targets"][0]
    assert "editable" in fit["fit"]


def test_target_apps_and_a_selection_of_saved_searches(client, splunkd):
    target = make_target(client, splunkd)
    apps = client.get(f"/api/targets/{target['id']}/apps")
    assert apps.status_code == 200, apps.text
    names = [a["name"] for a in apps.json()]
    assert "search" in names and "launcher" in names

    conf = client.get(
        f"/api/targets/{target['id']}/savedsearches.conf",
        params=[("name", "Errors in the last hour"), ("name", "Disabled report")],
    ).text
    assert "[Errors in the last hour]" in conf
    assert "[Disabled report]" in conf
    assert "[Nightly summary writer]" not in conf
    assert "2 selected search(es)" in conf
