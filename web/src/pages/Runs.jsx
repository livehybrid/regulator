/*
 * The run history. The sparkline column is p95 over the run, which is what
 * tells you whether a run degraded late rather than being slow throughout: a
 * single p95 cannot say that.
 *
 * Which columns show is the operator's choice, remembered in this browser:
 * comparing two runs at a glance usually turns on one setting (one virtual
 * user against two), and that setting should be a column, not a click away.
 */
import React, { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Checkbox from '@splunk/react-ui/Checkbox';
import Chip from '@splunk/react-ui/Chip';
import Dropdown from '@splunk/react-ui/Dropdown';
import Heading from '@splunk/react-ui/Heading';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import Tooltip from '@splunk/react-ui/Tooltip';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import useLoader from '../useLoader';
import { Sparkline } from '../components/LineChart';
import { Mono, Muted, PageHead, Panel, RelTime, StatePill, Toolbar } from '../components/primitives';
import { api, isAuthError } from '../api';
import { DASH, fmtDur, fmtInt, fmtMs, fmtNum, fmtPct, isBlank } from '../format';

const COLUMNS_KEY = 'regulator.runs.columns';

/** "2 users", "30/min", with a live change shown after the planned figure. */
export function loadText(run) {
    const p = run.planned || {};
    let text;
    if (p.load_model === 'open') {
        text = isBlank(p.arrival_rate_per_min) ? DASH : `${fmtNum(p.arrival_rate_per_min, 1)}/min`;
    } else {
        text = isBlank(p.virtual_users)
            ? DASH
            : `${fmtInt(p.virtual_users)} user${p.virtual_users === 1 ? '' : 's'}`;
    }
    if (!isBlank(run.load_override)) {
        const now = p.load_model === 'open' ? `${fmtNum(run.load_override, 1)}/min` : fmtInt(run.load_override);
        text = `${text}, now ${now}`;
    }
    return text;
}

/*
 * Every column the table can show. `fixed` ones cannot be hidden (a row with
 * no label is no use); `on` is the default for an operator who has not chosen.
 */
const COLUMNS = [
    { key: 'label', label: 'Label', fixed: true, cell: (r) => r.label || '(no label)' },
    { key: 'scenario', label: 'Scenario', on: true, cell: (r) => <Mono>{r.scenario || ''}</Mono> },
    { key: 'target', label: 'Target', on: true, cell: (r) => r.target_name || r.target_id || '(deleted)' },
    { key: 'state', label: 'State', on: true, cell: (r) => <StatePill state={r.state} /> },
    { key: 'started', label: 'Started', on: true, cell: (r) => <RelTime value={r.created_at} /> },
    { key: 'load', label: 'Load', on: true, cell: (r) => loadText(r) },
    { key: 'workers', label: 'Workers', align: 'right', cell: (r) => fmtInt((r.planned || {}).workers) },
    {
        key: 'pacing',
        label: 'Pacing',
        align: 'right',
        cell: (r) => {
            const s = (r.planned || {}).pacing_s;
            return s ? `${fmtNum(s, 1)} s` : DASH;
        },
    },
    { key: 'duration', label: 'Duration', align: 'right', cell: (r) => fmtDur((r.planned || {}).duration_s) },
    { key: 'ran', label: 'Ran for', align: 'right', cell: (r, h) => fmtDur(h.elapsed_s) },
    { key: 'peak', label: 'Peak users', align: 'right', cell: (r, h) => fmtInt(h.peak_virtual_users) },
    { key: 'fleet', label: 'Fleet', cell: (r) => r.fleet || 'inprocess' },
    { key: 'seed', label: 'Seed', align: 'right', cell: (r) => (isBlank(r.seed) ? DASH : r.seed) },
    { key: 'executions', label: 'Executions', align: 'right', on: true, cell: (r, h) => fmtInt(h.executions) },
    {
        key: 'throughput',
        label: 'Searches/s',
        align: 'right',
        cell: (r, h) => (isBlank(h.throughput_per_s) ? DASH : fmtNum(h.throughput_per_s, 2)),
    },
    { key: 'p50', label: 'p50', align: 'right', cell: (r, h) => fmtMs(h.p50_ms) },
    { key: 'p95', label: 'p95', align: 'right', on: true, cell: (r, h) => fmtMs(h.p95_ms) },
    { key: 'p99', label: 'p99', align: 'right', cell: (r, h) => fmtMs(h.p99_ms) },
    { key: 'spark', label: 'p95 over the run', on: true, cell: (r, h, sparks) => <Sparkline values={sparks[r.id]} /> },
    { key: 'errors', label: 'Errors', align: 'right', on: true, cell: (r, h) => fmtPct(h.error_rate_pct) },
    { key: 'queued', label: 'Queued', align: 'right', on: true, cell: (r, h) => fmtInt(h.searches_queued) },
    { key: 'validity', label: 'Validity', on: true, cell: (r, h) => <Validity run={r} headline={h} /> },
    { key: 'note', label: 'Note', cell: (r) => (r.note ? <Muted $small>{r.note}</Muted> : DASH) },
];

const DEFAULT_COLUMNS = COLUMNS.filter((c) => c.fixed || c.on).map((c) => c.key);

function readColumns() {
    try {
        const stored = JSON.parse(window.localStorage.getItem(COLUMNS_KEY) || 'null');
        if (Array.isArray(stored)) {
            const known = new Set(COLUMNS.map((c) => c.key));
            return stored.filter((k) => known.has(k));
        }
    } catch (e) {
        // Storage disabled or a stale value: fall back to the defaults.
    }
    return DEFAULT_COLUMNS;
}

const ColumnList = styled.div`
    display: grid;
    grid-template-columns: repeat(2, max-content);
    gap: ${variables.spacingXSmall} ${variables.spacingLarge};
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
`;

const ColumnFoot = styled.div`
    padding: 0 ${variables.spacingMedium} ${variables.spacingSmall};
`;

/** The column picker: a checkbox per optional column, and a reset. */
function ColumnPicker({ shown, onChange }) {
    const toggle = (key) =>
        onChange(shown.includes(key) ? shown.filter((k) => k !== key) : [...shown, key]);
    return (
        <Dropdown toggle={<Button label="Columns" isMenu />} retainFocus={false}>
            <ColumnList>
                {COLUMNS.filter((c) => !c.fixed).map((c) => (
                    <Checkbox key={c.key} checked={shown.includes(c.key)} onChange={() => toggle(c.key)}>
                        {c.label}
                    </Checkbox>
                ))}
            </ColumnList>
            <ColumnFoot>
                <Button appearance="subtle" label="Reset to the defaults" onClick={() => onChange(DEFAULT_COLUMNS)} />
            </ColumnFoot>
        </Dropdown>
    );
}

ColumnPicker.propTypes = {
    shown: PropTypes.arrayOf(PropTypes.string).isRequired,
    onChange: PropTypes.func.isRequired,
};

export default function Runs({ navigate, onLaunch }) {
    const { data, error, loading } = useLoader(() => api('/runs'), []);
    const runs = data || [];
    const [sparks, setSparks] = useState({});
    const [shown, setShown] = useState(readColumns);
    useEffect(() => {
        try {
            window.localStorage.setItem(COLUMNS_KEY, JSON.stringify(shown));
        } catch (e) {
            // Not worth a message: the choice simply will not be remembered.
        }
    }, [shown]);
    // In the table's own order, whatever order they were ticked in.
    const columns = useMemo(() => COLUMNS.filter((c) => c.fixed || shown.includes(c.key)), [shown]);

    // Best effort and deliberately after the table: the page is useful without
    // the sparklines, so they must never be able to stop it rendering.
    //
    // The dependency is `data` rather than `runs`, because `data || []` is a
    // new array on every render and would re-run this on every render.
    useEffect(() => {
        if (!data || !data.length) {
            return undefined;
        }
        let live = true;
        (async () => {
            try {
                const map = await api(`/runs/sparklines?ids=${data.map((r) => r.id).join(',')}`);
                if (live) {
                    setSparks(map || {});
                }
            } catch (e) {
                // Not worth a message to the operator: the page works without
                // the sparklines, and a 401 has already sent them to sign in.
                if (!isAuthError(e) && live) {
                    setSparks({});
                }
            }
        })();
        return () => {
            live = false;
        };
    }, [data]);

    return (
        <div>
            <PageHead>
                <Heading level={1}>Runs</Heading>
                <Toolbar>
                    <ColumnPicker shown={shown} onChange={setShown} />
                    <Button
                        appearance="primary"
                        label="Launch a run"
                        onClick={() => onLaunch({ scenario: null })}
                    />
                </Toolbar>
            </PageHead>

            {loading && !data ? (
                <P>
                    <WaitSpinner /> Loading runs&hellip;
                </P>
            ) : error ? (
                <Panel>
                    <Muted>{error}</Muted>
                </Panel>
            ) : !runs.length ? (
                <Panel>
                    <Muted>No runs yet. Pick a scenario, pick a target and launch one.</Muted>
                </Panel>
            ) : (
                <Panel flush>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            {columns.map((c) => (
                                <Table.HeadCell key={c.key} align={c.align}>
                                    {c.label}
                                </Table.HeadCell>
                            ))}
                            <Table.HeadCell />
                        </Table.Head>
                        <Table.Body>
                            {runs.map((r) => {
                                const h = r.headline || {};
                                return (
                                    <Table.Row
                                        key={r.id}
                                        data={r}
                                        onClick={() => navigate(`run/${encodeURIComponent(r.id)}`)}
                                    >
                                        {columns.map((c) => (
                                            <Table.Cell key={c.key} align={c.align}>
                                                {c.cell(r, h, sparks)}
                                            </Table.Cell>
                                        ))}
                                        <Table.Cell>
                                            {/* Its own click: the row's opens the run. */}
                                            <Button
                                                appearance="subtle"
                                                label="Run again"
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    onLaunch({ scenario: r.scenario, initial: r });
                                                }}
                                            />
                                        </Table.Cell>
                                    </Table.Row>
                                );
                            })}
                        </Table.Body>
                    </Table>
                </Panel>
            )}
        </div>
    );
}

Runs.propTypes = {
    navigate: PropTypes.func.isRequired,
    onLaunch: PropTypes.func.isRequired,
};

function Validity({ run, headline }) {
    const cache = headline.cache_provenance ? (
        <Chip appearance={headline.cache_provenance === 'warm' ? 'outline' : 'warning'}>
            {headline.cache_provenance}
        </Chip>
    ) : null;

    if (headline.valid === false) {
        return (
            <>
                <Tooltip content={headline.invalid_reason || ''} defaultPlacement="left">
                    <Chip appearance="error">invalid</Chip>
                </Tooltip>{' '}
                {cache}
            </>
        );
    }
    return (
        <>
            {run.state === 'completed' ? (
                <Chip appearance="success">valid</Chip>
            ) : (
                <Chip appearance="outline">–</Chip>
            )}{' '}
            {cache}
        </>
    );
}

Validity.propTypes = { run: PropTypes.object, headline: PropTypes.object };
