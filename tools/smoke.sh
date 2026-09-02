#!/usr/bin/env bash
#
# End-to-end smoke test: start the fake splunkd, point a real agent at it, and
# assert the run completed, was valid and actually did some work.
#
# This is the gate CI runs before an image is signed. It is deliberately not a
# unit test: it exercises the packaged artefact the way an operator would, over
# a real socket, with configuration coming only from the environment. A test
# suite can be green while the entrypoint is broken, the scenario library did
# not make it into the image, or an environment variable was renamed on one
# side only. This catches all three.
#
# Usage:
#   tools/smoke.sh local                     run the agent from this checkout
#   tools/smoke.sh docker <image>            run the agent from a built image
#
set -euo pipefail

MODE="${1:-local}"
IMAGE="${2:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

# Tunables, so a slow or busy machine can be given more room without editing
# the script.
SMOKE_VUS="${SMOKE_VUS:-3}"
SMOKE_DURATION_S="${SMOKE_DURATION_S:-8}"
SMOKE_MIN_EXECUTIONS="${SMOKE_MIN_EXECUTIONS:-6}"
SMOKE_BASE_LATENCY_MS="${SMOKE_BASE_LATENCY_MS:-20}"

FAKE_PID=""
WORKDIR="$(mktemp -d)"

cleanup() {
    if [ -n "${FAKE_PID}" ] && kill -0 "${FAKE_PID}" 2>/dev/null; then
        kill "${FAKE_PID}" 2>/dev/null || true
        wait "${FAKE_PID}" 2>/dev/null || true
    fi
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

fail() {
    echo "SMOKE FAILED: $*" >&2
    if [ -s "${WORKDIR}/agent.log" ]; then
        echo "--- agent stderr (last 40 lines) ---" >&2
        tail -40 "${WORKDIR}/agent.log" >&2
    fi
    exit 1
}

# A free port, chosen by the kernel rather than guessed. Guessing is how a
# smoke test becomes intermittently red on a busy build machine.
PORT="$(${PYTHON} - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

echo "==> starting the fake splunkd on port ${PORT}"
${PYTHON} "${REPO_ROOT}/tools/fake_splunk.py" \
    --host 127.0.0.1 \
    --port "${PORT}" \
    --base-latency-ms "${SMOKE_BASE_LATENCY_MS}" \
    --jitter-ms 5 \
    --dispatch-latency-ms 2 \
    --seed 4242 \
    >"${WORKDIR}/fake.log" 2>&1 &
FAKE_PID=$!

# Wait for it to answer rather than sleeping a fixed amount.
for _ in $(seq 1 100); do
    if ${PYTHON} - "${PORT}" <<'PY' 2>/dev/null; then break; fi
import sys, urllib.request
port = sys.argv[1]
urllib.request.urlopen(f"http://127.0.0.1:{port}/services/server/info?output_mode=json", timeout=1).read()
PY
    if ! kill -0 "${FAKE_PID}" 2>/dev/null; then
        cat "${WORKDIR}/fake.log" >&2
        fail "the fake splunkd exited during startup"
    fi
    sleep 0.1
done

SUMMARY="${WORKDIR}/summary.json"

echo "==> running the agent (${MODE})"
case "${MODE}" in
local)
    env \
        REG_STANDALONE=1 \
        REG_SCENARIO="${REPO_ROOT}/scenarios/smoke" \
        REG_TARGET_URL="http://127.0.0.1:${PORT}" \
        REG_TARGET_TOKEN=smoke-token \
        REG_TARGET_VERIFY_TLS=0 \
        REG_VUS="${SMOKE_VUS}" \
        REG_DURATION_S="${SMOKE_DURATION_S}" \
        REG_POLL_INITIAL_MS=25 \
        REG_POLL_MAX_MS=100 \
        REG_SUMMARY_PATH="${SUMMARY}" \
        REG_OUTPUT="${WORKDIR}/steps.ndjson" \
        PYTHONPATH="${REPO_ROOT}/worker" \
        ${PYTHON} -m regulator_agent >"${WORKDIR}/agent.out" 2>"${WORKDIR}/agent.log" \
        || fail "the agent exited with code $?"
    ;;
docker)
    [ -n "${IMAGE}" ] || fail "docker mode needs an image: tools/smoke.sh docker <image>"
    # Host networking so the container reaches the fake splunkd on loopback.
    # The scenario comes from the image's own built-in library, which is the
    # point: it proves the library was actually packaged.
    docker run --rm --network host \
        -e REG_STANDALONE=1 \
        -e REG_SCENARIO=smoke \
        -e REG_TARGET_URL="http://127.0.0.1:${PORT}" \
        -e REG_TARGET_TOKEN=smoke-token \
        -e REG_TARGET_VERIFY_TLS=0 \
        -e REG_VUS="${SMOKE_VUS}" \
        -e REG_DURATION_S="${SMOKE_DURATION_S}" \
        -e REG_POLL_INITIAL_MS=25 \
        -e REG_POLL_MAX_MS=100 \
        "${IMAGE}" >"${WORKDIR}/agent.out" 2>"${WORKDIR}/agent.log" \
        || fail "the container exited with code $?"
    # The container cannot write to our summary path, so take the summary from
    # the last line of stdout, which is where the agent always puts it.
    tail -1 "${WORKDIR}/agent.out" >"${SUMMARY}"
    ;;
*)
    fail "unknown mode ${MODE}, expected local or docker"
    ;;
esac

[ -s "${SUMMARY}" ] || fail "no run summary was produced"

echo "==> checking the result"
${PYTHON} - "${SUMMARY}" "${SMOKE_MIN_EXECUTIONS}" <<'PY' || fail "the summary did not pass its checks"
import json
import sys

summary = json.load(open(sys.argv[1]))
minimum = int(sys.argv[2])
problems = []

if summary.get("outcome") != "completed":
    problems.append(f"outcome is {summary.get('outcome')!r}, expected 'completed'")
if not summary.get("valid"):
    problems.append(f"the run was marked invalid: {summary.get('invalid_reason')}")

stats = summary.get("stats", {})
executions = stats.get("executions", 0)
if executions < minimum:
    problems.append(f"only {executions} executions, expected at least {minimum}")
if stats.get("errors"):
    problems.append(f"{stats['errors']} step(s) failed: {stats.get('errors_by_class')}")

latency = stats.get("latency", {})
if not latency.get("p95_ms"):
    problems.append("no latency was recorded, so nothing was actually measured")

# The server-side half must be there too. If it is missing, the agent timed
# the request but never read the job's own accounting, which is half the value
# of the tool and exactly the kind of regression a green unit suite can hide.
steps = {s["step_id"]: s for s in stats.get("steps", [])}
if not steps:
    problems.append("no per-step statistics were produced")
for step_id, step in steps.items():
    if not step.get("scan_count_total"):
        problems.append(f"step {step_id} recorded no scanCount from the job")

if problems:
    print("SMOKE CHECKS FAILED:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    sys.exit(1)

print(
    f"  executions={executions} "
    f"p50={latency.get('p50_ms')}ms p95={latency.get('p95_ms')}ms "
    f"errors={stats.get('errors', 0)} "
    f"steps={sorted(steps)}"
)
PY

echo "SMOKE PASSED"
