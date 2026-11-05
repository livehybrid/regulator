/*
 * Rename one of the operator's own scenarios. Runs and baselines that name it
 * are moved to the new name, and so is its copy in the scenario source, so
 * history and comparisons follow it. Built-in scenarios cannot be renamed
 * (duplicate them instead), and a scenario with an unfinished run is refused.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Text from '@splunk/react-ui/Text';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Note } from '../components/primitives';
import { api } from '../api';

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export default function RenameScenarioModal({ scenario, onClose, onRenamed }) {
    const [name, setName] = useState(scenario.name);
    const valid = NAME_RE.test(name);
    const changed = name !== scenario.name;

    return (
        <Dialog
            title={`Rename ${scenario.name}`}
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="primary"
                    label="Rename"
                    disabled={!valid || !changed}
                    onClick={async () => {
                        const renamed = await api(
                            `/scenarios/${encodeURIComponent(scenario.name)}/rename`,
                            { method: 'POST', body: { name } }
                        );
                        onRenamed(renamed);
                    }}
                />
            }
        >
            <Note>
                Past runs and baselines of this scenario move to the new name. A run started
                after the rename will not match the scenario digest of earlier runs, because the
                name is part of scenario.yaml.
            </Note>
            <ControlGroup
                label="New name"
                labelPosition="top"
                help="Lowercase letters, digits, dashes and underscores."
                error={!valid}
            >
                <Text value={name} error={!valid} onChange={(e, { value }) => setName(value)} />
            </ControlGroup>
        </Dialog>
    );
}

RenameScenarioModal.propTypes = {
    scenario: PropTypes.object.isRequired,
    onClose: PropTypes.func.isRequired,
    onRenamed: PropTypes.func.isRequired,
};
