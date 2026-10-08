/*
 * Configuration: back this instance up, and restore one.
 *
 * The page exists so that a Regulator deployment is disposable. Configure it,
 * download the JSON, tear the environment down including its volumes, and
 * bring it back from the file. REG_CONFIG_IMPORT is the automatic path for a
 * rebuilt environment and needs no UI at all; this page is how an operator
 * gets the file to mount in the first place, and how one is applied to a
 * running instance without reaching for curl.
 */
import React, { useRef, useState } from 'react';
import Button from '@splunk/react-ui/Button';
import Heading from '@splunk/react-ui/Heading';
import P from '@splunk/react-ui/Paragraph';
import Switch from '@splunk/react-ui/Switch';

import useLoader from '../useLoader';
import { Mono, Muted, Note, PageHead, Panel, Toolbar } from '../components/primitives';
import { api, apiDownloadUrl } from '../api';
import { useNotify } from '../Notifications';

export default function Configuration() {
    const notify = useNotify();
    const fileInput = useRef(null);
    const [secrets, setSecrets] = useState(true);
    const [scenarios, setScenarios] = useState(true);
    const [busy, setBusy] = useState(false);
    const [report, setReport] = useState(null);
    // Shown so the page can say whether the shared library is in play at all,
    // rather than leaving the operator to infer it from two env vars.
    const { data: source } = useLoader(() => api('/scenario-source'), []);

    const query = [
        `secrets=${secrets ? 'include' : 'exclude'}`,
        `scenarios=${scenarios ? 'include' : 'exclude'}`,
    ].join('&');

    const onFile = async (event) => {
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
        setBusy(true);
        setReport(null);
        try {
            const text = await file.text();
            let document;
            try {
                document = JSON.parse(text);
            } catch (e) {
                throw new Error(`${file.name} is not valid JSON`);
            }
            const result = await api('/config/import', { method: 'POST', body: document });
            setReport(result);
            notify(
                `Restored ${result.targets + result.targets_updated} target(s) and ` +
                    `${result.scenarios + result.scenarios_updated} scenario(s)`,
                'success'
            );
        } catch (e) {
            notify(e.message, 'error');
        } finally {
            setBusy(false);
        }
    };

    return (
        <div>
            <PageHead>
                <Heading level={1}>Configuration</Heading>
            </PageHead>

            <Note>
                Back up the targets and scenarios you authored, or restore them onto a rebuilt
                instance. Runs, samples and the audit log are history rather than configuration
                and are never carried.
            </Note>

            <Panel title="Download">
                <P>
                    <Muted>
                        Your targets and your own scenario library as one JSON document. The
                        scenarios that ship in the image are left out: writing those into a
                        restore would shadow the newer copies after an upgrade. Baselines are left
                        out too, because a baseline is a label pointing at a run, and a rebuilt
                        instance has no runs for it to point at.
                    </Muted>
                </P>
                <Toolbar>
                    <Switch
                        value="secrets"
                        selected={secrets}
                        onClick={() => setSecrets((v) => !v)}
                        appearance="checkbox"
                    >
                        Include credentials
                    </Switch>
                    <Switch
                        value="scenarios"
                        selected={scenarios}
                        onClick={() => setScenarios((v) => !v)}
                        appearance="checkbox"
                    >
                        Include my scenarios
                    </Switch>
                </Toolbar>
                <Toolbar>
                    <Button
                        appearance="primary"
                        label="Download config"
                        to={apiDownloadUrl(`/config/export?${query}`)}
                    />
                </Toolbar>
                <Note>
                    {secrets ? (
                        <>
                            Credentials travel as the ciphertext they are stored as, so a restore
                            needs the same <Mono>REG_MASTER_KEY</Mono> — in Kubernetes that is a
                            Secret, which survives exactly the volume teardown this exists for.
                            The file records a fingerprint of the key, so restoring under a
                            different one tells you rather than leaving every target quietly
                            unable to authenticate.
                        </>
                    ) : (
                        <>
                            With credentials excluded the file is safe to commit: it restores every
                            target and scenario with the tokens and passwords blank, ready to be
                            filled in.
                        </>
                    )}
                </Note>
            </Panel>

            <Panel title="Restore">
                <P>
                    <Muted>
                        Applying a file is an idempotent upsert: re-applying the same one changes
                        nothing, and a partial restore can simply be run again. Matching is by
                        name, never by id, so a file restores onto a fresh instance whose row ids
                        differ.
                    </Muted>
                </P>
                <input
                    ref={fileInput}
                    type="file"
                    accept="application/json,.json"
                    style={{ display: 'none' }}
                    onChange={onFile}
                />
                <Toolbar>
                    <Button
                        label={busy ? 'Applying…' : 'Choose a config file…'}
                        disabled={busy}
                        onClick={() => fileInput.current && fileInput.current.click()}
                    />
                </Toolbar>
                {report && (
                    <div>
                        <P>
                            Created <strong>{report.targets}</strong> target(s) and{' '}
                            <strong>{report.scenarios}</strong> scenario(s); updated{' '}
                            <strong>{report.targets_updated}</strong> and{' '}
                            <strong>{report.scenarios_updated}</strong> already present.
                        </P>
                        {(report.warnings || []).map((line) => (
                            <Note key={line}>{line}</Note>
                        ))}
                        {(report.skipped || []).length > 0 && (
                            <Note>Skipped: {report.skipped.join('; ')}</Note>
                        )}
                    </div>
                )}
            </Panel>

            <Panel title="Restoring without the console">
                <P>
                    <Muted>
                        For a rebuilt environment, mount the file and set{' '}
                        <Mono>REG_CONFIG_IMPORT</Mono> to its path (or to the JSON itself). It is
                        applied at boot, before any traffic is served, and never raises: a
                        malformed file is logged and skipped, because a typo in a ConfigMap must
                        not stop the control plane starting.
                    </Muted>
                </P>
                <P>
                    <Muted>
                        A shared scenario library is the other half of a hands-off deployment.{' '}
                        {source && source.configured ? (
                            <>
                                This instance is pointed at <Mono>{source.location}</Mono>
                                {source.writable
                                    ? ', and pushes scenarios back to it.'
                                    : ' and pulls from it read only.'}
                            </>
                        ) : (
                            <>
                                Set <Mono>REG_SCENARIO_SOURCE</Mono> to an{' '}
                                <Mono>s3://bucket/prefix</Mono> or a mounted directory and every
                                instance pointed at it comes up with the same scenarios.
                            </>
                        )}
                    </Muted>
                </P>
            </Panel>
        </div>
    );
}
