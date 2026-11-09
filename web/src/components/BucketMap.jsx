/*
 * The bucket map: a target's buckets by index (rows) and time (columns),
 * coloured by how much of each bucket is on local disk.
 *
 * The cache forecast answers "how much of what this run reads is local" with
 * one line. This shows the shape behind it: which parts of which indexes are
 * warm, where the cold stretches are, and, with a scenario chosen, where its
 * searches will land.
 *
 * Read on demand, never on page load and never on a timer: listing every
 * bucket is load on the cluster under test, and the on-disk detail (dbinspect)
 * more so.
 *
 * Colour is an ordinal one-hue ramp, least ready to most ready for a search:
 * not local, partial, index files only, full. The steps are the documented
 * blue ramp, chosen per colour scheme and checked against the console's own
 * panel surfaces (light-end contrast, monotone lightness, step gaps). A local
 * bucket whose detail was not read is neutral grey: known local, no claim
 * about how much. An empty slice shows the track, so "no bucket here" and
 * "bucket not local" never look alike.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import styled from 'styled-components';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Multiselect from '@splunk/react-ui/Multiselect';
import Select from '@splunk/react-ui/Select';
import Switch from '@splunk/react-ui/Switch';
import Table from '@splunk/react-ui/Table';
import { useSplunkTheme, variables } from '@splunk/themes';

import BusyButton from './BusyButton';
import { Actions, Grid2, Mono, Muted, Note, Panel } from './primitives';
import { api } from '../api';
import { fmtBytes, fmtInt, fmtPct } from '../format';

const MAX_DETAIL_INDEXES = 10;
const BINS = 60;

const RANGES = [
    { value: 'all', label: 'All time', earliest: null },
    { value: '-24h', label: 'Last 24 hours', earliest: '-24h' },
    { value: '-7d', label: 'Last 7 days', earliest: '-7d' },
    { value: '-30d', label: 'Last 30 days', earliest: '-30d' },
    { value: '-90d', label: 'Last 90 days', earliest: '-90d' },
];

const LABELS = {
    remote: 'Not local',
    partial: 'Partial',
    index_only: 'Index files only',
    full: 'Full',
    local: 'Local',
};

// Ordinal ramp, least to most ready. Light: steps 250/400/550/700 on #ffffff.
// Dark: steps 550/400/250/100 on #1e2427 (the anchor flips so the step
// nearest the surface still means "least").
const RAMP = {
    light: { remote: '#86b6ef', partial: '#3987e5', index_only: '#1c5cab', full: '#0d366b' },
    dark: { remote: '#1c5cab', partial: '#3987e5', index_only: '#86b6ef', full: '#cde2fb' },
};

function colours(theme, detail) {
    const ramp = theme.isDark ? RAMP.dark : RAMP.light;
    return {
        ...ramp,
        // With only the cache manager's reading there are two steps, and local
        // is the top one. With on-disk detail, a local bucket dbinspect had no
        // row for is grey: local, amount unknown.
        local: detail === 'disk' ? theme.neutral400 : ramp.full,
    };
}

const Rows = styled.div`
    display: grid;
    grid-template-columns: minmax(90px, 200px) minmax(0, 1fr);
    column-gap: ${variables.spacingSmall};
    row-gap: 6px;
    align-items: center;
    position: relative;
`;

const IndexLabel = styled.div`
    font-family: ${variables.monoFontFamily};
    font-size: ${variables.fontSizeSmall};
    color: ${variables.contentColorDefault};
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: right;
`;

const Track = styled.div`
    position: relative;
    display: grid;
    grid-template-columns: repeat(${(p) => p.$bins}, minmax(0, 1fr));
    column-gap: 2px;
    height: 24px;
    background-color: ${variables.backgroundColorPage};
    border-radius: 2px;
`;

const Cell = styled.div`
    display: flex;
    flex-direction: column-reverse;
    gap: 2px;
    min-width: 0;
    outline: ${(p) => (p.$active ? `2px solid ${p.$ink}` : 'none')};
    outline-offset: 1px;
`;

const Segment = styled.div`
    min-height: 2px;
    border-radius: 1px;
`;

// Where a scenario's searches read: a line under the row, in ink rather than
// a data colour, so it cannot be mistaken for a bucket state.
const ReadBar = styled.div`
    position: absolute;
    bottom: -5px;
    height: 3px;
    border-radius: 1px;
    background-color: ${variables.contentColorDefault};
    pointer-events: none;
`;

const Axis = styled.div`
    display: flex;
    justify-content: space-between;
    font-size: ${variables.fontSizeSmall};
    color: ${variables.contentColorMuted};
    font-variant-numeric: tabular-nums;
    margin-top: 2px;
`;

// One vertical rhythm for the panel: spacingMedium between its parts
// (controls, action, map, table), spacingSmall inside a part.
const Caption = styled.div`
    margin-top: ${variables.spacingSmall};
`;

const TableWrap = styled.div`
    margin-top: ${variables.spacingMedium};
`;

const Legend = styled.div`
    display: flex;
    flex-wrap: wrap;
    gap: ${variables.spacingSmall} ${variables.spacingLarge};
    margin: 0 0 ${variables.spacingSmall};
    font-size: ${variables.fontSizeSmall};
    color: ${variables.contentColorDefault};
`;

const Key = styled.span`
    display: inline-flex;
    align-items: center;
    gap: 6px;
`;

const Swatch = styled.span`
    display: inline-block;
    width: 12px;
    height: 12px;
    border-radius: 2px;
`;

const LineKey = styled.span`
    display: inline-block;
    width: 16px;
    height: 3px;
    border-radius: 1px;
    background-color: ${variables.contentColorDefault};
`;

const Tip = styled.div`
    position: fixed;
    z-index: 2000;
    pointer-events: none;
    background-color: ${variables.backgroundColorPopup};
    border: 1px solid ${variables.borderColor};
    border-radius: ${variables.borderRadius};
    padding: ${variables.spacingSmall};
    font-size: ${variables.fontSizeSmall};
    color: ${variables.contentColorDefault};
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    min-width: 200px;
`;

const TipRow = styled.div`
    display: flex;
    align-items: center;
    gap: 6px;
    font-variant-numeric: tabular-nums;
`;

const pad = (n) => String(n).padStart(2, '0');
function fmtWhen(epoch, withTime) {
    const d = new Date(epoch * 1000);
    const day = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    return withTime ? `${day} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : day;
}

export default function BucketMap({ targetId }) {
    const theme = useSplunkTheme();
    const [map, setMap] = useState(null);
    const [scenarios, setScenarios] = useState([]);
    const [range, setRange] = useState('-30d');
    const [scenario, setScenario] = useState('');
    const [picked, setPicked] = useState([]);
    const [detail, setDetail] = useState(false);
    const [known, setKnown] = useState([]);
    const [tip, setTip] = useState(null);
    const live = useRef(true);

    useEffect(() => {
        live.current = true;
        api('/scenarios')
            .then((list) => live.current && setScenarios((list || []).map((s) => s.name)))
            .catch(() => {});
        return () => {
            live.current = false;
        };
    }, []);

    const canDetail = picked.length > 0 && picked.length <= MAX_DETAIL_INDEXES;

    const load = async () => {
        const params = new URLSearchParams({ bins: String(BINS) });
        const chosen = RANGES.find((r) => r.value === range);
        if (chosen && chosen.earliest) {
            params.set('earliest', chosen.earliest);
            params.set('latest', 'now');
        }
        if (picked.length) {
            params.set('indexes', picked.join(','));
        }
        if (detail && canDetail) {
            params.set('detail', 'true');
        }
        if (scenario) {
            params.set('scenario', scenario);
        }
        const r = await api(`/targets/${encodeURIComponent(targetId)}/buckets?${params}`);
        if (!live.current) {
            return;
        }
        setMap(r);
        // Remember every index seen, so the picker can offer ones a narrower
        // read left out.
        setKnown((prev) => Array.from(new Set([...prev, ...(r.indexes || []).map((i) => i.index)])).sort());
    };

    const palette = useMemo(() => colours(theme, map ? map.detail : 'cache'), [theme, map]);
    const span = map && map.available ? map.latest - map.earliest : 0;
    const shortRange = span > 0 && span <= 3 * 86400;
    const overlay = (map && map.overlay && map.overlay.spans) || [];
    const ticks = map && map.available ? [0, 0.25, 0.5, 0.75, 1].map((f) => map.earliest + f * span) : [];

    const showTip = (e, row, cell) => {
        setTip({
            x: Math.min(e.clientX + 14, window.innerWidth - 260),
            y: e.clientY + 14,
            key: `${row.index}:${cell.bin}`,
            index: row.index,
            from: map.earliest + cell.bin * map.bin_s,
            to: map.earliest + (cell.bin + 1) * map.bin_s,
            cell,
        });
    };

    return (
        <Panel title="Bucket map">
            <Grid2>
                <ControlGroup label="Time range" labelPosition="top">
                    <Select value={range} onChange={(e, { value }) => setRange(value)}>
                        {RANGES.map((r) => (
                            <Select.Option key={r.value} value={r.value} label={r.label} />
                        ))}
                    </Select>
                </ControlGroup>
                <ControlGroup
                    label="Indexes"
                    labelPosition="top"
                    help={known.length ? 'Blank shows the largest.' : 'Blank shows the largest. Load once to pick from a list.'}
                >
                    <Multiselect values={picked} allowNewValues onChange={(e, { values }) => setPicked(values)}>
                        {known.map((n) => (
                            <Multiselect.Option key={n} value={n} label={n} />
                        ))}
                    </Multiselect>
                </ControlGroup>
                <ControlGroup label="Show what a scenario reads" labelPosition="top">
                    <Select value={scenario} onChange={(e, { value }) => setScenario(value)}>
                        <Select.Option value="" label="None" />
                        {scenarios.map((n) => (
                            <Select.Option key={n} value={n} label={n} />
                        ))}
                    </Select>
                </ControlGroup>
                <ControlGroup
                    label="On-disk detail"
                    labelPosition="top"
                    help={`Full or partial, from dbinspect. Needs 1 to ${MAX_DETAIL_INDEXES} named indexes.`}
                >
                    <Switch
                        appearance="toggle"
                        selected={detail && canDetail}
                        disabled={!canDetail}
                        onClick={() => setDetail((v) => !v)}
                        value="detail"
                    >
                        Read dbinspect
                    </Switch>
                </ControlGroup>
            </Grid2>
            <Actions>
                <BusyButton appearance="primary" label={map ? 'Reload bucket map' : 'Load bucket map'} onClick={load} />
                <Muted $small>Read on demand: listing every bucket is load on the target.</Muted>
            </Actions>

            {map && !map.available && <Note>The bucket map could not be read: {map.reason}</Note>}

            {map && map.available && (
                <>
                    <Legend>
                        {map.states.map((s) => (
                            <Key key={s}>
                                <Swatch style={{ backgroundColor: palette[s] }} />
                                {s === 'local' && map.detail === 'disk' ? 'Local (detail not read)' : LABELS[s]}
                            </Key>
                        ))}
                        {overlay.length > 0 && (
                            <Key>
                                <LineKey />
                                Read by {map.overlay.scenario}
                            </Key>
                        )}
                    </Legend>

                    <Rows onMouseLeave={() => setTip(null)}>
                        {map.indexes.map((row) => (
                            <React.Fragment key={row.index}>
                                <IndexLabel title={row.index}>{row.index}</IndexLabel>
                                <Track $bins={map.bins}>
                                    {row.cells.map((cell) => {
                                        const total = Object.values(cell.bytes).reduce((a, b) => a + b, 0) || 1;
                                        return (
                                            <Cell
                                                key={cell.bin}
                                                style={{ gridColumn: cell.bin + 1 }}
                                                $active={tip && tip.key === `${row.index}:${cell.bin}`}
                                                $ink={theme.contentColorDefault}
                                                onMouseMove={(e) => showTip(e, row, cell)}
                                            >
                                                {map.states
                                                    .filter((s) => cell.bytes[s])
                                                    .map((s) => (
                                                        <Segment
                                                            key={s}
                                                            style={{
                                                                backgroundColor: palette[s],
                                                                flex: `${cell.bytes[s] / total} 1 0`,
                                                            }}
                                                        />
                                                    ))}
                                            </Cell>
                                        );
                                    })}
                                    {overlay
                                        .filter((o) => o.index === row.index)
                                        .map((o) => {
                                            const left = Math.max(0, (o.earliest - map.earliest) / span);
                                            const right = Math.min(1, (o.latest - map.earliest) / span);
                                            return (
                                                <ReadBar
                                                    key={`${o.earliest}-${o.latest}`}
                                                    style={{
                                                        left: `${left * 100}%`,
                                                        width: `max(3px, ${(right - left) * 100}%)`,
                                                    }}
                                                />
                                            );
                                        })}
                                </Track>
                            </React.Fragment>
                        ))}
                        <div />
                        <Axis>
                            {ticks.map((t) => (
                                <span key={t}>{fmtWhen(t, shortRange)}</span>
                            ))}
                        </Axis>
                    </Rows>
                    <Caption>
                        <Muted $small>Times are UTC. A bucket spanning several slices is drawn in each.</Muted>
                    </Caption>

                    {tip && (
                        <Tip style={{ left: tip.x, top: tip.y }}>
                            <Mono>{tip.index}</Mono>
                            <div>
                                <Muted $small>
                                    {fmtWhen(tip.from, true)} to {fmtWhen(tip.to, true)}
                                </Muted>
                            </div>
                            {map.states
                                .filter((s) => tip.cell.bytes[s])
                                .map((s) => (
                                    <TipRow key={s}>
                                        <Swatch style={{ backgroundColor: palette[s], width: 10, height: 3 }} />
                                        <strong>{fmtBytes(tip.cell.bytes[s])}</strong>
                                        <Muted $small>
                                            {s === 'local' && map.detail === 'disk' ? 'local (detail not read)' : LABELS[s].toLowerCase()}
                                        </Muted>
                                    </TipRow>
                                ))}
                            <Muted $small>{fmtInt(tip.cell.buckets)} bucket(s) overlap this slice</Muted>
                        </Tip>
                    )}

                    {(map.notes || []).concat((map.overlay && map.overlay.notes) || []).map((n) => (
                        <Note key={n}>{n}</Note>
                    ))}

                    {/* The same numbers as a table: nothing on the map is
                        reachable only by hovering. */}
                    <TableWrap>
                        <Table horizontalOverflow="scroll">
                            <Table.Head>
                                <Table.HeadCell>Index</Table.HeadCell>
                                <Table.HeadCell align="right">Buckets</Table.HeadCell>
                                <Table.HeadCell align="right">Size</Table.HeadCell>
                                <Table.HeadCell align="right">Local</Table.HeadCell>
                                {map.states.map((s) => (
                                    <Table.HeadCell key={s} align="right">
                                        {s === 'local' && map.detail === 'disk' ? 'Local (detail not read)' : LABELS[s]}
                                    </Table.HeadCell>
                                ))}
                            </Table.Head>
                            <Table.Body>
                                {map.indexes.map((row) => (
                                    <Table.Row key={row.index}>
                                        <Table.Cell>
                                            <Mono>{row.index}</Mono>
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtInt(row.buckets)}</Table.Cell>
                                        <Table.Cell align="right">{fmtBytes(row.bytes)}</Table.Cell>
                                        <Table.Cell align="right">{fmtPct(row.local_pct, 0)}</Table.Cell>
                                        {map.states.map((s) => (
                                            <Table.Cell key={s} align="right">
                                                {row.by_state[s]
                                                    ? `${fmtInt(row.by_state[s].buckets)} (${fmtBytes(row.by_state[s].bytes)})`
                                                    : fmtInt(0)}
                                            </Table.Cell>
                                        ))}
                                    </Table.Row>
                                ))}
                            </Table.Body>
                        </Table>
                    </TableWrap>
                </>
            )}
        </Panel>
    );
}

BucketMap.propTypes = {
    targetId: PropTypes.any.isRequired,
};
