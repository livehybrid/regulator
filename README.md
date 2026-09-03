# Regulator

**Search load generation for Splunk.** Simulates concurrent users, measures what
the cluster gives back, and stores every run as a reproducible, comparable
artefact you can gate a pipeline on.

Regulator is the sibling of [Stoker](https://github.com/livehybrid/stoker).
Stoker fills a Splunk cluster with realistic data. Regulator drives realistic
demand against it. On a steam locomotive the stoker shovels coal into the
firebox and the driver opens the regulator to demand work from the boiler.

> **Phases 0 to 7.** The API engine, the scenario format, the standalone worker,
> the control plane, the web UI, the headless-browser engine, the CI regression
> gate, server-side correlation, Splunk's own `savedsearches.conf` as a scenario
> source, a scenario for every Stoker pack, two adversarial review rounds
> folded back in, and a worker fleet on Docker Swarm and Kubernetes that lifts
> the in-process ceiling and lets one run mix API and browser cohorts.

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

cp infra/k8s/secret.example.yaml infra/k8s/secret.yaml   # git-ignored
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
$EDITOR infra/k8s/secret.yaml                             # the key, the password, CI tokens
kubectl apply -f infra/k8s/secret.yaml -f infra/k8s/regulator.yaml
```

The Secret is a separate, ignored file on purpose: a placeholder key is a fatal
boot error rather than a quiet default, and a real one must never be one
careless `git add` away from a public repository.

## Running it on Docker Swarm through Portainer

The homelab path, and the same shape as Stoker's:

```bash
cd infra/stacks/regulator
cp .env.example .env        # PORTAINER_HOST, PORTAINER_TOKEN, REG_ADMIN_PASSWORD, HEC, a seed target
python deploy.py --dry-run
python deploy.py            # creates or updates the stack, generates and keeps the master key
```

One service, one named volume carrying the database, the imported scenarios and
the run history, pinned to the node you name. Every run launched from the web
interface ships its telemetry to the HEC destination in `.env`, and the target
named there is registered at boot so a rebuilt stack is runnable at once.

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
network. With no gates it is report only, and says so: "0 of 0 gates met" used
to read as a pass, which is how a pipeline that lost its gate arguments went
green on a regression. The gate language is deliberately small, because a gate
nobody can read aloud in a review is a gate that will eventually be misread:

| Gate | Meaning |
|---|---|
| `p95 <= baseline + 15%` | The common one: no more than 15% slower |
| `p95 <= baseline + 200ms` | An absolute allowance over the baseline. A relative threshold must name its unit: `baseline + 200` is refused, because it used to mean 200 percent while a bare `200` meant 200 milliseconds |
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
- **It will not quietly compare different workloads.** A different scenario
  file (by content digest, not name), seed, load model, configured load,
  target or coordinated-omission setting is a different question, and each is
  called out. A step with fewer than 20 successful samples on either side is
  flagged, because its p95 is a coin toss.

The per-step table separates the two ways a benchmark gets slower:
`scanned more` means each of the candidate's searches did more work, so the
comparison was never valid. Latency up with scans per search flat is contention
or queueing, which is the finding you were looking for. Per search, not per
run: a faster cluster fits more iterations in and scans more in total, which is
not the same thing.

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
    token: ${{ secrets.REGULATOR_TOKEN }}     # one of the control plane's REG_API_TOKENS
    # or: password: ${{ secrets.REGULATOR_PASSWORD }}, exchanged for a session
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
describes the whole cluster and says which scenarios its data fits, **Cache**
shows what SmartStore holds locally on every indexer, **Evict** drops it,
**Saved searches** lists what the target already runs and turns it into a
scenario, and **Run** launches a scenario with live latency, throughput and
queueing while it goes. A finished run can be made a **baseline** and any run
**compared** against one, with the per-step table and the gates on the page.
The **Audit** view records who launched, stopped, evicted or imported what,
with the caller's address.

The run detail is the view that earns its place. Three things decide whether a
result means anything, and each gets a banner that cannot be missed: a run
marked **invalid** because the generator could not keep its own schedule, a run
where **queueing** was observed (amber, and worded as the finding it is rather
than an error), and the **cache provenance** when part of what was measured was
object storage rather than the search tier.

The control plane can run a scenario **in its own process**, which works with
no container orchestration at all and is honest about its limit: past a few
hundred virtual users the server becomes the constraint, a configurable ceiling
refuses the run and explains why, and the generator-drift guard catches anything
that slips past it. Past that, a **fleet** generates the load.

## The worker fleet

A fleet run launches worker containers, on Docker Swarm through Portainer or on
Kubernetes as an Indexed Job, and this control plane owns their identity. The
protocol is Stoker's, where it has been through real multi-node runs:

1. **Claim.** Each worker presents the run's own bearer token and takes the
   lowest free slot (or the one Kubernetes hinted through its completion
   index). The claim response carries everything a standalone worker would
   have read from its environment: the target and its credential, the
   scenario's files, this slot's share of the load, where telemetry goes.
   Nothing sensitive is projected into a container's environment.
2. **Ready.** The worker starts its engine, validates the scenario against the
   target and resolves parameters. When every slot is ready the control plane
   sets **one shared T0**, so the fleet starts together despite unsynchronised
   container starts. A straggler past the provisioning timeout is declared
   lost and the run proceeds without it, marked as not the configured load.
3. **Heartbeat.** Every couple of seconds, carrying the live aggregate for the
   run page. The response is the command channel: `release` with T0, `stop`,
   or `superseded` when the lease has been reissued to a replacement. An
   acknowledged heartbeat is the lease renewal; a lease that stops being
   renewed is lost and its share is not waited for.
4. **Final.** The run summary with the **raw histograms**. The control plane
   merges buckets and computes the fleet's percentiles once, over the whole,
   because averaging p95 across workers is meaningless. Cache provenance and
   the cluster's own account of the run are then taken by the control plane,
   which holds the target credential, exactly as for an in-process run.

Shares are dealt out by the load model: virtual users as evenly as the worker
count allows (with each worker's ramp scaled to its share), an arrival rate
divided, and the schedule model's cron steps dealt round the workers by slot
so each fires exactly once. A mixed scenario becomes two groups on two images,
the API cohort and the browser cohort, with the users split by persona weight.
Slot-disjoint virtual-user ids keep every record attributable.

Workers reach the control plane by **service name**: on Swarm they join the
stack's own overlay network, on Kubernetes they use the Service. No LAN address
is needed on either side; `REG_PUBLIC_BASE_URL` overrides it when workers live
elsewhere. The Swarm driver pins the worker image to its registry digest before
launching, because swarm does not re-pull a floating tag a node already holds.

| Variable | Default | Notes |
|---|---|---|
| `REG_DEFAULT_FLEET` | `inprocess` | `swarm` or `k8s`. The launch dialog offers whichever fleets are configured |
| `REG_VUS_PER_WORKER` / `REG_BROWSER_CONTEXTS_PER_WORKER` | `200` / `10` | Fleet sizing; an explicit worker count on the run overrides it |
| `REG_WORKER_IMAGE` / `REG_BROWSER_WORKER_IMAGE` | the ghcr images | |
| `REG_PUBLIC_BASE_URL` | the service name | Where workers reach this control plane |
| `PORTAINER_HOST` / `PORTAINER_TOKEN` / `PORTAINER_ENDPOINT` | | The Swarm fleet. The same key the stack is deployed with |
| `REG_SWARM_NETWORK` / `REG_SWARM_CONSTRAINTS` | the stack's network / none | Keep workers off the node that runs the Splunk under test: `node.hostname != macdev` |
| `REG_K8S_NAMESPACE` / `REG_K8S_NODE_SELECTOR` / `REG_K8S_IN_CLUSTER` / `REG_KUBECONFIG` | `regulator` / none / auto / auto | The Kubernetes fleet. In-cluster uses the pod's service account and the Role in `infra/k8s/regulator.yaml` |
| `REG_HEARTBEAT_S` / `REG_LEASE_S` / `REG_PROVISION_TIMEOUT_S` | `2` / `20` / `180` | The protocol's clocks |

A fleet member is the same image as a standalone worker. Given `REG_RUN_ID`,
`REG_CONTROL_URL` and `REG_RUN_JWT` it claims instead of reading its target from
the environment; everything downstream of the claim is the standalone code.

| Variable | Default | Notes |
|---|---|---|
| `REG_ADMIN_PASSWORD` | unset | The operator password. Unset means **no authentication at all**, which the server refuses unless `REG_ALLOW_UNAUTHENTICATED=1` says a laptop on a network you control really is what this is. This thing can evict a production cache |
| `REG_API_TOKENS` | unset | Comma-separated bearer tokens for pipelines and scripts, 16 characters or more each. As powerful as the password |
| `REG_MASTER_KEY` / `REG_MASTER_KEY_FILE` | generated | Encrypts target credentials at rest. Unset means a throwaway key, so everything stored becomes unreadable at the next restart. An empty key file is a fatal boot error rather than a silent throwaway |
| `REG_DATABASE_URL` | `sqlite:///./regulator.db` | |
| `REG_USER_SCENARIOS_DIR` | next to the database | Where scenarios created through the web interface or the API are written. Separate from the built-in library, so an image upgrade never overwrites yours |
| `REG_HEC_URL` / `REG_HEC_TOKEN` / `REG_HEC_INDEX` / `REG_HEC_VERIFY_TLS` | off | Where every run launched here ships its telemetry, exactly as the worker's variables of the same name |
| `REG_SEED_TARGET_URL` and friends | unset | A target registered or updated at boot (`_NAME`, `_WEB_URL`, `_TOKEN` or `_USERNAME` and `_PASSWORD`, `_VERIFY_TLS`). What makes a nightly-rebuilt deployment runnable with no web step |
| `REG_MAX_VIRTUAL_USERS` | `500` | The in-process ceiling described above. The open model is held to the same ceiling of in-flight searches |
| `REG_MAX_CONCURRENT_RUNS` | `2` | More at once would mean measuring contention between your own tests |
| `REG_MAX_RUN_DURATION_S` | `14400` | The longest run accepted, so a mistyped duration cannot hold a slot for ever |
| `REG_SESSION_TTL_S` | `43200` | Sessions are bound to the password as well as the key: changing the password signs everyone out |

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
| `REG_SEED` | from the scenario | Reseeds every draw (parameters, persona assignment, think time, jitter) and is recorded as `effective_seed`. Overriding it changes the workload, so two runs with different seeds are not comparable and the comparison says so |
| `REG_MAX_IN_FLIGHT` | `512` | Open model shedding ceiling. Hitting it invalidates the run |
| `REG_DRAIN_BUDGET_S` | `60` | How long in-flight searches may finish after the run ends. Anything still running is recorded as an abandoned failure with the time it had accrued, never silently dropped |
| `REG_LINT_STRICT` | `1` | `0` downgrades blocking lint to warnings |
| `REG_SUT_SETTLE_S` | `8` | How long to wait for the target's own logging before asking it about the run |

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
| `REG_INDEXER_URLS` | the search peers | The cache lives on the indexers, and on a distributed target the search head has none. Empty means discover the search peers and reuse the target's credential on each; set it to name them outright |
| `REG_INDEXER_TOKEN`, or `REG_INDEXER_USERNAME` and `REG_INDEXER_PASSWORD` | the target's | A credential valid on the indexers when the search head's is not |

Eviction is confirmed rather than assumed. The cache manager answers 200 to a
request it then declines (a bucket with a live reader, or one inside the
hotlist window), so the result reports both the accepted count and the number
of buckets that actually left, and says why they differ when they do.

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
| `REG_HTTP2`, `REG_CONNECT_TIMEOUT_S`, `REG_READ_TIMEOUT_S` | `0`, `10`, `300` | Every dispatched job also carries `auto_cancel` a minute past the read timeout, so a search this worker gives up on is cancelled by splunkd rather than run to completion for nobody |

---

## Scenarios

A scenario is the payload: a directory with a `scenario.yaml` and, for most of
the library, a `savedsearches.conf` in Splunk's own format next to it.
Git-syncable, linted before it runs, content-addressed so two runs of the same
files are provably the same test. Twenty-two ship in the box.

| Scenario | What it is for |
|---|---|
| `smoke` | Two trivial searches needing no indexed data. Proves the path works, measures nothing |
| `search-classes` | One search per class (dense, sparse, rare, accelerated, heavy, subsearch) against Stoker-generated data, so a regression can be attributed to a component |
| `soc-analyst-morning` | Persona-weighted SOC triage: hunters, dashboard watchers and a reporter, at realistic think times |
| `dashboard-triage` | The browser engine opening the two shipped dashboards, with the time range and index pinned |
| `pack-<stoker pack>` | One per Stoker pack, seventeen in all: the searches an operations or security team would schedule against that data, written against the fields the pack actually emits. Five to eight searches each, in `savedsearches.conf` form, classified in the YAML |
| `stoker-scheduler` | Every event pack's scheduled searches in one file, fired on their own cron as Splunk's scheduler would fire them. The top-of-the-hour burst is the point |

### Splunk's own format

Every Splunk deployment already has its workload written down, in the
`savedsearches.conf` files of its apps, and nobody transcribes four hundred
searches into another format. So Regulator reads that one:

```
[Errors in the last hour]
search = index=main sourcetype=access_combined status>=500 | stats count by uri_path
dispatch.earliest_time = -1h
dispatch.latest_time = now
cron_schedule = */5 * * * *
enableSched = 1
```

The parser is the conf grammar as splunkd reads it: stanzas, backslash line
continuations with the newline kept, `#` only at the start of a line,
`[default]` folded in. A search without a leading `search` keyword gets one, as
the scheduler would give it. `dispatch.earliest_time` and `dispatch.latest_time`
become the step's window: `-24h@h` is evaluated as Splunk evaluates it, then
rolled with the scenario's jitter so the working set moves (or passed through
untouched with `time_from_saved: as_saved`, which replays the schedule exactly
and measures the page cache too, and lint says so). The cron schedule becomes
the weight: a five-minute search carries 288 times the weight of a daily one.

Three ways to use it in a scenario:

```yaml
searches:
  file: savedsearches.conf
  only_enabled: true            # disabled = 1 stays out
  only_scheduled: false         # or only what has a cron
  allow_side_effects: false     # a search ending in collect is refused unless you say so
  classes:                      # the class of each stanza, kept out of the conf so it stays plain Splunk
    Errors in the last hour: sparse

personas:
  - name: analyst
    steps_from: saved           # every selected search, sampled by cron weight
    weight_by: cron
    walk: sample
```

```yaml
    steps:
      - saved: Errors in the last hour     # one stanza, as a step
        class: sparse
```

```yaml
      - saved: Errors in the last hour
        dispatch: saved                    # run the TARGET's copy by name, with everything its stanza declares
        app: search
```

The third form needs no local file at all: the search head dispatches its own
saved search exactly as its scheduler would, alert actions never triggered. Lint
checks the search exists on the target before the run.

Every search that writes somewhere (`collect`, `outputlookup`, `sendemail` and
their relatives) is left out with the reason recorded, because a load test
replays a search hundreds of times and a summary-index writer replayed at two
hundred virtual users writes two hundred copies. `allow_side_effects` turns
that off when it really is the question.

**From a target.** The web interface's **Saved searches** button lists what a
target already runs, per app, with each search's cron, class and whether it
would be left out and why, then exports them as a `savedsearches.conf` a Splunk
admin would recognise and makes a scenario of them in one click. Over the API:
`GET /api/targets/{id}/savedsearches.conf?app=…`, then
`POST /api/scenarios` with the text.

### The schedule model

```yaml
load:
  model: schedule               # each step fires on its own cron
  duration: 1800s
  schedule_start: "08:55"       # the virtual clock, so the 09:00 burst lands early in the run
```

The scheduler is the workload. Every step with a cron fires at its cron minutes
on a virtual clock, all the hourly ones together, which is where a real cluster
queues. Splunk would skip searches over its concurrency limit; Regulator
dispatches them all and records the queueing, since seeing it is the point.
Each firing is an open-model arrival with its intended start at the cron minute,
so latency is coordinated-omission corrected. The closed and open models are
still there for population-shaped load; `arrivals: poisson` (the default) makes
open-model arrivals burst the way a real population does, because evenly spaced
arrivals never queue and find an optimistic saturation point.

The pack library is generated from `tools/build_pack_scenarios.py`, which is
where the searches are written; `make scenarios` regenerates it.

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

**Two flags that stop a fast number lying.** `partial` marks a job that finished
DONE and said in its own messages that it did less than asked: a peer dropped
out, a limit truncated it, it was finalized early, or a dashboard did not use
the pinned time range. Its latency is real; its work was not. `cancelled` marks
a search still in flight when the run ended and abandoned after the drain
budget, recorded as a failure with the time it had accrued so the tail is a
floor rather than a gap. Both are counted in the summary and shown on the run.

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
| `savedsearches.py` | Splunk's conf grammar, relative time modifiers, cron, side-effect detection and a conservative class guess |
| `managed.py` | A fleet member: claim, ready, heartbeat, final |
| `server/…/fleet.py`, `drivers/` | Planning shares, the lease protocol, merging, and the Swarm, Kubernetes and fake drivers |
| `smartstore.py` | Cache state and eviction per indexer, discovered from the search peers, with evictions confirmed rather than assumed |
| `sut.py` | The seven correlation probes against the cluster's own indexes |
| `compare.py` | Baselines, per-step deltas and the gate language |

---

## Development

```bash
make test          # worker and tools unit suites
make server-test   # the control plane, end to end against the fake splunkd
make smoke         # end to end against the fake splunkd
make scenarios     # regenerate the per-pack scenario library
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
the publishing jobs are grouped per SHA and never cancelled. Each publishing job
signs the digest it pushed in the same job, so nothing can land between the
push and the signature. Because `build` needs `test`, a cancelled test means
the stale build never starts at all. Every push and pull request also builds
the worker and control-plane images without publishing them and smokes the
packaged worker against the fake splunkd, so a broken Dockerfile fails the pull
request rather than the merge.

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
| **5** | **Adversarial review, twice** (done): measurement defects that let a failing target look good (empty histograms reading as zero, leaked jobs under saturation, dropped tails, unit-ambiguous gates, per-run scan totals), control-plane defects (unbounded durations, the open model bypassing the ceiling, zombie runs, unattributable evictions) and Splunk-facing defects (cache state read from a search head, doubled audit counts, dashboards ignoring the pinned range) fixed with regression tests |
| **6** | **Splunk's own search format** (done): `savedsearches.conf` as a scenario source, import and export from a target, dispatch by name, the schedule load model, and a scenario for every Stoker pack |
| **7** | **The worker fleet** (done): Swarm services through Portainer and Kubernetes Indexed Jobs driven by Stoker's claim protocol (fenced leases, a shared T0, heartbeats as the command channel, finals merged from raw histograms), proven with real worker processes over a real socket in the test suite |

---

## Licence

Apache 2.0. See `LICENSE`.
