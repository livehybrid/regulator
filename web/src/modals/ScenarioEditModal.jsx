/*
 * Edit one of the operator's own scenarios: its saved searches, its details,
 * or its files as text.
 *
 * The server stages every edit on a copy and swaps it in only once it loads
 * and lints, so a bad save is an error message here and the scenario that was
 * there still runs. Built-in scenarios never reach this dialog: they are
 * duplicated first (see DuplicateScenarioModal).
 */
import React, { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Message from '@splunk/react-ui/Message';
import Multiselect from '@splunk/react-ui/Multiselect';
import P from '@splunk/react-ui/Paragraph';
import Switch from '@splunk/react-ui/Switch';
import TabBar from '@splunk/react-ui/TabBar';
import Text from '@splunk/react-ui/Text';
import TextArea from '@splunk/react-ui/TextArea';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Actions, Grid2, Muted, Note, Toolbar } from '../components/primitives';
import { api } from '../api';
import { useNotify } from '../Notifications';

// A card's rows (name and switches, SPL, time range and cron) are one part,
// so they sit spacingSmall apart rather than touching.
const SearchCard = styled.div`
    display: flex;
    flex-direction: column;
    gap: ${variables.spacingSmall};
    border: 1px solid ${variables.borderColor};
    border-radius: 4px;
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
    margin-bottom: ${variables.spacingSmall};
`;

// TextArea takes no style for its input, so the SPL and the files are set
// in the monospace face from outside.
const MonoArea = styled.div`
    textarea {
        font-family: ${variables.monoFontFamily};
    }
`;

const blankRow = () => ({
    key: `new-${Math.random().toString(36).slice(2, 10)}`,
    original_name: null,
    name: '',
    search: '',
    earliest: '-15m',
    latest: 'now',
    cron: '*/15 * * * *',
    scheduled: true,
    disabled: false,
});

function SearchEditor({ row, onChange, onRemove }) {
    const set = (key) => (e, { value }) => onChange({ ...row, [key]: value });
    return (
        <SearchCard>
            <Toolbar>
                <Text
                    value={row.name}
                    placeholder="Saved search name"
                    onChange={set('name')}
                    style={{ flex: '1 1 260px' }}
                />
                <Switch
                    selected={row.scheduled}
                    onClick={() => onChange({ ...row, scheduled: !row.scheduled })}
                    appearance="toggle"
                >
                    Scheduled
                </Switch>
                <Switch
                    selected={!row.disabled}
                    onClick={() => onChange({ ...row, disabled: !row.disabled })}
                    appearance="toggle"
                >
                    Enabled
                </Switch>
                <Button appearance="subtle" label="Delete" onClick={onRemove} />
            </Toolbar>
            <MonoArea>
                <TextArea
                    value={row.search}
                    placeholder="SPL"
                    rowsMin={2}
                    rowsMax={10}
                    onChange={set('search')}
                />
            </MonoArea>
            <Toolbar>
                <Text
                    value={row.earliest}
                    placeholder="earliest"
                    onChange={set('earliest')}
                    style={{ width: 140 }}
                />
                <Text
                    value={row.latest}
                    placeholder="latest"
                    onChange={set('latest')}
                    style={{ width: 140 }}
                />
                <Text
                    value={row.cron}
                    placeholder="cron"
                    onChange={set('cron')}
                    style={{ width: 180 }}
                />
            </Toolbar>
        </SearchCard>
    );
}

SearchEditor.propTypes = {
    row: PropTypes.object.isRequired,
    onChange: PropTypes.func.isRequired,
    onRemove: PropTypes.func.isRequired,
};

export default function ScenarioEditModal({ scenario, onClose, onSaved }) {
    const notify = useNotify();
    const hasConf = !!scenario.searches_file;
    const [tab, setTab] = useState(hasConf ? 'searches' : 'details');
    const [rows, setRows] = useState(() =>
        (scenario.saved_searches_rows || []).map((r) => ({
            ...r,
            key: `orig-${r.name}`,
            original_name: r.name,
        }))
    );
    const [details, setDetails] = useState({
        description: scenario.description || '',
        tags: scenario.tags || [],
        index: scenario.index || '',
        sourcetypes: scenario.sourcetypes || [],
    });
    const [files, setFiles] = useState(scenario.files || {});
    const [dirty, setDirty] = useState({ searches: false, details: false, sourcetypes: false, raw: false });
    const [filter, setFilter] = useState('');
    const [error, setError] = useState(null);

    const mark = (part) => setDirty((d) => ({ ...d, [part]: true }));
    const anyDirty = dirty.searches || dirty.details || dirty.raw;

    const shown = useMemo(() => {
        const needle = filter.trim().toLowerCase();
        return needle
            ? rows.filter(
                  (r) =>
                      r.name.toLowerCase().includes(needle) ||
                      r.search.toLowerCase().includes(needle)
              )
            : rows;
    }, [rows, filter]);

    const save = async () => {
        setError(null);
        let body;
        if (dirty.raw) {
            // Raw text is the more specific statement, so it wins outright.
            body = { files };
        } else {
            body = {};
            if (dirty.details) {
                body.description = details.description;
                body.tags = details.tags;
                body.index = details.index;
            }
            if (dirty.sourcetypes) {
                body.sourcetypes = details.sourcetypes;
            }
            if (dirty.searches) {
                const blank = rows.find((r) => !r.name.trim() || !r.search.trim());
                if (blank) {
                    setError('Every search needs a name and some SPL.');
                    return;
                }
                body.searches = rows.map(({ key, ...rest }) => rest);
            }
        }
        try {
            const updated = await api(`/scenarios/${encodeURIComponent(scenario.name)}`, {
                method: 'PUT',
                body,
            });
            notify(`Scenario “${scenario.name}” saved`, 'success');
            onSaved(updated);
        } catch (e) {
            // A 422 says what does not load or lint, which is the useful part.
            setError(e.message);
        }
    };

    const tagOptions = details.tags.map((t) => <Multiselect.Option key={t} label={t} value={t} />);
    const stOptions = details.sourcetypes.map((t) => (
        <Multiselect.Option key={t} label={t} value={t} />
    ));

    return (
        <Dialog
            title={`Edit ${scenario.name}`}
            width="1080px"
            onClose={onClose}
            closeLabel={anyDirty ? 'Discard' : 'Close'}
            footer={
                <BusyButton appearance="primary" label="Save" disabled={!anyDirty} onClick={save} />
            }
        >
            <Note>
                Saving changes the scenario&apos;s digest, so runs before and after the edit no
                longer compare as the same workload against a baseline.
            </Note>
            {error && (
                <Message appearance="fill" type="error">
                    {error}
                </Message>
            )}
            <TabBar activeTabId={tab} onChange={(e, { selectedTabId }) => setTab(selectedTabId)}>
                {hasConf && <TabBar.Tab label={`Searches (${rows.length})`} tabId="searches" />}
                <TabBar.Tab label="Details" tabId="details" />
                <TabBar.Tab label="Files" tabId="raw" />
            </TabBar>

            {dirty.raw && tab !== 'raw' && (
                <Note>
                    The files were edited as text, so saving uses the files and ignores changes on the
                    other tabs.
                </Note>
            )}

            {tab === 'searches' && (
                <div>
                    <Actions>
                        <Text
                            value={filter}
                            placeholder="Filter by name or SPL"
                            onChange={(e, { value }) => setFilter(value)}
                            style={{ width: 280 }}
                        />
                        <Button
                            label="Add a search"
                            onClick={() => {
                                setRows((r) => [...r, blankRow()]);
                                setFilter('');
                                mark('searches');
                            }}
                        />
                        <Muted $small>
                            Comments in savedsearches.conf are not kept when saving from here. Keys
                            this form does not show (alert actions, dispatch settings) are.
                        </Muted>
                    </Actions>
                    {!shown.length && <Muted>No searches match.</Muted>}
                    {shown.map((row) => (
                        <SearchEditor
                            key={row.key}
                            row={row}
                            onChange={(next) => {
                                setRows((all) => all.map((r) => (r.key === row.key ? next : r)));
                                mark('searches');
                            }}
                            onRemove={() => {
                                setRows((all) => all.filter((r) => r.key !== row.key));
                                mark('searches');
                            }}
                        />
                    ))}
                </div>
            )}

            {tab === 'details' && (
                <div>
                    <ControlGroup label="Description" labelPosition="top">
                        <TextArea
                            value={details.description}
                            rowsMin={2}
                            onChange={(e, { value }) => {
                                setDetails((d) => ({ ...d, description: value }));
                                mark('details');
                            }}
                        />
                    </ControlGroup>
                    <ControlGroup label="Tags" labelPosition="top" help="Type a tag and press Enter.">
                        <Multiselect
                            values={details.tags}
                            allowNewValues
                            onChange={(e, { values }) => {
                                setDetails((d) => ({ ...d, tags: values.map(String) }));
                                mark('details');
                            }}
                        >
                            {tagOptions}
                        </Multiselect>
                    </ControlGroup>
                    <Grid2>
                        <ControlGroup
                            label="Corpus index"
                            labelPosition="top"
                            help="Used by the empty-corpus check before a run starts."
                        >
                            <Text
                                value={details.index}
                                onChange={(e, { value }) => {
                                    setDetails((d) => ({ ...d, index: value }));
                                    mark('details');
                                }}
                            />
                        </ControlGroup>
                        <ControlGroup
                            label="Corpus sourcetypes"
                            labelPosition="top"
                            help={
                                hasConf
                                    ? 'Worked out from the searches on save unless you change them here.'
                                    : 'Decides which targets this scenario fits.'
                            }
                        >
                            <Multiselect
                                values={details.sourcetypes}
                                allowNewValues
                                onChange={(e, { values }) => {
                                    setDetails((d) => ({ ...d, sourcetypes: values.map(String) }));
                                    mark('sourcetypes');
                                }}
                            >
                                {stOptions}
                            </Multiselect>
                        </ControlGroup>
                    </Grid2>
                    <Muted $small>
                        Changing these fields rewrites scenario.yaml; its comments are kept where a
                        field sits on one line, which is the usual case.
                    </Muted>
                </div>
            )}

            {tab === 'raw' && (
                <div>
                    {!Object.keys(files).length && (
                        <P>
                            <WaitSpinner /> Loading files&hellip;
                        </P>
                    )}
                    {Object.entries(files).map(([name, content]) => (
                        <ControlGroup key={name} label={name} labelPosition="top">
                            <MonoArea style={{ width: '100%' }}>
                                <TextArea
                                    value={content}
                                    rowsMin={8}
                                    rowsMax={30}
                                    onChange={(e, { value }) => {
                                        setFiles((f) => ({ ...f, [name]: value }));
                                        mark('raw');
                                    }}
                                />
                            </MonoArea>
                        </ControlGroup>
                    ))}
                </div>
            )}
        </Dialog>
    );
}

ScenarioEditModal.propTypes = {
    scenario: PropTypes.object.isRequired,
    onClose: PropTypes.func.isRequired,
    onSaved: PropTypes.func.isRequired,
};
