"""Reading which indexes, over which time range, a search reads."""

from __future__ import annotations

import datetime as dt

import pytest

from regulator_agent.splscope import Clause, clause_span, evaluate, read_scope


def scoped(spl):
    return [(c.indexes, c.earliest, c.latest, c.origin) for c in read_scope(spl).clauses]


@pytest.mark.parametrize(
    "spl, expected",
    [
        ("search index=main | stats count", [(("main",), None, None, "search")]),
        ("index=web earliest=-4h latest=-1h | stats count", [(("web",), "-4h", "-1h", "search")]),
        ('index=web latest="01/02/2026:00:00:00"', [(("web",), None, "01/02/2026:00:00:00", "search")]),
        ("| tstats count where index=net earliest=-2d by host", [(("net",), "-2d", None, "search")]),
        ('index IN (a, "b", c*) | head 1', [(("a", "b", "c*"), None, None, "search")]),
    ],
)
def test_the_base_search(spl, expected):
    assert scoped(spl) == expected


def test_or_groups_each_carry_their_own_index_and_bounds():
    assert scoped("(index=a earliest=-1h) OR (index=b earliest=-7d@d latest=@d) | stats count") == [
        (("a",), "-1h", None, "or-group"),
        (("b",), "-7d@d", "@d", "or-group"),
    ]


def test_an_or_alternative_outside_the_groups_is_its_own_clause():
    assert scoped("index=a OR (index=b earliest=-7d)") == [
        (("a",), None, None, "search"),
        (("b",), "-7d", None, "or-group"),
    ]


def test_a_group_without_scope_terms_is_a_filter_not_a_clause():
    assert scoped("index=web (status=500 OR status=503) earliest=-4h") == [
        (("web",), "-4h", None, "search"),
    ]


def test_a_group_inherits_the_index_it_qualifies():
    assert scoped("index=web (sourcetype=a earliest=-1h) OR (sourcetype=b earliest=-2h)") == [
        (("web",), "-1h", None, "or-group"),
        (("web",), "-2h", None, "or-group"),
    ]


def test_appended_and_joined_searches_are_read_on_their_own_terms():
    spl = (
        "index=web | append [search index=fw earliest=-30d | stats count] "
        "| join host [search index=cmdb latest=-1d] | append [| inputlookup hosts.csv]"
    )
    assert scoped(spl) == [
        (("web",), None, None, "search"),
        (("fw",), "-30d", None, "subsearch"),
        (("cmdb",), None, "-1d", "subsearch"),
    ]


def test_multisearch_and_union_and_a_filter_subsearch():
    assert scoped("| multisearch [search index=a] [search index=b latest=-1d]") == [
        (("a",), None, None, "subsearch"),
        (("b",), None, "-1d", "subsearch"),
    ]
    assert scoped("| union [search index=a] [search index=b]") == [
        (("a",), None, None, "subsearch"),
        (("b",), None, None, "subsearch"),
    ]
    assert scoped("index=a [search index=b earliest=-1h | fields host]") == [
        (("a",), None, None, "search"),
        (("b",), "-1h", None, "subsearch"),
    ]


def test_a_nested_subsearch_is_read_too():
    assert ("c",) in [c[0] for c in scoped("index=a | append [search index=b [search index=c | fields x]]")]


def test_map_search_is_read_and_a_token_bound_is_noted():
    scope = read_scope('| makeresults | map search="search index=z earliest=$e$"')
    assert [(c.indexes, c.origin) for c in scope.clauses] == [(("z",), "map")]
    assert any("token" in note for note in scope.notes)


@pytest.mark.parametrize(
    "spl",
    [
        'index=a | eval earliest=relative_time(now(), "-1d") | search host=x',
        'index=a | stats earliest(_time) as first',
        'index=a msg="earliest=-9d index=nope"',
        'index=a _index_earliest=-1d',
    ],
)
def test_things_that_look_like_bounds_but_are_not(spl):
    assert scoped(spl) == [(("a",), None, None, "search")]


def test_generating_commands_that_read_no_buckets():
    assert scoped("| rest /services/server/info") == []
    assert scoped("| inputlookup x.csv | stats count") == []
    assert scoped("| makeresults count=5") == []


def test_an_index_less_search_is_kept_for_the_corpus_fallback():
    assert scoped("sourcetype=x | stats count") == [((), None, None, "search")]
    scope = read_scope("`my_macro` | stats count")
    assert any("macro" in note for note in scope.notes)


def test_mstats_is_marked_as_metrics():
    clause, = read_scope("| mstats avg(cpu) where index=metrics span=1m").clauses
    assert clause.indexes == ("metrics",) and clause.metrics


# ------------------------------------------------------------------ time


NOW = dt.datetime(2026, 11, 9, 12, 30, tzinfo=dt.timezone.utc).timestamp()


def test_evaluate_relative_absolute_and_epoch():
    assert evaluate("-1h", NOW) == NOW - 3600
    assert evaluate("@d", NOW) == dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc).timestamp()
    assert evaluate("1700000000", NOW) == 1700000000.0
    assert evaluate("01/02/2026:00:00:00", NOW) == dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).timestamp()
    assert evaluate("now", NOW) == NOW
    assert evaluate("$tok$", NOW) is None
    assert evaluate("nonsense", NOW) is None


def test_a_clause_with_no_bounds_reads_the_dispatch_window():
    span, notes = clause_span(Clause(("a",)), (100.0, 200.0), NOW, NOW + 60)
    assert span == (100.0, 200.0) and notes == []


def test_relative_bounds_move_with_the_run():
    span, _ = clause_span(Clause(("a",), "-7d", None), (NOW - 3600, NOW + 60), NOW, NOW + 60)
    assert span == (NOW - 7 * 86400, NOW + 60)
    span, _ = clause_span(Clause(("a",), "-2d", "-1d"), (NOW - 3600, NOW + 60), NOW, NOW + 600)
    assert span == (NOW - 2 * 86400, NOW + 600 - 86400)


def test_an_unreadable_bound_falls_back_and_says_so():
    span, notes = clause_span(Clause(("a",), "$e$", None), (1.0, 2.0), NOW, NOW)
    assert span == (1.0, 2.0) and notes
