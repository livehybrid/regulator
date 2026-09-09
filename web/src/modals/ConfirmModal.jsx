import React from 'react';
import PropTypes from 'prop-types';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';

/** A yes/no dialog for the small destructive actions. */
export default function ConfirmModal({ title, children, confirmLabel, onConfirm, onClose }) {
    return (
        <Dialog
            title={title}
            width="520px"
            onClose={onClose}
            closeLabel="Cancel"
            footer={
                <BusyButton
                    appearance="destructive"
                    label={confirmLabel}
                    onClick={async () => {
                        await onConfirm();
                        onClose();
                    }}
                />
            }
        >
            {children}
        </Dialog>
    );
}

ConfirmModal.propTypes = {
    title: PropTypes.node,
    children: PropTypes.node,
    confirmLabel: PropTypes.string.isRequired,
    onConfirm: PropTypes.func.isRequired,
    onClose: PropTypes.func.isRequired,
};
