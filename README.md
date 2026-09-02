# Regulator

**Search load generation for Splunk.** Simulates concurrent users, measures what
the cluster gives back, and stores every run as a reproducible, comparable
artefact you can gate a pipeline on.

Regulator is the sibling of [Stoker](https://github.com/livehybrid/stoker).
Stoker fills a Splunk cluster with realistic data. Regulator drives realistic
demand against it. On a steam locomotive the stoker shovels coal into the
firebox and the driver opens the regulator to demand work from the boiler.

> **Phase 0.** The API engine, the scenario format and the standalone worker are
> here and tested. The control plane, the web UI and the headless-browser engine
> are on the roadmap below.

---

## Why this exists

There is no maintained tool for this. Splunk publishes a methodology (the .conf19
scale-testing talk describes 120 simulated users mixing dense and sparse
searches) but no tool. The community options are dormant: a JMeter test plan
with no recent activity, a one-commit audit-replay script, an empty repo. Nobody
has published a Locust, k6 or Gatling harness for Splunk, and nobody has
published a headless-browser approach at all.

Meanwhile the easy version of this tool produces confident nonsense, because
Splunk is unusually good at making a naive benchmark look fast:

| Trap | Why it ruins a benchmark | What Regulator does |
|---|---|---|
| **Dispatch result cache** | An identical search string over an identical time range returns a cached artefact, so a repeat run measures nothing | Every dispatch carries a per-iteration nonce inside a Splunk comment, changing the search string without changing the work |
| **OS page cache** | Buckets read once stay hot in RAM, so the second run looks dramatically faster | Time windows rotate across the corpus from a seeded sequence, so the working set moves between iterations |
| **Coordinated omission** | A generator that waits for response N before issuing N+1 never issues the requests that would have queued behind a stall, so the tail vanishes | Iterations are scheduled against wall clock and latency is measured from *intended* start. Both the corrected `latency_ms` and the raw `service_time_ms` are recorded |
| **Averaged percentiles** | Averaging p95 across workers is meaningless | Each worker keeps a mergeable histogram; percentiles are computed once over the merged whole |
| **The generator is the bottleneck** | One load box saturates long before a real indexer tier notices | Every record carries how late its schedule was. A run whose generator drifted is marked **invalid**, not merely slow |
| **Empty index** | A search against an index that is not there returns nothing in milliseconds and looks like a very fast cluster | Online lint parses every search against the target and checks the corpus exists, before the run starts |

The last two matter most. "The cluster is too slow" and "the load box was too
small" need opposite responses, so they never share an outcome or an exit code.

---

## Quick start

No control plane, no cluster, nothing to install beyond the dependencies. This
runs against the bundled fake splunkd, so it works with no Splunk at all:

```bash
pip install -r worker/requirements.txt
tools/smoke.sh local
```

Against a real Splunk:

```bash
export REG_STANDALONE=1
export REG_SCENARIO=scenarios/search-classes
export REG_TARGET_URL=https://splunk.example:8089
export REG_TARGET_TOKEN=eyJraWQiOiJzcGx1bmsuc2VjcmV0...
export REG_VUS=50
export REG_DURATION_S=600

PYTHONPATH=worker python -m regulator_agent | tee run.ndjson
```

Or from the image, where the scenario library ships inside it so a cold start
needs no volume and no git access:

```bash
docker run --rm \
  -e REG_STANDALONE=1 \
  -e REG_SCENARIO=search-classes \
  -e REG_TARGET_URL=https://splunk.example:8089 \
  -e REG_TARGET_TOKEN="$SPLUNK_TOKEN" \
  -e REG_VUS=50 -e REG_DURATION_S=600 \
  ghcr.io/livehybrid/regulator-worker:latest
```

Useful before committing to a long run:

```bash
python -m regulator_agent --target-report  # describe a cluster you have never tested
python -m regulator_agent --probe-only     # just the version, roles and ceiling
python -m regulator_agent --lint-only      # would this scenario actually run
```

### Pointing it at an unfamiliar cluster

`--target-report` needs only a URL and a credential, no scenario, and answers
the questions that decide whether a benchmark is worth running and how to read
it afterwards. JSON on stdout, a human summary on stderr:

```
target        https://splunk.example:8089 (session)
instance      Splunk 10.4.0 on Linux, 8 cores, 15896 MB
roles         indexer, license_master, deployment_server, kv_store
search peers  0
concurrency   max_hist_searches=14 (base 6 + 1/cpu x 8)
smartstore    6 index(es)
can dispatch  True
recommended   search-classes, but read it as a harness check rather than a sizing
              exercise: a single instance has no distributed search to measure

indexes with data:
  main                        819,494,531 events     80764 MB  event  smartstore
  cultivar_web                104,283,980 events     11429 MB  event

notes:
  - no distributed search peers: this is a single instance, so a run here exercises
    nothing of bundle replication, the map phase across peers, or the search-head
    concurrency split
  - 6 index(es) use SmartStore: a rare search over a wide window may be measuring a
    cache miss and an object-storage fetch rather than the search tier
```

Every probe it makes is optional. A permission the account lacks, or an
endpoint the node does not have, becomes a note rather than a failure, so the
report is useful with whatever access you were given and honest about the rest.

### Exit codes

The CI contract. Codes 2 and 3 are deliberately different.

| Code | Meaning |
|---|---|
| 0 | The run completed and the result is valid |
| 1 | The run failed outright: bad configuration, unreachable target, credentials rejected |
| 2 | A guard rail stopped it: the target breached an error-rate or latency ceiling. A real result, not a tooling failure |
| 3 | The result is **invalid**: the generator could not keep to its own schedule, so the numbers describe this worker rather than Splunk |
| 4 | The scenario failed lint and never started |

---

## Configuration

Everything is an environment variable. That is what makes an ephemeral
deployment disposable and a CI job a single `docker run`.

### Target

| Variable | Default | Notes |
|---|---|---|
| `REG_TARGET_URL` | required | splunkd management URI, normally port 8089 |
| `REG_TARGET_TOKEN` | | Bearer token. **Preferred**: no login round trip per request, and no session expiry mid-run |
| `REG_TARGET_USERNAME` / `REG_TARGET_PASSWORD` | | Session-key auth instead. Re-logins once on a 401, guarded so 500 concurrent users produce one login |
| `REG_TARGET_WEB_URL` | | Splunk Web, port 8000. Browser engine only (Phase 2) |
| `REG_TARGET_VERIFY_TLS` | `1` | |
| `REG_TARGET_APP` / `REG_TARGET_OWNER` | `search` / `nobody` | Dispatch namespace. A search in the wrong app sees different knowledge objects, so this is part of the test definition |
| `REG_TARGET_API_VERSION` | `v2` | `v1` for the deprecated endpoint on an old stack |

### Load

| Variable | Default | Notes |
|---|---|---|
| `REG_VUS` | from the scenario | Closed model: fixed virtual users |
| `REG_ARRIVAL_RATE_PER_MIN` | | Open model: fixed arrival rate. Mutually exclusive with `REG_VUS` |
| `REG_PACING_S` | from the scenario | Gives each virtual user a timetable, which is what enables coordinated-omission correction in the closed model |
| `REG_DURATION_S` | from the scenario | |
| `REG_SEED` | from the scenario | Overriding it changes the workload, so two runs with different seeds are not comparable |
| `REG_MAX_IN_FLIGHT` | `512` | Open model shedding ceiling. Hitting it invalidates the run |

### Telemetry

| Variable | Default | Notes |
|---|---|---|
| `REG_HEC_URL` / `REG_HEC_TOKEN` | off | Ship results to Splunk. Both or neither, and setting only one is a hard boot error rather than silently shipping nothing |
| `REG_HEC_VERIFY_TLS` | `1` | Set `0` for a self-signed collector, which is the normal case for an on-premises indexer or an in-cluster service name. **Worth knowing:** this module swallows its own errors by design, so without this flag telemetry against a self-signed endpoint disappears into TLS failures the run never mentions |
| `REG_HEC_INDEX` | the token's default | Omitted from the envelope when unset, because sending an explicit null is a 400 |
| `REG_HEC_SOURCE` | `regulator` | |
| `REG_HEC_SOURCETYPE_STEP` / `_RUN` | `regulator:step` / `regulator:run` | |
| `REG_HEC_GZIP` | `1` | |
| `REG_HEC_BATCH_BYTES` / `REG_HEC_BATCH_MS` | `524288` / `200` | Flush thresholds. Batching keeps the telemetry round trip off the hot path entirely, so it never lands in the latency being measured |
| `REG_OUTPUT` | stdout | NDJSON step records |
| `REG_SUMMARY_PATH` | | Where to write the run summary as JSON |

Telemetry is **off by default and separately addressed** on purpose: writing
results into the cluster you are measuring adds load to it. When the telemetry
host matches the target host, the run record carries `self_instrumented: true`
so nobody later compares it against a clean run without noticing.

### Client

| Variable | Default | Notes |
|---|---|---|
| `REG_POLL_INITIAL_MS` / `REG_POLL_MAX_MS` | `250` / `1000` | Adaptive job polling. Polling hard is itself load on the target: a thousand users at 100 ms is ten thousand REST calls a second |
| `REG_DELETE_JOBS` | `1` | Clean up artefacts on the search head. Leaving thousands behind is itself a load characteristic |
| `REG_CACHE_BUST` | `1` | The nonce comment. Only turn it off to demonstrate what the cache is worth |
| `REG_HTTP2`, `REG_CONNECT_TIMEOUT_S`, `REG_READ_TIMEOUT_S` | `0`, `10`, `300` | |
| `REG_LINT_STRICT` | `1` | `0` downgrades blocking lint to warnings |

---

## Scenarios

A scenario is the payload: a directory with a `scenario.yaml`, git-syncable,
linted before it runs. Three ship in the box.

| Scenario | What it is for |
|---|---|
| `smoke` | Two trivial searches needing no indexed data. Proves the path works, measures nothing |
| `search-classes` | One search per class (dense, sparse, rare, accelerated, heavy, subsearch) against Stoker-generated data, so a regression can be attributed to a component |
| `soc-analyst-morning` | Persona-weighted SOC triage: hunters, dashboard watchers and a reporter, at realistic think times |

```yaml
name: soc-analyst-morning
engine: api
seed: 90210                   # no seed means no reproducibility, and lint says so

corpus:
  requires_packs: [aws-cloudtrail, splunk-tutorial-secure]   # which Stoker packs
  index: main

parameters:
  src_ip:                     # drawn from the data itself at run start
    type: choice_from_search
    spl: 'search index=main sourcetype=aws:cloudtrail | stats count by sourceIPAddress | head 200'
    field: sourceIPAddress

time_policy:
  mode: rolling
  window: 24h
  jitter: 45m                 # moves the working set so the page cache is not the thing measured

personas:
  - name: hunter
    weight: 20
    think_time: {dist: lognormal, median_s: 25, sigma: 0.7, max_s: 180}
    steps:
      - id: hunt-rare-event
        type: search
        class: rare
        spl: 'search index=main sourcetype=aws:cloudtrail sourceIPAddress="{{src_ip}}" | stats count by eventName'

load:
  model: closed
  virtual_users: 50
  pacing_s: 90
  ramp: [{to: 10, over_s: 60}, {to: 50, over_s: 300}, {hold_s: 900}]

abort_if:
  error_rate_pct: 10
  p95_ms: 90000
  generator_drift_ms: 3000
```

### Search classes

They stress completely different parts of the stack, and a benchmark that runs
only one tells you about one third of your cluster.

- **dense**: most events match. Aggregation and pipeline bound.
- **sparse**: a minority match. Filter bound.
- **rare**: a needle in a haystack. I/O and bloom-filter bound, and the best
  detector of a SmartStore cache miss, because a cold bucket has to come back
  from object storage before the filter can even run.
- **accelerated**: `tstats` and `mstats`. Should be near-instant, so a
  regression is loud.
- **heavy**: lookups, transactions, joins. Search-head memory and CPU.
- **subsearch**: the classic killer, and the most reliable way to expose a
  search head that is fine at low concurrency and falls over at high.

### Virtual users are not concurrent searches

A real analyst is idle most of the time, so:

```
observed_concurrent_searches ≈ virtual_users × (mean_search_duration / mean_iteration_period)
```

Regulator reads `base_max_searches` and `max_searches_per_cpu` from the target
at startup, computes `max_hist_searches = base_max_searches + (max_searches_per_cpu × cores)`,
and reports it alongside what your load actually produced. The run summary gives
you both numbers, because the gap between them is why "how many users can we
support" is harder than it sounds.

---

## What gets measured

Per step execution, both halves:

**Client side.** `latency_ms` (coordinated-omission corrected), `service_time_ms`
(raw), `late_by_ms`, `dispatch_ms` (POST to sid, which dominates short searches
on a wide cluster), `ttfr_ms` (time to first result, which is what a user
perceives), `queued_ms` (detects crossing the concurrency ceiling),
`results_fetch_ms`, `result_bytes`, `poll_count`.

**Server side, from the job's own REST record.** `run_duration_s`, `scan_count`,
`event_count`, `result_count`, `dispatch_state`, `is_finalized`, and the derived
`events_per_s = scan_count / run_duration_s`.

Plus the dimensions that make it sliceable: `sid`, `spl_hash` (stable across
cache-busting, so the same logical search aggregates), `step_class`, `persona`,
`vu_id`, `iteration`, the resolved parameter values, and the exact time window.

Records go to stdout as NDJSON, one object per line, and optionally to Splunk
over HEC as `regulator:step` and `regulator:run`.

---

## Architecture

```
scenario.yaml ──► scheduler ──► engine ──► splunkd REST ──► Splunk
                     │             │
              virtual users   dispatch, poll,
              ramps, pacing,  read job stats
              guard rails
                     │
                     ▼
            step records ──► NDJSON, HEC, merged histograms
```

The scheduler owns *when* work happens, the engine owns *how* one step runs.
That line is what lets the browser engine arrive without the scheduler learning
anything about browsers, and what lets the scheduler be tested against a fake
engine with no Splunk anywhere.

| Module | Job |
|---|---|
| `config.py` | Environment parsed once into a frozen dataclass. Secrets `repr=False`. A malformed value is a hard boot error, never a silent default |
| `scenario.py` | The scenario format, plus offline lint. Advisory findings are prefixed so they warn rather than block |
| `params.py` | Deterministic generation. Every draw derives its own seed from (scenario seed, virtual user, iteration, step, parameter), so a draw never depends on execution order |
| `timepolicy.py` | Rolling and pinned time windows, aligned and jittered |
| `scheduler.py` | Virtual users, ramps, pacing, the corrected-latency arithmetic, guard rails, drain |
| `engines/api.py` | Dispatch, adaptive polling, job stats, results, cleanup |
| `splunk.py` | Async splunkd client: bearer or session auth, connection pooling, typed errors |
| `histogram.py` | Mergeable log-linear latency sketch, ~1.6% worst-case relative error, exact count, sum, min and max |
| `results.py` | The step record, emitters, the live aggregate |
| `hec.py` | Best-effort batched telemetry that can never take down the run |

---

## Development

```bash
make test          # worker and tools unit suites
make smoke         # end to end against the fake splunkd
make docker-build
make docker-smoke  # the packaged image against the fake splunkd
```

`tools/fake_splunk.py` is a standard-library fake splunkd that emulates auth,
`/services/search/v2/jobs` dispatch and polling, results, preview, cancel, the
SPL parser, index existence and the limits endpoint. It simulates admission
queueing past a concurrency ceiling and concurrency-dependent slowdown, so the
tests can assert Regulator actually notices degradation. Run it standalone:

```bash
python tools/fake_splunk.py --port 8089 --max-concurrent 6 --per-concurrent-penalty-ms 50
```

### CI

Superseded runs are cancelled to save Actions minutes, but a publish is never
interrupted: test jobs are grouped per ref with `cancel-in-progress: true`, and
build and sign jobs are grouped per SHA and never cancelled. Because `build`
needs `test`, a cancelled test means the stale build never starts at all.

---

## Roadmap

| Phase | Deliverable |
|---|---|
| **0** | **API engine, scenario format, standalone worker, fake splunkd, CI** (done, and validated against a real Splunk 10.4.0) |
| 1 | Control plane: FastAPI, Postgres, React UI with live charts, worker fleet over Docker Swarm, merged histograms across the fleet |
| 2 | Browser engine: Playwright, persistent contexts per virtual user, Navigation Timing and LCP, XHR-to-sid correlation so a browser step joins its own server-side job stats |
| 3 | CI/CD: baselines, run comparison, regression gates, a GitHub Action |
| 4 | Kubernetes and Splunk Operator: Indexed Jobs, dedicated node groups so the generator never shares a node with the system under test, and correlation against `_audit`, `_introspection` and the scheduler's skipped-search counts |

---

## Licence

Apache 2.0. See `LICENSE`.
