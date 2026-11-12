/*
 * How stale is what a search returns: the ingest-to-searchable probe.
 *
 * Regulator sent a small event to the target over HEC every few seconds and
 * searched for it, so the time from sending to searchable is read from one
 * clock and carries no clock skew. The split into "indexing" and "searchable
 * once indexed" uses _indextime, the indexer's clock, so it does, and to the
 * second: it is shown as a split, never as the headline.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Table from '@splunk/react-ui/Table';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import LineChart from './LineChart';
import { Mono, Muted, Note, Panel, SectionHeading } from './primitives';
import { DASH, fmtInt, isBlank } from '../format';

const Findings = styled.ul`
    margin: 0;
    padding-left: ${variables.spacingLarge};
`;

const Caption = styled.div`
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
`;

const secs = (v) => (isBlank(v) ? DASH : `${Number(v).toFixed(1)} s`);

function Row({ label, dist, note }) {
    const d = dist || {};
    return (
        <Table.Row>
            <Table.Cell>
                {/* One element: a cell lays its children out as a flex row. */}
                <span>
                    {label}
                    {note && <Muted $small> {note}</Muted>}
                </span>
            </Table.Cell>
            <Table.Cell align="right">{d.count ? fmtInt(d.count) : DASH}</Table.Cell>
            <Table.Cell align="right">{secs(d.p50_s)}</Table.Cell>
            <Table.Cell align="right">{secs(d.p95_s)}</Table.Cell>
            <Table.Cell align="right">{secs(d.p99_s)}</Table.Cell>
            <Table.Cell align="right">{secs(d.max_s)}</Table.Cell>
        </Table.Row>
    );
}

Row.propTypes = { label: PropTypes.string.isRequired, dist: PropTypes.object, note: PropTypes.string };

export default function Freshness({ freshness, run }) {
    if (!freshness) {
        return null;
    }
    const findings = freshness.findings || [];
    if (!freshness.available) {
        return (
            <>
                <SectionHeading>Ingest to searchable</SectionHeading>
                <Note>
                    {findings[0] || `Not measured: ${freshness.reason || 'no probe event was accepted'}.`}
                </Note>
            </>
        );
    }
    const points = freshness.points || [];
    const markers = [
        { at: run.started_at, label: 'the run started', kind: 'run' },
        { at: run.ended_at, label: 'the run ended', kind: 'run' },
    ].filter((m) => !isBlank(m.at));
    const skew = freshness.skew_evident;
    return (
        <>
            <SectionHeading>Ingest to searchable</SectionHeading>
            <Panel>
                <Findings>
                    {findings.map((line) => (
                        <li key={line}>{line}</li>
                    ))}
                </Findings>
            </Panel>
            <LineChart
                title="Seconds from sending an event to finding it in a search"
                leftTitle="seconds"
                markers={markers}
                series={[
                    { label: 'sent to searchable', points: points.map((p) => [p[0], p[1]]) },
                    ...(skew ? [] : [{ label: 'sent to indexed', dashed: true, points: points.map((p) => [p[0], p[2]]) }]),
                ]}
                note={`One point per probe, placed at the time it was sent. Each is known to within about ${secs(
                    freshness.resolution_s
                )}: the gap between the last search that missed it and the first that found it.`}
            />
            <Panel flush title="Over the probes sent during the run">
                <Table>
                    <Table.Head>
                        <Table.HeadCell>Measure</Table.HeadCell>
                        <Table.HeadCell align="right">Probes</Table.HeadCell>
                        <Table.HeadCell align="right">p50</Table.HeadCell>
                        <Table.HeadCell align="right">p95</Table.HeadCell>
                        <Table.HeadCell align="right">p99</Table.HeadCell>
                        <Table.HeadCell align="right">Max</Table.HeadCell>
                    </Table.Head>
                    <Table.Body>
                        <Row label="Sent to searchable" dist={freshness.latency} note="one clock" />
                        {(freshness.before_run || {}).count ? (
                            <Row label="The same, before the load started" dist={freshness.before_run} />
                        ) : null}
                        {!skew && <Row label="Sent to indexed" dist={freshness.index_delay} note="two clocks, to the second" />}
                        {!skew && (
                            <Row label="Indexed to searchable" dist={freshness.after_index} note="two clocks, to the second" />
                        )}
                    </Table.Body>
                </Table>
                <Caption>
                    <Muted $small>
                        {fmtInt(freshness.sent)} probe events to index <Mono>{freshness.index}</Mono> every{' '}
                        {fmtInt(freshness.interval_s)} s, {fmtInt(freshness.found)} of {fmtInt(freshness.during_run)}{' '}
                        sent during the run found, in {fmtInt(freshness.searches)} searches (one every{' '}
                        {fmtInt(freshness.poll_s)} s). Search for them with{' '}
                        <Mono>
                            index={freshness.index} sourcetype=&quot;{freshness.sourcetype}&quot; source=&quot;
                            {freshness.source}&quot;
                        </Mono>
                        .
                    </Muted>
                    {(freshness.errors || []).map((e) => (
                        <Note key={e}>{e}</Note>
                    ))}
                </Caption>
            </Panel>
        </>
    );
}

Freshness.propTypes = { freshness: PropTypes.object, run: PropTypes.object.isRequired };
