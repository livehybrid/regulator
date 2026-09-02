"""Launch a benchmark, wait for it, judge it, and report.

Runs inside a GitHub Action step, so it has no dependencies beyond the standard
library: a `pip install` on a runner is a failure mode nobody needs when the job
is already going to take twenty minutes.

The exit contract mirrors the agent's, because a pipeline needs to tell three
different things apart and treating them the same is how a team ends up
ignoring the check:

* **0** every gate met, or no gates were given and this is report-only.
* **1** something broke: the control plane was unreachable, the run failed to
  start, the scenario does not exist.
* **2** a gate was breached. A real result, and the one the pull request should
  argue about.
* **3** the run was invalid, so it measured the load generator rather than
  Splunk. That is a tooling problem, not a regression, and a pipeline that
  reports it as a performance failure will send somebody hunting for a slowdown
  that never happened.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_GATE = 2
EXIT_INVALID = 3

TERMINAL = ("completed", "stopped", "aborted", "failed")
POLL_INTERVAL_S = 10.0


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def call(
    path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None
) -> Any:
    server = env("REGULATOR_SERVER").rstrip("/")
    request = urllib.request.Request(
        server + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    request.add_header("Content-Type", "application/json")
    token = env("REGULATOR_TOKEN")
    if token:
        # Both shapes: a session cookie for a password-protected control plane,
        # and a bearer for when token auth lands.
        request.add_header("Cookie", f"regulator_session={token}")
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
        return json.loads(payload) if payload else None


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"::error::{message}")
    emit("verdict", "blocked")
    sys.exit(EXIT_FAILED)


def emit(name: str, value: str) -> None:
    """Set a step output, handling the multi-line case."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        if "\n" in value:
            handle.write(f"{name}<<REGULATOR_EOF\n{value}\nREGULATOR_EOF\n")
        else:
            handle.write(f"{name}={value}\n")


def summarise(report: str, verdict: str) -> None:
    """Put the report in the job summary, where a reviewer will actually see it."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"## Regulator benchmark: {verdict}\n\n```\n{report}\n```\n")


def main() -> int:
    for required in ("REGULATOR_SERVER", "REGULATOR_TARGET", "REGULATOR_SCENARIO"):
        if not env(required):
            fail(f"{required} is required")

    launch: Dict[str, Any] = {
        "target_id": int(env("REGULATOR_TARGET")),
        "scenario": env("REGULATOR_SCENARIO"),
        "label": env("GITHUB_REF_NAME", "ci") + "-" + env("GITHUB_SHA", "")[:7],
    }
    if env("REGULATOR_VUS"):
        launch["virtual_users"] = int(env("REGULATOR_VUS"))
    if env("REGULATOR_DURATION"):
        launch["duration_s"] = float(env("REGULATOR_DURATION"))

    try:
        run = call("/api/runs", "POST", launch)
    except urllib.error.HTTPError as exc:
        fail(f"could not start the run: HTTP {exc.code} {exc.read().decode()[:300]}")
    except Exception as exc:  # noqa: BLE001
        fail(f"could not reach the control plane: {exc}")

    run_id = run["id"]
    emit("run-id", str(run_id))
    print(f"launched run {run_id} ({launch['scenario']}), waiting for it to finish")

    deadline = time.time() + float(env("REGULATOR_TIMEOUT", "3600"))
    state = run["state"]
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        try:
            run = call(f"/api/runs/{run_id}")
        except Exception as exc:  # noqa: BLE001 - a blip must not fail the job
            print(f"  (polling failed, retrying: {exc})")
            continue
        if run["state"] != state:
            state = run["state"]
            print(f"  state: {state}")
        stats = run.get("stats") or {}
        if stats.get("executions"):
            print(
                f"  {stats['executions']} executions, "
                f"p95 {(stats.get('latency') or {}).get('p95_ms', 0):.0f}ms, "
                f"{stats.get('error_rate_pct', 0):.1f}% errors"
            )
        if state in TERMINAL:
            break
    else:
        fail(f"the run did not finish within the timeout (last state: {state})")

    gates: List[str] = [g.strip() for g in env("REGULATOR_GATES").splitlines() if g.strip()]
    compare_body: Dict[str, Any] = {"gates": gates}
    if env("REGULATOR_BASELINE"):
        compare_body["baseline_label"] = env("REGULATOR_BASELINE")

    try:
        result = call(f"/api/runs/{run_id}/compare", "POST", compare_body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        if exc.code == 404 and "baseline" in detail:
            # The first run of a new benchmark has nothing to compare against.
            # Reporting that as a failure would make everyone disable the check.
            print(f"::notice::no baseline yet ({detail}), reporting without judging")
            compare_body.pop("baseline_label", None)
            compare_body["gates"] = [g for g in gates if "baseline" not in g]
            result = call(f"/api/runs/{run_id}/compare", "POST", compare_body)
        else:
            fail(f"comparison failed: HTTP {exc.code} {detail}")
    except Exception as exc:  # noqa: BLE001
        fail(f"comparison failed: {exc}")

    report = result.get("report") or json.dumps(result, indent=2)
    print(report)

    for warning in result.get("warnings") or []:
        print(f"::warning::{warning}")

    if result.get("blocked"):
        emit("verdict", "blocked")
        emit("report", report)
        summarise(report, "blocked")
        print(f"::error::{result['blocked']}")
        return EXIT_INVALID

    verdict = "pass" if result.get("ok") else "fail"
    emit("verdict", verdict)
    emit("report", report)
    summarise(report, verdict)

    if not result.get("ok"):
        for gate in result.get("gates") or []:
            if not gate.get("passed"):
                print(f"::error::gate breached: {gate['gate']}: {gate['detail']}")
        return EXIT_GATE

    promote = env("REGULATOR_PROMOTE")
    if promote:
        try:
            call("/api/baselines", "POST", {"run_id": run_id, "label": promote})
            print(f"::notice::run {run_id} promoted to the baseline {promote!r}")
        except Exception as exc:  # noqa: BLE001 - promotion failing is not a test failure
            print(f"::warning::could not promote the baseline: {exc}")

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
