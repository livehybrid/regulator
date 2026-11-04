import React, { useState } from 'react';
import PropTypes from 'prop-types';
import P from '@splunk/react-ui/Paragraph';
import Switch from '@splunk/react-ui/Switch';

import ConfirmModal from './ConfirmModal';
import { Mono, Muted } from '../components/primitives';

/*
 * Confirm deleting a user scenario, optionally from the scenario source too.
 *
 * The boot sync imports anything in the source that the local library lacks,
 * so a scenario deleted only here comes straight back at the next restart.
 * The checkbox appears only when the source is writable; it is off by default
 * because the shared library is the thing other instances read from.
 */
export default function DeleteScenarioModal({ name, source, onConfirm, onClose }) {
    const writable = !!(source && source.writable);
    const [alsoRemote, setAlsoRemote] = useState(false);
    return (
        <ConfirmModal
            title="Delete scenario"
            confirmLabel="Delete"
            onClose={onClose}
            onConfirm={() => onConfirm(writable && alsoRemote)}
        >
            <P>
                Delete the scenario <strong>{name}</strong> from this instance? Runs already
                made with it are kept.
            </P>
            {writable && (
                <Switch
                    appearance="checkbox"
                    selected={alsoRemote}
                    onClick={() => setAlsoRemote(!alsoRemote)}
                >
                    Also delete its archive from <Mono>{source.location}</Mono>
                </Switch>
            )}
            {source && source.configured && !(writable && alsoRemote) && (
                <P>
                    <Muted>
                        {writable
                            ? 'Left in the scenario source, it is imported again at the next restart.'
                            : 'The scenario source is read-only here, so a copy in it is left alone and is imported again at the next restart.'}
                    </Muted>
                </P>
            )}
        </ConfirmModal>
    );
}

DeleteScenarioModal.propTypes = {
    name: PropTypes.string.isRequired,
    source: PropTypes.shape({
        configured: PropTypes.bool,
        writable: PropTypes.bool,
        location: PropTypes.string,
    }),
    onConfirm: PropTypes.func.isRequired,
    onClose: PropTypes.func.isRequired,
};
