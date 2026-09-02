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

from .config import Config, ConfigError, load_config
from .engines import get_engine
from .hec import HecEmitter
from .params import ParameterResolver
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


async def _run(config: Config, args: argparse.Namespace) -> int:
    scenario_path = _resolve_scenario_path(config)
    scenario = load_scenario(scenario_path)
    log.info("scenario %s from %s", scenario.name, scenario_path)

    strict = os.environ.get("REG_LINT_STRICT", "1").strip().lower() not in ("0", "false", "no")
    if not _report_lint(lint(scenario), strict, "offline lint"):
        return EXIT_LINT

    # Phase 0 ships the api engine only. Catch a browser step here rather than
    # letting it reach an engine that cannot run it: a scenario that half runs
    # produces a report that looks complete and is not.
    browser_steps = [s.id for s in scenario.steps if s.engine != "api"]
    if browser_steps:
        log.error(
            "scenario %s contains %d step(s) needing the browser engine (%s), which "
            "arrives in Phase 2. Run an api-only scenario, or drop those steps",
            scenario.name,
            len(browser_steps),
            ", ".join(browser_steps[:5]),
        )
        return EXIT_LINT

    engine = get_engine("api", config)
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

        progress_task = asyncio.create_task(_progress(stats, stop_progress), name="progress")
        summary = await scheduler.run()
        stop_progress.set()

        _report_summary(summary, hec, config)
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


def _report_summary(summary: RunSummary, hec: Optional[HecEmitter], config: Config) -> None:
    payload = summary.to_dict()
    if hec is not None:
        payload["telemetry"] = hec.stats()
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
        "--probe-only",
        action="store_true",
        help="report what the target is and what its search-concurrency ceiling is, then exit",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
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
