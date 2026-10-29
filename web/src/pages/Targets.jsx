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
import styled from 'styled-components';
import { variables } from '@splunk/themes';

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

// The write-only credentials. An edit sends one only when it was typed, so a
// blank field keeps what is stored.
const SECRETS = ['token', 'password', 'indexer_token', 'indexer_password'];
// Sent on an edit only when it differs from what the target has now.
const PLAIN = ['name', 'mgmt_url', 'web_url', 'app', 'owner', 'api_version', 'verify_tls'];

/** The form's starting values: blank for a new target, the target's own for an edit. */
function formFrom(target) {
    if (!target) {
        return EMPTY_TARGET;
    }
    return {
        ...EMPTY_TARGET,
        name: target.name || '',
        mgmt_url: target.mgmt_url || '',
        web_url: target.web_url || '',
        app: target.app || '',
        owner: target.owner || '',
        api_version: target.api_version || '',
        indexer_urls: (target.indexer_urls || '').split(',').filter(Boolean).join(', '),
        indexer_username: target.indexer_username || '',
        verify_tls: target.verify_tls !== false,
    };
}

function splitUrls(text) {
    return (text || '')
        .split(',')
        .map((x) => x.trim())
        .filter(Boolean);
}

/** A table cell with more than one line: Splunk's cells lay children out in a row. */
const Stack = styled.div`
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    min-width: 0;
`;

const ResultHead = styled.div`
    display: flex;
    align-items: center;
    gap: ${variables.spacingSmall};
    flex-wrap: wrap;

    & > :first-child {
        flex-shrink: 0;
    }
`;

/** A form field with a control under it: ControlGroup lays its children in a row. */
const Field = styled.div`
    display: flex;
    flex-direction: column;
    gap: ${variables.spacingXSmall};
    width: 100%;
`;

/** What a Test found, as label and value pairs rather than one run-on line. */
const Facts = styled.dl`
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: ${variables.spacingXSmall} ${variables.spacingLarge};
    margin: ${variables.spacingSmall} 0 0;

    dt {
        color: ${variables.contentColorMuted};
    }
    dd {
        margin: 0;
        display: flex;
        flex-wrap: wrap;
        gap: ${variables.spacingXSmall};
    }
`;

export default function Targets({ navigate, onLaunch }) {
    const { data, error, loading, reload } = useLoader(() => api('/targets'), []);
    const targets = data || [];
    const [adding, setAdding] = useState(false);
    const [editing, setEditing] = useState(null);
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
                        onClick={() => {
                            setEditing(null);
                            setAdding((v) => !v);
                        }}
                    />
                    <Button
                        appearance="primary"
                        label="Launch a run"
                        onClick={() => onLaunch({ scenario: null })}
                    />
                </Toolbar>
            </PageHead>

            {adding && (
                <TargetForm
                    onCancel={() => setAdding(false)}
                    onSaved={() => {
                        setAdding(false);
                        notify('Target created', 'success');
                        reload();
                    }}
                />
            )}
            {editing && (
                <TargetForm
                    key={editing.id}
                    target={editing}
                    onCancel={() => setEditing(null)}
                    onSaved={(saved) => {
                        setEditing(null);
                        setTests((t) => ({ ...t, [saved.id]: undefined }));
                        notify(`Target ${saved.name} updated. Test it to check the change.`, 'success');
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
                                                onClick={(e, row) => {
                                                    setAdding(false);
                                                    setEditing(row);
                                                    window.scrollTo({ top: 0, behavior: 'smooth' });
                                                }}
                                            >
                                                Edit…
                                            </Menu.Item>
                                            <Menu.Divider />
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
                                        <Stack>
                                            <Mono $break>{t.mgmt_url || ''}</Mono>
                                            {t.web_url && <Muted $small>web {t.web_url}</Muted>}
                                        </Stack>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Stack>
                                            <Mono>
                                                {t.app || '–'}
                                                {t.owner ? ` / ${t.owner}` : ''}
                                            </Mono>
                                            {t.indexer_urls && (
                                                <Muted $small>
                                                    {splitUrls(t.indexer_urls).length} indexer(s)
                                                </Muted>
                                            )}
                                        </Stack>
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
            <div>
                <ResultHead>
                    <Chip appearance="error">failed</Chip>
                    <Mono $break>{result.detail || 'no detail given'}</Mono>
                </ResultHead>
            </div>
        );
    }
    return (
        <div>
            <ResultHead>
                <Chip appearance="success">reachable</Chip>
                <Muted>Regulator reached splunkd and read what it can run.</Muted>
            </ResultHead>
            <Facts>
                <dt>Version</dt>
                <dd>
                    <Mono>{result.version || '–'}</Mono>
                </dd>
                <dt>Roles</dt>
                <dd>
                    {result.roles && result.roles.length ? (
                        result.roles.map((role) => (
                            <Chip key={role} appearance="outline">
                                {role}
                            </Chip>
                        ))
                    ) : (
                        <Muted>none reported</Muted>
                    )}
                </dd>
                <dt>Cores</dt>
                <dd>{isBlank(result.cores) ? '–' : fmtInt(result.cores)}</dd>
                <dt>Search ceiling</dt>
                <dd>
                    {isBlank(result.max_hist_searches)
                        ? '–'
                        : `${fmtInt(result.max_hist_searches)} concurrent historical searches`}
                </dd>
            </Facts>
        </div>
    );
}

TestResult.propTypes = { result: PropTypes.object };

/**
 * The add and edit form: a Splunk instance and, optionally, the indexers behind it.
 *
 * Editing keeps the target's id, so its runs, re-runs and baselines stay with
 * it. Credentials are never sent back by the API, so on an edit each secret
 * field starts blank: left blank it keeps what is stored, typed it replaces
 * it, and "Remove" drops it (say, moving from a password to a token).
 */
function TargetForm({ target, onCancel, onSaved }) {
    const editing = Boolean(target);
    const [form, setForm] = useState(() => formFrom(target));
    const [clear, setClear] = useState({});
    const set = (k) => (e, data) => setForm((f) => ({ ...f, [k]: data.value }));
    const toggleClear = (k) => setClear((c) => ({ ...c, [k]: !c[k] }));

    const stored = (k) => editing && target[`has_${k}`];
    const secretProps = (k) => ({
        placeholder: stored(k) ? 'Stored. Leave blank to keep it.' : '',
        disabled: Boolean(clear[k]),
    });

    const createBody = () => {
        const body = { name: form.name, mgmt_url: form.mgmt_url, verify_tls: form.verify_tls };
        ['web_url', 'username', 'app', 'owner', 'api_version', 'indexer_username', ...SECRETS].forEach((k) => {
            const v = (form[k] || '').trim();
            if (v) {
                body[k] = v;
            }
        });
        const idx = splitUrls(form.indexer_urls);
        if (idx.length) {
            body.indexer_urls = idx;
        }
        return body;
    };

    const editBody = () => {
        const before = formFrom(target);
        const body = {};
        PLAIN.forEach((k) => {
            const v = typeof form[k] === 'string' ? form[k].trim() : form[k];
            if (v !== before[k] && (v !== '' || k === 'web_url')) {
                body[k] = v;
            }
        });
        if (splitUrls(form.indexer_urls).join(',') !== splitUrls(before.indexer_urls).join(',')) {
            body.indexer_urls = splitUrls(form.indexer_urls);
        }
        ['username', 'indexer_username', ...SECRETS].forEach((k) => {
            const v = (form[k] || '').trim();
            if (v && !clear[k]) {
                body[k] = v;
            }
        });
        const cleared = Object.keys(clear).filter((k) => clear[k]);
        if (cleared.length) {
            body.clear = cleared;
        }
        return body;
    };

    const save = async () => {
        const saved = editing
            ? await api(`/targets/${encodeURIComponent(target.id)}`, { method: 'PATCH', body: editBody() })
            : await api('/targets', { method: 'POST', body: createBody() });
        setForm(EMPTY_TARGET);
        onSaved(saved);
    };

    const removeBox = (k, label) =>
        stored(k) && (
            <Checkbox checked={Boolean(clear[k])} onChange={() => toggleClear(k)}>
                {label}
            </Checkbox>
        );

    return (
        <Panel title={editing ? `Edit target ${target.name}` : 'Add a target'}>
            <Note>
                {editing
                    ? 'Only what you change is saved. Credentials are never shown: leave a credential blank to keep the stored one, or type a new one to replace it, for example a token that has expired. The target keeps its id, so its runs and baselines stay with it.'
                    : 'A bearer token is preferred over a username and password: it can be scoped, revoked and never puts an account password in the store. Fill in either the token or both credential fields.'}
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
                    <Field>
                        <Text
                            type="password"
                            value={form.token}
                            autoComplete="off"
                            onChange={set('token')}
                            {...secretProps('token')}
                        />
                        {removeBox('token', 'Remove the stored token')}
                    </Field>
                </ControlGroup>
                <ControlGroup label="Username" labelPosition="top" help="Only without a token.">
                    <Text
                        value={form.username}
                        autoComplete="off"
                        onChange={set('username')}
                        {...secretProps('username')}
                    />
                </ControlGroup>
                <ControlGroup label="Password" labelPosition="top" help="Only without a token.">
                    <Field>
                        <Text
                            type="password"
                            value={form.password}
                            autoComplete="new-password"
                            onChange={set('password')}
                            {...secretProps('password')}
                        />
                        {stored('password') && (
                            <Checkbox
                                checked={Boolean(clear.password)}
                                onChange={() =>
                                    setClear((c) => ({
                                        ...c,
                                        password: !c.password,
                                        username: !c.password,
                                    }))
                                }
                            >
                                Remove the stored username and password
                            </Checkbox>
                        )}
                    </Field>
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
                    <Field>
                        <Text
                            type="password"
                            value={form.indexer_token}
                            autoComplete="off"
                            onChange={set('indexer_token')}
                            {...secretProps('indexer_token')}
                        />
                        {removeBox('indexer_token', 'Remove the stored indexer token')}
                    </Field>
                </ControlGroup>
                <ControlGroup label="Indexer username" labelPosition="top">
                    <Text
                        value={form.indexer_username}
                        autoComplete="off"
                        onChange={set('indexer_username')}
                    />
                </ControlGroup>
                <ControlGroup label="Indexer password" labelPosition="top">
                    <Field>
                        <Text
                            type="password"
                            value={form.indexer_password}
                            autoComplete="new-password"
                            onChange={set('indexer_password')}
                            {...secretProps('indexer_password')}
                        />
                        {removeBox('indexer_password', 'Remove the stored indexer password')}
                    </Field>
                </ControlGroup>
            </Grid2>

            <Checkbox
                checked={form.verify_tls}
                onChange={() => setForm((f) => ({ ...f, verify_tls: !f.verify_tls }))}
            >
                Verify TLS certificates
            </Checkbox>

            <Toolbar>
                <BusyButton
                    appearance="primary"
                    label={editing ? 'Save changes' : 'Create target'}
                    onClick={save}
                />
                <Button appearance="secondary" label="Cancel" onClick={onCancel} />
            </Toolbar>
        </Panel>
    );
}

TargetForm.propTypes = {
    target: PropTypes.object,
    onCancel: PropTypes.func.isRequired,
    onSaved: PropTypes.func.isRequired,
};
