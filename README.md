# Regulator

**Search load generation for Splunk.** Simulates concurrent users, measures what
the cluster gives back, and stores every run as a reproducible, comparable
artefact you can gate a pipeline on.

Regulator is the sibling of [Stoker](https://github.com/livehybrid/stoker).
Stoker fills a Splunk cluster with realistic data. Regulator drives realistic
demand against it. On a steam locomotive the stoker shovels coal into the
firebox and the driver opens the regulator to demand work from the boiler.

> **Phases 0 to 4.** The API engine, the scenario format, the standalone worker,
> the control plane, the web UI, the headless-browser engine, the CI regression
> gate and server-side correlation are here and tested. The distributed worker
> fleet is the remaining piece.

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

## What the cluster thought was happening

Everything above measures from the outside. After each run Regulator asks the
cluster its own opinion, because the difference between "p95 went from 4 s to
11 s" and "p95 went from 4 s to 11 s **because** indexer CPU hit 95% and 340
scheduled searches were skipped" is the difference between a load test and a
benchmark.

Six questions, each one search against Splunk's own internal indexes:

| Probe | What it answers |
|---|---|
| `_audit`, this run | The cluster's own account of the searches we dispatched, matched by the marker each one carries |
| `_audit`, everything | All searches in the window, so other traffic on a shared cluster is visible rather than silently mixed in |
| `search_telemetry` | Where the time went by phase. Indexer-side elapsed time separates a busy search head from busy indexers, which no client-side timing can |
| Scheduler | Scheduled searches skipped or deferred. Past the concurrency ceiling the first casualty is usually somebody else's scheduled work, a cost that is otherwise invisible |
| Cache manager | SmartStore downloads and evictions over the wire, corroborating the bucket-level provenance |
| Resource usage | Search head and indexer CPU, so the latency curve can be laid against the machine's own load |

Two honest caveats, both established against a live 10.4 instance rather than
assumed. **The count of this run's own searches is a floor, not a total**: audit
records keep arriving for a minute or two after a run and correlation happens
immediately, so reason with the aggregate probes. And **these searches are
themselves load**: they run on the cluster under test, after the run rather than
during it, and the run record says so rather than pretending otherwise.

Everything degrades. A load-test account frequently cannot read `_audit` or
`_introspection`, and a benchmark that refused to report because it could not
also ask the cluster's opinion would be worse than useless. Each probe that
fails becomes a note, and the run stands on its client-side measurement.

## Running it in the cluster it measures

`infra/k8s/` deploys the control plane onto a **dedicated node group**, and the
placement is the point rather than a detail. A load generator sharing nodes with
the system under test competes with it for CPU, memory and network, so the
benchmark measures the contention it created. Every pod is pinned to
`workload=regulator` and tolerates a matching taint, so nothing else lands there
and it lands nowhere else.

```bash
eksctl create nodegroup --cluster <name> --name regulator \
  --node-labels workload=regulator --node-type m6i.2xlarge --nodes 1 \
  --taints workload=regulator:NoSchedule

kubectl apply -f infra/k8s/regulator.yaml
```

Note the deployment sets **memory limits but no CPU limit**. A throttled load
generator cannot keep to its own schedule, and the result is a run marked
invalid rather than a slow one. Requests reserve the capacity; a limit would cap
it at exactly the wrong moment.

## Gating a pipeline on it

A single run is a number. Two runs are a comparison, which is the point: did
this release make search slower, does adding two indexers help, is this cluster
shape faster than that one.

```bash
python -m regulator_agent \
  --compare run.json --baseline main-green.json \
  --gate "p95 <= baseline + 15%" \
  --gate "error_rate <= 2%" \
  --gate "queued == 0"
```

```
FAIL: 1/2 gates met
  FAIL p95 <= baseline + 15%: 1420.0 versus a baseline of 1000.0 (+42.0%, worse), limit 1150.0
  ok   error_rate <= 2%: 0.0 against a limit of 2.0

  step                           baseline  candidate      delta
  rare-bucket-policy                2000ms      4100ms    +105.0%  (scanned more)
  dense-web-status                   800ms       820ms      +2.5%
```

Comparison is arithmetic over two JSON documents, so it needs no target and no
network. The gate language is deliberately small, because a gate nobody can read
aloud in a review is a gate that will eventually be misread:

| Gate | Meaning |
|---|---|
| `p95 <= baseline + 15%` | The common one: no more than 15% slower |
| `p95 <= 5000ms` | An absolute ceiling, no baseline needed |
| `throughput >= baseline - 10%` | Where a bigger number is better, worse means down |
| `error_rate <= 2%` | |
| `queued == 0` | Nothing waited at the concurrent-search ceiling |
| `valid` | The run measured Splunk rather than itself |
| `p95[rare-bucket-policy] <= baseline + 25%` | One step, so a slow rare search is not hidden by a fast average |

### Three things it refuses to do

Each of these would let it lie, and somebody would merge on the strength of it.

- **It will not compare an invalid run.** A run whose generator could not keep
  to its own schedule measured the load box, not Splunk. The comparison is
  blocked with that as the reason rather than producing a confident percentage.
- **It will not silently compare a warm run to a cold one.** On SmartStore those
  are different work, sometimes by an order of magnitude. The comparison still
  runs, because sometimes that is exactly what you are measuring, but the
  mismatch is reported as a warning.
- **It will not quietly compare different workloads.** A different scenario,
  seed, concurrency, target or coordinated-omission setting is a different
  question, and each is called out.

The per-step table separates the two ways a benchmark gets slower:
`scanned more` means the candidate did more work, so the comparison was never
valid. Latency up with scan count flat is contention or queueing, which is the
finding you were looking for.

### The exit contract

| Code | Meaning |
|---|---|
| 0 | Every gate met, or report-only |
| 1 | Something broke: unreachable control plane, a scenario that does not exist |
| 2 | A gate was breached. A real result, and the thing the pull request should argue about |
| 3 | The run was invalid, so it measured the generator. A tooling problem, not a regression |

Codes 2 and 3 are separate so a pipeline never sends somebody hunting for a
slowdown that never happened.

### GitHub Action

```yaml
- uses: livehybrid/regulator/.github/actions/benchmark@main
  with:
    server: https://regulator.example
    token: ${{ secrets.REGULATOR_TOKEN }}
    target: 1
    scenario: search-classes
    virtual-users: 200
    baseline: main-green
    gates: |
      p95 <= baseline + 15%
      error_rate <= 2%
      valid
    # Only a green main becomes the new baseline.
    promote-baseline: ${{ github.ref == 'refs/heads/main' && 'main-green' || '' }}
```

It launches the run, streams progress into the job log, writes the comparison
into the job summary where a reviewer will actually see it, and annotates each
breached gate. The first run of a new benchmark has nothing to compare against,
so it reports rather than failing: a check that goes red the day you add it is a
check everybody disables.

## The web interface

```bash
pip install -r worker/requirements.txt -r server/requirements.txt
REG_ADMIN_PASSWORD=choose-one PYTHONPATH=worker:server \
  python -m uvicorn regulator_server.app:app --port 8080
```

Or from the image, with a volume so targets and runs survive a replacement:

```bash
docker run --rm -p 8080:8080 -v regulator-data:/data \
  -e REG_ADMIN_PASSWORD=choose-one \
  -e REG_MASTER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  ghcr.io/livehybrid/regulator:latest
```

Add a target, and the buttons do the rest: **Test** probes it, **Report**
describes the whole cluster, **Cache** shows what SmartStore holds locally,
**Evict** drops it, and **Run** launches a scenario with live latency,
throughput and queueing while it goes.

The run detail is the view that earns its place. Three things decide whether a
result means anything, and each gets a banner that cannot be missed: a run
marked **invalid** because the generator could not keep its own schedule, a run
where **queueing** was observed (amber, and worded as the finding it is rather
than an error), and the **cache provenance** when part of what was measured was
object storage rather than the search tier.

The control plane runs scenarios **in its own process**. That is the same idea
as Stoker's in-process driver: the tool works with no container orchestration at
all, and it is honest about the limit, because past a few hundred virtual users
the server becomes the constraint. There is a configurable ceiling that refuses
the run and explains why, and the generator-drift guard catches anything that
slips past it.

| Variable | Default | Notes |
|---|---|---|
| `REG_ADMIN_PASSWORD` | unset | Unset means **no authentication at all**, and the server says so on every start and in `/api/auth/status`. This thing can evict a production cache |
| `REG_MASTER_KEY` / `REG_MASTER_KEY_FILE` | generated | Encrypts target credentials at rest. Unset means a throwaway key, so everything stored becomes unreadable at the next restart |
| `REG_DATABASE_URL` | `sqlite:///./regulator.db` | |
| `REG_MAX_VIRTUAL_USERS` | `500` | The in-process ceiling described above |
| `REG_MAX_CONCURRENT_RUNS` | `2` | More at once would mean measuring contention between your own tests |

**The UI is one self-contained HTML file with no build step.** No npm, no
node_modules in the image, no build stage in CI, nothing to go stale, and an
operator can read its source. The control-plane image has no Node in it at all.
If the UI ever needs real charting libraries, that is the point to reconsider.

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

### SmartStore

On a SmartStore indexer the local disk is a cache in front of object storage,
which means the same search over the same data is two different pieces of work
depending on what is already local. Warm, it reads local disk and you are
measuring the search tier. Cold, it downloads buckets first and you are largely
measuring object storage and the network.

Both are worth having. Not knowing which one you got is not. **Every run
records the cache state before and after**, so the summary carries a
`provenance` of `warm`, `cold`, `mixed` or `unknown`, along with how many
buckets and bytes the run pulled down.

There are two ways to force the cold path, because they answer different
questions.

**Evict before a run.** `REG_EVICT_CACHE=1` drops the cache, then runs. Use it
when the question is "how does this workload behave against unlocalised data".

```bash
REG_EVICT_CACHE=1 REG_EVICT_CACHE_INDEXES=main python -m regulator_agent
```

**Evict now, then do whatever you like.** `--evict-cache` drops the cache and
exits, so you can run a search by hand, watch the indexer, or start a run from
somewhere else afterwards. Use it when you are investigating rather than
benchmarking.

```bash
python -m regulator_agent --evict-cache --index main --index cultivar_web
python -m regulator_agent --evict-cache --all-indexes    # explicit on purpose
```

It reports the cache before and after, and how many buckets refused eviction
(a bucket with a live reader cannot be evicted, which is correct behaviour
rather than a fault). Without `--index` or `--all-indexes` it refuses: there is
no undo beyond waiting for everything to re-download, and on a shared cluster
most of that cache belongs to other people's dashboards.

| Variable | Default | Notes |
|---|---|---|
| `REG_EVICT_CACHE` | `0` | Evict before the run, so it measures the cold path. Opt-in and never a default |
| `REG_EVICT_CACHE_INDEXES` | the scenario's corpus index | Comma-separated. Also supplies the default index list for `--evict-cache` |

**One caveat worth knowing when reading a cold run.** Splunk re-localises as
the run proceeds, so the first searches read cold and later ones increasingly
do not. The provenance comes back as `mixed` rather than `cold`, and the
`buckets_downloaded` count is what tells you how much of the run actually paid
for the network. For a *sustained* cold measurement, point the scenario at a
time range outside the cache's hotlist window instead: every iteration then
reads cold, repeatably, without evicting anything or disturbing anyone else.

The target report also gives you the picture before you start:

```
cache   720/1386 buckets local (52%), 78.8 GB of 80.5 GB, 98% full, policy lru
```

That 98% is a finding in its own right. A cache at its ceiling evicts buckets
other searches are using, so the run measures churn as much as search, and the
report says so.

### The browser engine

The API engine answers the search-tier question. The browser engine answers a
different one that REST cannot: when an analyst opens a dashboard, how long
until they see data. That includes the JavaScript bundle, the panel layout and
the browser's own rendering, and it is the number a user would quote at you.

```bash
docker run --rm \
  -e REG_STANDALONE=1 \
  -e REG_SCENARIO=dashboard-triage \
  -e REG_TARGET_URL=https://splunk.example:8089 \
  -e REG_TARGET_WEB_URL=https://splunk.example:8000 \
  -e REG_TARGET_USERNAME=loadtest -e REG_TARGET_PASSWORD="$PW" \
  -e REG_VUS=10 -e REG_DURATION_S=600 \
  ghcr.io/livehybrid/regulator-worker:browser
```

Four things it does deliberately:

- **One persistent browser context per virtual user**, reused across
  iterations. A fresh context re-downloads the whole Splunk Web bundle every
  time, which inflates page timings against any real returning user. The first
  iteration is still recorded as `first_visit`, because a cold bundle load is a
  real event, just not the common one.
- **Login once per context.** A real user logs in once a day, not once a
  dashboard.
- **Every search the page fires is captured with its sid**, so a browser step
  joins back to exactly the same server-side job statistics the API engine
  reads. Without that the two channels produce unrelated numbers.
- **The time range is pinned** to the step's window, so the browser asks the
  same question of the same data as the API engine rather than whatever the
  dashboard was saved with.

Two constraints worth knowing before planning a big test. A Chromium context is
roughly 150 to 300 MB, so a browser cohort is measured in tens while an API
cohort is measured in hundreds; the realistic shape is both at once, so the
dashboards are opened while the cluster is genuinely busy. And **Splunk Web
needs a native account**: a bearer token authenticates the REST API but not a
web session, and SAML cannot be driven headlessly at all, because the password
never reaches Splunk in an assertion.

### Concurrency and queueing

Queueing is a **measurement, not a failure**. When load crosses the target's
concurrent-search ceiling, splunkd holds searches in `QUEUED` before running
them, and that moment is exactly what a capacity test is looking for. Regulator
records `queued_ms` per search, counts how many queued, and reports it
prominently at the end of the run:

```
QUEUEING OBSERVED: 340 of 1200 searches (28.3%) waited in QUEUED before running,
p95 4100ms. The target was at its concurrent-search ceiling.
```

The run carries on. If you want a run to stop when latency degrades, that is
what `abort_if.p95_ms` is for, and for a deliberate saturation test you should
set it high or leave it out.

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
| **1** | **Control plane and web interface** (done): targets, the report, cache inspection and eviction, scenario launch, and a live run detail with an inline latency chart. Scenarios execute in the control plane's own process |
| 1b | Worker fleet over Docker Swarm, Postgres, merged histograms across the fleet, which lifts the in-process virtual-user ceiling |
| **2** | **Browser engine** (done): Playwright, a persistent context per virtual user, Navigation Timing and LCP, and every search the page fires captured from the wire and joined back to its own server-side job statistics |
| **3** | **CI and regression gates** (done): named baselines, run comparison with per-step deltas, a small gate language, and a GitHub Action |
| **4** | **Server-side correlation and Kubernetes** (done): every run pulls back the cluster's own account of it from `_audit`, `_introspection`, the scheduler and the cache manager, and manifests deploy it onto a dedicated node group so the generator never shares a node with the system under test |
| 5 | Distributed worker fleet: Indexed Jobs and Swarm services driven by a claim protocol, which lifts the in-process virtual-user ceiling |

---

## Licence

Apache 2.0. See `LICENSE`.
