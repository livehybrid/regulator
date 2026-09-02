"""Deterministic parameter generation.

The headline property is in the first two tests: the same coordinates always
produce the same value, whatever order the event loop happened to run things
in. Everything about comparing two runs rests on that. If it breaks, every
stored baseline silently becomes meaningless, which is why the seed derivation
itself is pinned to an expected value below.
"""

from __future__ import annotations

import ipaddress
import random

import pytest

from conftest import tiny_scenario_dict
from regulator_agent.params import (
    DrawContext,
    ParameterError,
    ParameterResolver,
    apply_cache_bust,
    cache_bust_marker,
    derive_seed,
    persona_for_vu,
    think_time_for,
    weighted_pick,
)
from regulator_agent.scenario import ThinkTime, parse_scenario


def make_resolver(**parameters) -> ParameterResolver:
    doc = tiny_scenario_dict()
    if parameters:
        doc["parameters"] = parameters
    return ParameterResolver(parse_scenario(doc))


def ctx(vu=3, iteration=7, step="one", seed=7) -> DrawContext:
    return DrawContext(scenario_seed=seed, vu_id=vu, iteration=iteration, step_id=step)


# ------------------------------------------------- the reproducibility core


def test_the_same_coordinates_always_give_the_same_value_whatever_the_order():
    """Draws must not depend on execution order.

    With hundreds of virtual users on one event loop, a shared random stream
    would hand out values in scheduling order, so the same seed would produce a
    different workload every run and two runs would not be comparable.
    """
    resolver = make_resolver(n={"type": "int_range", "min": 0, "max": 10 ** 9})
    coordinates = [ctx(vu=v, iteration=i) for v in range(5) for i in range(5)]

    first = {(c.vu_id, c.iteration): resolver.value("n", c) for c in coordinates}

    shuffled = list(coordinates)
    random.Random(99).shuffle(shuffled)
    for c in shuffled * 4:
        assert resolver.value("n", c) == first[(c.vu_id, c.iteration)]


def test_two_separately_built_resolvers_agree():
    a = make_resolver(n={"type": "int_range", "min": 0, "max": 10 ** 9})
    b = make_resolver(n={"type": "int_range", "min": 0, "max": 10 ** 9})
    assert [a.value("n", ctx(iteration=i)) for i in range(20)] == [
        b.value("n", ctx(iteration=i)) for i in range(20)
    ]


def test_derive_seed_is_pinned():
    """Pinned deliberately.

    A change to the hash would silently invalidate every baseline ever stored,
    while every test that only checks internal consistency would stay green.
    This test is the tripwire: if it fails, the change is a breaking one and
    old baselines must be discarded on purpose rather than by accident.
    """
    assert derive_seed(42, "vu", 3) == derive_seed(42, "vu", 3)
    assert derive_seed(42, "vu", 3) != derive_seed(42, "vu", 4)
    # A fixed, known value, so a future refactor of the hashing is caught here.
    assert derive_seed("regulator", 1, 2, 3) == 8511000382219094524


def test_different_iterations_produce_a_real_distribution():
    """Reproducible must not mean constant, or nothing defeats the cache."""
    resolver = make_resolver(n={"type": "int_range", "min": 0, "max": 10 ** 6})
    values = {resolver.value("n", ctx(iteration=i)) for i in range(50)}
    assert len(values) > 30


# ------------------------------------------------------------- each generator


def test_literal():
    assert make_resolver(x={"type": "literal", "value": "fixed"}).value("x", ctx()) == "fixed"


def test_choice_stays_within_its_values():
    resolver = make_resolver(x={"type": "choice", "values": ["a", "b", "c"]})
    drawn = {resolver.value("x", ctx(iteration=i)) for i in range(60)}
    assert drawn <= {"a", "b", "c"}
    assert len(drawn) == 3


def test_int_range_bounds_are_inclusive_and_respected():
    resolver = make_resolver(x={"type": "int_range", "min": 5, "max": 7})
    drawn = {resolver.value("x", ctx(iteration=i)) for i in range(200)}
    assert drawn == {5, 6, 7}


def test_ipv4_defaults_to_the_documentation_range():
    resolver = make_resolver(x={"type": "ipv4"})
    network = ipaddress.ip_network("203.0.113.0/24")
    for i in range(50):
        assert ipaddress.ip_address(resolver.value("x", ctx(iteration=i))) in network


def test_ipv4_honours_an_explicit_cidr():
    resolver = make_resolver(x={"type": "ipv4", "cidr": "10.20.0.0/16"})
    network = ipaddress.ip_network("10.20.0.0/16")
    for i in range(50):
        assert ipaddress.ip_address(resolver.value("x", ctx(iteration=i))) in network


def test_nonce_is_hex_of_the_requested_length_and_varies():
    resolver = make_resolver(x={"type": "nonce", "length": 8})
    values = [resolver.value("x", ctx(iteration=i)) for i in range(20)]
    assert all(len(v) == 8 and int(v, 16) >= 0 for v in values)
    assert len(set(values)) > 15


def test_choice_from_search_requires_binding_first():
    resolver = make_resolver(
        ip={"type": "choice_from_search", "spl": "search x", "field": "src"}
    )
    with pytest.raises(ParameterError) as excinfo:
        resolver.value("ip", ctx())
    assert "never bound" in str(excinfo.value)

    resolver.bind("ip", ["10.0.0.1", "10.0.0.2"])
    assert resolver.value("ip", ctx()) in {"10.0.0.1", "10.0.0.2"}


def test_binding_nothing_is_an_error():
    """An empty resolver result means the scenario has no data to work with.

    Failing loudly at startup beats running happily and measuring nothing.
    """
    resolver = make_resolver(
        ip={"type": "choice_from_search", "spl": "search x", "field": "src"}
    )
    with pytest.raises(ParameterError) as excinfo:
        resolver.bind("ip", [])
    assert "no values" in str(excinfo.value)


def test_binding_an_unknown_parameter_is_an_error():
    with pytest.raises(ParameterError):
        make_resolver().bind("nope", ["x"])


def test_dynamic_parameters_lists_only_the_ones_needing_the_target():
    resolver = make_resolver(
        a={"type": "choice", "values": [1]},
        b={"type": "choice_from_search", "spl": "search x", "field": "f"},
    )
    assert set(resolver.dynamic_parameters) == {"b"}


# ---------------------------------------------------------------- rendering


def test_render_substitutes_and_reports_what_it_used():
    resolver = make_resolver(n={"type": "literal", "value": 42})
    text, used = resolver.render("search index=main n={{n}} m={{ n }}", ctx())
    assert text == "search index=main n=42 m=42"
    assert used == {"n": 42}


def test_render_leaves_ordinary_text_alone():
    resolver = make_resolver(n={"type": "literal", "value": 1})
    text, used = resolver.render("search index=main | stats count", ctx())
    assert text == "search index=main | stats count"
    assert used == {}


def test_render_raises_for_an_undeclared_placeholder():
    with pytest.raises(ParameterError):
        make_resolver().render("search x={{nope}}", ctx())


def test_cache_bust_appends_a_splunk_comment_and_preserves_the_search():
    marker = cache_bust_marker("run7", 3, 9, "step-a")
    spl = "search index=main | stats count"
    busted = apply_cache_bust(spl, marker)
    assert busted.startswith(spl)
    assert busted.endswith("```")
    assert marker in busted


def test_cache_bust_markers_are_unique_per_execution():
    a = cache_bust_marker("run", 1, 1, "s")
    b = cache_bust_marker("run", 1, 2, "s")
    assert a != b


# ------------------------------------------------------- weights and pacing


def test_weighted_pick_respects_the_weights():
    rng = random.Random(4)
    picks = [weighted_pick(["a", "b"], [90, 10], rng) for _ in range(10000)]
    share = picks.count("a") / len(picks)
    assert 0.86 < share < 0.94


def test_weighted_pick_rejects_zero_weights():
    with pytest.raises(ParameterError):
        weighted_pick(["a"], [0], random.Random(1))


def test_persona_assignment_is_stable_and_roughly_proportional():
    personas = parse_scenario(
        {
            **tiny_scenario_dict(),
            "personas": [
                {**tiny_scenario_dict()["personas"][0], "name": "big", "weight": 80},
                {**tiny_scenario_dict()["personas"][0], "name": "small", "weight": 20},
            ],
        }
    ).personas

    # A virtual user keeps its persona: real people do not change job role
    # between searches, and a run must be comparable to the next one.
    assert persona_for_vu(personas, 17, 99).name == persona_for_vu(personas, 17, 99).name

    names = [persona_for_vu(personas, vu, 99).name for vu in range(2000)]
    share = names.count("big") / len(names)
    assert 0.75 < share < 0.85


@pytest.mark.parametrize("dist", ["fixed", "uniform", "lognormal", "exponential"])
def test_think_time_is_never_negative_and_honours_the_clamps(dist):
    think = ThinkTime(dist=dist, value_s=5, median_s=5, sigma=1.0, min_s=1, max_s=9)
    for i in range(200):
        value = think_time_for(think, ctx(iteration=i), 7)
        assert 1 <= value <= 9


def test_fixed_think_time_is_exact():
    think = ThinkTime(dist="fixed", value_s=2.5)
    assert think_time_for(think, ctx(), 7) == 2.5


def test_think_time_is_reproducible_for_the_same_coordinates():
    think = ThinkTime(dist="lognormal", median_s=10, sigma=0.5)
    a = think_time_for(think, ctx(vu=2, iteration=5), 7)
    b = think_time_for(think, ctx(vu=2, iteration=5), 7)
    assert a == b
