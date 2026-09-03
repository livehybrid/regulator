# regulator_sut

The dashboards a browser run opens.

They exist so a browser benchmark is **comparable between environments**. Point
a browser scenario at whatever dashboards a site happens to have and its numbers
are comparable only with themselves, which is fine for tracking one cluster over
time and useless for comparing two. Shipping the dashboards makes the workload
part of the test definition rather than an accident of the environment.

| Dashboard | Shape | What it is for |
|---|---|---|
| `triage_overview` | 4 panels, mostly accelerated | The light one. A healthy cluster paints the first panel in well under three seconds. Open it with `wait_for: first_result`, because the number that matters is when the analyst sees anything |
| `ops_overview` | 8 panels: dense, sparse, rare and one `transaction` | The heavy one. The transaction panel is the first thing to degrade when a search head is under concurrency pressure, which is why it is there. Open it with `wait_for: all_panels` |

The searches deliberately span the classes: accelerated `tstats` that should be
near instant, dense aggregation over web access logs, a sparse filter over
authentication failures, and a rare CloudTrail term that on SmartStore is the
panel most likely to pay for an object-storage fetch. A dashboard that never
reads a cold bucket tells you nothing about cache behaviour.

## Installing it

```bash
cp -r splunk-app/regulator_sut $SPLUNK_HOME/etc/apps/
$SPLUNK_HOME/bin/splunk restart
```

The searches take their index from an `index` token, defaulting to `main`, and
their time range from a `range` token. The browser engine pins both through the
URL (`form.index` from the scenario's `index` parameter, `form.range.earliest`
and `form.range.latest` from the step's window), so the browser channel asks the
same question of the same data as the API channel. A dashboard scenario that
points at your own dashboards should set `time_token` to that dashboard's time
input token, or the page runs whatever range it was saved with, and the run
record says so: a browser step whose searches did not use the pinned range is
flagged `partial` with `time_pinned: false`.
