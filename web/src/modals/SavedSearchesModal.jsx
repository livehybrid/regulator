/*
 * The searches this Splunk already runs, and the door from them into a
 * scenario. The file Regulator keeps is Splunk's own savedsearches.conf, so
 * what is replayed is what the cluster actually schedules.
 *
 * An estate can hold thousands of saved searches, so the app is chosen from
 * the target's own list (falling back to typing one when the account cannot
 * list apps), and the scenario is built from the searches ticked here rather
 * than from everything that came back.
 */
import React, { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import ComboBox from '@splunk/react-ui/ComboBox';
import P from '@splunk/react-ui/Paragraph';
import Switch from '@splunk/react-ui/Switch';
import Text from '@splunk/react-ui/Text';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import SavedSearchTable, { rowKey } from '../components/SavedSearchTable';
import { Actions, Muted, Note, Toolbar } from '../components/primitives';
import { api, apiText } from '../api';

const ALL_APPS = 'All apps';

export default function SavedSearchesModal({ target, onClose, onImport }) {
    // The target's own app is the likely one, and loading every search on
    // the estate is the expensive default this replaces.
    const [app, setApp] = useState(target.app && target.app !== '-' ? target.app : '');
    const [apps, setApps] = useState(null);
    const [rows, setRows] = useState(null);
    const [loadedApp, setLoadedApp] = useState('');
    const [selected, setSelected] = useState(() => new Set());
    const [filter, setFilter] = useState('');
    const [includedOnly, setIncludedOnly] = useState(false);
    const [scheduledOnly, setScheduledOnly] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    const base = `/targets/${encodeURIComponent(target.id)}`;

    useEffect(() => {
        let live = true;
        api(`${base}/apps`)
            .then((list) => live && setApps(list || []))
            .catch(() => live && setApps([]));
        return () => {
            live = false;
        };
    }, [base]);

    const appValue = app.trim() === ALL_APPS ? '' : app.trim();

    const load = async () => {
        setLoading(true);
        setError(null);
        try {
            const query = appValue ? `?app=${encodeURIComponent(appValue)}` : '';
            const list = (await api(`${base}/savedsearches${query}`)) || [];
            setRows(list);
            setLoadedApp(appValue);
            // Start with what a scenario would include anyway; the ones that
            // would be left out stay visible with their reason, unticked.
            setSelected(new Set(list.filter((r) => !r.skipped_reason).map(rowKey)));
        } catch (e) {
            setError(e.message);
            setRows([]);
        } finally {
            setLoading(false);
        }
    };

    const visible = useMemo(() => {
        const needle = filter.trim().toLowerCase();
        return (rows || []).filter(
            (r) =>
                (!needle ||
                    r.name.toLowerCase().includes(needle) ||
                    (r.search || '').toLowerCase().includes(needle)) &&
                (!includedOnly || !r.skipped_reason) &&
                (!scheduledOnly || r.scheduled)
        );
    }, [rows, filter, includedOnly, scheduledOnly]);

    const picked = (rows || []).filter((r) => selected.has(rowKey(r)));

    const toggle = (key) =>
        setSelected((current) => {
            const next = new Set(current);
            if (next.has(key)) {
                next.delete(key);
            } else {
                next.add(key);
            }
            return next;
        });

    // Select-all acts on what is shown, so a filter then select-all picks
    // exactly the filtered rows.
    const toggleAll = () =>
        setSelected((current) => {
            const next = new Set(current);
            const allShown = visible.length > 0 && visible.every((r) => next.has(rowKey(r)));
            visible.forEach((r) => (allShown ? next.delete(rowKey(r)) : next.add(rowKey(r))));
            return next;
        });

    const confQuery = () => {
        const params = new URLSearchParams();
        if (loadedApp) {
            params.set('app', loadedApp);
        }
        picked.forEach((r) => params.append('name', r.name));
        const text = params.toString();
        return text ? `?${text}` : '';
    };

    const create = async () => {
        const conf = await apiText(`${base}/savedsearches.conf${confQuery()}`);
        onImport(conf, `${target.name || 'target'}-${loadedApp || 'all'}`);
    };

    return (
        <Dialog
            title={`Saved searches on ${target.name || target.id}`}
            width="1080px"
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="primary"
                    label={
                        picked.length
                            ? `Create a scenario from ${picked.length} selected`
                            : 'Select searches to create a scenario'
                    }
                    disabled={!picked.length}
                    onClick={create}
                />
            }
        >
            <Note>
                These are the searches this Splunk already runs. Pick an app, tick the searches you
                want, then turn them into a scenario: the file Regulator keeps is Splunk&apos;s own
                savedsearches.conf, and a search that writes anywhere is left out unless you say
                otherwise.
            </Note>
            <Actions>
                <ComboBox
                    value={app}
                    placeholder={apps && !apps.length ? 'app name (blank for all)' : 'Search apps'}
                    isLoadingOptions={apps === null}
                    onChange={(e, { value }) => setApp(value)}
                    style={{ width: 280 }}
                >
                    <ComboBox.Option value={ALL_APPS} />
                    {(apps || []).map((a) => (
                        <ComboBox.Option
                            key={a.name}
                            value={a.name}
                            description={a.label !== a.name ? a.label : undefined}
                        />
                    ))}
                </ComboBox>
                <BusyButton label="Load" onClick={load} />
                <Button
                    label="Open as .conf"
                    to={`/api${base}/savedsearches.conf${confQuery()}`}
                    disabled={!picked.length}
                    openInNewContext
                />
            </Actions>

            {rows !== null && rows.length > 0 && (
                <Toolbar>
                    <Text
                        value={filter}
                        placeholder="Filter by name or SPL"
                        onChange={(e, { value }) => setFilter(value)}
                        style={{ width: 280 }}
                    />
                    <Switch
                        selected={includedOnly}
                        onClick={() => setIncludedOnly((v) => !v)}
                        appearance="toggle"
                    >
                        Included only
                    </Switch>
                    <Switch
                        selected={scheduledOnly}
                        onClick={() => setScheduledOnly((v) => !v)}
                        appearance="toggle"
                    >
                        Scheduled only
                    </Switch>
                    <Muted $small>
                        {picked.length} of {rows.length} selected
                        {visible.length !== rows.length ? `, ${visible.length} shown` : ''}
                    </Muted>
                </Toolbar>
            )}

            {loading && (
                <P>
                    <WaitSpinner /> Reading saved searches from the target&hellip;
                </P>
            )}
            {error && <Muted>{error}</Muted>}
            {!loading && rows === null && <Muted>Choose an app and press Load.</Muted>}
            {!loading && rows !== null && (
                <SavedSearchTable
                    rows={visible}
                    selected={selected}
                    onToggle={toggle}
                    onToggleAll={toggleAll}
                />
            )}
        </Dialog>
    );
}

SavedSearchesModal.propTypes = {
    target: PropTypes.object.isRequired,
    onClose: PropTypes.func.isRequired,
    onImport: PropTypes.func.isRequired,
};
