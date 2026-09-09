/*
 * Saved searches as Regulator sees them: what would take part in a scenario
 * built from this savedsearches.conf, and why the rest would not.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Chip from '@splunk/react-ui/Chip';
import Table from '@splunk/react-ui/Table';
import Tooltip from '@splunk/react-ui/Tooltip';

import { Mono, Muted } from './primitives';
import { DASH, fmtNum, isBlank } from '../format';

export default function SavedSearchTable({ rows }) {
    if (!rows || !rows.length) {
        return <Muted>No saved searches were returned.</Muted>;
    }
    return (
        <Table horizontalOverflow="scroll">
            <Table.Head>
                <Table.HeadCell>Name</Table.HeadCell>
                <Table.HeadCell>App</Table.HeadCell>
                <Table.HeadCell>Cron</Table.HeadCell>
                <Table.HeadCell align="right">Fires/day</Table.HeadCell>
                <Table.HeadCell>Class</Table.HeadCell>
                <Table.HeadCell>Range</Table.HeadCell>
                <Table.HeadCell>Would be</Table.HeadCell>
            </Table.Head>
            <Table.Body>
                {rows.map((r) => (
                    <Table.Row key={`${r.app || ''}:${r.name}`}>
                        <Table.Cell>
                            <Tooltip content={r.search} defaultPlacement="right">
                                <Mono>{r.name}</Mono>
                            </Tooltip>
                        </Table.Cell>
                        <Table.Cell>
                            <Mono>{r.app || ''}</Mono>
                        </Table.Cell>
                        <Table.Cell>
                            <Mono>{r.cron || ''}</Mono>
                        </Table.Cell>
                        <Table.Cell align="right">
                            {isBlank(r.firings_per_day) ? DASH : fmtNum(r.firings_per_day, 2)}
                        </Table.Cell>
                        <Table.Cell>
                            <Chip appearance="outline">{r.guessed_class}</Chip>
                        </Table.Cell>
                        <Table.Cell>
                            <Mono>
                                {r.earliest || ''} → {r.latest || 'now'}
                            </Mono>
                        </Table.Cell>
                        <Table.Cell>
                            {r.skipped_reason ? (
                                <>
                                    <Chip appearance="warning">left out</Chip>{' '}
                                    <Muted $small>{r.skipped_reason}</Muted>
                                </>
                            ) : (
                                <Chip appearance="success">included</Chip>
                            )}
                        </Table.Cell>
                    </Table.Row>
                ))}
            </Table.Body>
        </Table>
    );
}

SavedSearchTable.propTypes = { rows: PropTypes.array };
