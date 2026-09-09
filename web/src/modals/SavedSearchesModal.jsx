/*
 * The searches this Splunk already runs, and the door from them into a
 * scenario. The file Regulator keeps is Splunk's own savedsearches.conf, so
 * what is replayed is what the cluster actually schedules.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import P from '@splunk/react-ui/Paragraph';
import Text from '@splunk/react-ui/Text';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import SavedSearchTable from '../components/SavedSearchTable';
import { Muted, Note, Toolbar } from '../components/primitives';
import { api, apiText } from '../api';

export default function SavedSearchesModal({ target, onClose, onImport }) {
    const [app, setApp] = useState('');
    const [rows, setRows] = useState(null);
    const [conf, setConf] = useState('');
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    const query = app.trim() ? `?app=${encodeURIComponent(app.trim())}` : '';
    const confUrl = `/api/targets/${encodeURIComponent(target.id)}/savedsearches.conf${query}`;

    const load = async () => {
        setLoading(true);
        setError(null);
        try {
            const list = await api(`/targets/${encodeURIComponent(target.id)}/savedsearches${query}`);
            setRows(list || []);
            setConf(await apiText(`/targets/${encodeURIComponent(target.id)}/savedsearches.conf${query}`));
        } catch (e) {
            setError(e.message);
            setRows([]);
        } finally {
            setLoading(false);
        }
    };

    return (
        <Dialog
            title={`Saved searches on ${target.name || target.id}`}
            width="1080px"
            onClose={onClose}
            footer={
                <Button
                    appearance="primary"
                    label="Create a scenario from these"
                    disabled={!conf}
                    onClick={() =>
                        onImport(conf, `${target.name || 'target'}-${app.trim() || 'all'}`)
                    }
                />
            }
        >
            <Note>
                These are the searches this Splunk already runs. Pick an app, then turn them into a
                scenario: the file Regulator keeps is Splunk&apos;s own savedsearches.conf, and a
                search that writes anywhere is left out unless you say otherwise.
            </Note>
            <Toolbar>
                <Text
                    value={app}
                    placeholder="app (blank for all)"
                    onChange={(e, { value }) => setApp(value)}
                    style={{ width: 220 }}
                />
                <BusyButton label="Load" onClick={load} />
                <Button label="Open as .conf" to={confUrl} openInNewContext />
            </Toolbar>

            {loading && (
                <P>
                    <WaitSpinner /> Reading savedsearches.conf from the target&hellip;
                </P>
            )}
            {error && <Muted>{error}</Muted>}
            {!loading && rows === null && <Muted>Press Load.</Muted>}
            {!loading && rows !== null && <SavedSearchTable rows={rows} />}
        </Dialog>
    );
}

SavedSearchesModal.propTypes = {
    target: PropTypes.object.isRequired,
    onClose: PropTypes.func.isRequired,
    onImport: PropTypes.func.isRequired,
};
