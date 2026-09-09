/*
 * One run, live or finished.
 *
 * While a run is running this page polls every two seconds and appends only
 * what is new, so a long run does not re-download its whole history on every
 * tick. A finished run loads its samples once, which is what makes a run
 * opened a week later show exactly the charts it showed while it was live.
 *
 * The prose around the numbers is not decoration. A load test that reports a
 * p95 without saying whether the cache was cold, whether the target was at its
 * concurrency ceiling, or whether the generator itself was the bottleneck, is
 * a number two people will read differently.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Chip from '@splunk/react-ui/Chip';
import Heading from '@splunk/react-ui/Heading';
import Message from '@splunk/react-ui/Message';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import Tooltip from '@splunk/react-ui/Tooltip';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import ComparisonPanel from '../components/ComparisonPanel';
import CompareModal from '../modals/CompareModal';
import FleetLogsModal from '../modals/FleetLogsModal';
import PromoteBaselineModal from '../modals/PromoteBaselineModal';
import PurgeModal from '../modals/PurgeModal';
import RunCharts from '../components/RunCharts';
import SutSection from '../components/SutSection';
import WorkersSection from '../components/WorkersSection';
import {
    Mono,
    Muted,
    Note,
    PageHead,
    Panel,
    RelTime,
    SectionHeading,
    StatePill,
    Tile,
    TileGrid,
    Toolbar,
} from '../components/primitives';
import { api, isAuthError } from '../api';
import { copyText } from '../clipboard';
import { fmtBytes, fmtDur, fmtInt, fmtMs, fmtNum, fmtPct, isBlank } from '../format';
import { useNotify } from '../Notifications';

const POLL_MS = 2000;
const MAX_ROWS = 5000;
const MAX_SLOT_ROWS = 20000;

const freshSeries = (id) => ({
    runId: id,
    rows: [],
    slotRows: [],
    lastAt: null,
    lastSlotAt: null,
    markers: [],
    startedAt: null,
    loaded: false,
});

export default function RunDetail({ runId, navigate }) {
    const [run, setRun] = useState(null);
    const [error, setError] = useState(null);
    const [comparison, setComparison] = useState(null);
    const [dialog, setDialog] = useState(null);
    const notify = useNotify();

    // The samples live in a ref, not in state: they are appended to on every
    // poll and a state update per append would re-render the page for each
    // one. A version counter is what tells React the charts moved.
    const series = useRef(freshSeries(runId));
    const [, setTick] = useState(0);

    useEffect(() => {
        series.current = freshSeries(runId);
        setRun(null);
        setComparison(null);
        setError(null);
    }, [runId]);

    const loadSamples = useCallback(async (current, live) => {
        const s = series.current;
        try {
            const base = `/runs/${encodeURIComponent(current.id)}/samples`;
            const doc = await api(base + (s.lastAt ? `?since=${s.lastAt}` : ''));
            (doc.samples || []).forEach((row) => {
                s.rows.push(row);
                s.lastAt = row.at;
            });
            if (s.rows.length > MAX_ROWS) {
                s.rows.splice(0, s.rows.length - MAX_ROWS);
            }
            if (current.fleet && current.fleet !== 'inprocess') {
                const sdoc = await api(
                    `${base}?slots=1${s.lastSlotAt ? `&since=${s.lastSlotAt}` : ''}`
                );
                (sdoc.samples || []).forEach((row) => {
                    s.slotRows.push(row);
                    s.lastSlotAt = row.at;
                });
                if (s.slotRows.length > MAX_SLOT_ROWS) {
                    s.slotRows.splice(0, s.slotRows.length - MAX_SLOT_ROWS);
                }
            }
            s.markers = doc.markers || s.markers || [];
            s.startedAt =
                current.started_at || doc.started_at || (s.rows.length ? s.rows[0].at : null);
            if (!live) {
                s.loaded = true;
            }
        } catch (e) {
            // The charts must never break the page: a finished run whose
            // samples cannot be read still shows its summary.
            if (!live) {
                s.loaded = true;
            }
        }
    }, []);

    useEffect(() => {
        let cancelled = false;
        let timer = null;

        const tick = async () => {
            let current;
            try {
                current = await api(`/runs/${encodeURIComponent(runId)}`);
            } catch (e) {
                if (isAuthError(e) || cancelled) {
                    return;
                }
                // One transient failure must not end the live view for good.
                setError(e.message);
                timer = setTimeout(tick, 5000);
                return;
            }
            if (cancelled) {
                return;
            }
            setError(null);
            const live = current.state === 'running' || current.state === 'pending';
            if (live || !series.current.loaded) {
                await loadSamples(current, live);
            }
            if (cancelled) {
                return;
            }
            setRun(current);
            setTick((t) => t + 1);
            if (live) {
                timer = setTimeout(tick, POLL_MS);
            }
        };

        tick();
        return () => {
            cancelled = true;
            if (timer) {
                clearTimeout(timer);
            }
        };
    }, [runId, loadSamples]);

    if (!run) {
        return error ? (
            <Panel>
                <Muted>{error}</Muted>
            </Panel>
        ) : (
            <P>
                <WaitSpinner /> Loading run&hellip;
            </P>
        );
    }

    const stats = run.stats || {};
    const summary = run.summary || {};
    const latency = stats.latency || {};
    const queueing = stats.queueing || {};
    const cache = summary.cache || {};
    const delta = cache.delta || {};
    const ceiling = (stats.target || {}).max_hist_searches;
    const peak = stats.peak_in_flight;
    const live = run.state === 'running' || run.state === 'pending';
    const atCeiling = !isBlank(peak) && !isBlank(ceiling) && peak >= ceiling;

    const copySummary = async () => {
        const ok = await copyText(JSON.stringify(run, null, 2));
        notify(
            ok ? 'Run JSON copied to the clipboard' : 'The clipboard refused; the JSON is in the browser console',
            ok ? 'success' : 'warning'
        );
    };

    const stop = async () => {
        const r = await api(`/runs/${encodeURIComponent(run.id)}/stop`, { method: 'POST' });
        setRun(r);
        notify('Stop requested', 'success');
    };

    const openLogs = async () => {
        setDialog({ kind: 'logs', loading: true, payload: null });
        try {
            const logs = await api(`/runs/${encodeURIComponent(run.id)}/logs?tail=300`);
            setDialog((d) => (d && d.kind === 'logs' ? { ...d, loading: false, payload: logs } : d));
        } catch (e) {
            setDialog(null);
            throw e;
        }
    };

    const openPurge = async () => {
        setDialog({ kind: 'purge', loading: true, payload: null });
        try {
            const c = await api(`/targets/${encodeURIComponent(run.target_id)}/cache`);
            setDialog((d) => (d && d.kind === 'purge' ? { ...d, loading: false, payload: c } : d));
        } catch (e) {
            setDialog((d) => (d && d.kind === 'purge' ? { ...d, loading: false } : d));
        }
    };

    /* ------------------------------------------------------------ banners */

    const banners = [];
    if (summary.valid === false) {
        banners.push(
            <Message appearance="fill" type="error" key="invalid">
                <Message.Title>
                    Invalid run: {summary.invalid_reason || 'reason not given'}
                </Message.Title>
                These numbers describe the load generator rather than Splunk. Do not compare them
                with other runs, and do not draw a capacity conclusion from them.
            </Message>
        );
    }
    if (!isBlank(queueing.searches_queued) && queueing.searches_queued > 0) {
        banners.push(
            <Message appearance="fill" type="warning" key="queued">
                <Message.Title>
                    Queueing observed: {fmtInt(queueing.searches_queued)} of{' '}
                    {fmtInt(stats.executions)} searches ({fmtPct(queueing.queued_pct)}) waited in
                    QUEUED before running, p95 {fmtMs((queueing.queued_ms || {}).p95_ms)}
                </Message.Title>
                The target was at its concurrent-search ceiling. This is a finding, not an error: it
                is the moment a capacity test exists to find.
            </Message>
        );
    }
    if (delta.provenance && delta.provenance !== 'warm') {
        banners.push(
            <Message appearance="fill" type="warning" key="cache">
                <Message.Title>Cache provenance: {delta.provenance}</Message.Title>
                {fmtInt(delta.buckets_downloaded)} buckets ({fmtBytes(delta.bytes_downloaded)}) came
                down from object storage during this run, so part of what was measured is object
                storage and the network rather than the search tier.
            </Message>
        );
    }
    if (summary.abort_reason) {
        banners.push(
            <Message appearance="fill" type="error" key="abort">
                <Message.Title>Aborted</Message.Title>
                {summary.abort_reason}
            </Message>
        );
    }
    if (run.error) {
        banners.push(
            <Message appearance="fill" type="error" key="error">
                <Message.Title>Error</Message.Title>
                <Mono>{run.error}</Mono>
            </Message>
        );
    }

    const quiet = [];
    if (delta.provenance === 'warm') {
        quiet.push(
            <Note key="warm">
                Cache provenance: warm. Nothing was downloaded during the run, so these numbers
                describe the search tier.
            </Note>
        );
    }
    if (summary.co_corrected === false) {
        quiet.push(
            <Note key="co">
                Coordinated omission was not corrected: the latency percentiles are service times,
                so throughput is the signal to read.
            </Note>
        );
    }
    if (summary.self_instrumented) {
        quiet.push(
            <Note key="self">
                Self-instrumented: telemetry was written to the system under test, so the run added
                a little load to its own target.
            </Note>
        );
    }
    if (stats.partial) {
        quiet.push(
            <Note key="partial">
                {fmtInt(stats.partial)} search(es) finished DONE but reported incomplete results (a
                peer dropped out, a limit truncated them, or the dashboard did not honour the pinned
                time range). Their latency is real; their work was less than asked.
            </Note>
        );
    }
    if (stats.abandoned) {
        quiet.push(
            <Note key="abandoned">
                {fmtInt(stats.abandoned)} search(es) were still in flight when the run ended and are
                recorded as cancelled failures with the time they had accrued, so the tail is a
                floor rather than a measurement.
            </Note>
        );
    }
    if (!isBlank(summary.peak_virtual_users)) {
        quiet.push(
            <Note key="vu">
                Peak virtual users: {fmtInt(summary.peak_virtual_users)}. Virtual users are not
                concurrent searches: a user idles between iterations, so the in-flight figure is the
                one to compare against the ceiling.
            </Note>
        );
    }

    /* -------------------------------------------------------------- tiles */

    const errTone = isBlank(stats.error_rate_pct)
        ? undefined
        : stats.error_rate_pct >= 5
          ? 'error'
          : stats.error_rate_pct > 0
            ? 'warning'
            : 'success';

    /* --------------------------------------------------------- per class */

    const byClass = {};
    (stats.steps || []).forEach((s) => {
        const k = s.class || 'unclassified';
        const b = byClass[k] || (byClass[k] = { execs: 0, errors: 0, worst: null, steps: 0 });
        b.execs += s.executions || 0;
        b.errors += s.errors || 0;
        b.steps += 1;
        const p = (s.latency || {}).p95_ms;
        if (!isBlank(p) && (b.worst === null || p > b.worst)) {
            b.worst = p;
        }
    });
    const classKeys = Object.keys(byClass).sort();

    const steps = (stats.steps || [])
        .slice()
        .sort((a, b) => ((b.latency || {}).p95_ms || 0) - ((a.latency || {}).p95_ms || 0));
    const cycled = steps.some((s) => s.cold || s.warm);

    const epochs = cache.epochs || [];
    const epochStats = {};
    (stats.epochs || []).forEach((e) => {
        epochStats[e.epoch] = e;
    });

    return (
        <div>
            <PageHead>
                <div>
                    <Heading level={1}>
                        {run.label || run.scenario || 'Run'} <StatePill state={run.state} />
                    </Heading>
                    <Muted $small>
                        <Mono>{run.scenario || ''}</Mono> against{' '}
                        {run.target_name || run.target_id || '?'} · id {run.id} · started{' '}
                        <RelTime value={run.started_at || run.created_at} />
                        {isBlank(stats.elapsed_s) ? '' : ` · elapsed ${fmtDur(stats.elapsed_s)}`}
                        {summary.outcome ? ` · outcome ${summary.outcome}` : ''}
                        {summary.load_model ? ` · ${summary.load_model} model` : ''}
                        {isBlank(summary.effective_seed) ? '' : ` · seed ${summary.effective_seed}`}
                        {summary.scenario_digest ? ` · digest ${summary.scenario_digest}` : ''}
                    </Muted>
                </div>
                <Toolbar>
                    <Button label="All runs" onClick={() => navigate('runs')} />
                    {live ? (
                        <>
                            <BusyButton appearance="destructive" label="Stop" onClick={stop} />
                            {run.target_id && (
                                <Tooltip
                                    content="Evict every cached bucket on the target now, so the searches that follow read cold"
                                    defaultPlacement="below"
                                >
                                    <Button label="Purge cache" onClick={openPurge} />
                                </Tooltip>
                            )}
                        </>
                    ) : (
                        run.state === 'completed' && (
                            <>
                                <Button
                                    label="Make baseline"
                                    onClick={() => setDialog({ kind: 'promote' })}
                                />
                                <Button
                                    label="Compare"
                                    onClick={() => setDialog({ kind: 'compare' })}
                                />
                            </>
                        )
                    )}
                    <BusyButton label="Copy JSON" onClick={copySummary} />
                </Toolbar>
            </PageHead>

            {banners}
            {quiet}

            <TileGrid>
                <Tile
                    label="Executions"
                    value={fmtInt(stats.executions)}
                    sub={`${fmtInt(stats.errors)} errors`}
                />
                <Tile
                    label="Throughput"
                    value={fmtNum(stats.throughput_per_s, 2)}
                    sub="searches per second"
                />
                <Tile label="p50" value={fmtMs(latency.p50_ms)} sub={`n=${fmtInt(latency.count)}`} />
                <Tile label="p95" value={fmtMs(latency.p95_ms)} sub={`max ${fmtMs(latency.max_ms)}`} />
                <Tile
                    label="p99"
                    value={fmtMs(latency.p99_ms)}
                    sub={`mean ${fmtMs(latency.mean_ms)}`}
                />
                <Tile label="Error rate" value={fmtPct(stats.error_rate_pct)} tone={errTone} />
                <Tile
                    label="Peak in flight"
                    value={`${fmtInt(peak)}${isBlank(ceiling) ? '' : ` / ${fmtInt(ceiling)}`}`}
                    sub={isBlank(ceiling) ? 'ceiling unknown' : 'ceiling'}
                    tone={atCeiling ? 'error' : undefined}
                />
                {stats.partial ? (
                    <Tile
                        label="Partial"
                        value={fmtInt(stats.partial)}
                        sub="finished with less than asked"
                        tone="warning"
                    />
                ) : null}
                {stats.abandoned ? (
                    <Tile
                        label="Abandoned"
                        value={fmtInt(stats.abandoned)}
                        sub="still running at the end"
                        tone="warning"
                    />
                ) : null}
            </TileGrid>

            <RunCharts run={run} live={live} series={series.current} />

            {classKeys.length > 0 && (
                <Panel flush>
                    <Table>
                        <Table.Head>
                            <Table.HeadCell>Class</Table.HeadCell>
                            <Table.HeadCell align="right">Steps</Table.HeadCell>
                            <Table.HeadCell align="right">Execs</Table.HeadCell>
                            <Table.HeadCell align="right">Err %</Table.HeadCell>
                            <Table.HeadCell align="right">Worst p95</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {classKeys.map((k) => {
                                const b = byClass[k];
                                return (
                                    <Table.Row key={k}>
                                        <Table.Cell>
                                            <Chip appearance="outline">{k}</Chip>
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtInt(b.steps)}</Table.Cell>
                                        <Table.Cell align="right">{fmtInt(b.execs)}</Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtPct(b.execs ? (100 * b.errors) / b.execs : null)}
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtMs(b.worst)}</Table.Cell>
                                    </Table.Row>
                                );
                            })}
                        </Table.Body>
                    </Table>
                </Panel>
            )}

            <WorkersSection run={run} stats={stats} summary={summary} onLogs={openLogs} />

            {epochs.length > 0 && (
                <Panel title="Cache epochs" flush>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell align="right">Epoch</Table.HeadCell>
                            <Table.HeadCell>Evicted at</Table.HeadCell>
                            <Table.HeadCell>By</Table.HeadCell>
                            <Table.HeadCell>Result</Table.HeadCell>
                            <Table.HeadCell align="right">Confirmed / attempted</Table.HeadCell>
                            <Table.HeadCell align="right">Bytes</Table.HeadCell>
                            <Table.HeadCell align="right">Took</Table.HeadCell>
                            <Table.HeadCell align="right">Execs in epoch</Table.HeadCell>
                            <Table.HeadCell align="right">p50</Table.HeadCell>
                            <Table.HeadCell align="right">p95</Table.HeadCell>
                            <Table.HeadCell> </Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {epochs.map((e) => {
                                const es = epochStats[e.epoch] || {};
                                const el = es.latency || {};
                                const partial = e.attempted && e.confirmed < e.attempted;
                                return (
                                    <Table.Row key={e.epoch}>
                                        <Table.Cell align="right">E{e.epoch}</Table.Cell>
                                        <Table.Cell>
                                            <RelTime value={e.requested_at} />
                                        </Table.Cell>
                                        <Table.Cell>{e.source || ''}</Table.Cell>
                                        <Table.Cell>
                                            {e.status ? (
                                                <Chip appearance="warning">{e.status}</Chip>
                                            ) : partial ? (
                                                <Chip appearance="warning">partial</Chip>
                                            ) : (
                                                <Chip appearance="success">ok</Chip>
                                            )}
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtInt(e.confirmed)} / {fmtInt(e.attempted)}
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtBytes(e.bytes_evicted)}
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtNum(e.duration_s, 1)}s
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtInt(es.executions)}</Table.Cell>
                                        <Table.Cell align="right">{fmtMs(el.p50_ms)}</Table.Cell>
                                        <Table.Cell align="right">{fmtMs(el.p95_ms)}</Table.Cell>
                                        <Table.Cell>
                                            <Muted $small>{e.note || ''}</Muted>
                                        </Table.Cell>
                                    </Table.Row>
                                );
                            })}
                        </Table.Body>
                    </Table>
                    {run.cold_window_s ? (
                        <Muted $small>
                            Executions within {fmtNum(run.cold_window_s, 0)} s of an eviction count
                            as cold; the step table shows cold and warm p95 apart.
                        </Muted>
                    ) : null}
                </Panel>
            )}

            <ComparisonPanel comparison={comparison} />

            <SutSection sut={summary.sut} />

            <SectionHeading>Steps, worst p95 first</SectionHeading>
            {steps.length ? (
                <Panel flush>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Step</Table.HeadCell>
                            <Table.HeadCell>Class</Table.HeadCell>
                            <Table.HeadCell align="right">Execs</Table.HeadCell>
                            <Table.HeadCell align="right">Errors</Table.HeadCell>
                            <Table.HeadCell align="right">Err %</Table.HeadCell>
                            <Table.HeadCell align="right">p50</Table.HeadCell>
                            <Table.HeadCell align="right">p95</Table.HeadCell>
                            {cycled && <Table.HeadCell align="right">Cold p95</Table.HeadCell>}
                            {cycled && <Table.HeadCell align="right">Warm p95</Table.HeadCell>}
                            <Table.HeadCell
                                align="right"
                                tooltip="Time from the dispatch call being made to Splunk accepting the job."
                            >
                                Dispatch p95
                            </Table.HeadCell>
                            <Table.HeadCell
                                align="right"
                                tooltip="TTFR, time to first result: how long until the search returns its first row. This is what a user actually feels; the total time is how long the whole search took, and on a wide cluster the two diverge a lot."
                            >
                                Time to first result p95
                            </Table.HeadCell>
                            <Table.HeadCell align="right">Scanned</Table.HeadCell>
                            <Table.HeadCell align="right">Events/s</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {steps.map((s) => {
                                const l = s.latency || {};
                                const d = s.dispatch || {};
                                const f = s.ttfr || {};
                                return (
                                    <Table.Row key={s.step_id}>
                                        <Table.Cell>
                                            <Mono>{s.step_id}</Mono>
                                        </Table.Cell>
                                        <Table.Cell>
                                            <Chip appearance="outline">{s.class || '?'}</Chip>
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtInt(s.executions)}</Table.Cell>
                                        <Table.Cell align="right">{fmtInt(s.errors)}</Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtPct(s.error_rate_pct)}
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtMs(l.p50_ms)}</Table.Cell>
                                        <Table.Cell align="right">{fmtMs(l.p95_ms)}</Table.Cell>
                                        {cycled && (
                                            <Table.Cell align="right">
                                                {s.cold ? (
                                                    <>
                                                        {fmtMs(s.cold.p95_ms)}{' '}
                                                        <Muted $small>n={fmtInt(s.cold.count)}</Muted>
                                                    </>
                                                ) : null}
                                            </Table.Cell>
                                        )}
                                        {cycled && (
                                            <Table.Cell align="right">
                                                {s.warm ? (
                                                    <>
                                                        {fmtMs(s.warm.p95_ms)}{' '}
                                                        <Muted $small>n={fmtInt(s.warm.count)}</Muted>
                                                    </>
                                                ) : null}
                                            </Table.Cell>
                                        )}
                                        <Table.Cell align="right">{fmtMs(d.p95_ms)}</Table.Cell>
                                        <Table.Cell align="right">{fmtMs(f.p95_ms)}</Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtInt(s.scan_count_total)}
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtInt(s.mean_events_per_s)}
                                        </Table.Cell>
                                    </Table.Row>
                                );
                            })}
                        </Table.Body>
                    </Table>
                </Panel>
            ) : (
                <Panel>
                    <Muted>No per-step figures yet.</Muted>
                </Panel>
            )}

            {dialog && dialog.kind === 'promote' && (
                <PromoteBaselineModal runId={run.id} onClose={() => setDialog(null)} />
            )}
            {dialog && dialog.kind === 'compare' && (
                <CompareModal
                    runId={run.id}
                    onClose={() => setDialog(null)}
                    onResult={setComparison}
                />
            )}
            {dialog && dialog.kind === 'logs' && (
                <FleetLogsModal
                    runId={run.id}
                    logs={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                />
            )}
            {dialog && dialog.kind === 'purge' && (
                <PurgeModal
                    targetId={run.target_id}
                    cache={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                />
            )}
        </div>
    );
}

RunDetail.propTypes = {
    runId: PropTypes.string.isRequired,
    navigate: PropTypes.func.isRequired,
};
