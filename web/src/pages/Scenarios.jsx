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
import React, { useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Card from '@splunk/react-ui/Card';
import CardLayout from '@splunk/react-ui/CardLayout';
import DL from '@splunk/react-ui/DefinitionList';
import Dropdown from '@splunk/react-ui/Dropdown';
import Heading from '@splunk/react-ui/Heading';
import Menu from '@splunk/react-ui/Menu';
import Multiselect from '@splunk/react-ui/Multiselect';
import P from '@splunk/react-ui/Paragraph';
import Select from '@splunk/react-ui/Select';
import Text from '@splunk/react-ui/Text';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import BusyButton from '../components/BusyButton';
import DuplicateScenarioModal from '../modals/DuplicateScenarioModal';
import RenameScenarioModal from '../modals/RenameScenarioModal';
import ImportScenarioModal from '../modals/ImportScenarioModal';
import DeleteScenarioModal from '../modals/DeleteScenarioModal';
import ScenarioDetailModal from '../modals/ScenarioDetailModal';
import ScenarioEditModal from '../modals/ScenarioEditModal';
import ScenarioTagsModal from '../modals/ScenarioTagsModal';
import useLoader from '../useLoader';
import { Mono, Muted, Note, PageHead, Panel, Toolbar } from '../components/primitives';
import { api, apiDownloadUrl, apiUpload } from '../api';
import { fmtDur, fmtInt, isBlank } from '../format';
import { useNotify } from '../Notifications';


// The filter survives page changes, reloads and image rebuilds by living in
// the browser, like the Runs page's column choice. Per browser, not per user:
// it is a view preference, not something the server needs to know.
const FILTER_KEY = 'regulator.scenarios.filter';
const DEFAULT_FILTER = { q: '', tags: [], origin: 'all', fit: 'fits' };
// Full means every declared sourcetype is in the target's census; unknown
// means the scenario declares none, so it cannot fail the check.
const PASSING = new Set(['full', 'unknown']);
const TAGS_SHOWN = 3;

function readFilter() {
    try {
        const stored = JSON.parse(window.localStorage.getItem(FILTER_KEY) || 'null');
        if (stored && typeof stored === 'object') {
            return {
                q: typeof stored.q === 'string' ? stored.q : '',
                tags: Array.isArray(stored.tags) ? stored.tags.map(String) : [],
                origin: ['all', 'builtin', 'user'].includes(stored.origin) ? stored.origin : 'all',
                fit: typeof stored.fit === 'string' ? stored.fit : 'fits',
            };
        }
    } catch (e) {
        // Storage disabled or a stale value: start from the defaults.
    }
    return DEFAULT_FILTER;
}

const TagRow = styled.div`
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: ${variables.spacingXSmall} 0 ${variables.spacingSmall};
`;

const TagPill = styled.button`
    font-size: ${variables.fontSizeSmall};
    line-height: 18px;
    padding: 0 8px;
    border-radius: 9px;
    border: 1px solid ${variables.borderColor};
    background: ${(props) => (props.$active ? variables.interactiveColorPrimary : 'transparent')};
    color: ${(props) => (props.$active ? variables.contentColorInverted : variables.contentColorDefault)};
    cursor: pointer;
    max-width: 160px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
`;

/** A scenario's tags as small pills: the first few, and the rest on request. */
function Tags({ tags, active, onPick }) {
    const [open, setOpen] = useState(false);
    if (!tags.length) {
        return null;
    }
    const shown = open ? tags : tags.slice(0, TAGS_SHOWN);
    return (
        <TagRow>
            {shown.map((t) => (
                <TagPill
                    key={t}
                    type="button"
                    title={`Filter by ${t}`}
                    $active={active.includes(t)}
                    onClick={() => onPick(t)}
                >
                    {t}
                </TagPill>
            ))}
            {tags.length > TAGS_SHOWN && (
                <TagPill type="button" onClick={() => setOpen((v) => !v)}>
                    {open ? 'less' : `+${tags.length - TAGS_SHOWN}`}
                </TagPill>
            )}
        </TagRow>
    );
}

Tags.propTypes = {
    tags: PropTypes.arrayOf(PropTypes.string).isRequired,
    active: PropTypes.arrayOf(PropTypes.string).isRequired,
    onPick: PropTypes.func.isRequired,
};

export default function Scenarios({ onLaunch }) {
    const { data, error, loading, reload } = useLoader(() => api('/scenarios'), []);
    const scenarios = useMemo(() => data || [], [data]);
    const [dialog, setDialog] = useState(null);
    const [busy, setBusy] = useState(null);
    const fileInput = useRef(null);
    const notify = useNotify();
    // Deployment environment, so it cannot change without a restart: loaded
    // once. A failure simply leaves Push hidden rather than breaking the page.
    const { data: source } = useLoader(() => api('/scenario-source'), []);
    const canPush = !!(source && source.writable);
    const { data: fitData, reload: reloadFit } = useLoader(() => api('/scenario-fit'), []);

    const [filter, setFilter] = useState(readFilter);
    useEffect(() => {
        try {
            window.localStorage.setItem(FILTER_KEY, JSON.stringify(filter));
        } catch (e) {
            // Not worth a message: the filter simply will not be remembered.
        }
    }, [filter]);
    const setF = (patch) => setFilter((f) => ({ ...f, ...patch }));
    const pickTag = (t) =>
        setF({ tags: filter.tags.includes(t) ? filter.tags.filter((x) => x !== t) : [...filter.tags, t] });

    const fitTargets = (fitData && fitData.targets) || [];
    const tested = fitTargets.filter((t) => t.fit);
    const untested = fitTargets.filter((t) => !t.fit);
    // A remembered target that has since been deleted falls back to "any".
    let fitMode = filter.fit;
    if (fitMode.startsWith('t:') && !tested.some((t) => `t:${t.id}` === fitMode)) {
        fitMode = 'fits';
    }
    const fitsFor = (name) =>
        tested.filter((t) => t.fit[name] && PASSING.has(t.fit[name].fit)).map((t) => t.name);

    const allTags = useMemo(
        () => Array.from(new Set(scenarios.flatMap((s) => s.tags || []))).sort(),
        [scenarios]
    );

    const needle = filter.q.trim().toLowerCase();
    const visible = scenarios.filter((s) => {
        if (filter.origin !== 'all' && (s.origin === 'user' ? 'user' : 'builtin') !== filter.origin) {
            return false;
        }
        const tags = s.tags || [];
        if (filter.tags.some((t) => !tags.includes(t))) {
            return false;
        }
        if (needle) {
            const hay = [
                s.name,
                s.description,
                ...tags,
                ...(s.sourcetypes || []),
                ...(s.requires_packs || []),
            ]
                .join(' ')
                .toLowerCase();
            if (!hay.includes(needle)) {
                return false;
            }
        }
        if (fitMode === 'fits' && tested.length) {
            return fitsFor(s.name).length > 0;
        }
        if (fitMode.startsWith('t:')) {
            const t = tested.find((x) => `t:${x.id}` === fitMode);
            return !!(t && t.fit[s.name] && PASSING.has(t.fit[s.name].fit));
        }
        return true;
    });
    const hiddenByFit = fitMode !== 'all' && tested.length ? scenarios.length - visible.length : 0;
    const filtered =
        filter.q.trim() || filter.tags.length || filter.origin !== 'all' || fitMode !== 'all';

    const refreshFit = async () => {
        const results = await Promise.allSettled(
            fitTargets.map((t) =>
                api(`/targets/${encodeURIComponent(t.id)}/report`, { method: 'POST' })
            )
        );
        const failed = results.filter((r) => r.status === 'rejected').length;
        reloadFit();
        if (failed) {
            notify(`${failed} of ${results.length} target report(s) failed`, 'warning');
        } else {
            notify('Fit refreshed from a new report on every target', 'success');
        }
    };

    const afterChange = () => {
        reload();
        reloadFit();
    };

    const openEdit = async (name) => {
        try {
            const d = await api(`/scenarios/${encodeURIComponent(name)}`);
            setDialog({ kind: 'edit', name, payload: d });
        } catch (e) {
            notify(e.message, 'error');
        }
    };

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

            <Toolbar>
                <Text
                    value={filter.q}
                    placeholder="Search name, description, tag, sourcetype"
                    onChange={(e, { value }) => setF({ q: value })}
                    style={{ width: 300 }}
                    canClear
                />
                <Multiselect
                    compact
                    values={filter.tags}
                    placeholder="Tags"
                    onChange={(e, { values }) => setF({ tags: values.map(String) })}
                    style={{ width: 220 }}
                >
                    {allTags.map((t) => (
                        <Multiselect.Option key={t} label={t} value={t} />
                    ))}
                </Multiselect>
                <Select value={filter.origin} onChange={(e, { value }) => setF({ origin: value })}>
                    <Select.Option label="Built in and imported" value="all" />
                    <Select.Option label="Built in" value="builtin" />
                    <Select.Option label="Imported" value="user" />
                </Select>
                <Select value={fitMode} onChange={(e, { value }) => setF({ fit: value })}>
                    <Select.Option label="Fits a target" value="fits" />
                    {tested.map((t) => (
                        <Select.Option key={t.id} label={`Fits ${t.name}`} value={`t:${t.id}`} />
                    ))}
                    <Select.Option label="Any corpus" value="all" />
                </Select>
                {fitTargets.length > 0 && (
                    <BusyButton appearance="subtle" label="Refresh fit" onClick={refreshFit} />
                )}
                {filtered && (
                    <Button
                        appearance="subtle"
                        label="Clear filters"
                        onClick={() => setFilter({ ...DEFAULT_FILTER, fit: 'all' })}
                    />
                )}
            </Toolbar>
            {data && (
                <Note>
                    Showing {visible.length} of {scenarios.length}.
                    {hiddenByFit > 0 &&
                        ` ${hiddenByFit} hidden because no ${
                            fitMode === 'fits' ? 'target' : 'chosen target'
                        } holds the sourcetypes they search (from each target's last Report).`}
                    {fitMode !== 'all' && !tested.length && fitTargets.length > 0 &&
                        ' No target has a Report yet, so nothing is hidden by fit: press Refresh fit.'}
                    {fitMode !== 'all' && !fitTargets.length &&
                        ' Add a target and run its Report to see which scenarios it can run.'}
                    {fitMode !== 'all' && tested.length > 0 && untested.length > 0 &&
                        ` Not reported yet: ${untested.map((t) => t.name).join(', ')}.`}
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
            ) : !visible.length ? (
                <Panel>
                    <Muted>No scenario matches these filters.</Muted>
                </Panel>
            ) : (
                <CardLayout cardMinWidth={300} wrapCards>
                    {visible.map((s) => (
                        <Card key={s.name}>
                            <Card.Header
                                title={s.name}
                                subtitle={s.origin === 'user' ? 'imported' : 'built in'}
                            />
                            <Card.Body>
                                <P>{s.description || 'No description.'}</P>
                                <Tags tags={s.tags || []} active={filter.tags} onPick={pickTag} />
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
                                    {tested.length > 0 && (
                                        <>
                                            <DL.Term>Fits</DL.Term>
                                            <DL.Description>
                                                {(s.sourcetypes || []).length
                                                    ? fitsFor(s.name).join(', ') ||
                                                      'no reported target'
                                                    : 'not checked: declares no sourcetypes'}
                                            </DL.Description>
                                        </>
                                    )}
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
                                    <Button
                                        label="Edit"
                                        onClick={() =>
                                            s.origin === 'user'
                                                ? openEdit(s.name)
                                                : setDialog({ kind: 'duplicate', scenario: s })
                                        }
                                    />
                                    <Dropdown
                                        toggle={
                                            <Button
                                                label={busy === s.name ? 'Pushing\u2026' : 'More'}
                                                isMenu
                                                disabled={busy === s.name}
                                            />
                                        }
                                    >
                                        <Menu>
                                            <Menu.Item
                                                onClick={() => setDialog({ kind: 'tags', scenario: s })}
                                            >
                                                Tags&hellip;
                                            </Menu.Item>
                                            <Menu.Item
                                                onClick={() =>
                                                    setDialog({ kind: 'duplicate', scenario: s })
                                                }
                                            >
                                                Duplicate&hellip;
                                            </Menu.Item>
                                            {s.origin === 'user' && (
                                                <Menu.Item
                                                    onClick={() => setDialog({ kind: 'rename', scenario: s })}
                                                >
                                                    Rename&hellip;
                                                </Menu.Item>
                                            )}
                                            {/* A plain link, not a fetch: the browser
                                                streams the archive to disk and sends
                                                the session cookie itself. */}
                                            <Menu.Item
                                                to={apiDownloadUrl(
                                                    `/scenarios/${encodeURIComponent(s.name)}/export`
                                                )}
                                            >
                                                Download
                                            </Menu.Item>
                                            {s.origin === 'user' && (
                                                <Menu.Item
                                                    onClick={() => setDialog({ kind: 'delete', name: s.name })}
                                                >
                                                    Delete&hellip;
                                                </Menu.Item>
                                            )}
                                            {canPush && (
                                                <Menu.Item onClick={() => onPush(s.name)}>
                                                    Push
                                                </Menu.Item>
                                            )}
                                        </Menu>
                                    </Dropdown>
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
                    onDelete={() => setDialog({ kind: 'delete', name: dialog.name })}
                />
            )}
            {dialog && dialog.kind === 'delete' && (
                <DeleteScenarioModal
                    name={dialog.name}
                    source={source}
                    onClose={() => setDialog(null)}
                    onConfirm={async (remote) => {
                        const result = await api(
                            `/scenarios/${encodeURIComponent(dialog.name)}${remote ? '?remote=true' : ''}`,
                            { method: 'DELETE' }
                        );
                        if (result && result.remote_key) {
                            notify(
                                result.remote_existed
                                    ? `Scenario deleted, and ${result.remote_key} removed from the scenario source`
                                    : `Scenario deleted (${result.remote_key} was not in the scenario source)`,
                                'success'
                            );
                        } else {
                            notify('Scenario deleted', 'success');
                        }
                        afterChange();
                    }}
                />
            )}
            {dialog && dialog.kind === 'edit' && (
                <ScenarioEditModal
                    scenario={dialog.payload}
                    onClose={() => setDialog(null)}
                    onSaved={() => {
                        setDialog(null);
                        afterChange();
                    }}
                />
            )}
            {dialog && dialog.kind === 'duplicate' && (
                <DuplicateScenarioModal
                    scenario={dialog.scenario}
                    onClose={() => setDialog(null)}
                    onCreated={(created) => {
                        notify(`Created \u201c${created.name}\u201d`, 'success');
                        afterChange();
                        openEdit(created.name);
                    }}
                />
            )}
            {dialog && dialog.kind === 'rename' && (
                <RenameScenarioModal
                    scenario={dialog.scenario}
                    onClose={() => setDialog(null)}
                    onRenamed={(renamed) => {
                        setDialog(null);
                        const moved = renamed.runs_updated
                            ? ` (${renamed.runs_updated} run${renamed.runs_updated === 1 ? '' : 's'} moved)`
                            : '';
                        if (renamed.warning) {
                            notify(
                                `Renamed \u201c${renamed.old_name}\u201d to \u201c${renamed.name}\u201d, but ${renamed.warning}`,
                                'warning'
                            );
                        } else {
                            notify(
                                `Renamed \u201c${renamed.old_name}\u201d to \u201c${renamed.name}\u201d${moved}`,
                                'success'
                            );
                        }
                        afterChange();
                    }}
                />
            )}
            {dialog && dialog.kind === 'tags' && (
                <ScenarioTagsModal
                    scenario={dialog.scenario}
                    knownTags={allTags}
                    onClose={() => setDialog(null)}
                    onSaved={() => {
                        setDialog(null);
                        notify(`Tags saved for \u201c${dialog.scenario.name}\u201d`, 'success');
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
                        afterChange();
                    }}
                />
            )}
        </div>
    );
}

Scenarios.propTypes = {
    onLaunch: PropTypes.func.isRequired,
};
