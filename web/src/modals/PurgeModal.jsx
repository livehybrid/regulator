/*
 * Purge: every cached bucket on the target, behind a typed confirmation.
 *
 * Splunk UI's guidance is that a modal is the right place to block progress
 * for a destructive action, and this is the most destructive thing the console
 * can do to somebody else's cluster.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Message from '@splunk/react-ui/Message';
import P from '@splunk/react-ui/Paragraph';
import Text from '@splunk/react-ui/Text';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { api } from '../api';
import { fmtBytes, fmtInt, fmtNum } from '../format';
import { useNotify } from '../Notifications';

export default function PurgeModal({ targetId, cache, loading, onClose, onDone }) {
    const [confirm, setConfirm] = useState('');
    const notify = useNotify();

    const purge = async () => {
        const r = await api(`/targets/${encodeURIComponent(targetId)}/evict`, {
            method: 'POST',
            body: { all_indexes: true },
        });
        const e = r.eviction || {};
        notify(
            `Purged: ${fmtInt(e.confirmed)} of ${fmtInt(e.attempted)} buckets confirmed evicted in ${fmtNum(r.duration_s, 1)}s`,
            'success'
        );
        onClose();
        if (onDone) {
            onDone();
        }
    };

    return (
        <Dialog
            title="Purge the SmartStore cache"
            onClose={onClose}
            footer={
                <BusyButton
                    appearance="destructive"
                    label="Purge"
                    disabled={confirm.trim().toLowerCase() !== 'purge'}
                    onClick={purge}
                />
            }
        >
            {loading ? (
                <P>
                    <WaitSpinner /> Reading the cache&hellip;
                </P>
            ) : (
                <Message appearance="fill" type="warning">
                    <Message.Title>Cache only, nothing is deleted from the object store.</Message.Title>
                    Every cached bucket on every indexer of this target is evicted:{' '}
                    {fmtInt(cache ? cache.local_buckets : null)} buckets,{' '}
                    {cache && cache.local_bytes ? fmtBytes(cache.local_bytes) : 'an unknown amount'}.
                    Everything comes back down from object storage as it is searched, paid for by
                    every user of this cluster, not only this test. Buckets a running search holds
                    open, hot buckets and bloom filters inside the hotlist window stay warm. Runs in
                    flight on this target start a new cache epoch here.
                </Message>
            )}
            <ControlGroup label="Type purge to confirm" labelPosition="top">
                <Text value={confirm} autoComplete="off" onChange={(e, { value }) => setConfirm(value)} />
            </ControlGroup>
        </Dialog>
    );
}

PurgeModal.propTypes = {
    targetId: PropTypes.any.isRequired,
    cache: PropTypes.object,
    loading: PropTypes.bool,
    onClose: PropTypes.func.isRequired,
    onDone: PropTypes.func,
};
