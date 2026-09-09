/*
 * A fleet run's workers: who holds which slot, what it has done, when it last
 * spoke. A slot that went quiet is the first thing to look at when a fleet run
 * is marked invalid.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Chip from '@splunk/react-ui/Chip';
import Table from '@splunk/react-ui/Table';

import BusyButton from './BusyButton';
import { Mono, Muted, Panel, RelTime, StatePill, Toolbar } from './primitives';
import { fmtInt, fmtMs } from '../format';

export default function WorkersSection({ run, stats, summary, onLogs }) {
    if (!run.fleet || run.fleet === 'inprocess') {
        return null;
    }
    const live = Array.isArray(stats.workers) ? stats.workers : [];
    const final = Array.isArray(summary.workers) ? summary.workers : [];
    const workers = live.length ? live : final;

    return (
        <Panel
            title={`Workers (${run.fleet}${run.fleet_state ? `, ${run.fleet_state}` : ''})`}
            actions={
                <Toolbar>
                    <BusyButton label="Worker logs" onClick={onLogs} />
                </Toolbar>
            }
        >
            {stats.latency_note && <Muted $small>{stats.latency_note}</Muted>}
            {workers.length ? (
                <Table horizontalOverflow="scroll">
                    <Table.Head>
                        <Table.HeadCell align="right">Slot</Table.HeadCell>
                        <Table.HeadCell>Engine</Table.HeadCell>
                        <Table.HeadCell>State</Table.HeadCell>
                        <Table.HeadCell>Holder</Table.HeadCell>
                        <Table.HeadCell align="right">Execs</Table.HeadCell>
                        <Table.HeadCell align="right">Errors</Table.HeadCell>
                        <Table.HeadCell align="right">p95</Table.HeadCell>
                        <Table.HeadCell>Last heartbeat</Table.HeadCell>
                    </Table.Head>
                    <Table.Body>
                        {workers.map((w) => (
                            <Table.Row key={w.slot}>
                                <Table.Cell align="right">{w.slot}</Table.Cell>
                                <Table.Cell>
                                    <Mono>{w.engine || ''}</Mono>
                                </Table.Cell>
                                <Table.Cell>
                                    <StatePill state={w.state || w.outcome || ''} />
                                </Table.Cell>
                                <Table.Cell>
                                    <Mono>{w.holder || ''}</Mono>
                                </Table.Cell>
                                <Table.Cell align="right">{fmtInt(w.executions)}</Table.Cell>
                                <Table.Cell align="right">{fmtInt(w.errors)}</Table.Cell>
                                <Table.Cell align="right">{fmtMs(w.p95_ms)}</Table.Cell>
                                <Table.Cell>
                                    {w.last_heartbeat_at ? <RelTime value={w.last_heartbeat_at} /> : null}{' '}
                                    {w.restarts ? (
                                        <Chip appearance="warning">{w.restarts} restart(s)</Chip>
                                    ) : null}
                                </Table.Cell>
                            </Table.Row>
                        ))}
                    </Table.Body>
                </Table>
            ) : (
                <Muted>No worker has claimed a slot yet.</Muted>
            )}
        </Panel>
    );
}

WorkersSection.propTypes = {
    run: PropTypes.object.isRequired,
    stats: PropTypes.object.isRequired,
    summary: PropTypes.object.isRequired,
    onLogs: PropTypes.func.isRequired,
};
