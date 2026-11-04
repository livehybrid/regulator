/*
 * Copy a scenario into the operator's library under a new name. This is how a
 * built-in scenario is edited: the image's copy stays as shipped, and the
 * duplicate lives on the data volume where edits survive an upgrade.
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

export default function DuplicateScenarioModal({ scenario, onClose, onCreated }) {
    const [name, setName] = useState(`${scenario.name}-custom`.slice(0, 64));
    const valid = NAME_RE.test(name);

    return (
        <Dialog
            title={`Duplicate ${scenario.name}`}
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="primary"
                    label="Duplicate and edit"
                    disabled={!valid}
                    onClick={async () => {
                        const created = await api(
                            `/scenarios/${encodeURIComponent(scenario.name)}/duplicate`,
                            { method: 'POST', body: { name } }
                        );
                        onCreated(created);
                    }}
                />
            }
        >
            {scenario.origin !== 'user' && (
                <Note>
                    Built-in scenarios ship with the image and are read only. The copy is yours to
                    edit and keeps the original&apos;s comments.
                </Note>
            )}
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

DuplicateScenarioModal.propTypes = {
    scenario: PropTypes.object.isRequired,
    onClose: PropTypes.func.isRequired,
    onCreated: PropTypes.func.isRequired,
};
