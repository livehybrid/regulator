/*
 * The bucket map before and after a run, on the same rows and the same axis.
 *
 * Built by the control plane from the two cache manager readings every run
 * already takes, at its start (after any warm-up or eviction) and at its end,
 * so drawing it cost the target nothing more and nothing was read during the
 * run. That is also why it shows local or remote only: the dbinspect detail
 * the forecast reads at the start describes one side, and refining one side
 * and not the other would make the pair disagree about buckets that did not
 * change.
 */
import React from 'react';
import PropTypes from 'prop-types';
import styled from 'styled-components';
import Table from '@splunk/react-ui/Table';
import { variables } from '@splunk/themes';

import { BucketMapView } from './BucketMap';
import { Mono, Muted, Note, Panel } from './primitives';
import { fmtBytes, fmtInt, fmtPct } from '../format';

const Side = styled.div`
    & + & {
        margin-top: ${variables.spacingLarge};
    }
`;

const SideTitle = styled.div`
    font-weight: ${variables.fontWeightSemiBold};
    margin-bottom: ${variables.spacingSmall};
`;

const TableWrap = styled.div`
    margin-top: ${variables.spacingMedium};
`;

const SOURCES = {
    forecast: 'The indexes this run reads, from its forecast, largest first.',
    changed: 'No forecast was taken, so these are the indexes whose local bucket count changed.',
    largest: 'No forecast was taken and nothing changed, so these are the largest indexes.',
};

const localBytes = (row) =>
    row ? Object.entries(row.by_state || {}).reduce((sum, [k, v]) => (k === 'remote' ? sum : sum + v.bytes), 0) : 0;

export default function RunBucketMap({ cache }) {
    const pair = cache && cache.map;
    if (!pair) {
        return null;
    }
    if (!pair.available) {
        return (
            <Panel title="Bucket map, before and after">
                <Note>The bucket map could not be built: {pair.reason}</Note>
            </Panel>
        );
    }
    const { before, after } = pair;
    const spans = pair.overlay || [];
    const afterByIndex = {};
    ((after && after.indexes) || []).forEach((row) => {
        afterByIndex[row.index] = row;
    });
    const beforeByIndex = {};
    ((before && before.indexes) || []).forEach((row) => {
        beforeByIndex[row.index] = row;
    });
    const names = Array.from(
        new Set([...((before && before.indexes) || []), ...((after && after.indexes) || [])].map((r) => r.index))
    );
    const draw = (doc, title) =>
        doc && doc.available ? (
            <Side>
                <SideTitle>{title}</SideTitle>
                <BucketMapView
                    map={doc}
                    spans={spans}
                    spansLabel="Read by this run"
                    notes={doc.notes || []}
                    table={false}
                    caption="Times are UTC. Local or remote, from the cache manager."
                />
            </Side>
        ) : (
            <Note>
                {title}: {(doc && doc.reason) || 'not read'}
            </Note>
        );

    return (
        <Panel title="Bucket map, before and after">
            <Muted $small>
                {SOURCES[pair.source] || ''} Taken from the cache readings at the start of the load and at
                its end; nothing more was read from the target to draw it.
            </Muted>
            {(pair.notes || []).map((n) => (
                <Note key={n}>{n}</Note>
            ))}
            {draw(before, 'Before the run')}
            {draw(after, 'After the run')}
            <TableWrap>
                <Table horizontalOverflow="scroll">
                    <Table.Head>
                        <Table.HeadCell>Index</Table.HeadCell>
                        <Table.HeadCell align="right">Buckets</Table.HeadCell>
                        <Table.HeadCell align="right">Local before</Table.HeadCell>
                        <Table.HeadCell align="right">Local after</Table.HeadCell>
                        <Table.HeadCell align="right">Local bytes, change</Table.HeadCell>
                    </Table.Head>
                    <Table.Body>
                        {names.map((name) => {
                            const b = beforeByIndex[name];
                            const a = afterByIndex[name];
                            const change = localBytes(a) - localBytes(b);
                            return (
                                <Table.Row key={name}>
                                    <Table.Cell>
                                        <Mono>{name}</Mono>
                                    </Table.Cell>
                                    <Table.Cell align="right">{fmtInt((a || b).buckets)}</Table.Cell>
                                    <Table.Cell align="right">{b ? fmtPct(b.local_pct, 0) : '-'}</Table.Cell>
                                    <Table.Cell align="right">{a ? fmtPct(a.local_pct, 0) : '-'}</Table.Cell>
                                    <Table.Cell align="right">
                                        {change === 0 ? 'none' : `${change > 0 ? '+' : '-'}${fmtBytes(Math.abs(change))}`}
                                    </Table.Cell>
                                </Table.Row>
                            );
                        })}
                    </Table.Body>
                </Table>
            </TableWrap>
        </Panel>
    );
}

RunBucketMap.propTypes = {
    cache: PropTypes.object,
};

RunBucketMap.defaultProps = {
    cache: null,
};
