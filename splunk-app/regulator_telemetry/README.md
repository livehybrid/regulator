# Regulator telemetry app

Field extractions and one dashboard for everything Regulator ships over HEC.

## What arrives

| Sourcetype | From | When | What |
|---|---|---|---|
| `regulator:step` | every worker | per step execution | the step record: timings, sid, scan count, outcome, cache epoch |
| `regulator:run` | every worker and the control plane | at the end | the run summary; `scope` is `worker` (one slot), `fleet` (the merged whole) or `inprocess` |
| `regulator:sample` | the control plane | every 5 s (`REG_SAMPLE_INTERVAL_S`) | throughput, in flight, interval and cumulative percentiles; the aggregate row has no `slot`, fleet runs also send one row per slot |
| `regulator:lifecycle` | the control plane | as things happen | `kind` is one of `run_created`, `run_started`, `run_released`, `run_completed`, `run_stopped`, `run_failed`, `worker_claimed`, `worker_ready`, `worker_running`, `worker_lost`, `worker_done`, `cache_before`, `cache_after`, `cache_evicted`, `correlation` |
| `regulator:health` | the control plane | every 60 s | active runs, the fleets on offer, the emitter's own counters, uptime |

Control-plane events (`regulator:sample`, `regulator:lifecycle`,
`regulator:run`) carry the join keys `run_no`, `run_label`, `scenario`,
`target` and `fleet` (and `slot` where it applies). A worker's
`regulator:step` records carry the run as `run_id` instead, which is the run
label (`r12-nightly`), so the dashboards take the number from it with
`rex field=run_id "^r(?<run_no>\d+)"`. An aggregate sample has no `slot` key at
all, so `NOT slot=*` selects it and `slot=*` the per-worker rows.

## Install

1. Copy this directory to `$SPLUNK_HOME/etc/apps/regulator_telemetry` and restart Splunk.
2. Create an index (`regulator` is the default the dashboard looks in) and a HEC token that writes to it. Leave the token's sourcetype unset: every event names its own.
3. Point Regulator at it. On the control plane:

```bash
REG_HEC_URL=http://splunk.example:8088
REG_HEC_TOKEN=...
REG_HEC_INDEX=regulator
REG_HEC_VERIFY_TLS=0     # for a self-signed collector
```

Workers of a fleet run inherit the same destination through their claim. A
standalone worker takes the same `REG_HEC_*` variables directly.

## Dashboards

**Regulator runs.** Pick a run (or every run) and a window. Headline figures
from the samples, p95 per interval, throughput against searches in flight and
searches Splunk queued, failures by class, the slowest steps, per-worker p95
and figures for a fleet run, the finished runs (click one to select it), the
lifecycle feed and the control plane's own health.

**Ingest vs search.** For a test that runs Stoker and Regulator together:
Stoker's delivered events/s against Regulator's search p95 on one timeline,
ingest MB/s against searches/s, searches Splunk queued, search p95 by ingest
band (under 10k, 10k to 50k, 50k to 100k, 100k eps and over), Stoker's delivery
per run, and both tools' starts and stops in one feed. It reads Stoker's
dogfood telemetry (`stoker:metrics`, `stoker:job`, enabled by
`DOGFOOD_HEC_URL`), so set the Stoker index to wherever that token writes
(`stoker_telemetry` by default here). This app carries the JSON extraction
for those two sourcetypes, identical to Stoker's own Stoker Observability app,
so either can be installed alone.
