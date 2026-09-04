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

Every event carries the join keys `run_no`, `run_label`, `scenario`, `target`
and `fleet` (and `slot` where it applies) as indexed fields, so a search can
filter on them without extracting the body.

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

The dashboard is **Regulator runs**: pick a run (or every run) and a window.
