/*
 * The searches of a run worth opening one at a time, and why they went wrong.
 *
 * A run's aggregates say how many searches failed. This is the list of which:
 * every search that failed, was cut off by the end of the run or came back
 * partial (a bounded sample when there are many), and the slowest that
 * succeeded. Each row opens to what the job said about itself, which is most
 * of what Splunk's Job Inspector shows: counts, where the time went by phase,
 * and what each indexer contributed.
 *
 * Diagnose then asks the target about those search ids: what its audit trail
 * recorded for each, what each indexer did for it, and what splunkd logged
 * about it. It is offered only once the run is over, because those are
 * searches on the system the run was measuring.
 *
 * Not here: events per index. splunkd does not keep that for a finished
 * search outside the job's own search.log, and Regulator deletes its jobs.
 */
import React from 'react';
import PropTypes from 'prop-types';
import DL from '@splunk/react-ui/DefinitionList';
import Table from '@splunk/react-ui/Table';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import BusyButton from './BusyButton';
import { Mono, Muted, Note, Panel, RelTime, SectionHeading, Toolbar } from './primitives';
import { api } from '../api';
import { DASH, fmtDur, fmtInt, fmtMs, fmtNum, isBlank, toDate } from '../format';

const OUTCOMES = {
    failed: 'Failed',
    unfinished: 'Cut off at the end, deleted',
    finished_after_end: 'Cut off at the end, finished after',
    still_running: 'Cut off at the end, still running when deleted',
    failed_after_end: 'Cut off at the end, then failed',
    partial: 'Partial results',
    slow: 'Completed',
};

// What the audit trail calls how a search ended, in the console's spelling.
const ENDED = {
    completed: 'completed',
    canceled: 'cancelled',
    failed: 'failed',
    granted: 'dispatched, no end recorded',
};

// A table sits flush in its panel; the words around it need the panel's inset.
const Caption = styled.div`
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
`;

const Detail = styled.div`
    display: flex;
    flex-direction: column;
    gap: ${variables.spacingMedium};
    padding: ${variables.spacingSmall} 0;
`;

const DetailHeading = styled.div`
    font-weight: ${variables.fontWeightSemiBold};
    margin-bottom: ${variables.spacingXSmall};
`;

// One finding list under another: spacingSmall between the parts of the
// diagnosis, as between the parts of any panel.
const Part = styled.div`
    margin-bottom: ${variables.spacingSmall};
`;

const Findings = styled.ul`
    margin: ${variables.spacingXSmall} 0 0;
    padding-left: ${variables.spacingLarge};
`;

const when = (v) => {
    const d = toDate(v);
    return d ? d.toLocaleString('en-GB') : DASH;
};

const seconds = (v) => (isBlank(v) ? DASH : fmtDur(Number(v)));
const whole = (v) => (isBlank(v) ? DASH : fmtInt(Number(v)));

function outcomeLabel(entry) {
    const label = OUTCOMES[entry.outcome] || entry.outcome;
    return entry.outcome === 'failed' && entry.error_class ? `${label}: ${entry.error_class}` : label;
}

/** How far into the run a search started, which is what places it. */
function startedOffset(entry, run) {
    if (isBlank(entry.started_at) || isBlank(run.started_at)) {
        return DASH;
    }
    return `+${fmtDur(Math.max(0, entry.started_at - run.started_at))}`;
}

/** Links into the target's own search page, or the searches to paste there. */
function TargetLinks({ entry, run, span }) {
    const { sid } = entry;
    if (!sid) {
        return null;
    }
    const searches = [
        ['Audit records for this search', `index=_audit action=search search_id="*${sid}*"`],
        ['Indexer records for this search', `index=_internal sourcetype=splunkd_remote_searches search_id="*${sid}*"`],
        ['splunkd log lines naming it', `index=_internal sourcetype=splunkd "*${sid}*"`],
    ];
    const earliest = Math.floor((span && span.earliest) || (run.started_at || 0) - 60);
    const latest = Math.ceil((span && span.latest) || (run.ended_at || Date.now() / 1000) + 600);
    const base = (run.target_web_url || '').replace(/\/+$/, '');
    return (
        <div>
            <DetailHeading>Look it up on the target</DetailHeading>
            {base ? (
                <Findings>
                    {searches.map(([label, spl]) => (
                        <li key={label}>
                            <a
                                href={`${base}/app/search/search?q=${encodeURIComponent(`search ${spl}`)}&earliest=${earliest}&latest=${latest}`}
                                target="_blank"
                                rel="noreferrer"
                            >
                                {label}
                            </a>
                        </li>
                    ))}
                </Findings>
            ) : (
                <>
                    <Muted $small>
                        Give the target a web address to turn these into links. Run them over the
                        time of the run:
                    </Muted>
                    <Findings>
                        {searches.map(([label, spl]) => (
                            <li key={label}>
                                {label}: <Mono $break>{spl}</Mono>
                            </li>
                        ))}
                    </Findings>
                </>
            )}
            <Muted $small>
                The job itself is deleted when Regulator has read it, so Splunk&apos;s Job Inspector
                cannot open it; these records are what remains.
            </Muted>
        </div>
    );
}

TargetLinks.propTypes = {
    entry: PropTypes.object.isRequired,
    run: PropTypes.object.isRequired,
    span: PropTypes.object,
};

function PhaseTable({ performance }) {
    const phases = (performance && performance.phases) || [];
    const indexers = (performance && performance.indexers) || [];
    if (!phases.length && !indexers.length) {
        return null;
    }
    return (
        <>
            {phases.length > 0 && (
                <div>
                    <DetailHeading>Where the job&apos;s time went, costliest first</DetailHeading>
                    <Table>
                        <Table.Head>
                            <Table.HeadCell>Phase</Table.HeadCell>
                            <Table.HeadCell align="right">Seconds</Table.HeadCell>
                            <Table.HeadCell align="right">Invocations</Table.HeadCell>
                            <Table.HeadCell align="right">In</Table.HeadCell>
                            <Table.HeadCell align="right">Out</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {phases.map((row) => (
                                <Table.Row key={row.name}>
                                    <Table.Cell>
                                        <Mono>{row.name}</Mono>
                                    </Table.Cell>
                                    <Table.Cell align="right">{fmtNum(row.seconds, 2)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.invocations)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.input)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.output)}</Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                    <Muted $small>
                        The job&apos;s own performance table, as the Job Inspector shows it. Phases
                        nest and overlap, so they do not add up to the run time
                        {performance.phases_dropped
                            ? `; ${fmtInt(performance.phases_dropped)} cheaper ones are left out`
                            : ''}
                        .
                    </Muted>
                </div>
            )}
            {indexers.length > 0 && (
                <div>
                    <DetailHeading>What each indexer streamed back, by the job&apos;s account</DetailHeading>
                    <Table>
                        <Table.Head>
                            <Table.HeadCell>Indexer</Table.HeadCell>
                            <Table.HeadCell align="right">Seconds</Table.HeadCell>
                            <Table.HeadCell align="right">Invocations</Table.HeadCell>
                            <Table.HeadCell align="right">Out</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {indexers.map((row) => (
                                <Table.Row key={row.name}>
                                    <Table.Cell>
                                        <Mono>{row.name}</Mono>
                                    </Table.Cell>
                                    <Table.Cell align="right">{fmtNum(row.seconds, 2)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.invocations)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.output)}</Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                </div>
            )}
        </>
    );
}

PhaseTable.propTypes = { performance: PropTypes.object };

/** What the target said about one search, once Diagnose has asked. */
function TargetSaid({ found }) {
    const { audit } = found;
    const indexers = found.indexers || [];
    const logs = found.logs || [];
    const lookups =
        audit &&
        (audit.index_cache_hits || 0) +
            (audit.index_cache_misses || 0) +
            (audit.rawdata_cache_hits || 0) +
            (audit.rawdata_cache_misses || 0);
    return (
        <>
            <div>
                <DetailHeading>The target&apos;s audit trail</DetailHeading>
                {audit ? (
                    <DL termWidth="220px">
                        <DL.Term>How it ended</DL.Term>
                        <DL.Description>
                            {ENDED[audit.ended] || audit.ended}
                            {audit.user ? ` (as ${audit.user})` : ''}
                            {audit.host ? ` on ${audit.host}` : ''}
                        </DL.Description>
                        {!isBlank(audit.last_seen) && (
                            <>
                                <DL.Term>Last record</DL.Term>
                                <DL.Description>{when(audit.last_seen)}</DL.Description>
                            </>
                        )}
                        {!isBlank(audit.total_run_time) && (
                            <>
                                <DL.Term>Run time</DL.Term>
                                <DL.Description>{seconds(audit.total_run_time)}</DL.Description>
                            </>
                        )}
                        {!(isBlank(audit.scan_count) && isBlank(audit.event_count) && isBlank(audit.result_count)) && (
                            <>
                                <DL.Term>Scanned, events, results</DL.Term>
                                <DL.Description>
                                    {whole(audit.scan_count)}, {whole(audit.event_count)}, {whole(audit.result_count)}
                                </DL.Description>
                            </>
                        )}
                        {!(isBlank(audit.searched_buckets) && isBlank(audit.eliminated_buckets)) && (
                            <>
                                <DL.Term>Buckets searched, eliminated</DL.Term>
                                <DL.Description>
                                    {whole(audit.searched_buckets)}, {whole(audit.eliminated_buckets)}
                                </DL.Description>
                            </>
                        )}
                        {lookups > 0 && (
                            <>
                                <DL.Term>Bucket cache misses</DL.Term>
                                <DL.Description>
                                    {whole((audit.index_cache_misses || 0) + (audit.rawdata_cache_misses || 0))} of{' '}
                                    {whole(lookups)} lookups, costing{' '}
                                    {seconds((audit.index_miss_s || 0) + (audit.rawdata_miss_s || 0))}
                                </DL.Description>
                            </>
                        )}
                    </DL>
                ) : (
                    <Muted>
                        No audit record names this search: it was refused before a job existed, or the
                        account cannot read _audit.
                    </Muted>
                )}
            </div>
            {indexers.length > 0 && (
                <div>
                    <DetailHeading>What each indexer did, from its own log</DetailHeading>
                    <Table>
                        <Table.Head>
                            <Table.HeadCell>Indexer</Table.HeadCell>
                            <Table.HeadCell align="right">Seconds</Table.HeadCell>
                            <Table.HeadCell align="right">Scanned</Table.HeadCell>
                            <Table.HeadCell align="right">Events</Table.HeadCell>
                            <Table.HeadCell align="right">Buckets considered</Table.HeadCell>
                            <Table.HeadCell align="right">Eliminated</Table.HeadCell>
                            <Table.HeadCell align="right">Cache misses</Table.HeadCell>
                            <Table.HeadCell align="right">Waiting on misses</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {indexers.map((row) => (
                                <Table.Row key={row.host}>
                                    <Table.Cell>
                                        <Mono>{row.host}</Mono>
                                    </Table.Cell>
                                    <Table.Cell align="right">{fmtNum(row.elapsed_s, 2)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.scan_count)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.events)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.considered_buckets)}</Table.Cell>
                                    <Table.Cell align="right">{whole(row.eliminated_buckets)}</Table.Cell>
                                    <Table.Cell align="right">
                                        {isBlank(row.index_cache_misses) && isBlank(row.rawdata_cache_misses)
                                            ? DASH
                                            : fmtInt((row.index_cache_misses || 0) + (row.rawdata_cache_misses || 0))}
                                    </Table.Cell>
                                    <Table.Cell align="right">
                                        {isBlank(row.index_miss_wait_s) && isBlank(row.rawdata_miss_wait_s)
                                            ? DASH
                                            : fmtDur((row.index_miss_wait_s || 0) + (row.rawdata_miss_wait_s || 0))}
                                    </Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                </div>
            )}
            {logs.length > 0 && (
                <div>
                    <DetailHeading>What splunkd logged about it</DetailHeading>
                    <Findings>
                        {logs.map((row) => (
                            <li key={`${row.host}-${row.component}-${row.message}`}>
                                <strong>
                                    {row.log_level} {row.component}
                                </strong>
                                {row.host ? ` on ${row.host}` : ''}
                                {row.count > 1 ? ` (x${fmtInt(row.count)})` : ''}: <Mono $break>{row.message}</Mono>
                            </li>
                        ))}
                    </Findings>
                </div>
            )}
        </>
    );
}

TargetSaid.propTypes = { found: PropTypes.object.isRequired };

function SearchDetail({ entry, run, diagnosis }) {
    const found = diagnosis && diagnosis.searches && entry.sid ? diagnosis.searches[entry.sid] : null;
    const after = entry.after_end;
    return (
        <Detail>
            <DL termWidth="220px">
                <DL.Term>Search id</DL.Term>
                <DL.Description>
                    {entry.sid ? <Mono $break>{entry.sid}</Mono> : 'none: no job was created'}
                </DL.Description>
                <DL.Term>Who ran it</DL.Term>
                <DL.Description>
                    {entry.persona}, virtual user {fmtInt(entry.vu)}, iteration {fmtInt(entry.iteration)}
                    {entry.slot ? `, worker ${entry.slot}` : ''}
                </DL.Description>
                <DL.Term>Dispatched</DL.Term>
                <DL.Description>
                    {when(entry.started_at)} ({startedOffset(entry, run)} into the run)
                </DL.Description>
                {!isBlank(entry.earliest) && (
                    <>
                        <DL.Term>Time range searched</DL.Term>
                        <DL.Description>
                            {when(entry.earliest)} to {when(entry.latest)}
                        </DL.Description>
                    </>
                )}
                <DL.Term>As the user saw it</DL.Term>
                <DL.Description>
                    {fmtMs(entry.service_time_ms)} in all; accepted in {fmtMs(entry.dispatch_ms)}
                    {entry.queued_ms ? `, queued ${fmtMs(entry.queued_ms)}` : ''}
                    {!isBlank(entry.ttfr_ms) ? `, first result at ${fmtMs(entry.ttfr_ms)}` : ''}
                </DL.Description>
                <DL.Term>As the job reported it</DL.Term>
                <DL.Description>
                    {entry.dispatch_state || 'no state read'}
                    {!isBlank(entry.done_progress) && entry.dispatch_state !== 'DONE'
                        ? `, ${fmtNum(entry.done_progress * 100, 0)}% done`
                        : ''}
                    {!isBlank(entry.run_duration_s) ? `, run time ${seconds(entry.run_duration_s)}` : ''}; scanned{' '}
                    {whole(entry.scan_count)}, events {whole(entry.event_count)}, results{' '}
                    {whole(entry.result_count)}
                </DL.Description>
                {after && (
                    <>
                        <DL.Term>After the run</DL.Term>
                        <DL.Description>
                            {after.finished && `Followed until it finished, ${seconds(after.run_duration_s)} of run time. `}
                            {!after.finished &&
                                (entry.outcome === 'failed_after_end'
                                    ? 'Followed until the job failed on the target. '
                                    : 'Still running when Regulator stopped following it; deleted then, which the target records as cancelled. ')}
                            It had been running {fmtMs(entry.service_time_ms)} when the run stopped waiting.
                        </DL.Description>
                    </>
                )}
                {entry.error_detail && (
                    <>
                        <DL.Term>What went wrong</DL.Term>
                        <DL.Description>
                            {entry.http_status ? `HTTP ${entry.http_status}. ` : ''}
                            {entry.error_detail}
                        </DL.Description>
                    </>
                )}
                {entry.messages && entry.messages.length > 0 && (
                    <>
                        <DL.Term>What the job said</DL.Term>
                        <DL.Description>
                            {entry.messages.map((m) => (
                                <div key={m}>{m}</div>
                            ))}
                        </DL.Description>
                    </>
                )}
            </DL>
            <PhaseTable performance={entry.performance} />
            {found && <TargetSaid found={found} />}
            {diagnosis && !found && entry.sid && diagnosis.queries && diagnosis.queries.length > 0 && (
                <Muted>The diagnosis found nothing on the target that names this search.</Muted>
            )}
            <TargetLinks entry={entry} run={run} span={diagnosis && diagnosis.window} />
        </Detail>
    );
}

SearchDetail.propTypes = {
    entry: PropTypes.object.isRequired,
    run: PropTypes.object.isRequired,
    diagnosis: PropTypes.object,
};

const COLUMNS = 9;

function LedgerTable({ entries, run, diagnosis }) {
    return (
        <Table rowExpansion="single" horizontalOverflow="scroll">
            <Table.Head>
                <Table.HeadCell>Step</Table.HeadCell>
                <Table.HeadCell>What happened</Table.HeadCell>
                <Table.HeadCell align="right">Started</Table.HeadCell>
                <Table.HeadCell align="right">Took</Table.HeadCell>
                <Table.HeadCell align="right">Job run time</Table.HeadCell>
                <Table.HeadCell align="right">Scanned</Table.HeadCell>
                <Table.HeadCell align="right">Events</Table.HeadCell>
                <Table.HeadCell align="right">Results</Table.HeadCell>
                <Table.HeadCell>Search id</Table.HeadCell>
            </Table.Head>
            <Table.Body>
                {entries.map((entry, i) => {
                    const found = diagnosis && diagnosis.searches && entry.sid ? diagnosis.searches[entry.sid] : null;
                    const ended = found && found.audit ? ENDED[found.audit.ended] || found.audit.ended : null;
                    // The sid where there is one; a search refused at dispatch
                    // has none, and its place in the list is all it has.
                    const key = entry.sid || `${entry.step}-${entry.vu}-${entry.iteration}-${i}`;
                    return (
                        <Table.Row
                            key={key}
                            expansionRow={
                                <Table.Row key={`${key}-detail`}>
                                    <Table.Cell colSpan={COLUMNS}>
                                        <SearchDetail entry={entry} run={run} diagnosis={diagnosis} />
                                    </Table.Cell>
                                </Table.Row>
                            }
                        >
                            <Table.Cell>
                                <Mono>{entry.step}</Mono>
                            </Table.Cell>
                            <Table.Cell>
                                {/* One element: a cell lays its children out as a
                                    flex row, which drops a bare space between them. */}
                                <span>
                                    {outcomeLabel(entry)}
                                    {ended && <Muted $small> · target: {ended}</Muted>}
                                </span>
                            </Table.Cell>
                            <Table.Cell align="right">{startedOffset(entry, run)}</Table.Cell>
                            <Table.Cell align="right">{fmtMs(entry.service_time_ms)}</Table.Cell>
                            <Table.Cell align="right">{seconds(entry.run_duration_s)}</Table.Cell>
                            <Table.Cell align="right">{whole(entry.scan_count)}</Table.Cell>
                            <Table.Cell align="right">{whole(entry.event_count)}</Table.Cell>
                            <Table.Cell align="right">{whole(entry.result_count)}</Table.Cell>
                            <Table.Cell>
                                <Mono $break>{entry.sid || DASH}</Mono>
                            </Table.Cell>
                        </Table.Row>
                    );
                })}
            </Table.Body>
        </Table>
    );
}

LedgerTable.propTypes = {
    entries: PropTypes.arrayOf(PropTypes.object).isRequired,
    run: PropTypes.object.isRequired,
    diagnosis: PropTypes.object,
};

function Diagnosis({ diagnosis }) {
    const unanswered = (diagnosis.queries || []).filter((q) => !q.available);
    const load = diagnosis.self_load;
    return (
        <Caption>
            <Part>
                <DetailHeading>What Regulator recorded</DetailHeading>
                <Findings>
                    {(diagnosis.own || []).map((line) => (
                        <li key={line}>{line}</li>
                    ))}
                </Findings>
            </Part>
            {(diagnosis.target || []).length > 0 && (
                <Part>
                    <DetailHeading>What the target said</DetailHeading>
                    <Findings>
                        {diagnosis.target.map((line) => (
                            <li key={line}>{line}</li>
                        ))}
                    </Findings>
                </Part>
            )}
            {diagnosis.note && <Note>{diagnosis.note}</Note>}
            {unanswered.map((q) => (
                <Note key={q.name}>
                    Not answered: {q.description}. {q.reason}
                </Note>
            ))}
            <Muted $small>
                Diagnosed <RelTime value={diagnosis.at} />
                {load
                    ? `: ${fmtInt(diagnosis.sids)} search ids looked up in ${fmtInt(load.queries)} searches on the target, after the run.`
                    : '.'}{' '}
                Open a search below for what was found about it.
            </Muted>
        </Caption>
    );
}

Diagnosis.propTypes = { diagnosis: PropTypes.object.isRequired };

export default function SearchLedger({ run, stats, summary, onDiagnosed }) {
    const live = run.state === 'running' || run.state === 'pending';
    const ledger = (stats && stats.searches) || null;
    const diagnosis = (summary && summary.diagnosis) || null;
    const troubled = ((stats && stats.errors) || 0) + (((stats && stats.unfinished) || {}).count || 0) > 0;
    if (live || (!ledger && !diagnosis && !troubled)) {
        return null;
    }
    const problems = (ledger && ledger.problems) || [];
    const slowest = (ledger && ledger.slowest) || [];
    const dropped = Object.values((ledger && ledger.dropped) || {}).reduce((sum, n) => sum + (Number(n) || 0), 0);

    const diagnose = async () => {
        const result = await api(`/runs/${encodeURIComponent(run.id)}/diagnose`, { method: 'POST' });
        onDiagnosed(result);
    };

    return (
        <>
            <SectionHeading>Searches to look at</SectionHeading>
            <Panel
                flush
                title="Failed, cut off and partial searches"
                actions={
                    <Toolbar>
                        <BusyButton label={diagnosis ? 'Diagnose again' : 'Diagnose'} onClick={diagnose} />
                    </Toolbar>
                }
            >
                <Caption>
                    <Muted $small>
                        Diagnose reads this run&apos;s record, then asks the target what its audit trail,
                        its indexers and its own log say about these search ids: three small searches
                        over the time of the run. It is refused while anything is running on the target.
                    </Muted>
                </Caption>
                {diagnosis && <Diagnosis diagnosis={diagnosis} />}
                {!ledger && (
                    <Caption>
                        <Note>
                            This run finished before Regulator kept a list of individual searches, so
                            there are no search ids to open or look up.
                        </Note>
                    </Caption>
                )}
                {ledger && problems.length === 0 && (
                    <Caption>
                        <Muted>No search in this run failed, was cut off or came back partial.</Muted>
                    </Caption>
                )}
                {problems.length > 0 && <LedgerTable entries={problems} run={run} diagnosis={diagnosis} />}
                {dropped > 0 && (
                    <Caption>
                        <Muted $small>
                            A sample: {fmtInt(dropped)} more with a problem are in the totals above but not
                            listed. Up to {fmtInt((ledger.limits || {}).per_kind)} of each kind are kept.
                        </Muted>
                    </Caption>
                )}
            </Panel>
            {slowest.length > 0 && (
                <Panel flush title="The slowest searches that completed">
                    <LedgerTable entries={slowest} run={run} diagnosis={diagnosis} />
                </Panel>
            )}
        </>
    );
}

SearchLedger.propTypes = {
    run: PropTypes.object.isRequired,
    stats: PropTypes.object,
    summary: PropTypes.object,
    onDiagnosed: PropTypes.func.isRequired,
};
