/*
 * Targets: the Splunk instances Regulator is allowed to point load at.
 */
import React, { useCallback, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Checkbox from '@splunk/react-ui/Checkbox';
import Chip from '@splunk/react-ui/Chip';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Heading from '@splunk/react-ui/Heading';
import Menu from '@splunk/react-ui/Menu';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import Text from '@splunk/react-ui/Text';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import CacheModal from '../modals/CacheModal';
import ConfirmModal from '../modals/ConfirmModal';
import EvictModal from '../modals/EvictModal';
import ImportScenarioModal from '../modals/ImportScenarioModal';
import PurgeModal from '../modals/PurgeModal';
import ReportModal from '../modals/ReportModal';
import SavedSearchesModal from '../modals/SavedSearchesModal';
import useLoader from '../useLoader';
import {
    Grid2,
    HealthPill,
    Mono,
    Muted,
    Note,
    PageHead,
    Panel,
    RelTime,
    SectionHeading,
    Toolbar,
} from '../components/primitives';
import { api } from '../api';
import { fmtInt, isBlank } from '../format';
import { useNotify } from '../Notifications';

const EMPTY_TARGET = {
    name: '',
    mgmt_url: '',
    web_url: '',
    token: '',
    username: '',
    password: '',
    app: '',
    owner: '',
    api_version: '',
    indexer_urls: '',
    indexer_token: '',
    indexer_username: '',
    indexer_password: '',
    verify_tls: true,
};

export default function Targets({ navigate, onLaunch }) {
    const { data, error, loading, reload } = useLoader(() => api('/targets'), []);
    const targets = data || [];
    const [adding, setAdding] = useState(false);
    const [tests, setTests] = useState({});
    const [expanded, setExpanded] = useState(null);
    const [dialog, setDialog] = useState(null);
    const notify = useNotify();

    // Every per-target dialog reads something from the instance first. Opening
    // the dialog immediately and filling it in when the call returns is what
    // stops a click looking like it did nothing for forty seconds.
    const openWith = useCallback(async (kind, target, fetcher) => {
        setDialog({ kind, target, loading: true, payload: null });
        try {
            const payload = await fetcher();
            setDialog((d) => (d && d.kind === kind ? { ...d, loading: false, payload } : d));
        } catch (e) {
            setDialog(null);
            throw e;
        }
    }, []);

    const runTest = async (target) => {
        const r = await api(`/targets/${encodeURIComponent(target.id)}/test`, { method: 'POST' });
        setTests((t) => ({ ...t, [target.id]: r }));
        setExpanded(target.id);
        reload();
    };

    return (
        <div>
            <PageHead>
                <Heading level={1}>Targets</Heading>
                <Toolbar>
                    <Button
                        label={adding ? 'Cancel' : 'Add target'}
                        onClick={() => setAdding((v) => !v)}
                    />
                    <Button
                        appearance="primary"
                        label="Launch a run"
                        onClick={() => onLaunch({ scenario: null })}
                    />
                </Toolbar>
            </PageHead>

            {adding && (
                <AddTargetForm
                    onCancel={() => setAdding(false)}
                    onCreated={() => {
                        setAdding(false);
                        notify('Target created', 'success');
                        reload();
                    }}
                />
            )}

            {loading && !data ? (
                <P>
                    <WaitSpinner /> Loading targets&hellip;
                </P>
            ) : error ? (
                <Panel>
                    <Muted>Could not load targets: {error}</Muted>
                </Panel>
            ) : !targets.length ? (
                <Panel>
                    <Muted>
                        No targets yet. Add the Splunk instance you want to drive load at, then use
                        Test to check Regulator can reach it and dispatch a search.
                    </Muted>
                </Panel>
            ) : (
                <Panel flush>
                    <Table rowExpansion="controlled" horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Name</Table.HeadCell>
                            <Table.HeadCell>Management URL</Table.HeadCell>
                            <Table.HeadCell>App / owner</Table.HeadCell>
                            <Table.HeadCell>TLS</Table.HeadCell>
                            <Table.HeadCell>Health</Table.HeadCell>
                            <Table.HeadCell>Added</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {targets.map((t) => (
                                <Table.Row
                                    key={t.id}
                                    data={t}
                                    expanded={expanded === t.id}
                                    onExpansion={() =>
                                        setExpanded((e) => (e === t.id ? null : t.id))
                                    }
                                    expansionRow={
                                        <Table.Row key={`${t.id}-detail`}>
                                            <Table.Cell colSpan={6}>
                                                <TestResult result={tests[t.id]} />
                                            </Table.Cell>
                                        </Table.Row>
                                    }
                                    actionPrimary={
                                        <BusyButton
                                            appearance="subtle"
                                            label="Test"
                                            onClick={(e, row) => runTest(row)}
                                        />
                                    }
                                    actionsSecondary={
                                        <Menu>
                                            <Menu.Item
                                                onClick={(e, row) =>
                                                    openWith('report', row, () =>
                                                        api(
                                                            `/targets/${encodeURIComponent(row.id)}/report`,
                                                            { method: 'POST' }
                                                        )
                                                    )
                                                }
                                            >
                                                Report
                                            </Menu.Item>
                                            <Menu.Item
                                                onClick={(e, row) =>
                                                    openWith('cache', row, () =>
                                                        api(`/targets/${encodeURIComponent(row.id)}/cache`)
                                                    )
                                                }
                                            >
                                                SmartStore cache
                                            </Menu.Item>
                                            <Menu.Item
                                                onClick={(e, row) =>
                                                    openWith('evict', row, () =>
                                                        api(
                                                            `/targets/${encodeURIComponent(row.id)}/cache`
                                                        ).catch(() => null)
                                                    )
                                                }
                                            >
                                                Evict cache…
                                            </Menu.Item>
                                            <Menu.Item
                                                onClick={(e, row) =>
                                                    openWith('purge', row, () =>
                                                        api(
                                                            `/targets/${encodeURIComponent(row.id)}/cache`
                                                        ).catch(() => null)
                                                    )
                                                }
                                            >
                                                Purge cache…
                                            </Menu.Item>
                                            <Menu.Divider />
                                            <Menu.Item
                                                onClick={(e, row) => navigate(`target/${encodeURIComponent(row.id)}`)}
                                            >
                                                Cache history
                                            </Menu.Item>
                                            <Menu.Item
                                                onClick={(e, row) =>
                                                    setDialog({ kind: 'saved', target: row })
                                                }
                                            >
                                                Saved searches
                                            </Menu.Item>
                                            <Menu.Divider />
                                            <Menu.Item
                                                onClick={(e, row) =>
                                                    setDialog({ kind: 'delete', target: row })
                                                }
                                            >
                                                Delete
                                            </Menu.Item>
                                        </Menu>
                                    }
                                >
                                    <Table.Cell>{t.name || '(unnamed)'}</Table.Cell>
                                    <Table.Cell>
                                        <Mono $break>{t.mgmt_url || ''}</Mono>
                                        {t.web_url && (
                                            <div>
                                                <Muted $small>{t.web_url}</Muted>
                                            </div>
                                        )}
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Mono>
                                            {t.app || '–'}
                                            {t.owner ? ` / ${t.owner}` : ''}
                                        </Mono>
                                        {t.indexer_urls && (
                                            <div>
                                                <Muted $small>
                                                    {String(t.indexer_urls).split(',').length}{' '}
                                                    indexer(s)
                                                </Muted>
                                            </div>
                                        )}
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Chip appearance={t.verify_tls === false ? 'warning' : 'success'}>
                                            {t.verify_tls === false ? 'off' : 'on'}
                                        </Chip>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <HealthPill health={t.health} detail={t.health_detail} />
                                    </Table.Cell>
                                    <Table.Cell>
                                        <RelTime value={t.created_at} />
                                    </Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                </Panel>
            )}

            {dialog && dialog.kind === 'report' && (
                <ReportModal
                    report={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                />
            )}
            {dialog && dialog.kind === 'cache' && (
                <CacheModal
                    cache={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                />
            )}
            {dialog && dialog.kind === 'evict' && (
                <EvictModal
                    targetId={dialog.target.id}
                    cache={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                    onDone={reload}
                />
            )}
            {dialog && dialog.kind === 'purge' && (
                <PurgeModal
                    targetId={dialog.target.id}
                    cache={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                    onDone={reload}
                />
            )}
            {dialog && dialog.kind === 'saved' && (
                <SavedSearchesModal
                    target={dialog.target}
                    onClose={() => setDialog(null)}
                    onImport={(conf, name) => setDialog({ kind: 'import', conf, name })}
                />
            )}
            {dialog && dialog.kind === 'import' && (
                <ImportScenarioModal
                    prefill={dialog.conf}
                    suggestedName={dialog.name}
                    onClose={() => setDialog(null)}
                    onCreated={() => {
                        setDialog(null);
                        navigate('scenarios');
                    }}
                />
            )}
            {dialog && dialog.kind === 'delete' && (
                <ConfirmModal
                    title="Delete target"
                    confirmLabel="Delete"
                    onClose={() => setDialog(null)}
                    onConfirm={async () => {
                        await api(`/targets/${encodeURIComponent(dialog.target.id)}`, {
                            method: 'DELETE',
                        });
                        notify('Target deleted', 'success');
                        reload();
                    }}
                >
                    <P>
                        Delete <strong>{dialog.target.name || dialog.target.id}</strong>? Past runs
                        against it stay in the run history, but you will not be able to launch new
                        ones without adding it again.
                    </P>
                </ConfirmModal>
            )}
        </div>
    );
}

Targets.propTypes = {
    navigate: PropTypes.func.isRequired,
    onLaunch: PropTypes.func.isRequired,
};

/** What a Test told us, in the row's expansion. */
function TestResult({ result }) {
    if (!result) {
        return <Muted>Press Test to check Regulator can reach this instance.</Muted>;
    }
    if (!result.ok) {
        return (
            <>
                <Chip appearance="error">failed</Chip>{' '}
                <Mono>{result.detail || 'no detail given'}</Mono>
            </>
        );
    }
    const bits = [];
    if (result.version) {
        bits.push(`version ${result.version}`);
    }
    if (result.roles && result.roles.length) {
        bits.push(`roles ${result.roles.join(', ')}`);
    }
    if (!isBlank(result.cores)) {
        bits.push(`${result.cores} cores`);
    }
    if (!isBlank(result.max_hist_searches)) {
        bits.push(`ceiling ${fmtInt(result.max_hist_searches)} concurrent historical searches`);
    }
    return (
        <>
            <Chip appearance="success">reachable</Chip> <Mono>{bits.join(' · ')}</Mono>{' '}
            {result.detail && <Muted>{result.detail}</Muted>}
        </>
    );
}

TestResult.propTypes = { result: PropTypes.object };

/** The add form: a Splunk instance and, optionally, the indexers behind it. */
function AddTargetForm({ onCancel, onCreated }) {
    const [form, setForm] = useState(EMPTY_TARGET);
    const set = (k) => (e, data) => setForm((f) => ({ ...f, [k]: data.value }));

    const create = async () => {
        const body = {
            name: form.name,
            mgmt_url: form.mgmt_url,
            verify_tls: form.verify_tls,
        };
        [
            'web_url',
            'token',
            'username',
            'password',
            'app',
            'owner',
            'api_version',
            'indexer_token',
            'indexer_username',
            'indexer_password',
        ].forEach((k) => {
            const v = (form[k] || '').trim();
            if (v) {
                body[k] = v;
            }
        });
        const idx = form.indexer_urls
            .split(',')
            .map((x) => x.trim())
            .filter(Boolean);
        if (idx.length) {
            body.indexer_urls = idx;
        }
        await api('/targets', { method: 'POST', body });
        setForm(EMPTY_TARGET);
        onCreated();
    };

    return (
        <Panel title="Add a target">
            <Note>
                A bearer token is preferred over a username and password: it can be scoped, revoked
                and never puts an account password in the store. Fill in either the token or both
                credential fields.
            </Note>
            <Grid2>
                <ControlGroup label="Name" labelPosition="top">
                    <Text value={form.name} onChange={set('name')} />
                </ControlGroup>
                <ControlGroup
                    label="Management URL"
                    labelPosition="top"
                    help="splunkd, usually port 8089."
                >
                    <Text
                        value={form.mgmt_url}
                        placeholder="https://splunk.example:8089"
                        onChange={set('mgmt_url')}
                    />
                </ControlGroup>
                <ControlGroup label="Web URL" labelPosition="top" help="Optional.">
                    <Text
                        value={form.web_url}
                        placeholder="https://splunk.example:8000"
                        onChange={set('web_url')}
                    />
                </ControlGroup>
                <ControlGroup label="Bearer token" labelPosition="top" help="Preferred.">
                    <Text type="password" value={form.token} autoComplete="off" onChange={set('token')} />
                </ControlGroup>
                <ControlGroup label="Username" labelPosition="top" help="Only without a token.">
                    <Text value={form.username} autoComplete="off" onChange={set('username')} />
                </ControlGroup>
                <ControlGroup label="Password" labelPosition="top" help="Only without a token.">
                    <Text
                        type="password"
                        value={form.password}
                        autoComplete="new-password"
                        onChange={set('password')}
                    />
                </ControlGroup>
                <ControlGroup label="App context" labelPosition="top">
                    <Text value={form.app} placeholder="search" onChange={set('app')} />
                </ControlGroup>
                <ControlGroup label="Owner" labelPosition="top">
                    <Text value={form.owner} placeholder="nobody" onChange={set('owner')} />
                </ControlGroup>
                <ControlGroup label="API version" labelPosition="top" help="Optional.">
                    <Text value={form.api_version} onChange={set('api_version')} />
                </ControlGroup>
            </Grid2>

            <SectionHeading>Indexers, for SmartStore</SectionHeading>
            <Note>
                The cache lives on the indexers. Against a search head, list their management URLs
                (or leave empty to discover the search peers) and, if the search head&apos;s
                credential is not valid on them, a credential that is.
            </Note>
            <Grid2>
                <ControlGroup
                    label="Indexer management URLs"
                    labelPosition="top"
                    help="Comma separated."
                >
                    <Text
                        value={form.indexer_urls}
                        placeholder="https://idx1:8089, https://idx2:8089"
                        onChange={set('indexer_urls')}
                    />
                </ControlGroup>
                <ControlGroup label="Indexer bearer token" labelPosition="top">
                    <Text
                        type="password"
                        value={form.indexer_token}
                        autoComplete="off"
                        onChange={set('indexer_token')}
                    />
                </ControlGroup>
                <ControlGroup label="Indexer username" labelPosition="top">
                    <Text
                        value={form.indexer_username}
                        autoComplete="off"
                        onChange={set('indexer_username')}
                    />
                </ControlGroup>
                <ControlGroup label="Indexer password" labelPosition="top">
                    <Text
                        type="password"
                        value={form.indexer_password}
                        autoComplete="new-password"
                        onChange={set('indexer_password')}
                    />
                </ControlGroup>
            </Grid2>

            <Checkbox
                checked={form.verify_tls}
                onChange={() => setForm((f) => ({ ...f, verify_tls: !f.verify_tls }))}
            >
                Verify TLS certificates
            </Checkbox>

            <Toolbar>
                <BusyButton appearance="primary" label="Create target" onClick={create} />
                <Button appearance="secondary" label="Cancel" onClick={onCancel} />
            </Toolbar>
        </Panel>
    );
}

AddTargetForm.propTypes = {
    onCancel: PropTypes.func.isRequired,
    onCreated: PropTypes.func.isRequired,
};
