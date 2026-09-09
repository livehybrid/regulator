import React, { useState } from 'react';
import PropTypes from 'prop-types';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Text from '@splunk/react-ui/Text';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Note } from '../components/primitives';
import { api } from '../api';
import { useNotify } from '../Notifications';

export default function PromoteBaselineModal({ runId, onClose }) {
    const [label, setLabel] = useState('');
    const [note, setNote] = useState('');
    const notify = useNotify();

    const promote = async () => {
        if (!label.trim()) {
            notify('Give the baseline a label', 'warning');
            return;
        }
        await api('/baselines', {
            method: 'POST',
            body: {
                run_id: parseInt(runId, 10),
                label: label.trim(),
                note: note.trim() || null,
            },
        });
        notify(`Baseline ${label.trim()} now points at run ${runId}`, 'success');
        onClose();
    };

    return (
        <Dialog
            title="Make this run a baseline"
            width="560px"
            onClose={onClose}
            closeLabel="Cancel"
            footer={<BusyButton appearance="primary" label="Promote" onClick={promote} />}
        >
            <Note>
                A baseline is a label pointing at a run. A pipeline compares against the label, so
                promoting a newer run moves it without touching the pipeline.
            </Note>
            <ControlGroup label="Label" labelPosition="top">
                <Text value={label} placeholder="main-green" onChange={(e, { value }) => setLabel(value)} />
            </ControlGroup>
            <ControlGroup label="Note" labelPosition="top" help="Optional.">
                <Text value={note} onChange={(e, { value }) => setNote(value)} />
            </ControlGroup>
        </Dialog>
    );
}

PromoteBaselineModal.propTypes = {
    runId: PropTypes.any.isRequired,
    onClose: PropTypes.func.isRequired,
};
