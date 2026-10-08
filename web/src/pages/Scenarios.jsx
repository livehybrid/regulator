/*
 * Scenarios: what gets run. A scenario is a directory (a scenario.yaml and,
 * for imported workloads, the savedsearches.conf it came from, in Splunk's own
 * format), so what is replayed is inspectable rather than implied.
 *
 * A scenario also moves between instances as a single .tar.gz. Download and
 * Upload are the manual directions; a configured scenario source (S3 or a
 * mounted directory) is the same artefact pulled at boot and pushed on save,
 * which is what lets a second control plane come up with the same library and
 * no shell access at all. Push appears only when a source is configured for
 * writing, so it is never a button that cannot work.
 */
import React, { useRef, useState } from 'react';
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
import { Mono, Muted, Note, PageHead, Panel, Toolbar } from '../components/primitives';
import { api, apiDownloadUrl, apiUpload } from '../api';
import { fmtDur, fmtInt, isBlank } from '../format';
import { useNotify } from '../Notifications';

export default function Scenarios({ onLaunch }) {
    const { data, error, loading, reload } = useLoader(() => api('/scenarios'), []);
    const scenarios = data || [];
    const [dialog, setDialog] = useState(null);
    const [busy, setBusy] = useState(null);
    const fileInput = useRef(null);
    const notify = useNotify();
    // Deployment environment, so it cannot change without a restart: loaded
    // once. A failure simply leaves Push hidden rather than breaking the page.
    const { data: source } = useLoader(() => api('/scenario-source'), []);
    const canPush = !!(source && source.writable);

    const onUpload = async (event) => {
        const file = event.target.files && event.target.files[0];
        // Clear through the ref rather than the event's own target: picking the
        // same file twice in a row must fire change again, and mutating the
        // handler's argument is what the lint objects to.
        if (fileInput.current) {
            fileInput.current.value = '';
        }
        if (!file) {
            return;
        }
        setBusy('upload');
        try {
            const created = await apiUpload('/scenarios/upload', file);
            notify(`Scenario \u201c${created.name}\u201d uploaded`, 'success');
            reload();
        } catch (e) {
            // 422 here is the useful case: the archive extracted but the
            // scenario does not load or lint, and the message says which.
            notify(e.message, 'error');
        } finally {
            setBusy(null);
        }
    };

    const onPush = async (name) => {
        setBusy(name);
        try {
            const result = await api(`/scenarios/${encodeURIComponent(name)}/publish`, {
                method: 'POST',
            });
            notify(`Pushed \u201c${name}\u201d as ${result.key}`, 'success');
        } catch (e) {
            notify(e.message, 'error');
        } finally {
            setBusy(null);
        }
    };

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
                    <input
                        ref={fileInput}
                        type="file"
                        accept=".tar.gz,.tgz,.tar,.zip,application/gzip,application/zip"
                        style={{ display: 'none' }}
                        onChange={onUpload}
                    />
                    <Button
                        label={busy === 'upload' ? 'Uploading\u2026' : 'Upload scenario'}
                        disabled={busy === 'upload'}
                        onClick={() => fileInput.current && fileInput.current.click()}
                    />
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
                volume. Download any of them as a .tar.gz and Upload it on another instance.
            </Note>

            {source && source.configured && (
                <Note>
                    {source.error ? (
                        <>Scenario source unusable: {source.error}</>
                    ) : (
                        <>
                            Shared scenario source: <Mono>{source.location}</Mono>.{' '}
                            {source.writable
                                ? 'Scenarios are pushed here when they are created, and each card has a Push button to send one again.'
                                : 'Read only: scenarios are pulled from here at startup. Set REG_SCENARIO_SOURCE_WRITE=1 to push as well.'}
                        </>
                    )}
                </Note>
            )}

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
                                    {/* A plain link, not a fetch: the browser
                                        streams the archive to disk and sends
                                        the session cookie itself. */}
                                    <Button
                                        label="Download"
                                        to={apiDownloadUrl(
                                            `/scenarios/${encodeURIComponent(s.name)}/export`
                                        )}
                                    />
                                    {canPush && (
                                        <Button
                                            label={busy === s.name ? 'Pushing\u2026' : 'Push'}
                                            disabled={busy === s.name}
                                            onClick={() => onPush(s.name)}
                                        />
                                    )}
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
