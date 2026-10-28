/*
 * Launching a run: pick a target, pick a scenario, decide what the numbers
 * will mean.
 *
 * Every field left blank keeps the scenario's own default, so the quickest
 * path through this dialog is to change nothing. Given an earlier run
 * (``initial``) it opens filled in with what that run was launched with, seed
 * included, so "Run again" repeats the same test unless something is changed.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import Checkbox from '@splunk/react-ui/Checkbox';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Message from '@splunk/react-ui/Message';
// Not imported as Number: that shadows the global, and Number(v) then calls
// the component as a function, which React reports as error #321.
import NumberInput from '@splunk/react-ui/Number';
import P from '@splunk/react-ui/Paragraph';
import Select from '@splunk/react-ui/Select';
import Text from '@splunk/react-ui/Text';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Grid2, Mono, Muted, Note, SectionHeading } from '../components/primitives';
import { api, isAuthError } from '../api';
import { useNotify } from '../Notifications';

const numOrUndefined = (v) => (v === '' || v === null || v === undefined ? undefined : Number(v));
const asField = (v) => (v === null || v === undefined ? '' : v);

export default function LaunchModal({ scenario, initial, onClose, onLaunched }) {
    const from = initial || {};
    const [loading, setLoading] = useState(true);
    const [targets, setTargets] = useState([]);
    const [scenarios, setScenarios] = useState([]);
    const [fleets, setFleets] = useState([]);
    const [blocked, setBlocked] = useState(null);
    const [form, setForm] = useState({
        target_id: from.target_id ? String(from.target_id) : '',
        scenario: scenario || from.scenario || '',
        label: from.label || '',
        virtual_users: asField(from.virtual_users),
        duration_s: asField(from.duration_s),
        pacing_s: asField(from.pacing_s),
        arrival_rate_per_min: asField(from.arrival_rate_per_min),
        seed: asField(from.seed),
        fleet: from.fleet || '',
        workers: asField(from.workers),
        evict_every_s: asField(from.evict_every_s),
        cold_window_s: asField(from.cold_window_s),
    });
    const fromIndexes = (from.evict_cache_indexes || '').split(',').filter((i) => i && i !== '*');
    const [evict, setEvict] = useState(Boolean(from.evict_cache));
    const [evictIndexes, setEvictIndexes] = useState(fromIndexes);
    const [evictAll, setEvictAll] = useState((from.evict_cache_indexes || '').split(',').includes('*'));
    const [cacheIndexes, setCacheIndexes] = useState(null);
    const notify = useNotify();

    useEffect(() => {
        let live = true;
        (async () => {
            try {
                const [t, s, f] = await Promise.all([
                    api('/targets'),
                    api('/scenarios'),
                    api('/fleets').catch(() => []),
                ]);
                if (!live) {
                    return;
                }
                const targetList = t || [];
                const scenarioList = s || [];
                setTargets(targetList);
                setScenarios(scenarioList);
                setFleets(f || []);
                if (!targetList.length) {
                    setBlocked('Add a target before launching a run.');
                } else if (!scenarioList.length) {
                    setBlocked('This server has no scenarios registered, so there is nothing to run.');
                }
                const chosen = scenarioList.find((x) => x.name === scenario);
                if (chosen && chosen.runnable_here === false) {
                    setBlocked(chosen.not_runnable_reason);
                }
                const defaultFleet = (f || []).find((x) => x.default && x.available);
                setForm((prev) => ({
                    ...prev,
                    target_id: prev.target_id || (targetList[0] ? String(targetList[0].id) : ''),
                    scenario:
                        prev.scenario ||
                        (scenarioList.find((x) => x.runnable_here !== false) || {}).name ||
                        '',
                    fleet: prev.fleet || (defaultFleet ? defaultFleet.kind : ''),
                }));
            } catch (e) {
                if (!isAuthError(e)) {
                    setBlocked(e.message);
                }
            } finally {
                if (live) {
                    setLoading(false);
                }
            }
        })();
        return () => {
            live = false;
        };
    }, [scenario]);

    // The eviction scope comes from the target's own per-index breakdown, so it
    // is read when eviction is turned on and again if the target changes.
    useEffect(() => {
        if (!evict || !form.target_id) {
            return undefined;
        }
        let live = true;
        setCacheIndexes(null);
        (async () => {
            try {
                const c = await api(`/targets/${encodeURIComponent(form.target_id)}/cache`);
                if (live) {
                    setCacheIndexes(Object.keys((c && c.per_index) || {}));
                }
            } catch (e) {
                if (live) {
                    setCacheIndexes([]);
                }
            }
        })();
        return () => {
            live = false;
        };
    }, [evict, form.target_id]);

    const set = (k) => (e, data) => setForm((f) => ({ ...f, [k]: data.value }));
    const chosenScenario = scenarios.find((s) => s.name === form.scenario);

    const launch = async () => {
        const body = {
            target_id: /^\d+$/.test(form.target_id) ? parseInt(form.target_id, 10) : form.target_id,
            scenario: form.scenario,
        };
        const optional = {
            virtual_users: numOrUndefined(form.virtual_users),
            duration_s: numOrUndefined(form.duration_s),
            pacing_s: numOrUndefined(form.pacing_s),
            arrival_rate_per_min: numOrUndefined(form.arrival_rate_per_min),
            seed: numOrUndefined(form.seed),
            workers: numOrUndefined(form.workers),
            evict_every_s: numOrUndefined(form.evict_every_s),
            cold_window_s: numOrUndefined(form.cold_window_s),
        };
        Object.entries(optional).forEach(([k, v]) => {
            if (v !== undefined && !Number.isNaN(v)) {
                body[k] = v;
            }
        });
        if (form.fleet) {
            body.fleet = form.fleet;
        }
        if (form.label.trim()) {
            body.label = form.label.trim();
        }
        // A periodic eviction needs a scope, and the eviction list is what
        // decides it: asking for one without the other would evict nothing.
        if (body.evict_every_s && !evict) {
            setEvict(true);
            notify(
                'Periodic eviction uses the eviction scope: tick the indexes (or every index) and launch again',
                'warning'
            );
            return;
        }
        if (evict) {
            body.evict_cache = true;
            body.evict_cache_indexes = evictIndexes;
            if (evictAll) {
                body.evict_all_indexes = true;
            }
        }
        const run = await api('/runs', { method: 'POST', body });
        onLaunched(run.id);
    };

    return (
        <Dialog
            title={initial ? `Run again: ${from.label || from.scenario || `run ${from.id}`}` : 'Launch a run'}
            width="860px"
            onClose={onClose}
            closeLabel="Cancel"
            footer={
                !loading &&
                !blocked && <BusyButton appearance="primary" label="Launch" onClick={launch} />
            }
        >
            {loading ? (
                <P>
                    <WaitSpinner /> Reading targets, scenarios and fleets&hellip;
                </P>
            ) : blocked ? (
                <Message appearance="fill" type="warning">
                    {blocked}
                </Message>
            ) : (
                <>
                    <Grid2>
                        <ControlGroup label="Target" labelPosition="top">
                            <Select value={form.target_id} onChange={set('target_id')}>
                                {targets.map((t) => (
                                    <Select.Option
                                        key={t.id}
                                        value={String(t.id)}
                                        label={`${t.name} (${t.mgmt_url})`}
                                    />
                                ))}
                            </Select>
                        </ControlGroup>
                        <ControlGroup
                            label="Scenario"
                            labelPosition="top"
                            help={chosenScenario ? chosenScenario.description : undefined}
                        >
                            <Select value={form.scenario} onChange={set('scenario')}>
                                {scenarios.map((s) => (
                                    <Select.Option
                                        key={s.name}
                                        value={s.name}
                                        disabled={s.runnable_here === false}
                                        label={
                                            s.runnable_here === false
                                                ? `${s.name} (not runnable here)`
                                                : s.name
                                        }
                                    />
                                ))}
                            </Select>
                        </ControlGroup>
                        <ControlGroup label="Label" labelPosition="top" help="Optional.">
                            <Text
                                value={form.label}
                                placeholder="baseline before the indexer change"
                                onChange={set('label')}
                            />
                        </ControlGroup>
                        <ControlGroup
                            label="Virtual users"
                            labelPosition="top"
                            help={
                                chosenScenario && chosenScenario.virtual_users
                                    ? `Blank keeps the scenario default of ${chosenScenario.virtual_users}.`
                                    : 'Blank keeps the scenario default.'
                            }
                        >
                            <NumberInput value={form.virtual_users} min={1} onChange={set('virtual_users')} />
                        </ControlGroup>
                        <ControlGroup
                            label="Duration in seconds"
                            labelPosition="top"
                            help={
                                chosenScenario && chosenScenario.duration_s
                                    ? `Blank keeps the scenario default of ${chosenScenario.duration_s}.`
                                    : 'Blank keeps the scenario default.'
                            }
                        >
                            <NumberInput value={form.duration_s} min={1} onChange={set('duration_s')} />
                        </ControlGroup>
                        <ControlGroup label="Pacing in seconds per iteration" labelPosition="top">
                            <NumberInput value={form.pacing_s} min={0} step={0.1} onChange={set('pacing_s')} />
                        </ControlGroup>
                        <ControlGroup label="Arrival rate per minute" labelPosition="top" help="Open model.">
                            <NumberInput
                                value={form.arrival_rate_per_min}
                                min={0}
                                onChange={set('arrival_rate_per_min')}
                            />
                        </ControlGroup>
                        <ControlGroup
                            label="Seed"
                            labelPosition="top"
                            help="Blank keeps the scenario's, so runs stay comparable."
                        >
                            <NumberInput value={form.seed} min={1} onChange={set('seed')} />
                        </ControlGroup>
                        <ControlGroup label="Fleet" labelPosition="top">
                            <Select value={form.fleet} onChange={set('fleet')}>
                                {fleets.map((f) => (
                                    <Select.Option
                                        key={f.kind}
                                        value={f.kind}
                                        disabled={!f.available}
                                        label={`${f.kind}${f.available ? '' : ' (unavailable)'}: ${f.detail}`}
                                    />
                                ))}
                            </Select>
                        </ControlGroup>
                        <ControlGroup
                            label="Workers"
                            labelPosition="top"
                            help="Blank sizes the fleet from the users per worker."
                        >
                            <NumberInput value={form.workers} min={1} onChange={set('workers')} />
                        </ControlGroup>
                    </Grid2>

                    <Note>
                        Pacing decides what the latency numbers mean: with pacing, iterations are
                        scheduled against the wall clock and the run reports coordinated-omission
                        corrected latency; without it each user waits for its own response, so the
                        figures are service times.
                    </Note>

                    <SectionHeading>SmartStore cache</SectionHeading>
                    <Checkbox checked={evict} onChange={() => setEvict((v) => !v)}>
                        Evict the SmartStore cache first (measures the cold path)
                    </Checkbox>

                    {evict && (
                        <>
                            <Message appearance="fill" type="warning">
                                <Message.Title>Eviction affects everyone on this target</Message.Title>
                                Nothing is destroyed, but the next searches over these indexes pay
                                to re-download from object storage. Leave the list empty to let the
                                scenario&apos;s own corpus index decide.
                            </Message>
                            {cacheIndexes === null ? (
                                <P>
                                    <WaitSpinner /> Reading the cache&hellip;
                                </P>
                            ) : cacheIndexes.length ? (
                                cacheIndexes.map((n) => (
                                    <Checkbox
                                        key={n}
                                        checked={evictIndexes.includes(n)}
                                        onChange={() =>
                                            setEvictIndexes((s) =>
                                                s.includes(n) ? s.filter((x) => x !== n) : [...s, n]
                                            )
                                        }
                                    >
                                        <Mono>{n}</Mono>
                                    </Checkbox>
                                ))
                            ) : (
                                <Muted $small>
                                    No per-index breakdown was returned for this target.
                                </Muted>
                            )}
                            <Checkbox checked={evictAll} onChange={() => setEvictAll((v) => !v)}>
                                <strong>Every index</strong>{' '}
                                <Muted $small>(the whole cache on this target)</Muted>
                            </Checkbox>
                        </>
                    )}

                    <Grid2>
                        <ControlGroup
                            label="Evict again every N seconds"
                            labelPosition="top"
                            help="Blank: never."
                        >
                            <NumberInput value={form.evict_every_s} min={60} onChange={set('evict_every_s')} />
                        </ControlGroup>
                        <ControlGroup
                            label="Cold window in seconds after each eviction"
                            labelPosition="top"
                            help="Blank: half the interval."
                        >
                            <NumberInput value={form.cold_window_s} min={1} onChange={set('cold_window_s')} />
                        </ControlGroup>
                    </Grid2>
                    <Note>
                        Periodic eviction returns the cache to cold on a clock, so one run measures
                        both paths: each step reports a cold p95 (inside the window after an
                        eviction) and a warm p95, and the charts mark every epoch. It uses the same
                        index scope as the eviction above.
                    </Note>
                </>
            )}
        </Dialog>
    );
}

LaunchModal.propTypes = {
    scenario: PropTypes.string,
    // An earlier run to repeat: the fields it was launched with.
    initial: PropTypes.shape({
        id: PropTypes.number,
        target_id: PropTypes.number,
        scenario: PropTypes.string,
        label: PropTypes.string,
        virtual_users: PropTypes.number,
        duration_s: PropTypes.number,
        pacing_s: PropTypes.number,
        arrival_rate_per_min: PropTypes.number,
        seed: PropTypes.number,
        fleet: PropTypes.string,
        workers: PropTypes.number,
        evict_cache: PropTypes.bool,
        evict_cache_indexes: PropTypes.string,
        evict_every_s: PropTypes.number,
        cold_window_s: PropTypes.number,
    }),
    onClose: PropTypes.func.isRequired,
    onLaunched: PropTypes.func.isRequired,
};
