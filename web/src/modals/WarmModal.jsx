/*
 * Warming part of a SmartStore cache, from a plan the operator confirms.
 *
 * Regulator suggests a plan from what a scenario's searches would read: the
 * indexes, and an earliest and latest time for each. That reading is a best
 * effort (a macro, an eventtype or a token can hide what a search reads), and
 * warming the wrong range is real load on somebody's cluster. So the
 * suggestion fills an editable table, nothing is warmed until the operator
 * ticks that they have checked it, and what is warmed is exactly the table as
 * they left it. The audit trail records it as their confirmed plan.
 */
import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import Checkbox from '@splunk/react-ui/Checkbox';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import DL from '@splunk/react-ui/DefinitionList';
import Message from '@splunk/react-ui/Message';
// Not imported as Number: that shadows the global (see LaunchModal).
import NumberInput from '@splunk/react-ui/Number';
import P from '@splunk/react-ui/Paragraph';
import Select from '@splunk/react-ui/Select';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import {
    ForecastSummary,
    SuggestionNotes,
    WarmPlanTable,
    planLines,
    planValid,
    rowsFromSuggestion,
} from '../components/WarmPlan';
import { Grid2, Note, SectionHeading } from '../components/primitives';
import { api, isAuthError } from '../api';
import { fmtInt } from '../format';
import { useNotify } from '../Notifications';

const POLL_MS = 2000;
export default function WarmModal({ targetId, onClose, onDone }) {
    const [scenarios, setScenarios] = useState([]);
    const [scenario, setScenario] = useState('');
    const [duration, setDuration] = useState('');
    const [mode, setMode] = useState('tsidx');
    const [concurrency, setConcurrency] = useState(4);
    const [budgetMin, setBudgetMin] = useState(30);
    const [rows, setRows] = useState([]);
    const [suggestion, setSuggestion] = useState(null);
    const [confirmed, setConfirmed] = useState(false);
    const [job, setJob] = useState(null);
    const [loading, setLoading] = useState(true);
    const notify = useNotify();
    const live = useRef(true);
    const base = `/targets/${encodeURIComponent(targetId)}`;

    useEffect(() => {
        (async () => {
            try {
                const [list, status] = await Promise.all([
                    api('/scenarios'),
                    api(`${base}/warm`).catch(() => null),
                ]);
                if (!live.current) {
                    return;
                }
                const names = (list || []).map((s) => s.name);
                setScenarios(names);
                setScenario((prev) => prev || names[0] || '');
                if (status && status.state === 'running') {
                    setJob(status);
                }
            } catch (e) {
                if (!isAuthError(e)) {
                    notify(e.message, 'error');
                }
            } finally {
                if (live.current) {
                    setLoading(false);
                }
            }
        })();
        return () => {
            live.current = false;
        };
    }, [base, notify]);

    // Follow a running warm-up until it finishes.
    useEffect(() => {
        if (!job || job.state !== 'running') {
            return undefined;
        }
        const timer = setTimeout(async () => {
            try {
                const status = await api(`${base}/warm`);
                if (live.current) {
                    setJob(status);
                    if (status.state !== 'running' && onDone) {
                        onDone();
                    }
                }
            } catch (e) {
                if (live.current && !isAuthError(e)) {
                    notify(e.message, 'error');
                }
            }
        }, POLL_MS);
        return () => clearTimeout(timer);
    }, [job, base, notify, onDone]);

    // Any edit to the plan withdraws the confirmation: what was checked is
    // what gets warmed, not something edited afterwards.
    const edit = (next) => {
        setRows(next);
        setConfirmed(false);
    };

    const suggest = async () => {
        const body = { scenario, mode };
        if (duration !== '' && duration !== null) {
            body.duration_s = Number(duration);
        }
        const r = await api(`${base}/warm/suggest`, { method: 'POST', body });
        setSuggestion(r);
        edit(rowsFromSuggestion(r.plan));
        if (!(r.plan || []).length) {
            notify('Nothing to suggest: what this scenario reads is already local, or not SmartStore', 'info');
        }
    };

    const valid = planValid(rows);
    const running = job && job.state === 'running';

    const warm = async () => {
        const r = await api(`${base}/warm`, {
            method: 'POST',
            body: {
                plan: planLines(rows),
                mode,
                concurrency: Number(concurrency) || 4,
                budget_s: (Number(budgetMin) || 30) * 60,
                confirmed: true,
                scenario: suggestion ? suggestion.scenario : undefined,
            },
        });
        setJob(r);
        notify('Warm-up started', 'success');
    };

    const result = job && job.result;

    return (
        <Dialog
            title="Warm the SmartStore cache"
            width="960px"
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="primary"
                    label="Warm"
                    disabled={!valid || !confirmed || running}
                    onClick={warm}
                />
            }
        >
            <Message appearance="fill" type="info">
                <Message.Title>You decide what gets warmed</Message.Title>
                Regulator can suggest indexes and time ranges from what a scenario&apos;s searches
                would read, but that is a reading of the search strings: a macro, an eventtype, a
                tag or a token can hide what a search really reads. Check and edit every line.
                Warming is load on the target, and only the lines below, as you leave them, are
                warmed.
            </Message>

            <SectionHeading>Suggest from a scenario</SectionHeading>
            {loading ? (
                <P>
                    <WaitSpinner /> Reading the scenarios&hellip;
                </P>
            ) : (
                <Grid2>
                    <ControlGroup label="Scenario" labelPosition="top">
                        <Select value={scenario} onChange={(e, { value }) => setScenario(value)}>
                            {scenarios.map((name) => (
                                <Select.Option key={name} value={name} label={name} />
                            ))}
                        </Select>
                    </ControlGroup>
                    <ControlGroup
                        label="Run duration in seconds"
                        labelPosition="top"
                        help="Blank keeps the scenario's."
                    >
                        <NumberInput value={duration} min={1} onChange={(e, { value }) => setDuration(value)} />
                    </ControlGroup>
                    <ControlGroup
                        label="Mode"
                        labelPosition="top"
                        help="tsidx warms index files (cheap, enough for tstats); full also reads raw events."
                    >
                        <Select value={mode} onChange={(e, { value }) => { setMode(value); setConfirmed(false); }}>
                            <Select.Option value="tsidx" label="tsidx: index files only" />
                            <Select.Option value="full" label="full: index files and raw data" />
                        </Select>
                    </ControlGroup>
                    <ControlGroup label="&nbsp;" labelPosition="top">
                        <BusyButton label="Suggest a plan" disabled={!scenario || running} onClick={suggest} />
                    </ControlGroup>
                </Grid2>
            )}
            {suggestion && <ForecastSummary forecast={suggestion.forecast} label="What the run reads" />}
            <SuggestionNotes suggestion={suggestion} />

            <SectionHeading>Plan</SectionHeading>
            <WarmPlanTable rows={rows} onChange={edit} />

            <Grid2>
                <ControlGroup label="Searches at once" labelPosition="top">
                    <NumberInput value={concurrency} min={1} max={16} onChange={(e, { value }) => setConcurrency(value)} />
                </ControlGroup>
                <ControlGroup
                    label="Time budget in minutes"
                    labelPosition="top"
                    help="Searches not started by then are reported, not run."
                >
                    <NumberInput value={budgetMin} min={1} max={240} onChange={(e, { value }) => setBudgetMin(value)} />
                </ControlGroup>
            </Grid2>

            <Checkbox checked={confirmed} disabled={!valid} onChange={() => setConfirmed((v) => !v)} value="confirmed">
                I have checked these indexes and time ranges, and want them warmed
            </Checkbox>

            {job && (
                <>
                    <SectionHeading>Warm-up</SectionHeading>
                    {running ? (
                        <P>
                            <WaitSpinner /> Warming {fmtInt((job.plan || []).length)} line(s) in{' '}
                            {job.mode} mode&hellip;
                        </P>
                    ) : job.state === 'failed' ? (
                        <Message appearance="fill" type="error">
                            {job.error}
                        </Message>
                    ) : result ? (
                        <>
                            <DL termWidth="200px">
                                <DL.Term>Searches</DL.Term>
                                <DL.Description>
                                    {fmtInt(result.searches)} in {result.seconds}s,{' '}
                                    {fmtInt(result.failed)} failed, {fmtInt(result.not_started)} not
                                    started, {fmtInt(result.skipped_local)} skipped as already local
                                </DL.Description>
                            </DL>
                            <ForecastSummary forecast={result.before} label="Plan, before" />
                            <ForecastSummary forecast={result.after} label="Plan, after" />
                            {(result.errors || []).length > 0 && (
                                <Note>
                                    {result.errors.map((e) => (
                                        <div key={e}>{e}</div>
                                    ))}
                                </Note>
                            )}
                        </>
                    ) : null}
                </>
            )}
        </Dialog>
    );
}

WarmModal.propTypes = {
    targetId: PropTypes.any.isRequired,
    onClose: PropTypes.func.isRequired,
    onDone: PropTypes.func,
};
