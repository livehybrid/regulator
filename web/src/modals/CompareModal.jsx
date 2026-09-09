import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Select from '@splunk/react-ui/Select';
import TextArea from '@splunk/react-ui/TextArea';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Muted } from '../components/primitives';
import { api, isAuthError } from '../api';

const DEFAULT_GATES = ['p95 <= baseline + 15%', 'error_rate <= 2%', 'queued == 0', 'valid'].join('\n');

export default function CompareModal({ runId, onClose, onResult }) {
    const [baselines, setBaselines] = useState([]);
    const [label, setLabel] = useState('');
    const [gates, setGates] = useState(DEFAULT_GATES);

    useEffect(() => {
        let live = true;
        (async () => {
            try {
                const list = (await api('/baselines')) || [];
                if (live) {
                    setBaselines(list);
                    setLabel(list.length ? list[0].label : '');
                }
            } catch (e) {
                if (!isAuthError(e) && live) {
                    setBaselines([]);
                }
            }
        })();
        return () => {
            live = false;
        };
    }, []);

    const compare = async () => {
        const body = { gates: gates.split('\n').map((x) => x.trim()).filter(Boolean) };
        if (label) {
            body.baseline_label = label;
        }
        const r = await api(`/runs/${encodeURIComponent(runId)}/compare`, { method: 'POST', body });
        onResult(r);
        onClose();
    };

    return (
        <Dialog
            title={`Compare run ${runId} against a baseline`}
            width="640px"
            onClose={onClose}
            closeLabel="Cancel"
            footer={<BusyButton appearance="primary" label="Compare" onClick={compare} />}
        >
            {baselines.length ? (
                <ControlGroup label="Baseline" labelPosition="top">
                    <Select value={label} onChange={(e, { value }) => setLabel(value)}>
                        {baselines.map((b) => (
                            <Select.Option
                                key={b.label}
                                value={b.label}
                                label={`${b.label} (run ${b.run_id}, ${b.scenario})`}
                            />
                        ))}
                    </Select>
                </ControlGroup>
            ) : (
                <Muted>
                    No baselines yet. Make one from a completed run first, or compare against
                    absolute gates only.
                </Muted>
            )}
            <ControlGroup
                label="Gates, one per line"
                labelPosition="top"
                help="Blank reports without judging."
            >
                <TextArea
                    value={gates}
                    rowsMax={6}
                    spellCheck={false}
                    onChange={(e, { value }) => setGates(value)}
                />
            </ControlGroup>
        </Dialog>
    );
}

CompareModal.propTypes = {
    runId: PropTypes.any.isRequired,
    onClose: PropTypes.func.isRequired,
    onResult: PropTypes.func.isRequired,
};
