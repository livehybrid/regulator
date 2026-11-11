/*
 * What the target's machines were doing while the run was going.
 *
 * A latency chart says the target got slower; this says which tier ran out,
 * and of what. CPU, memory and disk for every host whose _introspection the
 * search head can search, read back after the run and sorted into search
 * heads and indexers.
 *
 * Each tier is drawn as two lines, its mean and its busiest host. A mean
 * alone hides one indexer pinned at 100% among nine idle ones, and a line a
 * host stops being readable past a handful: the table underneath has every
 * host. The charts run from three minutes before the run to a minute after
 * it, with the run's start and end marked, so "idle" is on the chart too.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Table from '@splunk/react-ui/Table';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import LineChart, { PALETTE } from './LineChart';
import { Mono, Muted, Note, Panel, SectionHeading } from './primitives';
import { DASH, fmtDur, fmtInt, fmtNum, isBlank } from '../format';

const Grid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    gap: ${variables.spacingMedium};
    margin-bottom: ${variables.spacingMedium};
`;

// A table sits flush in its panel; the words around it need the panel's inset.
const Caption = styled.div`
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
`;

// A host name is one token: wrapped at its hyphens it reads as three.
const Host = styled(Mono)`
    white-space: nowrap;
`;

const NoWrap = styled.span`
    white-space: nowrap;
`;

const Findings = styled.ul`
    margin: 0;
    padding-left: ${variables.spacingLarge};
`;

const TIERS = [
    ['search head', 'search heads'],
    ['indexer', 'indexers'],
    ['other', 'other hosts'],
    ['unknown', 'hosts of unknown tier'],
];

const pct = (v) => (isBlank(v) ? DASH : `${fmtNum(v, 0)}%`);
const num = (v, dp = 1) => (isBlank(v) ? DASH : fmtNum(v, dp));
const gb = (mb) => (isBlank(mb) ? DASH : `${fmtNum(mb / 1024, 0)} GB`);

/** `[epochSeconds, value]` pairs from a tier's parallel arrays. */
const points = (tier, key) =>
    (tier.t || []).map((t, i) => {
        const v = (tier[key] || [])[i];
        return [t, v === undefined ? null : v];
    });

/**
 * Two lines a tier, sharing a colour: the mean solid, the busiest host dashed.
 * No commas in a label: the chart library takes one as a separator in the
 * options that pick a series by name, and the dash and the second axis are
 * then silently not applied.
 * A tier of one host has one line, because the two would be the same line.
 */
function tierSeries(roles, meanKey, maxKey) {
    const out = [];
    TIERS.forEach(([role, plural], i) => {
        const tier = roles[role];
        if (!tier) {
            return;
        }
        const colour = PALETTE[i % PALETTE.length];
        if (tier.hosts > 1) {
            out.push({ label: `mean of ${tier.hosts} ${plural}`, color: colour, points: points(tier, meanKey) });
            out.push({ label: `busiest of the ${plural}`, color: colour, dashed: true, points: points(tier, maxKey) });
        } else {
            out.push({ label: role, color: colour, points: points(tier, maxKey) });
        }
    });
    return out;
}

/** The tier whose disks are worth drawing: the indexers, where there are any. */
function diskTier(roles) {
    const found = TIERS.map(([role]) => role).filter((role) => roles[role] && roles[role].read_mb_s);
    if (!found.length) {
        return null;
    }
    return found.includes('indexer') ? 'indexer' : found[0];
}

export default function HostResources({ resources }) {
    if (!resources) {
        return null;
    }
    if (!resources.available) {
        return (
            <>
                <SectionHeading>What the machines were doing</SectionHeading>
                <Note>
                    Not available for this run: {resources.reason || 'the target did not answer'}. This reads
                    splunk_resource_usage from the target&apos;s _introspection index after the run, so the
                    account needs to be able to search it.
                </Note>
            </>
        );
    }
    const roles = resources.roles || {};
    const hosts = resources.hosts || [];
    const mounts = resources.mounts || [];
    const span = resources.window || {};
    const markers = [
        { at: span.run_started_at, label: 'the run started', kind: 'run' },
        { at: span.run_ended_at, label: 'the run ended', kind: 'run' },
    ].filter((m) => !isBlank(m.at));
    const disk = diskTier(roles);
    const anyIo = hosts.some((h) => h.io);
    const diskLabel = disk ? (TIERS.find(([role]) => role === disk) || [disk, disk])[1] : '';

    return (
        <>
            <SectionHeading>What the machines were doing</SectionHeading>
            {(resources.findings || []).length > 0 && (
                <Panel>
                    <Findings>
                        {resources.findings.map((line) => (
                            <li key={line}>{line}</li>
                        ))}
                    </Findings>
                </Panel>
            )}
            <Grid>
                <LineChart
                    title="CPU by tier"
                    leftTitle="% of all cores"
                    markers={markers}
                    series={tierSeries(roles, 'cpu_avg', 'cpu_max')}
                    note="User plus system CPU for the whole host, so it includes whatever else the machine was doing. The dashed lines mark the run."
                />
                <LineChart
                    title="Memory used by tier"
                    leftTitle="% of memory"
                    markers={markers}
                    series={tierSeries(roles, 'mem_avg', 'mem_max')}
                />
                {disk && (
                    <LineChart
                        title={`Disk on the ${diskLabel}`}
                        leftTitle="MB/s, all hosts"
                        rightTitle="ms per request"
                        markers={markers}
                        series={[
                            { label: 'read MB/s', points: points(roles[disk], 'read_mb_s') },
                            { label: 'written MB/s', points: points(roles[disk], 'write_mb_s') },
                            {
                                label: 'slowest disk ms per request',
                                axis: 'right',
                                points: points(roles[disk], 'io_wait_max'),
                            },
                        ]}
                        note="Throughput added up across the tier's disks; the wait is the slowest disk on any host, queueing included."
                    />
                )}
                <LineChart
                    title="Load per core, busiest host of each tier"
                    leftTitle="runnable per core"
                    markers={markers}
                    series={TIERS.filter(([role]) => roles[role]).map(([role, plural]) => ({
                        label: roles[role].hosts > 1 ? `busiest of the ${plural}` : role,
                        color: PALETTE[TIERS.findIndex(([r]) => r === role) % PALETTE.length],
                        points: points(roles[role], 'load_max'),
                    }))}
                    note="The one-minute load average divided by the core count. Above 1 there is more runnable work than cores."
                />
            </Grid>
            <Panel flush title="By host, during the run">
                <Table horizontalOverflow="scroll">
                    <Table.Head>
                        <Table.HeadCell>Host</Table.HeadCell>
                        <Table.HeadCell>Tier</Table.HeadCell>
                        <Table.HeadCell align="right">Cores</Table.HeadCell>
                        <Table.HeadCell align="right">Memory</Table.HeadCell>
                        <Table.HeadCell align="right">CPU before</Table.HeadCell>
                        <Table.HeadCell align="right">CPU mean</Table.HeadCell>
                        <Table.HeadCell align="right">CPU peak</Table.HeadCell>
                        <Table.HeadCell align="right">Time at 90%+</Table.HeadCell>
                        <Table.HeadCell align="right">Mem peak</Table.HeadCell>
                        <Table.HeadCell align="right">Load/core peak</Table.HeadCell>
                        {anyIo && <Table.HeadCell align="right">Read MB/s</Table.HeadCell>}
                        {anyIo && <Table.HeadCell align="right">Disk ms/request</Table.HeadCell>}
                    </Table.Head>
                    <Table.Body>
                        {hosts.map((h) => {
                            const cpu = h.cpu || {};
                            const mem = h.mem || {};
                            const load = h.load || {};
                            const io = h.io || {};
                            return (
                                <Table.Row key={h.host}>
                                    <Table.Cell>
                                        <Host>{h.host}</Host>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <NoWrap>{h.role}</NoWrap>
                                    </Table.Cell>
                                    <Table.Cell align="right">{isBlank(h.cores) ? DASH : fmtInt(h.cores)}</Table.Cell>
                                    <Table.Cell align="right">{gb(h.mem_mb)}</Table.Cell>
                                    <Table.Cell align="right">{pct(cpu.before_pct)}</Table.Cell>
                                    <Table.Cell align="right">{pct(cpu.avg_pct)}</Table.Cell>
                                    <Table.Cell align="right">{pct(cpu.peak_pct)}</Table.Cell>
                                    <Table.Cell align="right">{cpu.hot_s ? fmtDur(cpu.hot_s) : DASH}</Table.Cell>
                                    <Table.Cell align="right">{pct(mem.peak_pct)}</Table.Cell>
                                    <Table.Cell align="right">{num(load.peak, 2)}</Table.Cell>
                                    {anyIo && <Table.Cell align="right">{num(io.read_mb_s, 1)}</Table.Cell>}
                                    {anyIo && (
                                        <Table.Cell align="right">
                                            {/* One element: a cell lays its children out as a
                                                flex row, which drops a bare space between them. */}
                                            <span>
                                                {num(io.wait_ms, 1)}
                                                {!isBlank(io.peak_wait_ms) && (
                                                    <Muted $small> peak {num(io.peak_wait_ms, 0)}</Muted>
                                                )}
                                            </span>
                                        </Table.Cell>
                                    )}
                                </Table.Row>
                            );
                        })}
                    </Table.Body>
                </Table>
                <Caption>
                    <Muted $small>
                        Read from the target&apos;s own _introspection after the run, in{' '}
                        {fmtInt((resources.self_load || {}).queries)} searches; nothing was sampled while the run
                        was going. Figures are over {fmtInt(resources.span_s)} s buckets wholly inside the run;
                        &quot;before&quot; is the three minutes ahead of it.
                        A host appears only if its _introspection is searchable from this search head.
                    </Muted>
                    {(resources.notes || []).map((note) => (
                        <Note key={note}>{note}</Note>
                    ))}
                </Caption>
            </Panel>
            {mounts.length > 0 && (
                <Panel flush title="Disks, slowest first">
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Host</Table.HeadCell>
                            <Table.HeadCell>Tier</Table.HeadCell>
                            <Table.HeadCell>Mount</Table.HeadCell>
                            <Table.HeadCell>Device</Table.HeadCell>
                            <Table.HeadCell align="right">ms per request, mean</Table.HeadCell>
                            <Table.HeadCell align="right">Peak</Table.HeadCell>
                            <Table.HeadCell align="right">Read MB/s</Table.HeadCell>
                            <Table.HeadCell align="right">Written MB/s</Table.HeadCell>
                            <Table.HeadCell align="right">Requests/s</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {mounts.slice(0, 12).map((m) => (
                                <Table.Row key={`${m.host}-${m.mount}`}>
                                    <Table.Cell>
                                        <Host>{m.host}</Host>
                                    </Table.Cell>
                                    <Table.Cell>{m.role}</Table.Cell>
                                    <Table.Cell>
                                        <Mono>{m.mount}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>
                                        {m.device || DASH}
                                        {m.fs_type ? ` (${m.fs_type})` : ''}
                                    </Table.Cell>
                                    <Table.Cell align="right">{num(m.avg_wait_ms, 1)}</Table.Cell>
                                    <Table.Cell align="right">{num(m.max_wait_ms, 0)}</Table.Cell>
                                    <Table.Cell align="right">{num(m.read_mb_s, 1)}</Table.Cell>
                                    <Table.Cell align="right">{num(m.write_mb_s, 1)}</Table.Cell>
                                    <Table.Cell align="right">{isBlank(m.iops) ? DASH : fmtInt(m.iops)}</Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                    <Caption>
                        <Muted $small>
                            Over the whole window the charts cover, not only the run.
                            {mounts.length > 12 ? ` The ${fmtInt(mounts.length - 12)} quicker ones are left out.` : ''}
                        </Muted>
                    </Caption>
                </Panel>
            )}
        </>
    );
}

HostResources.propTypes = { resources: PropTypes.object };
