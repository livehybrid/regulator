/*
 * What the cluster itself said about the run.
 *
 * This is the half no client-side timing can produce: whether the time went to
 * the indexers, whether somebody else's scheduled searches were skipped to
 * make room, and how hot the box got.
 *
 * The probe result used to read "5 row(s)", which tells you a probe matched
 * but not WHAT it matched, which is the whole point of running it. The count
 * is now a row you can expand to see the rows themselves.
 */
import React from 'react';
import PropTypes from 'prop-types';
import List from '@splunk/react-ui/List';
import Table from '@splunk/react-ui/Table';

import { Mono, Muted, Panel } from './primitives';
import { fmtInt } from '../format';

const MAX_ROWS = 200;

function ProbeRows({ rows }) {
    const cols = [];
    rows.forEach((r) =>
        Object.keys(r || {}).forEach((k) => {
            if (cols.indexOf(k) < 0) {
                cols.push(k);
            }
        })
    );
    return (
        <>
            <Table horizontalOverflow="scroll">
                <Table.Head>
                    {cols.map((c) => (
                        <Table.HeadCell key={c}>{c}</Table.HeadCell>
                    ))}
                </Table.Head>
                <Table.Body>
                    {rows.slice(0, MAX_ROWS).map((r, i) => (
                        // A probe returns raw search results, which have no
                        // identity of their own. The list is rendered once and
                        // never reordered, so the position is the key.
                        // eslint-disable-next-line react/no-array-index-key
                        <Table.Row key={i}>
                            {cols.map((c) => {
                                const v = (r || {})[c];
                                const text =
                                    v === null || v === undefined
                                        ? ''
                                        : typeof v === 'object'
                                          ? JSON.stringify(v)
                                          : String(v);
                                return (
                                    <Table.Cell key={c}>
                                        <Mono>{text}</Mono>
                                    </Table.Cell>
                                );
                            })}
                        </Table.Row>
                    ))}
                </Table.Body>
            </Table>
            {rows.length > MAX_ROWS && (
                <Muted $small>
                    Showing the first {MAX_ROWS} of {fmtInt(rows.length)} rows.
                </Muted>
            )}
        </>
    );
}

ProbeRows.propTypes = { rows: PropTypes.array.isRequired };

export default function SutSection({ sut }) {
    if (!sut) {
        return null;
    }
    const findings = sut.findings || [];
    const probes = Object.entries(sut.probes || {});

    return (
        <Panel title="What the cluster said">
            {findings.length ? (
                <List>
                    {findings.map((f) => (
                        <List.Item key={f}>{f}</List.Item>
                    ))}
                </List>
            ) : (
                <Muted>No server-side correlation was available.</Muted>
            )}

            {probes.length > 0 && (
                <Table rowExpansion="multi">
                    <Table.Head>
                        <Table.HeadCell>Probe</Table.HeadCell>
                        <Table.HeadCell>Result</Table.HeadCell>
                        <Table.HeadCell>Detail</Table.HeadCell>
                    </Table.Head>
                    <Table.Body>
                        {probes.map(([name, p]) => {
                            const has = p.available && p.rows && p.rows.length;
                            const state = p.available
                                ? has
                                    ? `${fmtInt(p.rows.length)} row(s)`
                                    : 'no matching events'
                                : 'unavailable';
                            return (
                                <Table.Row
                                    key={name}
                                    expansionRow={
                                        has ? (
                                            <Table.Row key={`${name}-rows`}>
                                                <Table.Cell colSpan={3}>
                                                    <ProbeRows rows={p.rows} />
                                                </Table.Cell>
                                            </Table.Row>
                                        ) : undefined
                                    }
                                >
                                    <Table.Cell>
                                        <Mono>{name}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>{state}</Table.Cell>
                                    <Table.Cell>
                                        <Muted $small>
                                            {p.available ? p.description || '' : p.reason || ''}
                                        </Muted>
                                    </Table.Cell>
                                </Table.Row>
                            );
                        })}
                    </Table.Body>
                </Table>
            )}

            <Muted $small>
                These searches ran on the target after the load stopped, so they add a small, known
                amount of work to the system being measured.
            </Muted>
        </Panel>
    );
}

SutSection.propTypes = { sut: PropTypes.object };
