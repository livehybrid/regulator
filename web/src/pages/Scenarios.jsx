/*
 * Scenarios: what gets run. A scenario is a directory (a scenario.yaml and,
 * for imported workloads, the savedsearches.conf it came from, in Splunk's own
 * format), so what is replayed is inspectable rather than implied.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Card from '@splunk/react-ui/Card';
import CardLayout from '@splunk/react-ui/CardLayout';
import Chip from '@splunk/react-ui/Chip';
import DL from '@splunk/react-ui/DefinitionList';
import Heading from '@splunk/react-ui/Heading';
import P from '@splunk/react-ui/Paragraph';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import ImportScenarioModal from '../modals/ImportScenarioModal';
import ScenarioDetailModal from '../modals/ScenarioDetailModal';
import useLoader from '../useLoader';
import { Muted, Note, PageHead, Panel, Toolbar } from '../components/primitives';
import { api } from '../api';
import { fmtDur, fmtInt, isBlank } from '../format';
import { useNotify } from '../Notifications';

export default function Scenarios({ onLaunch }) {
    const { data, error, loading, reload } = useLoader(() => api('/scenarios'), []);
    const scenarios = data || [];
    const [dialog, setDialog] = useState(null);
    const notify = useNotify();

    const openDetail = async (name) => {
        setDialog({ kind: 'detail', name, loading: true, payload: null });
        try {
            const d = await api(`/scenarios/${encodeURIComponent(name)}`);
            setDialog((s) => (s && s.name === name ? { ...s, loading: false, payload: d } : s));
        } catch (e) {
            setDialog(null);
            notify(e.message, 'error');
        }
    };

    return (
        <div>
            <PageHead>
                <Heading level={1}>Scenarios</Heading>
                <Toolbar>
                    <Button
                        label="Import saved searches"
                        onClick={() => setDialog({ kind: 'import' })}
                    />
                    <Button
                        appearance="primary"
                        label="Launch a run"
                        onClick={() => onLaunch({ scenario: null })}
                    />
                </Toolbar>
            </PageHead>

            <Note>
                Built-in scenarios ship with the image; the ones you import live on the data
                volume.
            </Note>

            {loading && !data ? (
                <P>
                    <WaitSpinner /> Loading scenarios&hellip;
                </P>
            ) : error ? (
                <Panel>
                    <Muted>{error}</Muted>
                </Panel>
            ) : !scenarios.length ? (
                <Panel>
                    <Muted>No scenarios are registered on this server.</Muted>
                </Panel>
            ) : (
                <CardLayout cardMinWidth={300} wrapCards>
                    {scenarios.map((s) => (
                        <Card key={s.name}>
                            <Card.Header
                                title={s.name}
                                subtitle={s.origin === 'user' ? 'imported' : 'built in'}
                            />
                            <Card.Body>
                                <P>{s.description || 'No description.'}</P>
                                <div>
                                    {(s.tags || []).map((t) => (
                                        <Chip key={t} appearance="outline">
                                            {t}
                                        </Chip>
                                    ))}
                                </div>
                                <DL termWidth="110px">
                                    <DL.Term>Engine</DL.Term>
                                    <DL.Description>
                                        {s.engine || '?'} / {s.load_model || 'closed'}
                                    </DL.Description>
                                    <DL.Term>Shape</DL.Term>
                                    <DL.Description>
                                        {fmtInt(s.personas)} personas, {fmtInt(s.steps)} steps
                                        {s.saved_searches
                                            ? ` (${fmtInt(s.saved_searches)} saved searches)`
                                            : ''}
                                    </DL.Description>
                                    <DL.Term>Default load</DL.Term>
                                    <DL.Description>
                                        {s.load_model === 'schedule'
                                            ? 'the schedule'
                                            : `${fmtInt(s.virtual_users)} users`}{' '}
                                        for{' '}
                                        {isBlank(s.duration_s)
                                            ? 'an unbounded run'
                                            : fmtDur(s.duration_s)}
                                    </DL.Description>
                                    <DL.Term>Corpus</DL.Term>
                                    <DL.Description>
                                        {s.index || '–'}
                                        {(s.sourcetypes || []).length
                                            ? `: ${s.sourcetypes.join(', ')}`
                                            : ''}
                                    </DL.Description>
                                    <DL.Term>Needs packs</DL.Term>
                                    <DL.Description>
                                        {(s.requires_packs || []).length
                                            ? s.requires_packs.join(', ')
                                            : 'none'}
                                    </DL.Description>
                                </DL>

                                {s.runnable_here === false && <Note>{s.not_runnable_reason}</Note>}
                                {/* Lint findings are the difference between a run and a wasted
                                    hour, so they sit on the card rather than behind a click. */}
                                {(s.lint || []).length > 0 && (
                                    <Note>Lint: {s.lint.join('; ')}</Note>
                                )}
                            </Card.Body>
                            <Card.Footer>
                                <Toolbar>
                                    <Button
                                        appearance="primary"
                                        label="Run"
                                        disabled={s.runnable_here === false}
                                        onClick={() => onLaunch({ scenario: s.name })}
                                    />
                                    <Button label="Details" onClick={() => openDetail(s.name)} />
                                </Toolbar>
                            </Card.Footer>
                        </Card>
                    ))}
                </CardLayout>
            )}

            {dialog && dialog.kind === 'detail' && (
                <ScenarioDetailModal
                    scenario={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                    onDelete={async () => {
                        await api(`/scenarios/${encodeURIComponent(dialog.name)}`, {
                            method: 'DELETE',
                        });
                        notify('Scenario deleted', 'success');
                        reload();
                    }}
                />
            )}
            {dialog && dialog.kind === 'import' && (
                <ImportScenarioModal
                    prefill=""
                    suggestedName="imported"
                    onClose={() => setDialog(null)}
                    onCreated={() => {
                        setDialog(null);
                        reload();
                    }}
                />
            )}
        </div>
    );
}

Scenarios.propTypes = {
    onLaunch: PropTypes.func.isRequired,
};
