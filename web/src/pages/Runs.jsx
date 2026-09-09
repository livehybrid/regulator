/*
 * The run history. The sparkline column is p95 over the run, which is what
 * tells you whether a run degraded late rather than being slow throughout: a
 * single p95 cannot say that.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Chip from '@splunk/react-ui/Chip';
import Heading from '@splunk/react-ui/Heading';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import Tooltip from '@splunk/react-ui/Tooltip';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import useLoader from '../useLoader';
import { Sparkline } from '../components/LineChart';
import { Mono, Muted, PageHead, Panel, RelTime, StatePill } from '../components/primitives';
import { api, isAuthError } from '../api';
import { fmtInt, fmtMs, fmtPct } from '../format';

export default function Runs({ navigate, onLaunch }) {
    const { data, error, loading } = useLoader(() => api('/runs'), []);
    const runs = data || [];
    const [sparks, setSparks] = useState({});

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
                <Button
                    appearance="primary"
                    label="Launch a run"
                    onClick={() => onLaunch({ scenario: null })}
                />
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
                            <Table.HeadCell>Label</Table.HeadCell>
                            <Table.HeadCell>Scenario</Table.HeadCell>
                            <Table.HeadCell>Target</Table.HeadCell>
                            <Table.HeadCell>State</Table.HeadCell>
                            <Table.HeadCell>Started</Table.HeadCell>
                            <Table.HeadCell align="right">Executions</Table.HeadCell>
                            <Table.HeadCell align="right">p95</Table.HeadCell>
                            <Table.HeadCell>p95 over the run</Table.HeadCell>
                            <Table.HeadCell align="right">Errors</Table.HeadCell>
                            <Table.HeadCell align="right">Queued</Table.HeadCell>
                            <Table.HeadCell>Validity</Table.HeadCell>
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
                                        <Table.Cell>{r.label || '(no label)'}</Table.Cell>
                                        <Table.Cell>
                                            <Mono>{r.scenario || ''}</Mono>
                                        </Table.Cell>
                                        <Table.Cell>
                                            {r.target_name || r.target_id || '(deleted)'}
                                        </Table.Cell>
                                        <Table.Cell>
                                            <StatePill state={r.state} />
                                        </Table.Cell>
                                        <Table.Cell>
                                            <RelTime value={r.created_at} />
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtInt(h.executions)}</Table.Cell>
                                        <Table.Cell align="right">{fmtMs(h.p95_ms)}</Table.Cell>
                                        <Table.Cell>
                                            <Sparkline values={sparks[r.id]} />
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtPct(h.error_rate_pct)}
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtInt(h.searches_queued)}
                                        </Table.Cell>
                                        <Table.Cell>
                                            <Validity run={r} headline={h} />
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
