"""Deterministic parameter generation.

Two requirements pull in opposite directions, and this module is where they are
reconciled.

**Reproducibility.** Two runs of the same scenario must issue the same sequence
of searches, otherwise comparing them measures the difference in the workload
rather than the difference in the cluster. That argues for a fixed seed.

**Cache defeat.** Splunk keeps a completed job's artefact for a while and will
hand back a cached result for an identical search string over an identical time
range. The host page cache separately keeps recently read buckets in RAM. Both
make a repeated benchmark look faster than the system is. That argues for
varying the workload.

The reconciliation: vary *within* a run, repeat *across* runs. Iteration 7 of
virtual user 3 asks about a different IP address than iteration 6 did, so
nothing is served from cache, but iteration 7 of virtual user 3 asks about the
*same* IP address it asked about in yesterday's run, so the two runs are
comparable.

That is only achievable if a draw does not depend on execution order. With
hundreds of virtual users on one event loop, a shared random stream hands out
values in whatever order the loop happened to schedule, so the same seed
produces a different assignment every time. Instead every draw derives its own
seed by hashing the coordinates of the draw::

    seed(scenario_seed, virtual_user, iteration, step_id, parameter_name)

Same coordinates, same value, regardless of what else the process was doing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .scenario import PLACEHOLDER_RE, Scenario

# A Splunk comment is three backticks around arbitrary text. Appending one
# changes the search string without changing a single thing the search does,
# which is the cheapest available way to miss the dispatch cache.
#
# Belt and braces, deliberately: the time-range jitter in timepolicy.py is the
# primary defence, because a different time range definitely changes what
# Splunk considers a reusable artefact and also moves the buckets being read so
# the host page cache is not doing the work either. The comment is the second
# line of defence in case a future Splunk normalises comments out of the key.
CACHE_BUST_TEMPLATE = " ```{marker}```"


def derive_seed(*parts: Any) -> int:
    """A stable 64-bit seed from the coordinates of a draw.

    blake2b rather than :func:`hash` because Python's string hashing is salted
    per process, so ``hash("x")`` differs between runs and would destroy the
    reproducibility this whole module exists to provide.
    """
    joined = "\x1f".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(joined, digest_size=8).digest()
    return int.from_bytes(digest, "big")


def rng_for(*parts: Any) -> random.Random:
    """A private random stream for one draw."""
    return random.Random(derive_seed(*parts))


@dataclass(frozen=True)
class DrawContext:
    """Where in the run a draw is happening.

    These four coordinates are the whole basis of reproducibility, so they are
    passed explicitly rather than read from any ambient state.
    """

    scenario_seed: int
    vu_id: int
    iteration: int
    step_id: str


class ParameterError(ValueError):
    """A parameter that cannot be resolved at run time."""


class ParameterResolver:
    """Resolves ``{{name}}`` placeholders in a search string.

    Generators that need the target (``choice_from_search``) are resolved once
    at run start and bound here, so the hot path never issues an extra search.
    """

    def __init__(self, scenario: Scenario, seed: Optional[int] = None) -> None:
        self._specs: Dict[str, Dict[str, Any]] = dict(scenario.parameters)
        self._seed = scenario.seed if seed is None else seed
        self._bound: Dict[str, Sequence[Any]] = {}

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def dynamic_parameters(self) -> Dict[str, Dict[str, Any]]:
        """Parameters that need a search against the target before the run starts."""
        return {
            name: spec
            for name, spec in self._specs.items()
            if str(spec.get("type", "")).lower() == "choice_from_search"
        }

    def bind(self, name: str, values: Sequence[Any]) -> None:
        """Supply the values for a dynamic parameter.

        Called once per run, after the resolver search has come back. An empty
        result is an error rather than a silent fallback: a scenario that hunts
        for source IPs against an index that turned out to be empty would
        otherwise run happily and measure nothing.
        """
        if name not in self._specs:
            raise ParameterError(f"cannot bind unknown parameter {name!r}")
        if not values:
            raise ParameterError(
                f"parameter {name!r} resolved to no values: its resolver search returned "
                "nothing, so the scenario has no data to work with"
            )
        self._bound[name] = list(values)

    def value(self, name: str, ctx: DrawContext) -> Any:
        """Draw one value for one parameter at one point in the run."""
        spec = self._specs.get(name)
        if spec is None:
            raise ParameterError(f"no parameter named {name!r} is declared")
        ptype = str(spec.get("type", "")).lower()
        rng = rng_for(self._seed, ctx.vu_id, ctx.iteration, ctx.step_id, name)

        if ptype == "literal":
            return spec.get("value")

        if ptype == "choice":
            values = spec.get("values") or []
            if not values:
                raise ParameterError(f"parameter {name!r}: no values")
            return rng.choice(list(values))

        if ptype == "int_range":
            low = int(spec["min"])
            high = int(spec["max"])
            return rng.randint(low, high)

        if ptype == "ipv4":
            # Defaults to TEST-NET-3 (203.0.113.0/24), which is reserved for
            # documentation and therefore safe to put in a search that might
            # end up in someone's logs.
            network = ipaddress.ip_network(str(spec.get("cidr", "203.0.113.0/24")), strict=False)
            offset = rng.randrange(network.num_addresses)
            return str(network[offset])

        if ptype == "nonce":
            length = int(spec.get("length", 12))
            return f"{derive_seed(self._seed, ctx.vu_id, ctx.iteration, ctx.step_id, name):016x}"[:length]

        if ptype == "choice_from_search":
            values = self._bound.get(name)
            if values is None:
                raise ParameterError(
                    f"parameter {name!r} is a choice_from_search but was never bound: "
                    "the resolver search must run before the load starts"
                )
            return rng.choice(list(values))

        raise ParameterError(f"parameter {name!r}: unknown type {ptype!r}")

    def render(self, text: str, ctx: DrawContext) -> Tuple[str, Dict[str, Any]]:
        """Substitute every placeholder, returning the text and what was used.

        The values are returned as well as substituted because they go onto the
        step record. Being able to see afterwards that a slow search was the
        one that happened to draw a very common source IP is the difference
        between a number and an explanation.
        """
        used: Dict[str, Any] = {}

        def _sub(match: "re.Match[str]") -> str:
            name = match.group(1)
            value = self.value(name, ctx)
            used[name] = value
            # Escaped, because a choice_from_search value comes from the
            # TARGET'S OWN DATA. Anyone who can get an event into the searched
            # index with a crafted field value could otherwise have the next
            # run dispatch their SPL under the load-test account: the shipped
            # scenarios substitute these inside quoted operands such as
            # sourceIPAddress="{{src_ip}}", and a double quote closes it.
            return _escape_spl_value(value)

        return PLACEHOLDER_RE.sub(_sub, text or ""), used


# The marker is concatenated into SPL, so anything outside this set is
# stripped. Three backticks close the Splunk comment the marker lives in, and a
# double quote closes the string operand it sits in inside the _audit
# correlation query, so an unfiltered run label was an SPL injection reaching
# every search a run dispatched. The run label is operator-supplied and, in the
# GitHub Action, comes from a branch name, which git permits backticks in.
_MARKER_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def sanitise_marker_part(value: str) -> str:
    """Strip anything that could escape the SPL comment or string it lands in."""
    return _MARKER_SAFE.sub("_", str(value))[:64]


def _escape_spl_value(value: Any) -> str:
    """Make a drawn value safe to substitute into an SPL string operand."""
    text = str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Numeric values often sit outside quotes (`| makeresults count={{n}}`),
        # and escaping them would break the search.
        return text
    return text.replace("\\", "\\\\").replace('"', '\\"')


def cache_bust_marker(run_id: str, vu_id: int, iteration: int, step_id: str) -> str:
    """A short, human-readable marker identifying one step execution.

    It goes into the search string as a comment, so it also turns up in the
    ``_audit`` index against the dispatched search. That is a useful accident:
    it lets a run be reconciled against the target's own audit trail without
    any correlation guesswork.

    Every component is sanitised, because this string is concatenated into SPL
    that runs under the target's own credentials.
    """
    return (
        f"reg:{sanitise_marker_part(run_id)}:vu{int(vu_id)}:"
        f"i{int(iteration)}:{sanitise_marker_part(step_id)}"
    )


def apply_cache_bust(spl: str, marker: str) -> str:
    """Append the marker as a Splunk comment."""
    return f"{spl.rstrip()}{CACHE_BUST_TEMPLATE.format(marker=marker)}"


def weighted_pick(items: Sequence[Any], weights: Sequence[float], rng: random.Random) -> Any:
    """Pick one item by weight.

    Written out rather than using :func:`random.choices` so the draw consumes a
    single, predictable amount of randomness from the stream. ``random.choices``
    is free to change its internal sampling between Python versions, which
    would silently change which persona a virtual user is assigned and break
    comparability against last month's baseline.
    """
    total = float(sum(weights))
    if total <= 0:
        raise ParameterError("weights must sum to more than zero")
    point = rng.random() * total
    upto = 0.0
    for item, weight in zip(items, weights):
        upto += float(weight)
        if point < upto:
            return item
    return items[-1]


def persona_for_vu(personas: Sequence[Any], vu_id: int, scenario_seed: int) -> Any:
    """Assign a virtual user to a persona, stably.

    A virtual user keeps its persona for the whole run (real people do not
    switch job role between searches), and gets the same persona in the next
    run of the same scenario, so a comparison is like for like.
    """
    rng = rng_for(scenario_seed, "persona", vu_id)
    return weighted_pick(list(personas), [p.weight for p in personas], rng)


def think_time_for(think: Any, ctx: DrawContext, scenario_seed: int) -> float:
    """Draw a think time in seconds from the persona's distribution."""
    rng = rng_for(scenario_seed, "think", ctx.vu_id, ctx.iteration)
    dist = think.dist
    if dist == "fixed":
        value = think.value_s
    elif dist == "uniform":
        low = think.min_s
        high = think.max_s if think.max_s > low else low
        value = rng.uniform(low, high)
    elif dist == "exponential":
        mean = think.value_s or think.median_s or 1.0
        value = rng.expovariate(1.0 / mean) if mean > 0 else 0.0
    elif dist == "lognormal":
        # Parameterised by median rather than mean because a median is what a
        # human can actually estimate about their own behaviour, and for a
        # lognormal the median is exp(mu), so mu = ln(median).
        import math

        median = think.median_s or think.value_s or 1.0
        mu = math.log(median) if median > 0 else 0.0
        value = rng.lognormvariate(mu, think.sigma or 0.5)
    else:
        value = think.value_s

    if think.min_s and value < think.min_s:
        value = think.min_s
    if think.max_s and value > think.max_s:
        value = think.max_s
    return max(0.0, float(value))
