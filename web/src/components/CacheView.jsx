/*
 * How a target's SmartStore cache is described, in the two places that
 * describe it: the cache dialog and the target report.
 */
import React from 'react';
import PropTypes from 'prop-types';
import DL from '@splunk/react-ui/DefinitionList';
import Message from '@splunk/react-ui/Message';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';

import { FillBar, Mono, Muted, SectionHeading } from './primitives';
import { fmtBytes, fmtInt, fmtPct, isBlank } from '../format';

/** The cache ceiling in bytes, or null when the instance has not set one. */
const ceilingBytes = (c) => (isBlank(c.max_cache_size_mb) ? null : c.max_cache_size_mb * 1024 * 1024);

/** One line, for a report or a table cell. */
export function CacheLine({ cache }) {
    if (!cache) {
        return <Muted>no cache information</Muted>;
    }
    if (cache.available === false) {
        return <Muted>SmartStore cache not reported: {cache.reason || 'no reason given'}</Muted>;
    }
    const ceiling = ceilingBytes(cache);
    return (
        <Mono>
            {fmtInt(cache.local_buckets)}/{fmtInt(cache.total_buckets)} buckets local (
            {fmtPct(cache.local_pct, 0)}), {fmtBytes(cache.local_bytes)} of{' '}
            {ceiling === null ? fmtBytes(cache.total_bytes) : fmtBytes(ceiling)},{' '}
            {fmtPct(cache.fill_pct, 0)} full, policy {cache.eviction_policy || 'unknown'}
        </Mono>
    );
}

CacheLine.propTypes = { cache: PropTypes.object };

/** The full breakdown. */
export default function CacheView({ cache }) {
    if (!cache) {
        return <P>No cache information was returned.</P>;
    }
    if (cache.available === false) {
        return (
            <Message appearance="fill" type="info">
                This target does not report a SmartStore cache: {cache.reason || 'no reason given'}.
                That is normal on an indexer with local storage only.
            </Message>
        );
    }
    const perIndex = Object.entries(cache.per_index || {});
    const ceiling = ceilingBytes(cache);

    return (
        <div>
            <DL termWidth="180px">
                <DL.Term>Local buckets</DL.Term>
                <DL.Description>{fmtInt(cache.local_buckets)}</DL.Description>
                <DL.Term>Remote buckets</DL.Term>
                <DL.Description>{fmtInt(cache.remote_buckets)}</DL.Description>
                <DL.Term>Total buckets</DL.Term>
                <DL.Description>
                    {fmtInt(cache.total_buckets)} ({fmtPct(cache.local_pct, 0)} local)
                </DL.Description>
                <DL.Term>Local bytes</DL.Term>
                <DL.Description>
                    {fmtBytes(cache.local_bytes)} of {fmtBytes(cache.total_bytes)}
                </DL.Description>
                <DL.Term>Cache ceiling</DL.Term>
                <DL.Description>{ceiling === null ? 'unset' : fmtBytes(ceiling)}</DL.Description>
                <DL.Term>Eviction policy</DL.Term>
                <DL.Description>{cache.eviction_policy || 'unknown'}</DL.Description>
            </DL>

            <SectionHeading>Fill against the ceiling: {fmtPct(cache.fill_pct, 0)}</SectionHeading>
            <FillBar pct={cache.fill_pct} />
            {cache.fill_pct >= 95 && (
                <Message appearance="fill" type="warning">
                    A cache at its ceiling evicts buckets other searches are using, so a run
                    measures churn as much as it measures search.
                </Message>
            )}

            <SectionHeading>Per index</SectionHeading>
            {perIndex.length ? (
                <Table>
                    <Table.Head>
                        <Table.HeadCell>Index</Table.HeadCell>
                        <Table.HeadCell align="right">Local</Table.HeadCell>
                        <Table.HeadCell align="right">Remote</Table.HeadCell>
                        <Table.HeadCell align="right">Local %</Table.HeadCell>
                        <Table.HeadCell align="right">Local bytes</Table.HeadCell>
                    </Table.Head>
                    <Table.Body>
                        {perIndex.map(([name, v]) => (
                            <Table.Row key={name}>
                                <Table.Cell>
                                    <Mono>{name}</Mono>
                                </Table.Cell>
                                <Table.Cell align="right">{fmtInt(v.local_buckets)}</Table.Cell>
                                <Table.Cell align="right">{fmtInt(v.remote_buckets)}</Table.Cell>
                                <Table.Cell align="right">{fmtPct(v.local_pct, 0)}</Table.Cell>
                                <Table.Cell align="right">{fmtBytes(v.local_bytes)}</Table.Cell>
                            </Table.Row>
                        ))}
                    </Table.Body>
                </Table>
            ) : (
                <Muted>No per-index breakdown was returned.</Muted>
            )}
        </div>
    );
}

CacheView.propTypes = { cache: PropTypes.object };
