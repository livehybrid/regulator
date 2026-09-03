"""Splunk's savedsearches.conf, read and written the way splunkd reads it."""

from __future__ import annotations

import datetime as dt

import pytest

from regulator_agent import savedsearches as ss

CONF = """# A comment line
[default]
disabled = 0
enableSched = 1

[Errors in the last hour]
search = index=main sourcetype=access_combined status>=500 \\
| stats count by uri_path
dispatch.earliest_time = -1h
dispatch.latest_time = now
cron_schedule = */5 * * * *
description = Five-minute error roll-up # not a comment
action.email = 1

[Nightly summary writer]
search = index=main | stats count by host | collect index=summary
dispatch.earliest_time = -24h@h
dispatch.latest_time = now
cron_schedule = 0 1 * * *

[Disabled report]
search = | tstats count where index=main by sourcetype
dispatch.earliest_time = -7d@d
dispatch.latest_time = @d
cron_schedule = 0 6 * * 1
disabled = 1

[_ScheduledView__weekly]
search = | makeresults
dispatch.earliest_time = 1
dispatch.latest_time = 2

[Realtime watcher]
search = index=main error
dispatch.earliest_time = rt-5m
dispatch.latest_time = rt
enableSched = 0
"""


# ------------------------------------------------------------------ parsing


def test_the_conf_grammar_is_read_as_splunkd_reads_it():
    stanzas = ss.parse_conf(CONF)
    assert list(stanzas) == [
        "Errors in the last hour",
        "Nightly summary writer",
        "Disabled report",
        "_ScheduledView__weekly",
        "Realtime watcher",
    ]
    errors = stanzas["Errors in the last hour"]
    # The continuation keeps the newline, so a multi-line search round-trips.
    assert errors["search"] == "index=main sourcetype=access_combined status>=500 \n| stats count by uri_path"
    # A hash inside a value is part of the value.
    assert errors["description"] == "Five-minute error roll-up # not a comment"
    # [default] is folded in.
    assert errors["disabled"] == "0"
    assert errors["enableSched"] == "1"
    # A stanza overrides the default.
    assert stanzas["Disabled report"]["disabled"] == "1"
    assert stanzas["Realtime watcher"]["enableSched"] == "0"


def test_a_byte_order_mark_and_windows_line_endings_are_tolerated():
    text = "﻿[one]\r\nsearch = index=main\r\n"
    assert ss.parse_conf(text) == {"one": {"search": "index=main"}}


def test_render_round_trips_a_multi_line_search():
    searches = ss.parse_savedsearches(CONF)
    rendered = ss.render_savedsearches(searches)
    again = ss.parse_savedsearches(rendered)
    assert [s.name for s in again] == [s.name for s in searches]
    assert again[0].raw_search == searches[0].raw_search
    assert again[0].cron == "*/5 * * * *"
    # A bare-term search is dispatchable after normalisation, and the file
    # keeps the original text.
    assert again[0].search.startswith("search index=main")
    assert "search = index=main" in rendered


def test_a_bare_term_search_gains_the_search_keyword_and_a_generating_one_does_not():
    assert ss.normalise_spl("error OR failed") == "search error OR failed"
    assert ss.normalise_spl("| tstats count") == "| tstats count"
    assert ss.normalise_spl("search index=main") == "search index=main"
    assert ss.normalise_spl("  ") == ""


def test_rest_entries_reduce_to_the_same_shape():
    entries = [
        {
            "name": "From REST",
            "acl": {"app": "myapp"},
            "content": {
                "search": "index=main | head 1",
                "dispatch.earliest_time": "-15m",
                "dispatch.latest_time": "now",
                "cron_schedule": "*/15 * * * *",
                "is_scheduled": True,
                "disabled": False,
                "action.email.to": "someone@example",
                "display.visualizations.charting.chart": "line",
            },
        }
    ]
    searches = ss.from_rest_entries(entries)
    assert len(searches) == 1
    search = searches[0]
    assert search.app == "myapp"
    assert search.scheduled is True
    assert search.disabled is False
    assert search.cron == "*/15 * * * *"
    # Alert plumbing and display settings do not come along.
    rendered = ss.render_savedsearches(searches)
    assert "action.email" not in rendered
    assert "display." not in rendered
    assert "request.ui_dispatch_app = myapp" in rendered


# --------------------------------------------------------------- selection


def test_selection_explains_every_search_it_leaves_out():
    searches = ss.parse_savedsearches(CONF)
    selection = ss.select_searches(searches)
    assert [s.name for s in selection.chosen] == ["Errors in the last hour"]
    assert "collect" in selection.skipped["Nightly summary writer"]
    assert selection.skipped["Disabled report"] == "disabled"
    assert "scheduled PDF view" in selection.skipped["_ScheduledView__weekly"]
    assert "real-time" in selection.skipped["Realtime watcher"]


def test_side_effects_can_be_allowed_explicitly_and_globs_select():
    searches = ss.parse_savedsearches(CONF)
    selection = ss.select_searches(
        searches, include=["Nightly*"], allow_side_effects=True, only_enabled=False
    )
    assert [s.name for s in selection.chosen] == ["Nightly summary writer"]


@pytest.mark.parametrize(
    "spl, expected",
    [
        ("index=main | stats count | collect index=summary", ["collect"]),
        ("| inputlookup x | outputlookup y.csv", ["outputlookup"]),
        ("index=main ```collect``` | stats count", []),
        ("index=main | sendemail to=a@b.c", ["sendemail"]),
        ("index=main | stats count", []),
    ],
)
def test_side_effects_are_found_at_the_head_of_a_pipe(spl, expected):
    assert ss.side_effects(spl) == expected


# ----------------------------------------------------------- classification


@pytest.mark.parametrize(
    "spl, earliest, expected",
    [
        ("| tstats count where index=main by sourcetype", "", "accelerated"),
        ("| mstats avg(_value) where index=m by host", "", "accelerated"),
        ("index=main [ search index=x | fields ip ] | stats count", "", "subsearch"),
        ("index=main | transaction sid | stats count", "", "heavy"),
        ("index=main | lookup hosts host OUTPUT owner", "", "heavy"),
        ("index=main error", "rt-5m", "realtime"),
        ("index=main error | stats count", "", "unclassified"),
    ],
)
def test_classification_is_conservative(spl, earliest, expected):
    assert ss.classify(spl, earliest) == expected


# ------------------------------------------------------------ relative time

NOW = dt.datetime(2026, 3, 11, 14, 37, 22, tzinfo=dt.timezone.utc)  # a Wednesday


@pytest.mark.parametrize(
    "modifier, expected",
    [
        ("now", NOW),
        ("-1h", NOW - dt.timedelta(hours=1)),
        ("-24h@h", dt.datetime(2026, 3, 10, 14, 0, tzinfo=dt.timezone.utc)),
        ("@d", dt.datetime(2026, 3, 11, 0, 0, tzinfo=dt.timezone.utc)),
        ("-1d@d+3h", dt.datetime(2026, 3, 10, 3, 0, tzinfo=dt.timezone.utc)),
        ("@w0", dt.datetime(2026, 3, 8, 0, 0, tzinfo=dt.timezone.utc)),
        ("@w1", dt.datetime(2026, 3, 9, 0, 0, tzinfo=dt.timezone.utc)),
        ("-1mon@mon", dt.datetime(2026, 2, 1, 0, 0, tzinfo=dt.timezone.utc)),
        ("-70m", NOW - dt.timedelta(minutes=70)),
        ("1700000000", dt.datetime.fromtimestamp(1700000000, tz=dt.timezone.utc)),
    ],
)
def test_relative_time_modifiers_evaluate_as_splunk_does(modifier, expected):
    assert ss.evaluate_relative(modifier, NOW) == expected


def test_all_time_and_real_time_are_told_apart():
    assert ss.evaluate_relative("0", NOW).year == 1970
    with pytest.raises(ss.RelativeTimeError):
        ss.evaluate_relative("rt-5m", NOW)
    with pytest.raises(ss.RelativeTimeError):
        ss.evaluate_relative("-1fortnight", NOW)


def test_a_dispatch_range_becomes_a_rolling_window():
    window = ss.derive_window("-24h@h", "now", NOW)
    assert window.window_s == pytest.approx(24 * 3600 + 37 * 60 + 22)
    assert window.offset_s == 0
    assert window.align_s == 3600
    # latest = @d ends the window at midnight, so the offset is the time since.
    ending_at_midnight = ss.derive_window("-7d@d", "@d", NOW)
    assert ending_at_midnight.window_s == 7 * 86400
    assert ending_at_midnight.offset_s == pytest.approx(14 * 3600 + 37 * 60 + 22)
    assert ss.derive_window("0", "now").all_time is True
    # Seen in the wild: dispatch.latest_time = 0. It cannot mean the epoch.
    assert ss.derive_window("-5m", "0", NOW).window_s == 300
    with pytest.raises(ss.RelativeTimeError):
        ss.derive_window("now", "-1h", NOW)


# ------------------------------------------------------------------- cron


@pytest.mark.parametrize(
    "expression, per_day",
    [
        ("* * * * *", 1440.0),
        ("*/5 * * * *", 288.0),
        ("0 * * * *", 24.0),
        ("0 6 * * 1", 1 / 7),
        ("05 08 * * 1-5", 5 / 7),
        ("0,30 9-17 * * *", 18.0),
    ],
)
def test_cron_frequency_is_the_weight(expression, per_day):
    assert ss.cron_firings_per_day(expression) == pytest.approx(per_day, rel=1e-6)


def test_cron_matches_a_moment_and_finds_the_next_one():
    cron = ss.parse_cron("*/15 9-17 * * 1-5")
    monday_nine = dt.datetime(2026, 1, 5, 9, 0)
    assert cron.matches(monday_nine)
    assert not cron.matches(monday_nine.replace(minute=7))
    saturday = dt.datetime(2026, 1, 10, 9, 0)
    assert not cron.matches(saturday)
    assert cron.next_after(monday_nine) == monday_nine.replace(minute=15)
    assert ss.parse_cron("0 0 * * 7").matches(dt.datetime(2026, 1, 4, 0, 0))  # 7 is Sunday


@pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "*/0 * * * *", "a b c d e"])
def test_bad_cron_is_refused(bad):
    with pytest.raises(ss.CronError):
        ss.parse_cron(bad)


def test_step_ids_are_narrow_and_never_collide():
    taken: set = set()
    first = ss.step_id_for("Errors in the last hour", taken)
    second = ss.step_id_for("Errors in the last hour!", taken)
    assert first == "errors-in-the-last-hour"
    assert second == "errors-in-the-last-hour-2"
