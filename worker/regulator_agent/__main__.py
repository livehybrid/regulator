"""Standalone entry point: ``python -m regulator_agent``.

Everything comes from the environment (see :mod:`regulator_agent.config`), so a
run is one ``docker run`` with a handful of variables and no control plane
anywhere. That is deliberate. A load generator you can only drive through a web
UI is one you cannot put in a pipeline, cannot bisect a regression with, and
cannot debug when it matters.

Exit codes are the CI contract:

==== ==================================================================
0    the run completed and the result is valid
1    the run failed outright (bad configuration, unreachable target,
     credentials rejected)
2    a guard rail stopped the run: the target breached an error-rate or
     latency ceiling. This is a real result, not a tooling failure
3    the run is invalid: the generator could not keep to its own
     schedule, so the numbers describe this worker rather than Splunk
4    the scenario failed lint and was never started
==== ==================================================================

Codes 2 and 3 are distinct on purpose. A pipeline that treats them the same
cannot tell "the cluster is too slow" from "the load box was too small", and
those need opposite responses.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from .compare import GateError, compare_runs
from .config import Config, ConfigError, load_config
from .engines import BrowserUnavailable, get_engine
from .hec import HecEmitter
from .params import ParameterResolver
from .report import render, target_report
from .smartstore import CacheState, cache_state, delta as cache_delta, evict_all
from .smartstore import render as render_cache
from .results import NdjsonEmitter, RunStats, RunSummary
from .scenario import ScenarioError, is_advice, lint, load_scenario
from .scheduler import Scheduler

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_GUARD_RAIL = 2
EXIT_INVALID = 3
EXIT_LINT = 4

log = logging.getLogger("regulator")

PROGRESS_INTERVAL_S = 5.0


def _setup_logging(level: str) -> None:
    # Logs go to stderr so stdout stays a clean NDJSON stream that can be piped
    # straight into another process without filtering.
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _resolve_scenario_path(config: Config) -> Path:
    """Find the scenario, allowing a bare name to mean a built-in one.

    The image ships the scenario library at ``REG_BUILTIN_SCENARIOS_DIR``, so
    ``REG_SCENARIO=search-classes`` works with no volume mount and no git
    access. That is what makes a cold container immediately useful.
    """
    candidate = Path(config.scenario_path)
    if candidate.exists():
        return candidate
    if config.builtin_scenarios_dir:
        builtin = Path(config.builtin_scenarios_dir) / config.scenario_path
        if builtin.exists():
            return builtin
    raise ScenarioError(
        f"no scenario at {config.scenario_path!r}"
        + (
            f" and none named that in {config.builtin_scenarios_dir}"
            if config.builtin_scenarios_dir
            else ""
        )
    )


def _report_lint(problems: List[str], strict: bool, what: str) -> bool:
    """Print lint output. Returns True when the run may proceed."""
    blocking = [p for p in problems if not is_advice(p)]
    advisory = [p for p in problems if is_advice(p)]

    for line in advisory:
        log.warning("%s: %s", what, line)
    for line in blocking:
        log.error("%s: %s", what, line)

    if blocking and strict:
        log.error(
            "%s found %d blocking problem(s). Fix them, or set REG_LINT_STRICT=0 to run "
            "anyway and accept that the results may be meaningless",
            what,
            len(blocking),
        )
        return False
    return True


async def _progress(stats: RunStats, stop: asyncio.Event) -> None:
    """A human-readable heartbeat on stderr while the run is in flight."""
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=PROGRESS_INTERVAL_S)
            return
        snap = stats.snapshot()
        latency = snap["latency"]
        log.info(
            "t=%.0fs executions=%d in_flight=%d throughput=%.1f/s p50=%.0fms p95=%.0fms "
            "errors=%.1f%%",
            snap["elapsed_s"],
            snap["executions"],
            snap["in_flight"],
            snap["throughput_per_s"],
            latency["p50_ms"],
            latency["p95_ms"],
            snap["error_rate_pct"],
        )


async def _do_evict(config: Config, args: argparse.Namespace) -> int:
    """Evict cached buckets now, and exit. Needs no scenario.

    Separate from the pre-run eviction on purpose. Sometimes you want to drop
    the cache, go and do something else, and come back: run a search by hand,
    watch the indexer, start a run from the control plane later. Tying eviction
    to a run would make that impossible.

    Refuses to flush the whole estate unless told to explicitly. On a shared
    cluster most of that cache belongs to other people's dashboards, and there
    is no undo beyond waiting for it to re-download.
    """
    indexes = list(args.index or []) or list(config.evict_cache_indexes)
    if not indexes and not args.all_indexes:
        log.error(
            "refusing to evict every index without being told to. Pass --index <name> "
            "(repeatable), or set REG_EVICT_CACHE_INDEXES, or pass --all-indexes if you "
            "really do mean the whole cache"
        )
        return EXIT_FAILED

    engine = get_engine("api", config)
    try:
        await engine.start()

        before = await cache_state(engine.client)
        if not before.available:
            log.error("no SmartStore cache to evict: %s", before.reason)
            return EXIT_FAILED

        sys.stderr.write("before:\n" + render_cache(before) + "\n")

        result = await evict_all(engine.client, indexes=indexes or None)
        after = await cache_state(engine.client)

        sys.stderr.write("after:\n" + render_cache(after) + "\n")
        log.info(
            "evicted %d of %d bucket(s), %.1f GB. Local buckets %d -> %d",
            result.evicted,
            result.attempted,
            result.bytes_evicted / 1e9,
            before.local_buckets,
            after.local_buckets,
        )
        if result.failed:
            # A bucket with a live reader cannot be evicted, which is correct
            # behaviour rather than a fault: something is searching it.
            log.warning(
                "%d bucket(s) refused eviction, most likely because a search is "
                "currently reading them",
                result.failed,
            )

        print(
            json.dumps(
                {
                    "indexes": indexes or "all",
                    "eviction": result.to_dict(),
                    "before": before.to_dict(),
                    "after": after.to_dict(),
                },
                indent=2,
                default=str,
            )
        )
        return EXIT_OK
    finally:
        with contextlib.suppress(Exception):
            await engine.close()


def _do_compare(args: argparse.Namespace) -> int:
    """Compare two run summaries and judge them against gates. No target needed.

    Deliberately offline: a comparison is arithmetic over two JSON documents, so
    it belongs in a pipeline step that has the artefacts and no access to
    Splunk at all.
    """
    try:
        candidate = json.loads(Path(args.compare).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("could not read the run summary %s: %s", args.compare, exc)
        return EXIT_FAILED

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("could not read the baseline %s: %s", args.baseline, exc)
            return EXIT_FAILED

    try:
        result = compare_runs(
            candidate,
            baseline,
            gates=args.gate or [],
            allow_invalid=args.allow_invalid,
        )
    except GateError as exc:
        log.error("%s", exc)
        return EXIT_FAILED

    sys.stderr.write(result.explain() + "\n")
    print(json.dumps(result.to_dict(), indent=2, default=str))

    if result.blocked:
        return EXIT_INVALID
    return EXIT_OK if result.ok else EXIT_GUARD_RAIL


async def _describe_target(config: Config) -> int:
    """Report on the target and exit. Needs no scenario.

    Deliberately ahead of everything else in _run: the whole point is to point
    this at a cluster nobody has benchmarked, and demanding a scenario that
    already matches it would beg the question it exists to answer.
    """
    engine = get_engine("api", config)
    try:
        await engine.start()
        report = await target_report(engine.client)
    finally:
        with contextlib.suppress(Exception):
            await engine.close()

    sys.stderr.write(render(report) + "\n")
    print(json.dumps(report, indent=2, default=str))
    return EXIT_OK if report.get("can_dispatch") else EXIT_FAILED


async def _run(config: Config, args: argparse.Namespace) -> int:
    if args.evict_cache:
        return await _do_evict(config, args)

    if args.target_report:
        return await _describe_target(config)

    scenario_path = _resolve_scenario_path(config)
    scenario = load_scenario(scenario_path)
    log.info("scenario %s from %s", scenario.name, scenario_path)

    strict = os.environ.get("REG_LINT_STRICT", "1").strip().lower() not in ("0", "false", "no")
    if not _report_lint(lint(scenario), strict, "offline lint"):
        return EXIT_LINT

    # A scenario mixing both engines needs the fleet to run each cohort with the
    # right resource profile, so for now it is one engine per run. Catching it
    # here beats letting half the steps reach an engine that cannot run them:
    # a scenario that half runs produces a report that looks complete and is not.
    engines_used = {s.engine for s in scenario.steps}
    if len(engines_used) > 1:
        log.error(
            "scenario %s mixes the %s engines in one run, which needs the worker fleet. "
            "Split it into an api scenario and a browser scenario",
            scenario.name,
            " and ".join(sorted(engines_used)),
        )
        return EXIT_LINT

    engine_name = next(iter(engines_used)) if engines_used else "api"
    try:
        engine = get_engine(engine_name, config)
    except (ValueError, BrowserUnavailable) as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    stats = RunStats(run_id=config.run_id, slot=config.slot)
    emitters: List[object] = []
    hec: Optional[HecEmitter] = None
    stop_progress = asyncio.Event()
    progress_task: Optional[asyncio.Task[None]] = None

    try:
        await engine.start()

        capabilities = await engine.probe()
        log.info(
            "target %s is Splunk %s (%s), %d cores, concurrent-search ceiling %s",
            config.target.url,
            capabilities.version or "unknown",
            ", ".join(capabilities.server_roles) or "unknown role",
            capabilities.cpu_count,
            capabilities.max_hist_searches
            if capabilities.max_hist_searches is not None
            else "unknown",
        )
        for note in capabilities.notes:
            log.warning("target: %s", note)

        if args.probe_only:
            print(json.dumps(capabilities.to_dict(), indent=2))
            return EXIT_OK


        online = await engine.validate(scenario)
        if not _report_lint(online, strict, "online lint"):
            return EXIT_LINT

        if args.lint_only:
            log.info("lint only: scenario %s is runnable", scenario.name)
            return EXIT_OK

        resolver = ParameterResolver(scenario, seed=config.seed)
        if resolver.dynamic_parameters:
            log.info(
                "resolving %d dynamic parameter(s) from the target",
                len(resolver.dynamic_parameters),
            )
            await engine.resolve_parameters(resolver)

        emitters.append(NdjsonEmitter(path=config.output_path))
        if config.hec is not None:
            hec = HecEmitter(config.hec, run_id=config.run_id)
            await hec.start()
            emitters.append(hec)
            if config.self_instrumented:
                log.warning(
                    "telemetry is being written to the same host as the target: this run "
                    "is self-instrumented and adds a small load to its own subject"
                )

        scheduler = Scheduler(
            scenario=scenario,
            config=config,
            engine=engine,
            resolver=resolver,
            stats=stats,
            emitters=emitters,
            capabilities=capabilities,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    sig, lambda s=sig: scheduler.request_stop(f"signal {s.name}")
                )

        # SmartStore provenance. Without knowing what was already on local disk,
        # a fast run and a slow run of the same scenario are not comparable:
        # one may have read local disk while the other paid for an
        # object-storage fetch before it could filter anything.
        indexes_in_play = config.evict_cache_indexes or (
            (scenario.corpus.index,) if scenario.corpus.index else ()
        )
        if config.evict_cache:
            log.warning(
                "REG_EVICT_CACHE is set: dropping the local SmartStore cache for %s "
                "before this run, so it measures the cold path",
                ", ".join(indexes_in_play) or "every index",
            )
            eviction = await evict_all(engine.client, indexes=indexes_in_play or None)
            log.warning(
                "evicted %d/%d bucket(s), %.1f GB",
                eviction.evicted,
                eviction.attempted,
                eviction.bytes_evicted / 1e9,
            )
        else:
            eviction = None

        cache_before = await cache_state(engine.client)
        if cache_before.available:
            log.info(render_cache(cache_before).splitlines()[0])

        progress_task = asyncio.create_task(_progress(stats, stop_progress), name="progress")
        summary = await scheduler.run()
        stop_progress.set()

        summary.cache = await _cache_provenance(engine.client, cache_before, eviction)

        await _report_summary(summary, hec, config)
        return _exit_code(summary)

    except ConfigError as exc:
        log.error("configuration: %s", exc)
        return EXIT_FAILED
    except ScenarioError as exc:
        log.error("scenario: %s", exc)
        return EXIT_LINT
    finally:
        stop_progress.set()
        if progress_task is not None:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await progress_task
        if hec is not None:
            await hec.close()
        for emitter in emitters:
            close = getattr(emitter, "close", None)
            if close is not None and not asyncio.iscoroutinefunction(close):
                with contextlib.suppress(Exception):
                    close()
        with contextlib.suppress(Exception):
            await engine.close()


async def _cache_provenance(client, before: CacheState, eviction) -> Optional[dict]:
    """What the run did to the SmartStore cache, and therefore what it measured.

    Reported on every run rather than only when eviction was asked for. The
    question "was this served from cache?" has to be answerable for a result to
    mean anything, and it is not answerable after the fact.
    """
    if not before.available:
        return {"available": False, "reason": before.reason}
    after = await cache_state(client)
    change = cache_delta(before, after)
    payload = {
        "before": before.to_dict(),
        "after": after.to_dict(),
        "delta": change.to_dict(),
    }
    if eviction is not None:
        payload["eviction"] = eviction.to_dict()

    if change.provenance == "warm":
        log.info(
            "cache: warm. Nothing was downloaded during this run, so the numbers "
            "describe the search tier reading local disk"
        )
    else:
        log.warning(
            "cache: %s. %d bucket(s) (%.1f GB) were downloaded during this run, so part "
            "of what was measured is object storage and the network, not the search tier",
            change.provenance,
            change.buckets_downloaded,
            change.bytes_downloaded / 1e9,
        )
    return payload


async def _report_summary(
    summary: RunSummary, hec: Optional[HecEmitter], config: Config
) -> None:
    payload = summary.to_dict()
    if hec is not None:
        # Flush before reading the counters, otherwise they report whatever the
        # background loop happened to have shipped at this instant and the
        # telemetry block undercounts every run.
        await hec.flush()
        payload["telemetry"] = hec.stats()
        # The summary event itself is emitted after the counters are read, so
        # it is deliberately not included in its own totals.
        hec.emit_summary(payload)

    # The summary goes to stdout as a single JSON object on its own line, after
    # every step record. A CI job reads the last line; a human pipes it to jq.
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()

    summary_path = os.environ.get("REG_SUMMARY_PATH")
    if summary_path:
        Path(summary_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("summary written to %s", summary_path)

    stats = summary.stats
    latency = stats["latency"]
    log.info(
        "%s: %d executions in %.0fs, %.1f/s, p50=%.0fms p95=%.0fms p99=%.0fms, errors %.2f%%",
        summary.outcome,
        stats["executions"],
        summary.duration_s,
        stats["throughput_per_s"],
        latency["p50_ms"],
        latency["p95_ms"],
        latency["p99_ms"],
        stats["error_rate_pct"],
    )
    queueing = stats.get("queueing", {})
    if queueing.get("searches_queued"):
        log.warning(
            "QUEUEING OBSERVED: %d of %d searches (%.1f%%) waited in QUEUED before "
            "running, p95 %.0fms. The target was at its concurrent-search ceiling, "
            "which is the point a capacity test is looking for",
            queueing["searches_queued"],
            stats["executions"],
            queueing["queued_pct"],
            queueing["queued_ms"]["p95_ms"],
        )

    if not summary.co_corrected:
        log.warning(
            "this run was not coordinated-omission corrected (closed model with no pacing): "
            "the latency percentiles are service times, so read throughput as the signal "
            "rather than the tail"
        )
    if summary.abort_reason:
        log.warning("abort reason: %s", summary.abort_reason)
    if not summary.valid:
        log.error("RESULT IS INVALID: %s", summary.invalid_reason)


def _exit_code(summary: RunSummary) -> int:
    if summary.outcome == "failed":
        return EXIT_FAILED
    if not summary.valid:
        return EXIT_INVALID
    if summary.outcome == "aborted":
        return EXIT_GUARD_RAIL
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="regulator_agent",
        description=(
            "Drive search load at a Splunk cluster and measure what comes back. "
            "Configuration is by environment variable, see the README."
        ),
    )
    parser.add_argument(
        "--lint-only",
        action="store_true",
        help="validate the scenario offline and against the target, then exit",
    )
    parser.add_argument(
        "--compare",
        metavar="RUN.json",
        help=(
            "compare a run summary against a baseline and judge it against --gate "
            "expressions, then exit. Needs no target: it is arithmetic over two JSON "
            "documents"
        ),
    )
    parser.add_argument(
        "--baseline",
        metavar="BASELINE.json",
        help="the run summary to compare against. Used with --compare",
    )
    parser.add_argument(
        "--gate",
        action="append",
        metavar="EXPR",
        help=(
            "a gate to judge the comparison by, repeatable. For example "
            "'p95 <= baseline + 15%%', 'error_rate <= 2%%', 'queued == 0', 'valid'"
        ),
    )
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help=(
            "compare even when a run is marked invalid. Off by default because an "
            "invalid run measured the load generator rather than Splunk"
        ),
    )
    parser.add_argument(
        "--evict-cache",
        action="store_true",
        help=(
            "evict cached SmartStore buckets now and exit, so the next searches read "
            "cold. Needs --index or --all-indexes"
        ),
    )
    parser.add_argument(
        "--index",
        action="append",
        metavar="NAME",
        help="index to evict, repeatable. Used with --evict-cache",
    )
    parser.add_argument(
        "--all-indexes",
        action="store_true",
        help=(
            "with --evict-cache, flush the entire cache rather than named indexes. On a "
            "shared cluster this drops other people's cached data too"
        ),
    )
    parser.add_argument(
        "--target-report",
        action="store_true",
        help=(
            "describe the target in full (version, roles, search peers, concurrency "
            "ceiling, indexes and their event counts, SmartStore, whether this account "
            "can dispatch) as JSON on stdout and a summary on stderr, then exit"
        ),
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="report what the target is and what its search-concurrency ceiling is, then exit",
    )
    args = parser.parse_args(argv)

    if args.compare:
        _setup_logging(os.environ.get("REG_LOG_LEVEL", "INFO").upper())
        return _do_compare(args)

    env = dict(os.environ)
    if (args.target_report or args.probe_only or args.evict_cache) and not env.get(
        "REG_SCENARIO"
    ):
        # Both modes exit before a scenario is used, so requiring one would be
        # a pointless obstacle when pointing this at an unfamiliar cluster.
        env["REG_SCENARIO"] = "smoke"

    try:
        config = load_config(env)
    except ConfigError as exc:
        _setup_logging("INFO")
        log.error("configuration: %s", exc)
        return EXIT_FAILED

    _setup_logging(config.log_level)
    started = time.time()
    try:
        code = asyncio.run(_run(config, args))
    except KeyboardInterrupt:
        log.warning("interrupted")
        return EXIT_GUARD_RAIL
    log.info("finished in %.1fs with exit code %d", time.time() - started, code)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised by the smoke test
    sys.exit(main())
