/*
 * Two views of a run that bring together numbers it already collects.
 *
 * "Search issues" answers "what went wrong, and where?": partial results,
 * searches that waited in QUEUED, unknown-sid responses absorbed inside the
 * grace window and errors by class, each on the step it happened to, with the
 * scheduled searches the target skipped or deferred meanwhile. These used to
 * be a banner here, a tile there and a probe row to expand.
 *
 * "Bucket attribution" turns "the run was partly cold" into "this step spent
 * 41% of its run time waiting for 212 bucket downloads", from the target's own
 * audit records split by step.
 *
 * Neither is split by search head cluster member: which member served a
 * request behind a load balancer is not something the run can tell yet.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Table from '@splunk/react-ui/Table';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import { Mono, Muted, Note, Panel } from './primitives';
import { DASH, fmtInt, fmtMs, fmtNum, fmtPct, isBlank } from '../format';

// The classes the worker assigns, in the order an operator triages them;
// anything else (client-side failures, a class from a newer worker) is Other.
const ERROR_CLASSES = ['timeout', 'quota', 'auth', 'server', 'parse'];
const CLASS_LABELS = { timeout: 'Timeout', quota: 'Quota', auth: 'Auth', server: 'Server', parse: 'Parse' };

// What sut.py calls an audit record whose search string lost the run marker.
const UNATTRIBUTED = '(step not in the audited search)';
// oneshot returns at most this many rows per probe.
const PROBE_ROW_LIMIT = 50;

// A table sits flush in its panel; the words under it need the panel's inset.
const Caption = styled.div`
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
`;

const num = (v) => {
    if (v === null || v === undefined || v === '') {
        return null;
    }
    const n = Number(v);
    return Number.isNaN(n) ? null : n;
};

// Every error that is not one of the named classes. Taken from the error
// total, so an error with no class recorded still shows up somewhere.
const otherErrors = (errors, byClass) =>
    Math.max(0, (errors || 0) - ERROR_CLASSES.reduce((sum, c) => sum + (Number((byClass || {})[c]) || 0), 0));

// A zero is shown as a dash: the table is read for where something happened.
const count = (v) => (isBlank(v) ? DASH : v > 0 ? fmtInt(v) : DASH);

function hasIssue(step) {
    return (
        (step.partial || 0) > 0 ||
        (step.queued || 0) > 0 ||
        ((step.unknown_sid || {}).responses || 0) > 0 ||
        (step.errors || 0) > 0
    );
}

function IssueRow({ label, execs, partial, queued, queuedP95, unknown, errors, byClass, strong }) {
    const name = strong ? <strong>{label}</strong> : <Mono>{label}</Mono>;
    return (
        <Table.Row>
            <Table.Cell>{name}</Table.Cell>
            <Table.Cell align="right">{fmtInt(execs)}</Table.Cell>
            <Table.Cell align="right">{count(partial)}</Table.Cell>
            <Table.Cell align="right">
                {/* One element: a table cell lays its children out as a flex
                    row, which drops a bare space between them. */}
                <span>
                    {count(queued)}
                    {queued > 0 && !isBlank(queuedP95) && <Muted $small> p95 {fmtMs(queuedP95)}</Muted>}
                </span>
            </Table.Cell>
            <Table.Cell align="right">
                {unknown === undefined
                    ? DASH
                    : (unknown.responses || 0) > 0
                      ? `${fmtInt(unknown.searches)} / ${fmtInt(unknown.responses)}`
                      : DASH}
            </Table.Cell>
            {ERROR_CLASSES.map((c) => (
                <Table.Cell key={c} align="right">
                    {count((byClass || {})[c] || 0)}
                </Table.Cell>
            ))}
            <Table.Cell align="right">{count(otherErrors(errors, byClass))}</Table.Cell>
        </Table.Row>
    );
}

IssueRow.propTypes = {
    label: PropTypes.string.isRequired,
    execs: PropTypes.number,
    partial: PropTypes.number,
    queued: PropTypes.number,
    queuedP95: PropTypes.number,
    unknown: PropTypes.shape({ searches: PropTypes.number, responses: PropTypes.number }),
    errors: PropTypes.number,
    byClass: PropTypes.object,
    strong: PropTypes.bool,
};

function SchedulerLine({ probe }) {
    if (!probe) {
        return null;
    }
    if (!probe.available) {
        return (
            <Muted $small>
                Scheduled searches skipped or deferred on the target were not read
                {probe.reason ? `: ${probe.reason}` : '.'}
            </Muted>
        );
    }
    const rows = probe.rows || [];
    if (!rows.length) {
        return (
            <Muted $small>
                No scheduled searches were skipped or deferred on the target during the run.
            </Muted>
        );
    }
    return (
        <Note>
            <strong>Scheduled searches on the target during the run:</strong>
            <ul>
                {rows.map((r) => (
                    <li key={`${r.status}:${r.reason}`}>
                        {fmtInt(num(r.count))} {r.status}
                        {r.reason ? `: ${r.reason}` : ''}
                    </li>
                ))}
            </ul>
        </Note>
    );
}

SchedulerLine.propTypes = { probe: PropTypes.object };

function IssuesPanel({ stats, sut }) {
    const steps = stats.steps || [];
    if (!steps.length) {
        return null;
    }
    const withIssues = steps.filter(hasIssue);
    // A run recorded before the per-step split has the run totals only.
    const split = steps.some((s) => s.queued !== undefined);
    const queueing = stats.queueing || {};
    const scheduler = sut && sut.probes ? sut.probes.scheduler : null;
    const totalIssues =
        (stats.partial || 0) +
        (queueing.searches_queued || 0) +
        ((stats.unknown_sid || {}).responses || 0) +
        (stats.errors || 0);

    return (
        <Panel title="Search issues" flush>
            {totalIssues > 0 ? (
                <>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Step</Table.HeadCell>
                            <Table.HeadCell align="right">Execs</Table.HeadCell>
                            <Table.HeadCell
                                align="right"
                                tooltip="Finished DONE but reported incomplete work: a peer dropped out or a limit truncated the results."
                            >
                                Partial
                            </Table.HeadCell>
                            <Table.HeadCell
                                align="right"
                                tooltip="Waited in QUEUED before running, with the p95 of that wait."
                            >
                                Queued
                            </Table.HeadCell>
                            <Table.HeadCell
                                align="right"
                                tooltip="Searches that met a search head cluster member which did not know the job yet, and how many 404s that was in all. Absorbed inside the grace window; the wait is in the latency."
                            >
                                Unknown sid
                            </Table.HeadCell>
                            {ERROR_CLASSES.map((c) => (
                                <Table.HeadCell key={c} align="right">
                                    {CLASS_LABELS[c]}
                                </Table.HeadCell>
                            ))}
                            <Table.HeadCell align="right" tooltip="Errors of any other class, client-side failures among them.">
                                Other
                            </Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {withIssues.map((s) => (
                                <IssueRow
                                    key={s.step_id}
                                    label={s.step_id}
                                    execs={s.executions}
                                    partial={s.partial}
                                    queued={s.queued}
                                    queuedP95={s.queued_p95_ms}
                                    unknown={s.unknown_sid}
                                    errors={s.errors}
                                    byClass={s.errors_by_class}
                                />
                            ))}
                            <IssueRow
                                strong
                                label="Whole run"
                                execs={stats.executions}
                                partial={stats.partial || 0}
                                queued={queueing.searches_queued || 0}
                                queuedP95={(queueing.queued_ms || {}).p95_ms}
                                unknown={stats.unknown_sid || { searches: 0, responses: 0 }}
                                errors={stats.errors}
                                byClass={stats.errors_by_class}
                            />
                        </Table.Body>
                    </Table>
                    <Caption>
                        <Muted $small>
                            Steps with nothing to report are left out. Unknown sid reads as
                            searches affected / 404 responses.
                            {!split &&
                                ' This run was recorded before queueing and unknown-sid responses were counted per step, so those two are shown for the whole run only.'}
                        </Muted>
                    </Caption>
                </>
            ) : (
                <Caption>
                    <Muted>
                        Nothing to report: no partial results, no queueing, no unknown-sid
                        responses and no errors on any step.
                    </Muted>
                </Caption>
            )}
            {scheduler && (
                <Caption>
                    <SchedulerLine probe={scheduler} />
                </Caption>
            )}
        </Panel>
    );
}

IssuesPanel.propTypes = { stats: PropTypes.object.isRequired, sut: PropTypes.object };

function AttributionPanel({ sut }) {
    const probe = sut && sut.probes ? sut.probes.our_searches_by_step : null;
    // Absent on a run recorded before the probe existed: say nothing.
    if (!probe) {
        return null;
    }
    const rows = probe.rows || [];
    return (
        <Panel title="Bucket attribution by step" flush>
            {!probe.available ? (
                <Caption>
                    <Muted>Not read from the audit trail{probe.reason ? `: ${probe.reason}` : '.'}</Muted>
                </Caption>
            ) : !rows.length ? (
                <Caption>
                    <Muted>The audit trail held no completed searches for this run when it was read.</Muted>
                </Caption>
            ) : (
                <>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Step</Table.HeadCell>
                            <Table.HeadCell align="right">Searches</Table.HeadCell>
                            <Table.HeadCell align="right" tooltip="Buckets the searches opened.">
                                Buckets searched
                            </Table.HeadCell>
                            <Table.HeadCell
                                align="right"
                                tooltip="Buckets ruled out without being opened, by time range or bloom filter."
                            >
                                Eliminated
                            </Table.HeadCell>
                            <Table.HeadCell align="right">Cache hits</Table.HeadCell>
                            <Table.HeadCell align="right">Cache misses</Table.HeadCell>
                            <Table.HeadCell align="right">Miss %</Table.HeadCell>
                            <Table.HeadCell
                                align="right"
                                tooltip="Seconds the step's searches spent waiting on bucket-cache misses, index files and raw data together."
                            >
                                Waiting on misses
                            </Table.HeadCell>
                            <Table.HeadCell align="right" tooltip="The searches' total run time, by the target's account.">
                                Run time
                            </Table.HeadCell>
                            <Table.HeadCell align="right">Share waiting</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {rows.map((r) => (
                                <Table.Row key={r.step}>
                                    <Table.Cell>
                                        {r.step === UNATTRIBUTED ? <Muted>{r.step}</Muted> : <Mono>{r.step}</Mono>}
                                    </Table.Cell>
                                    <Table.Cell align="right">{fmtInt(num(r.searches))}</Table.Cell>
                                    <Table.Cell align="right">{fmtInt(num(r.searched_buckets))}</Table.Cell>
                                    <Table.Cell align="right">{fmtInt(num(r.eliminated_buckets))}</Table.Cell>
                                    <Table.Cell align="right">{fmtInt(num(r.cache_hits))}</Table.Cell>
                                    <Table.Cell align="right">{fmtInt(num(r.cache_misses))}</Table.Cell>
                                    <Table.Cell align="right">{fmtPct(num(r.cache_miss_pct))}</Table.Cell>
                                    <Table.Cell align="right">{fmtNum(num(r.cold_path_s), 1)} s</Table.Cell>
                                    <Table.Cell align="right">{fmtNum(num(r.run_time_s), 1)} s</Table.Cell>
                                    <Table.Cell align="right">{fmtPct(num(r.cold_path_pct), 0)}</Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                    <Caption>
                        <Muted $small>
                            From the target&apos;s audit trail, split by the step each search names
                            in its run marker. Audit records keep arriving for a minute or two
                            after a run, so these counts are a floor. Waiting is added up across
                            indexers, so a step&apos;s share can pass 100%.
                            {rows.length >= PROBE_ROW_LIMIT &&
                                ` Showing the ${PROBE_ROW_LIMIT} steps that waited longest.`}
                        </Muted>
                    </Caption>
                </>
            )}
        </Panel>
    );
}

AttributionPanel.propTypes = { sut: PropTypes.object };

export default function SearchIssues({ stats, sut }) {
    return (
        <>
            <IssuesPanel stats={stats || {}} sut={sut} />
            <AttributionPanel sut={sut} />
        </>
    );
}

SearchIssues.propTypes = {
    stats: PropTypes.object,
    sut: PropTypes.object,
};
