/*
 * Retag a scenario. An imported scenario's tags are written into its
 * scenario.yaml; a built-in one's go to an overlay on the data volume, since
 * the built-in library is read only and replaced with every image.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Multiselect from '@splunk/react-ui/Multiselect';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Note } from '../components/primitives';
import { api } from '../api';

export default function ScenarioTagsModal({ scenario, knownTags, onClose, onSaved }) {
    const [tags, setTags] = useState(scenario.tags || []);
    const options = Array.from(new Set([...knownTags, ...tags])).sort();

    return (
        <Dialog
            title={`Tags for ${scenario.name}`}
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="primary"
                    label="Save tags"
                    onClick={async () => {
                        const updated = await api(
                            `/scenarios/${encodeURIComponent(scenario.name)}/tags`,
                            { method: 'PUT', body: { tags } }
                        );
                        onSaved(updated);
                    }}
                />
            }
        >
            {scenario.origin !== 'user' && (
                <Note>
                    A built-in scenario: these tags are kept on this instance&apos;s data volume, so
                    they survive an upgrade. Saving the tags it ships with clears the override.
                </Note>
            )}
            <ControlGroup
                label="Tags"
                labelPosition="top"
                help="Pick existing tags or type a new one and press Enter."
            >
                <Multiselect
                    values={tags}
                    allowNewValues
                    onChange={(e, { values }) => setTags(values.map(String))}
                >
                    {options.map((t) => (
                        <Multiselect.Option key={t} label={t} value={t} />
                    ))}
                </Multiselect>
            </ControlGroup>
        </Dialog>
    );
}

ScenarioTagsModal.propTypes = {
    scenario: PropTypes.object.isRequired,
    knownTags: PropTypes.arrayOf(PropTypes.string).isRequired,
    onClose: PropTypes.func.isRequired,
    onSaved: PropTypes.func.isRequired,
};
