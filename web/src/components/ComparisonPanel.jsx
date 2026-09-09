/*
 * The result of comparing this run against a baseline, and whether the gates
 * a pipeline would apply were met.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Chip from '@splunk/react-ui/Chip';
import Message from '@splunk/react-ui/Message';
import Table from '@splunk/react-ui/Table';

import { Mono, Note, Panel, SectionHeading } from './primitives';
import { DASH, fmtMs, fmtNum, isBlank } from '../format';

export default function ComparisonPanel({ comparison }) {
    if (!comparison) {
        return null;
    }
    const c = comparison;
    const gates = c.gates || [];
    const steps = (c.steps || [])
        .filter((s) => !isBlank(s.candidate_p95_ms) && !isBlank(s.baseline_p95_ms))
        .sort((a, b) => (b.delta_pct || 0) - (a.delta_pct || 0));

    return (
        <Panel title={`Comparison${c.baseline_run_id ? ` against run ${c.baseline_run_id}` : ''}`}>
            {c.blocked ? (
                <Message appearance="fill" type="error">
                    <Message.Title>Blocked</Message.Title>
                    {c.blocked}
                </Message>
            ) : gates.length ? (
                <Message appearance="fill" type={c.ok ? 'success' : 'error'}>
                    {c.ok ? 'Every gate met' : 'A gate was breached'}
                </Message>
            ) : (
                <Message appearance="fill" type="info">
                    <Message.Title>Report only</Message.Title>
                    No gates were given, so nothing was judged.
                </Message>
            )}

            {(c.warnings || []).map((w) => (
                <Note key={w}>{w}</Note>
            ))}

            {gates.length > 0 && (
                <Table>
                    <Table.Head>
                        <Table.HeadCell>Gate</Table.HeadCell>
                        <Table.HeadCell> </Table.HeadCell>
                        <Table.HeadCell>Detail</Table.HeadCell>
                    </Table.Head>
                    <Table.Body>
                        {gates.map((g) => (
                            <Table.Row key={g.gate}>
                                <Table.Cell>
                                    <Mono>{g.gate}</Mono>
                                </Table.Cell>
                                <Table.Cell>
                                    <Chip appearance={g.passed ? 'success' : 'error'}>
                                        {g.passed ? 'ok' : 'FAIL'}
                                    </Chip>
                                </Table.Cell>
                                <Table.Cell>{g.detail}</Table.Cell>
                            </Table.Row>
                        ))}
                    </Table.Body>
                </Table>
            )}

            {steps.length > 0 && (
                <>
                    <SectionHeading>Per step, p95</SectionHeading>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Step</Table.HeadCell>
                            <Table.HeadCell align="right">Baseline</Table.HeadCell>
                            <Table.HeadCell align="right">Candidate</Table.HeadCell>
                            <Table.HeadCell align="right">Delta</Table.HeadCell>
                            <Table.HeadCell> </Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {steps.map((s) => (
                                <Table.Row key={s.step_id}>
                                    <Table.Cell>
                                        <Mono>{s.step_id}</Mono>
                                    </Table.Cell>
                                    <Table.Cell align="right">{fmtMs(s.baseline_p95_ms)}</Table.Cell>
                                    <Table.Cell align="right">
                                        {fmtMs(s.candidate_p95_ms)}
                                    </Table.Cell>
                                    <Table.Cell align="right">
                                        {isBlank(s.delta_pct)
                                            ? DASH
                                            : `${s.delta_pct > 0 ? '+' : ''}${fmtNum(s.delta_pct, 1)}%`}
                                    </Table.Cell>
                                    <Table.Cell>
                                        {s.scanned_more && (
                                            <Chip appearance="warning">scanned more</Chip>
                                        )}{' '}
                                        {s.too_few_samples && (
                                            <Chip appearance="outline">few samples</Chip>
                                        )}
                                    </Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                </>
            )}
        </Panel>
    );
}

ComparisonPanel.propTypes = { comparison: PropTypes.object };
