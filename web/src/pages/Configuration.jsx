/*
 * Configuration: back this instance up, and restore one; and the shared copy
 * in REG_CONFIG_SOURCE, which also carries the run history.
 *
 * The page exists so that a Regulator deployment is disposable. Configure it,
 * download the JSON, tear the environment down including its volumes, and
 * bring it back from the file. REG_CONFIG_IMPORT is the automatic path for a
 * rebuilt environment and needs no UI at all; this page is how an operator
 * gets the file to mount in the first place, and how one is applied to a
 * running instance without reaching for curl.
 */
import React, { useRef, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Heading from '@splunk/react-ui/Heading';
import P from '@splunk/react-ui/Paragraph';
import Switch from '@splunk/react-ui/Switch';

import useLoader from '../useLoader';
import { Mono, Muted, Note, PageHead, Panel, RelTime, Toolbar } from '../components/primitives';
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
    const { data: copy, reload: reloadCopy } = useLoader(() => api('/config/source'), []);
    const [syncing, setSyncing] = useState(null);

    const sync = async (direction) => {
        setSyncing(direction);
        try {
            const result = await api(`/config/source/${direction}`, { method: 'POST' });
            if (direction === 'pull') {
                setReport(result);
                notify(
                    result.found
                        ? `Pulled ${result.targets} new target(s), ${result.scenarios} scenario(s), ` +
                              `${result.baselines} baseline(s) and ${result.runs.imported} run(s)`
                        : `Nothing in the shared copy yet; ${result.runs.imported} run(s) pulled`,
                    'success'
                );
            } else {
                notify(
                    `Pushed ${result.targets} target(s), ${result.scenarios} scenario(s) and ` +
                        `${result.baselines} baseline(s); ${result.runs} run(s) archived`,
                    'success'
                );
            }
        } catch (e) {
            notify(e.message, 'error');
        } finally {
            setSyncing(null);
            reloadCopy();
        }
    };

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
                Back up the targets, scenarios and baselines you authored, or restore them onto a
                rebuilt instance. Run history travels through the shared copy below, one file per
                run; the audit log never leaves this instance.
            </Note>

            <Panel title="Shared copy and run history">
                <SharedCopy copy={copy} syncing={syncing} onSync={sync} />
            </Panel>

            <Panel title="Download">
                <P>
                    <Muted>
                        Your targets and your own scenario library as one JSON document. The
                        scenarios that ship in the image are left out: writing those into a
                        restore would shadow the newer copies after an upgrade. Baselines are
                        included, naming their run by its uid; on another instance one lands only
                        if that run is there too, which the shared copy below arranges.
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
                            Created <strong>{report.targets}</strong> target(s),{' '}
                            <strong>{report.scenarios}</strong> scenario(s) and{' '}
                            <strong>{report.baselines || 0}</strong> baseline(s); updated{' '}
                            <strong>{report.targets_updated}</strong>,{' '}
                            <strong>{report.scenarios_updated}</strong> and{' '}
                            <strong>{report.baselines_updated || 0}</strong> already present.
                            {report.runs && (
                                <>
                                    {' '}
                                    <strong>{report.runs.imported}</strong> run(s) pulled.
                                </>
                            )}
                        </P>
                        {(report.baselines_waiting || []).length > 0 && (
                            <Note>
                                Baseline(s) {report.baselines_waiting.join(', ')} name runs that
                                are not on this instance, so they were not created.
                            </Note>
                        )}
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

function SyncLine({ label, result }) {
    if (!result) {
        return (
            <P>
                <Muted>{label}: not since this instance started.</Muted>
            </P>
        );
    }
    let what = '';
    if (!result.ok) {
        what = result.error || 'failed';
    } else if (result.found === false) {
        what = 'nothing there yet';
    } else if (result.created) {
        const c = result.created;
        what = `${c.targets} target(s), ${c.scenarios} scenario(s), ${c.baselines} baseline(s), ${c.runs} run(s) new`;
    } else if (result.counts) {
        const c = result.counts;
        what = `${c.targets} target(s), ${c.scenarios} scenario(s), ${c.baselines} baseline(s); ${result.runs || 0} run(s) archived`;
    }
    return (
        <P>
            <Muted>
                {label}: <RelTime value={result.at} /> {result.ok ? '✓' : '✗'} {what}
            </Muted>
        </P>
    );
}

SyncLine.propTypes = {
    label: PropTypes.string.isRequired,
    result: PropTypes.shape({
        at: PropTypes.string,
        ok: PropTypes.bool,
        error: PropTypes.string,
        found: PropTypes.bool,
        created: PropTypes.object,
        counts: PropTypes.object,
        runs: PropTypes.number,
    }),
};

function SharedCopy({ copy, syncing, onSync }) {
    if (!copy) {
        return (
            <P>
                <Muted>Checking the config source…</Muted>
            </P>
        );
    }
    if (!copy.configured) {
        return (
            <P>
                <Muted>
                    Not configured. Set <Mono>REG_CONFIG_SOURCE</Mono> to an{' '}
                    <Mono>s3://bucket/prefix</Mono> (or a mounted directory) and this instance
                    pulls its targets, scenarios, baselines and run history from there at boot, so
                    a fresh or rebuilt instance comes up ready to test. Add{' '}
                    <Mono>REG_CONFIG_SOURCE_WRITE=1</Mono> and every change, and every finished
                    run, is pushed back automatically.
                </Muted>
            </P>
        );
    }
    if (copy.error) {
        return <Note>The config source cannot be used: {copy.error}</Note>;
    }
    return (
        <div>
            <P>
                <Mono>
                    {copy.location}/{copy.object}
                </Mono>{' '}
                <Muted>{copy.writable ? '(pushes changes and finished runs)' : '(read only)'}</Muted>
            </P>
            <P>
                <Muted>
                    Pulled at boot and with Pull now: the configuration is the same upsert as a
                    restore, and a run is imported only if no run here has its uid, so a pull never
                    removes or duplicates anything.{' '}
                    {copy.writable
                        ? 'Every change here is pushed within seconds, credentials as ciphertext, and each run is archived once it finishes. Deleting a run removes its archive too.'
                        : 'This instance does not write the shared copy; set REG_CONFIG_SOURCE_WRITE=1 to let it. A run deleted here comes back at the next pull.'}
                </Muted>
            </P>
            <SyncLine label="Last pull" result={copy.last_pull} />
            <SyncLine label="Last push" result={copy.last_push} />
            {copy.carried > 0 && (
                <Note>
                    {copy.carried} row(s) in the shared copy could not be applied here (usually a
                    baseline whose run is not archived). They are kept in every push rather than
                    deleted from it.
                </Note>
            )}
            <Toolbar>
                <Button
                    label={syncing === 'pull' ? 'Pulling…' : 'Pull now'}
                    disabled={syncing !== null}
                    onClick={() => onSync('pull')}
                />
                {copy.writable && (
                    <Button
                        label={syncing === 'push' ? 'Pushing…' : 'Push now'}
                        disabled={syncing !== null}
                        onClick={() => onSync('push')}
                    />
                )}
            </Toolbar>
        </div>
    );
}

SharedCopy.propTypes = {
    copy: PropTypes.shape({
        configured: PropTypes.bool,
        writable: PropTypes.bool,
        location: PropTypes.string,
        object: PropTypes.string,
        error: PropTypes.string,
        carried: PropTypes.number,
        last_pull: PropTypes.object,
        last_push: PropTypes.object,
    }),
    syncing: PropTypes.string,
    onSync: PropTypes.func.isRequired,
};
