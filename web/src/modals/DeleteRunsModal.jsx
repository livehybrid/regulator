import React from 'react';
import PropTypes from 'prop-types';
import P from '@splunk/react-ui/Paragraph';

import ConfirmModal from './ConfirmModal';
import { api } from '../api';
import { Muted } from '../components/primitives';

/** States a run can be deleted in (the server refuses the rest). */
export const DELETABLE_STATES = new Set(['completed', 'stopped', 'aborted', 'failed']);

/*
 * Confirm, then delete one or more finished runs.
 *
 * One request per run, so a refusal on one (it is a baseline, or someone else
 * already deleted it) is reported against that run and the rest still go.
 */
export default function DeleteRunsModal({ ids, notify, onDeleted, onClose }) {
    const label = ids.length === 1 ? `run ${ids[0]}` : `${ids.length} runs`;
    return (
        <ConfirmModal
            title={ids.length === 1 ? `Delete run ${ids[0]}` : `Delete ${ids.length} runs`}
            confirmLabel="Delete"
            onClose={onClose}
            onConfirm={async () => {
                const results = await Promise.allSettled(
                    ids.map((id) => api(`/runs/${encodeURIComponent(id)}`, { method: 'DELETE' }))
                );
                const deleted = ids.filter((id, i) => results[i].status === 'fulfilled');
                const failed = ids
                    .map((id, i) =>
                        results[i].status === 'rejected' ? `${id}: ${results[i].reason.message}` : null
                    )
                    .filter(Boolean);
                if (deleted.length) {
                    notify(
                        deleted.length === 1 ? `Run ${deleted[0]} deleted` : `${deleted.length} runs deleted`,
                        'success'
                    );
                }
                if (failed.length) {
                    notify(`Not deleted: ${failed.join('; ')}`, 'error');
                }
                onDeleted(deleted);
            }}
        >
            <P>
                Delete {label}? Its results, samples and report are removed for good.
            </P>
            <P>
                <Muted>
                    A run that is still live, or is a saved baseline, is not deleted. Remove the
                    baseline first.
                </Muted>
            </P>
        </ConfirmModal>
    );
}

DeleteRunsModal.propTypes = {
    ids: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.number, PropTypes.string])).isRequired,
    notify: PropTypes.func.isRequired,
    onDeleted: PropTypes.func.isRequired,
    onClose: PropTypes.func.isRequired,
};
