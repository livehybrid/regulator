"""The scenario format: what load to apply, and what it is made of.

A scenario is Regulator's payload, the analogue of a Stoker pack. It is a
directory containing ``scenario.yaml``, it is git-syncable, it lints before it
runs, and it is content-addressed so two runs of the same scenario are provably
the same test.

The shape, in one glance::

    name, engine, seed, description, tags
    corpus:      what data this assumes exists (which Stoker packs, which index)
    parameters:  named generators, resolved per iteration from a seeded stream
    time_policy: how the search time range moves between iterations
    personas:    weighted user archetypes, each a list of steps with think time
    load:        closed (virtual users) or open (arrival rate), plus a ramp
    abort_if:    guard rails that stop a run before it hurts the target

Two decisions in here are load-bearing and worth stating plainly.

**Parameters are drawn from a derived seed, not a shared stream.** With
hundreds of virtual users running concurrently on an event loop, a shared
random stream would hand out values in whatever order the loop happened to
schedule, so the same scenario with the same seed would produce a different
sequence of searches every run. Instead each draw derives its own seed by
hashing (scenario seed, virtual user, iteration, step, parameter name). The
sequence is then reproducible regardless of execution order, which is the
entire basis for comparing two runs.

**Time ranges move on purpose.** Splunk caches a completed job's artefact for
an identical search string over an identical time range, and the host page
cache keeps recently read buckets in RAM. Both make a repeated benchmark look
faster than the system really is. The time policy rotates the window across the
corpus so the working set moves, and the parameter machinery injects a nonce
into a Splunk comment so the search string differs without the work differing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

VALID_ENGINES = ("api", "browser", "mixed")
VALID_STEP_TYPES = ("search", "dashboard")
VALID_STEP_ENGINES = ("api", "browser")
VALID_EXEC_MODES = ("normal", "blocking", "oneshot", "export")
VALID_LOAD_MODELS = ("closed", "open")
VALID_TIME_MODES = ("rolling", "pinned")
VALID_WAIT_FOR = ("first_result", "all_panels")

# Search classes follow the dense / sparse / rare distinction Splunk's own
# scale-testing methodology uses, because they stress completely different
# parts of the stack: dense searches are aggregation and pipeline bound, rare
# searches are I/O and bloom-filter bound, and an accelerated search should
# barely touch either. A benchmark that only runs one class tells you about one
# third of your cluster.
VALID_CLASSES = (
    "dense",
    "sparse",
    "rare",
    "accelerated",
    "heavy",
    "subsearch",
    "realtime",
    "unclassified",
)

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)$", re.IGNORECASE)
_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}
_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Some lint findings are genuine errors and some are advice about a choice that
# is legal but will quietly cost you accuracy. Both are worth printing, only
# one should stop a run, so advisory lines are prefixed and the caller decides.
ADVICE_PREFIX = "advice: "


def is_advice(line: str) -> bool:
    return line.startswith(ADVICE_PREFIX) or ": " + ADVICE_PREFIX in line


class ScenarioError(ValueError):
    """A scenario that cannot be parsed at all.

    Distinct from a lint failure: a lint failure is a well-formed document that
    says something invalid, and we want to report every one of those at once
    rather than stopping at the first.
    """


def parse_duration(value: Any, what: str) -> float:
    """Parse ``30m``, ``24h``, ``1500ms`` or a bare number of seconds."""
    if value is None:
        raise ScenarioError(f"{what}: a duration is required")
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_RE.match(str(value).strip())
    if not match:
        raise ScenarioError(
            f"{what}: expected a duration like 30s, 15m, 24h or 7d, got {value!r}"
        )
    return float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]


@dataclass(frozen=True)
class ThinkTime:
    """How long a simulated user pauses between iterations.

    Think time is not a detail. It is the whole difference between a virtual
    user and a concurrent search: a real analyst spends most of their time
    reading the screen, so a hundred users might only ever produce a handful of
    simultaneous searches. Getting the distribution roughly right is what makes
    a virtual-user count mean anything.
    """

    dist: str = "fixed"
    value_s: float = 0.0
    median_s: float = 0.0
    sigma: float = 0.0
    min_s: float = 0.0
    max_s: float = 0.0

    @staticmethod
    def parse(raw: Any, where: str) -> "ThinkTime":
        if raw is None:
            return ThinkTime(dist="fixed", value_s=0.0)
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: think_time must be a mapping")
        dist = str(raw.get("dist", "fixed")).lower()
        if dist not in ("fixed", "uniform", "lognormal", "exponential"):
            raise ScenarioError(
                f"{where}: think_time.dist must be fixed, uniform, lognormal or "
                f"exponential, got {dist!r}"
            )
        return ThinkTime(
            dist=dist,
            value_s=parse_duration(raw.get("value_s", raw.get("value", 0)), f"{where}.value_s"),
            median_s=parse_duration(raw.get("median_s", 0), f"{where}.median_s"),
            sigma=float(raw.get("sigma", 0.0)),
            min_s=parse_duration(raw.get("min_s", 0), f"{where}.min_s"),
            max_s=parse_duration(raw.get("max_s", 0), f"{where}.max_s"),
        )


@dataclass(frozen=True)
class TimePolicy:
    """How a step's search time range is chosen.

    ``rolling`` moves with wall clock and jitters, which is realistic and
    defeats both caches. ``pinned`` fixes absolute epochs, which is perfectly
    reproducible but will be served from cache on the second run, so it is only
    honest for a deliberately cache-warm comparison.
    """

    mode: str = "rolling"
    window_s: float = 86400.0
    jitter_s: float = 0.0
    align_s: float = 60.0
    earliest_epoch: Optional[float] = None
    latest_epoch: Optional[float] = None

    @staticmethod
    def parse(raw: Any, where: str = "time_policy") -> "TimePolicy":
        if raw is None:
            return TimePolicy()
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: must be a mapping")
        mode = str(raw.get("mode", "rolling")).lower()
        if mode not in VALID_TIME_MODES:
            raise ScenarioError(
                f"{where}.mode must be rolling or pinned, got {mode!r}"
            )
        return TimePolicy(
            mode=mode,
            window_s=parse_duration(raw.get("window", "24h"), f"{where}.window"),
            jitter_s=parse_duration(raw.get("jitter", 0), f"{where}.jitter"),
            align_s=parse_duration(raw.get("align", "1m"), f"{where}.align"),
            earliest_epoch=(
                float(raw["earliest_epoch"]) if raw.get("earliest_epoch") is not None else None
            ),
            latest_epoch=(
                float(raw["latest_epoch"]) if raw.get("latest_epoch") is not None else None
            ),
        )


@dataclass(frozen=True)
class Step:
    """One unit of work: a search dispatched, or a dashboard opened."""

    id: str
    type: str = "search"
    engine: str = "api"
    step_class: str = "unclassified"
    # Search steps.
    spl: Optional[str] = None
    exec_mode: str = "normal"
    result_count: int = 100
    want_preview: bool = False
    time_policy: Optional[TimePolicy] = None
    # Dashboard steps (browser engine, Phase 2).
    app: Optional[str] = None
    dashboard: Optional[str] = None
    wait_for: str = "first_result"
    # Weight within a persona when steps are sampled rather than walked in
    # order. Zero means "always run, in order", which is the default.
    weight: float = 0.0

    @staticmethod
    def parse(raw: Any, where: str) -> "Step":
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: a step must be a mapping")
        step_id = str(raw.get("id", "")).strip()
        if not step_id:
            raise ScenarioError(f"{where}: a step needs an id")
        return Step(
            id=step_id,
            type=str(raw.get("type", "search")).lower(),
            engine=str(raw.get("engine", "api")).lower(),
            step_class=str(raw.get("class", "unclassified")).lower(),
            spl=raw.get("spl"),
            exec_mode=str(raw.get("exec_mode", "normal")).lower(),
            result_count=int(raw.get("result_count", 100)),
            want_preview=bool(raw.get("want_preview", False)),
            time_policy=(
                TimePolicy.parse(raw["time_range"], f"{where}.time_range")
                if raw.get("time_range") is not None
                else None
            ),
            app=raw.get("app"),
            dashboard=raw.get("dashboard"),
            wait_for=str(raw.get("wait_for", "first_result")).lower(),
            weight=float(raw.get("weight", 0.0)),
        )


@dataclass(frozen=True)
class Persona:
    """A weighted user archetype."""

    name: str
    weight: float
    think_time: ThinkTime
    steps: List[Step]

    @staticmethod
    def parse(raw: Any, where: str) -> "Persona":
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: a persona must be a mapping")
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ScenarioError(f"{where}: a persona needs a name")
        steps_raw = raw.get("steps") or []
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ScenarioError(f"{where}({name}): a persona needs at least one step")
        return Persona(
            name=name,
            weight=float(raw.get("weight", 1.0)),
            think_time=ThinkTime.parse(raw.get("think_time"), f"{where}({name}).think_time"),
            steps=[Step.parse(s, f"{where}({name}).steps[{i}]") for i, s in enumerate(steps_raw)],
        )


@dataclass(frozen=True)
class RampStage:
    """One leg of a load ramp.

    Either climb to a target over a period, or hold at the current target for
    one. Staged ramps are not decoration: going from nothing to full load
    instantly measures a cold cluster's panic response rather than its steady
    state, and there is a documented case of a naive ramp kernel-panicking a
    search head.
    """

    to: Optional[float] = None
    over_s: float = 0.0
    hold_s: float = 0.0

    @staticmethod
    def parse(raw: Any, where: str) -> "RampStage":
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: a ramp stage must be a mapping")
        if "hold_s" in raw or "hold" in raw:
            return RampStage(hold_s=parse_duration(raw.get("hold_s", raw.get("hold")), f"{where}.hold"))
        if "to" not in raw:
            raise ScenarioError(f"{where}: a ramp stage needs either 'to' and 'over_s', or 'hold_s'")
        return RampStage(
            to=float(raw["to"]),
            over_s=parse_duration(raw.get("over_s", raw.get("over", 0)), f"{where}.over_s"),
        )


@dataclass(frozen=True)
class LoadModel:
    """Closed (fixed users) or open (fixed arrival rate).

    Closed is the dial people mean by "200 concurrent users" and it is what
    capacity planning wants. Open fixes arrivals independently of how slow the
    system gets, which is the only way to measure tail latency honestly at
    saturation: a closed loop stops issuing work when the server stalls, so the
    requests that would have queued are never sent and the tail disappears.

    Closed mode can still be honest about latency if ``pacing_s`` is set: each
    iteration then has an intended start time, and latency is measured from
    that rather than from when the loop actually got round to it.
    """

    model: str = "closed"
    virtual_users: int = 1
    arrival_rate_per_min: float = 0.0
    pacing_s: float = 0.0
    ramp: List[RampStage] = field(default_factory=list)
    duration_s: Optional[float] = None

    @property
    def co_corrected(self) -> bool:
        return self.model == "open" or self.pacing_s > 0

    @staticmethod
    def parse(raw: Any, where: str = "load") -> "LoadModel":
        if raw is None:
            return LoadModel()
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: must be a mapping")
        model = str(raw.get("model", "closed")).lower()
        if model not in VALID_LOAD_MODELS:
            raise ScenarioError(f"{where}.model must be closed or open, got {model!r}")
        ramp_raw = raw.get("ramp") or []
        if not isinstance(ramp_raw, list):
            raise ScenarioError(f"{where}.ramp must be a list of stages")
        return LoadModel(
            model=model,
            virtual_users=int(raw.get("virtual_users", 1)),
            arrival_rate_per_min=float(raw.get("arrival_rate_per_min", 0.0)),
            pacing_s=parse_duration(raw.get("pacing_s", 0), f"{where}.pacing_s"),
            ramp=[RampStage.parse(s, f"{where}.ramp[{i}]") for i, s in enumerate(ramp_raw)],
            duration_s=(
                parse_duration(raw["duration"], f"{where}.duration")
                if raw.get("duration") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class AbortIf:
    """Guard rails.

    A load test is meant to find the limit, not to cross it and take the
    cluster with it. Every one of these is checked continuously during a run
    and a breach ends the run as ``aborted`` with the predicate recorded, which
    is a result in its own right rather than a failure.
    """

    error_rate_pct: Optional[float] = None
    p95_ms: Optional[float] = None
    sut_cpu_pct: Optional[float] = None
    skipped_searches_delta: Optional[int] = None
    # Applies to the generator, not the target. A worker that cannot keep to
    # its own schedule is measuring itself, so the run is stopped and marked
    # invalid rather than reported as a Splunk result.
    generator_drift_ms: Optional[float] = 2000.0

    @staticmethod
    def parse(raw: Any, where: str = "abort_if") -> "AbortIf":
        if raw is None:
            return AbortIf()
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: must be a mapping")
        def _opt_float(key: str) -> Optional[float]:
            return float(raw[key]) if raw.get(key) is not None else None
        return AbortIf(
            error_rate_pct=_opt_float("error_rate_pct"),
            p95_ms=_opt_float("p95_ms"),
            sut_cpu_pct=_opt_float("sut_cpu_pct"),
            skipped_searches_delta=(
                int(raw["skipped_searches_delta"])
                if raw.get("skipped_searches_delta") is not None
                else None
            ),
            generator_drift_ms=(
                float(raw["generator_drift_ms"])
                if raw.get("generator_drift_ms") is not None
                else 2000.0
            ),
        )


@dataclass(frozen=True)
class Corpus:
    """What data the scenario assumes exists.

    Naming the Stoker packs a scenario depends on is not documentation, it is
    the comparability contract: two runs are only comparable if the data
    underneath them is. Phase 3 uses this to let a benchmark say "fill with
    these packs, then run this", so the fill stops being a manual step someone
    forgets.
    """

    requires_packs: List[str] = field(default_factory=list)
    index: str = "main"
    metric_index: Optional[str] = None

    @staticmethod
    def parse(raw: Any, where: str = "corpus") -> "Corpus":
        if raw is None:
            return Corpus()
        if not isinstance(raw, dict):
            raise ScenarioError(f"{where}: must be a mapping")
        packs = raw.get("requires_packs") or []
        if isinstance(packs, str):
            packs = [p.strip() for p in packs.split(",") if p.strip()]
        return Corpus(
            requires_packs=[str(p) for p in packs],
            index=str(raw.get("index", "main")),
            metric_index=(str(raw["metric_index"]) if raw.get("metric_index") else None),
        )


@dataclass(frozen=True)
class Scenario:
    """A parsed, not-yet-linted scenario."""

    name: str
    engine: str
    seed: int
    description: str
    tags: List[str]
    corpus: Corpus
    parameters: Dict[str, Dict[str, Any]]
    time_policy: TimePolicy
    personas: List[Persona]
    load: LoadModel
    abort_if: AbortIf
    source_path: Optional[Path] = None

    @property
    def steps(self) -> List[Step]:
        return [step for persona in self.personas for step in persona.steps]

    def resolve_time_policy(self, step: Step) -> TimePolicy:
        return step.time_policy or self.time_policy


def load_scenario(path: str | Path) -> Scenario:
    """Load a scenario from a directory or a ``scenario.yaml`` file."""
    p = Path(path)
    if p.is_dir():
        p = p / "scenario.yaml"
    if not p.is_file():
        raise ScenarioError(f"no scenario at {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{p}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"{p}: a scenario must be a YAML mapping")
    return parse_scenario(raw, source_path=p)


def parse_scenario(raw: Dict[str, Any], source_path: Optional[Path] = None) -> Scenario:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ScenarioError("a scenario needs a name")

    personas_raw = raw.get("personas") or []
    if not isinstance(personas_raw, list) or not personas_raw:
        raise ScenarioError(f"{name}: a scenario needs at least one persona")

    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ScenarioError(f"{name}: parameters must be a mapping of name to generator")

    return Scenario(
        name=name,
        engine=str(raw.get("engine", "api")).lower(),
        # A scenario with no seed is not reproducible, so refuse to invent one
        # silently: default to a fixed, obvious value and let lint say so.
        seed=int(raw.get("seed", 0)),
        description=str(raw.get("description", "")),
        tags=[str(t) for t in tags],
        corpus=Corpus.parse(raw.get("corpus")),
        parameters={str(k): (v if isinstance(v, dict) else {"type": "literal", "value": v})
                    for k, v in parameters.items()},
        time_policy=TimePolicy.parse(raw.get("time_policy")),
        personas=[Persona.parse(p, f"personas[{i}]") for i, p in enumerate(personas_raw)],
        load=LoadModel.parse(raw.get("load")),
        abort_if=AbortIf.parse(raw.get("abort_if")),
        source_path=source_path,
    )


# Parameter placeholders look like {{name}} with optional surrounding space.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def placeholders(text: str) -> List[str]:
    """Every ``{{name}}`` in a string, in order of appearance."""
    return PLACEHOLDER_RE.findall(text or "")


def lint(scenario: Scenario) -> List[str]:
    """Return every problem with a scenario, not just the first.

    Linting is a hard gate before a run launches, exactly as pack lint is in
    Stoker. The expensive failure mode this prevents is discovering after a
    twenty-minute run that one step referenced an index that does not exist, so
    every number in the report is quietly wrong.

    This is the offline half. The online half (parsing every SPL against the
    real target, confirming indexes and dashboards exist) needs a connection
    and lives in the engine's validate path.
    """
    errors: List[str] = []

    if not _IDENT_RE.match(scenario.name):
        errors.append(
            f"name {scenario.name!r} must be lowercase alphanumeric with dashes or "
            "underscores, up to 64 characters"
        )
    if scenario.engine not in VALID_ENGINES:
        errors.append(f"engine must be one of {', '.join(VALID_ENGINES)}, got {scenario.engine!r}")
    if scenario.seed == 0:
        errors.append(
            "seed is 0 or unset: set an explicit seed, otherwise parameter draws are not "
            "reproducible and two runs of this scenario are not comparable"
        )

    # Persona weights.
    total_weight = sum(p.weight for p in scenario.personas)
    if total_weight <= 0:
        errors.append("persona weights must sum to more than zero")
    for persona in scenario.personas:
        if persona.weight < 0:
            errors.append(f"persona {persona.name}: weight must not be negative")

    seen_persona: set[str] = set()
    for persona in scenario.personas:
        if persona.name in seen_persona:
            errors.append(f"duplicate persona name {persona.name!r}")
        seen_persona.add(persona.name)

        seen_step: set[str] = set()
        for step in persona.steps:
            where = f"persona {persona.name}, step {step.id}"
            if step.id in seen_step:
                errors.append(f"{where}: duplicate step id within the persona")
            seen_step.add(step.id)

            if step.type not in VALID_STEP_TYPES:
                errors.append(f"{where}: type must be one of {', '.join(VALID_STEP_TYPES)}")
            if step.engine not in VALID_STEP_ENGINES:
                errors.append(f"{where}: engine must be api or browser")
            if step.step_class not in VALID_CLASSES:
                errors.append(
                    f"{where}: class must be one of {', '.join(VALID_CLASSES)}, "
                    f"got {step.step_class!r}"
                )
            if step.exec_mode not in VALID_EXEC_MODES:
                errors.append(f"{where}: exec_mode must be one of {', '.join(VALID_EXEC_MODES)}")
            if step.wait_for not in VALID_WAIT_FOR:
                errors.append(f"{where}: wait_for must be first_result or all_panels")
            if step.result_count < 0:
                errors.append(f"{where}: result_count must not be negative")

            if step.type == "search":
                if not step.spl or not step.spl.strip():
                    errors.append(f"{where}: a search step needs spl")
                elif not _looks_like_spl(step.spl):
                    errors.append(
                        f"{where}: spl should start with 'search ', '|' or a bare search "
                        f"term, got {step.spl.strip()[:40]!r}"
                    )
                if step.engine != "api":
                    errors.append(f"{where}: a search step must use the api engine")
                if step.exec_mode in ("oneshot", "export"):
                    # Not an error, a caveat worth surfacing loudly, so it is
                    # reported as a lint line the operator has to read.
                    errors.append(
                        f"{where}: {ADVICE_PREFIX}exec_mode {step.exec_mode} creates no job "
                        "artefact, so runDuration, scanCount and resultCount cannot be "
                        "recorded. Use normal unless you are deliberately measuring the "
                        "cheap path"
                    )
            elif step.type == "dashboard":
                if not step.dashboard:
                    errors.append(f"{where}: a dashboard step needs a dashboard name")
                if not step.app:
                    errors.append(f"{where}: a dashboard step needs an app")
                if step.engine != "browser":
                    errors.append(f"{where}: a dashboard step must use the browser engine")

            # Every placeholder must have a generator behind it.
            for text in (step.spl or "", step.dashboard or ""):
                for token in placeholders(text):
                    if token not in scenario.parameters:
                        errors.append(
                            f"{where}: uses {{{{{token}}}}} but no parameter named "
                            f"{token!r} is declared"
                        )

    # Engine consistency: a scenario declared api-only must not contain browser
    # steps, otherwise a fleet gets provisioned with the wrong resource profile.
    engines_used = {step.engine for step in scenario.steps}
    if scenario.engine == "api" and "browser" in engines_used:
        errors.append("engine is api but the scenario contains browser steps: declare engine: mixed")
    if scenario.engine == "browser" and "api" in engines_used:
        errors.append("engine is browser but the scenario contains api steps: declare engine: mixed")

    # Parameter generators.
    for pname, spec in scenario.parameters.items():
        ptype = str(spec.get("type", "")).lower()
        if not ptype:
            errors.append(f"parameter {pname}: needs a type")
            continue
        if ptype not in PARAMETER_TYPES:
            errors.append(
                f"parameter {pname}: unknown type {ptype!r}, expected one of "
                f"{', '.join(sorted(PARAMETER_TYPES))}"
            )
            continue
        errors.extend(
            f"parameter {pname}: {problem}" for problem in PARAMETER_TYPES[ptype](spec)
        )

    # Load model.
    load = scenario.load
    if load.model == "closed":
        if load.virtual_users < 1:
            errors.append("load.virtual_users must be at least 1 in the closed model")
        if load.arrival_rate_per_min:
            errors.append("load.arrival_rate_per_min is meaningless in the closed model")
    else:
        if load.arrival_rate_per_min <= 0:
            errors.append("load.arrival_rate_per_min must be positive in the open model")
        if load.pacing_s:
            errors.append("load.pacing_s is meaningless in the open model, arrivals are already paced")
    for i, stage in enumerate(load.ramp):
        if stage.to is not None and stage.to < 0:
            errors.append(f"load.ramp[{i}]: target must not be negative")
        if stage.to is not None and stage.over_s < 0:
            errors.append(f"load.ramp[{i}]: over_s must not be negative")

    # Time policy.
    tp = scenario.time_policy
    if tp.mode == "pinned" and (tp.earliest_epoch is None or tp.latest_epoch is None):
        errors.append("time_policy.mode is pinned but earliest_epoch and latest_epoch are not both set")
    if tp.mode == "pinned":
        errors.append(
            f"{ADVICE_PREFIX}time_policy.mode is pinned: an identical time range will be "
            "served from Splunk's dispatch cache on a repeat run, so only use this for a "
            "deliberately cache-warm comparison"
        )
    if tp.mode == "rolling" and tp.window_s <= 0:
        errors.append("time_policy.window must be positive")
    if tp.mode == "rolling" and tp.jitter_s == 0 and len(scenario.steps) > 1:
        errors.append(
            f"{ADVICE_PREFIX}time_policy.jitter is 0: every iteration then reads the same "
            "buckets, which the host page cache will serve from RAM and make the cluster "
            "look faster than it is. Set a jitter of at least a few minutes"
        )

    # Guard rails.
    if scenario.abort_if.error_rate_pct is None and scenario.abort_if.p95_ms is None:
        errors.append(
            f"{ADVICE_PREFIX}abort_if declares no error-rate or latency ceiling: a run with "
            "no guard rail can drive a target into the ground before anyone notices"
        )

    return errors


def _looks_like_spl(spl: str) -> bool:
    stripped = spl.strip()
    if not stripped:
        return False
    if stripped.startswith("|"):
        return True
    lowered = stripped.lower()
    return lowered.startswith("search ") or lowered.startswith("search\n") or lowered.startswith("| ")


# ---------------------------------------------------------------------------
# Parameter generator validation. The generators themselves live in params.py;
# only their declaration is validated here, so lint stays offline and fast.
# ---------------------------------------------------------------------------

def _check_choice(spec: Dict[str, Any]) -> List[str]:
    values = spec.get("values")
    if not isinstance(values, list) or not values:
        return ["type choice needs a non-empty values list"]
    return []


def _check_int_range(spec: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    if spec.get("min") is None or spec.get("max") is None:
        problems.append("type int_range needs min and max")
    elif float(spec["min"]) > float(spec["max"]):
        problems.append("min must not exceed max")
    return problems


def _check_choice_from_search(spec: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    if not spec.get("spl"):
        problems.append("type choice_from_search needs spl")
    if not spec.get("field"):
        problems.append("type choice_from_search needs field")
    refresh = str(spec.get("refresh", "per_run")).lower()
    if refresh not in ("per_run", "never"):
        problems.append("refresh must be per_run or never")
    return problems


def _check_literal(spec: Dict[str, Any]) -> List[str]:
    return [] if "value" in spec else ["type literal needs a value"]


def _check_nonce(_spec: Dict[str, Any]) -> List[str]:
    return []


def _check_ipv4(_spec: Dict[str, Any]) -> List[str]:
    return []


PARAMETER_TYPES = {
    "choice": _check_choice,
    "int_range": _check_int_range,
    "choice_from_search": _check_choice_from_search,
    "literal": _check_literal,
    "nonce": _check_nonce,
    "ipv4": _check_ipv4,
}
