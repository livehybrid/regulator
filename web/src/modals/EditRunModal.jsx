/*
 * Editing a run: its label and note at any time, and its load while it runs.
 *
 * A load change is drawn on the run's charts and listed in its summary, so the
 * numbers either side of it stay readable; the run keeps the figures it was
 * launched with, and Run again repeats those, not the change.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import ControlGroup from '@splunk/react-ui/ControlGroup';
// Not imported as Number: that shadows the global (see LaunchModal).
import NumberInput from '@splunk/react-ui/Number';
import Text from '@splunk/react-ui/Text';
import TextArea from '@splunk/react-ui/TextArea';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Note } from '../components/primitives';
import { api } from '../api';
import { useNotify } from '../Notifications';

export default function EditRunModal({ run, onClose, onSaved }) {
    const running = run.state === 'running';
    const [label, setLabel] = useState(run.label || '');
    const [note, setNote] = useState(run.note || '');
    const [load, setLoad] = useState('');
    // Which figure sets this run's load: an arrival rate given at launch means
    // the open model; otherwise the scenario's own model decides.
    const [model, setModel] = useState(run.arrival_rate_per_min ? 'open' : null);
    const notify = useNotify();

    useEffect(() => {
        if (!running || model) {
            return undefined;
        }
        let live = true;
        (async () => {
            try {
                const scenarios = await api('/scenarios');
                const own = (scenarios || []).find((s) => s.name === run.scenario);
                if (live) {
                    setModel((own && own.load_model) || 'closed');
                }
            } catch (e) {
                if (live) {
                    setModel('closed');
                }
            }
        })();
        return () => {
            live = false;
        };
    }, [running, model, run.scenario]);

    const current =
        run.load_override !== null && run.load_override !== undefined
            ? run.load_override
            : model === 'open'
              ? run.arrival_rate_per_min
              : run.virtual_users;

    const save = async () => {
        const body = { label: label.trim(), note: note.trim() };
        if (running && load !== '' && load !== null && model !== 'schedule') {
            body[model === 'open' ? 'arrival_rate_per_min' : 'virtual_users'] = Number(load);
        }
        const updated = await api(`/runs/${encodeURIComponent(run.id)}`, { method: 'PATCH', body });
        notify('Run updated', 'success');
        onSaved(updated);
    };

    return (
        <Dialog
            title={`Edit run ${run.id}`}
            width="600px"
            onClose={onClose}
            closeLabel="Cancel"
            footer={<BusyButton appearance="primary" label="Save" onClick={save} />}
        >
            <ControlGroup label="Label" labelPosition="top">
                <Text value={label} onChange={(e, { value }) => setLabel(value)} />
            </ControlGroup>
            <ControlGroup label="Note" labelPosition="top" help="What changed, what this run was for.">
                <TextArea value={note} rowsMin={3} onChange={(e, { value }) => setNote(value)} />
            </ControlGroup>
            {running && model === 'schedule' && (
                <Note>
                    This run uses the schedule model, which fires each search on its own cron and
                    has no single load figure to change.
                </Note>
            )}
            {running && model && model !== 'schedule' && (
                <>
                    <ControlGroup
                        label={model === 'open' ? 'Arrival rate per minute' : 'Virtual users'}
                        labelPosition="top"
                        help={`Blank leaves it as it is${
                            current === null || current === undefined ? '' : ` (${current})`
                        }. Takes effect within a few seconds.`}
                    >
                        <NumberInput value={load} min={0} onChange={(e, { value }) => setLoad(value)} />
                    </ControlGroup>
                    <Note>
                        The change replaces the scenario&apos;s ramp from now on and is marked on the
                        charts, so the percentiles either side of it can be read apart.
                    </Note>
                </>
            )}
        </Dialog>
    );
}

EditRunModal.propTypes = {
    run: PropTypes.shape({
        id: PropTypes.number.isRequired,
        state: PropTypes.string,
        label: PropTypes.string,
        note: PropTypes.string,
        scenario: PropTypes.string,
        virtual_users: PropTypes.number,
        arrival_rate_per_min: PropTypes.number,
        load_override: PropTypes.number,
    }).isRequired,
    onClose: PropTypes.func.isRequired,
    onSaved: PropTypes.func.isRequired,
};
